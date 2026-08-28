"""Tamper-resistant, secret-free audit records for management actions."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping


# Treat any key containing one of these credential-bearing fragments as
# sensitive.  This intentionally implements wildcard matching such as
# ``*KEY*`` and ``*AUTH*`` rather than maintaining a brittle vocabulary of
# individual header names.
_SECRET_KEY = re.compile(r"(?:key|secret|token|pass(?:word)?|auth|credential|cookie|message|reason)", re.I)
_MAX_LINE = 16 * 1024
_MAX_ENTRIES = 200


def sanitize(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    """Return JSON-safe data with credential-bearing values removed.

    This is deliberately applied both before writing and before returning
    historical entries: an old or manually edited log cannot expose a secret
    through the web UI.
    """
    if isinstance(value, Mapping):
        return {
            str(key): "***" if _SECRET_KEY.search(str(key)) else sanitize(item, secrets=secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        output = value
        for secret in secrets:
            if secret:
                output = output.replace(secret, "***")
        # Defence in depth for common ``Header: value`` text passed from a
        # service wrapper.  Never persist a credential-shaped fragment.
        output = re.sub(
            r"(?i)\b([a-z0-9_-]*(?:key|secret|pass(?:word)?|token|auth|credential|cookie)[a-z0-9_-]*)\s*[:=]\s*[^\r\n]*",
            r"\1=***", output,
        )
        return output
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class AuditLog:
    """Append and read line-delimited JSON records owned by the manager user."""

    def __init__(self, state_root: str | Path, *, secrets: tuple[str, ...] = (),
                 expected_uid: int | None = None, expected_gid: int | None = None):
        self.state_root = Path(state_root)
        self.path = self.state_root / "audit.log"
        self.secrets = tuple(item for item in secrets if item)
        self.expected_uid = os.getuid() if expected_uid is None else expected_uid
        self.expected_gid = os.getgid() if expected_gid is None else expected_gid

    def _open_append(self) -> int:
        if not self.state_root.exists():
            self.state_root.mkdir(mode=0o750, parents=True, exist_ok=True)
        state_info = self.state_root.lstat()
        if stat.S_ISLNK(state_info.st_mode) or not stat.S_ISDIR(state_info.st_mode):
            raise OSError("audit state directory is unsafe")
        if not hasattr(os, "O_NOFOLLOW"):
            raise OSError("audit logging requires O_NOFOLLOW support")
        directory = os.open(
            self.state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        try:
            descriptor = os.open("audit.log", flags | os.O_NOFOLLOW, 0o640, dir_fd=directory)
        finally:
            os.close(directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(descriptor)
            raise OSError("audit log owner or file type is unsafe")
        # Root-owned CLI/service workflows may initialize the file before the
        # manager service has run.  Transfer only the newly expected manager
        # identity; unprivileged callers can never perform this operation.
        if hasattr(os, "geteuid") and os.geteuid() == 0 and (
                info.st_uid != self.expected_uid or info.st_gid != self.expected_gid):
            os.fchown(descriptor, self.expected_uid, self.expected_gid)
            info = os.fstat(descriptor)
        if info.st_uid != self.expected_uid or info.st_gid != self.expected_gid:
            os.close(descriptor)
            raise OSError("audit log owner or file type is unsafe")
        if stat.S_IMODE(info.st_mode) != 0o640:
            os.fchmod(descriptor, 0o640)
        return descriptor

    def record(self, *, source: str, action: str, status: str,
               details: Mapping[str, Any] | None = None, who: str | None = None) -> dict[str, Any]:
        entry = sanitize({
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": source, "who": who or source, "action": action,
            "status": status, "details": dict(details or {}),
        }, secrets=self.secrets)
        encoded = json.dumps(
            entry, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > _MAX_LINE:
            raise ValueError("audit entry is too large")
        descriptor = self._open_append()
        try:
            os.write(descriptor, encoded + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return entry

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or not 1 <= limit <= _MAX_ENTRIES:
            raise ValueError("audit limit must be between 1 and 200")
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != self.expected_uid or
                        info.st_gid != self.expected_gid or stat.S_IMODE(info.st_mode) != 0o640):
                    raise OSError("audit log is unsafe")
                # A bounded reverse window is sufficient: every valid record has
                # a hard line-size limit, and it avoids loading an unbounded log.
                start = max(0, info.st_size - (limit + 1) * _MAX_LINE)
                os.lseek(descriptor, start, os.SEEK_SET)
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                os.close(descriptor)
            raw = b"".join(chunks)
            if start:
                raw = raw.split(b"\n", 1)[-1]
            lines = raw.decode("utf-8").splitlines()[-limit:]
        except FileNotFoundError:
            return []
        except UnicodeDecodeError:
            return []
        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            if len(line.encode("utf-8")) > _MAX_LINE:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, Mapping):
                entries.append(sanitize(item, secrets=self.secrets))
        return entries
