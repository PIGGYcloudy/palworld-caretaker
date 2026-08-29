"""Fail-closed persistence for visual-editor configuration changes."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
from typing import Mapping

from .config import CaretakerConfig, SETTINGS_BACKUP_DIRECTORY, load_config
from .errors import ConfigError
from .paths import native_path
from .settings import SETTING_SPECS, validate_edit
from .windows import is_reparse_point


_EDITABLE_DIRECTORY = "editable"
_EDITABLE_FILES = frozenset({"caretaker.env", "server.env"})


class SettingsPersistenceError(RuntimeError):
    """A settings edit could not be durably and safely committed."""


_SAFE_UNQUOTED = re.compile(r"^[A-Za-z0-9._/:@+,-]*$")


def _sync_directory(path: Path) -> None:
    """Persist a directory entry where the host filesystem supports it."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular_file(path: Path, *, allow_missing: bool = False) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise SettingsPersistenceError(f"configuration file is missing: {path.name}") from None
    if (stat.S_ISLNK(info.st_mode) or is_reparse_point(info)
            or not stat.S_ISREG(info.st_mode)):
        raise SettingsPersistenceError(f"configuration file is unsafe: {path.name}")
    return info


def _safe_directory(path: Path, *, message: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise SettingsPersistenceError(message) from None
    if (stat.S_ISLNK(info.st_mode) or is_reparse_point(info)
            or not stat.S_ISDIR(info.st_mode)):
        raise SettingsPersistenceError(message)
    return info


def _env_value(value: str) -> str:
    if _SAFE_UNQUOTED.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_updates(source: bytes, updates: Mapping[str, str]) -> bytes:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SettingsPersistenceError("configuration must be UTF-8") from exc
    remaining = dict(updates)
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        content = line[:-1] if newline else line
        matched = re.match(r"^(\s*)([A-Z][A-Z0-9_]*)(\s*=).*$", content)
        if matched and matched.group(2) in remaining:
            lines.append(f"{matched.group(1)}{matched.group(2)}{matched.group(3)}{_env_value(remaining.pop(matched.group(2)))}{newline}")
        else:
            lines.append(line)
    if remaining:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines.append("\n")
        lines.extend(f"{key}={_env_value(value)}\n" for key, value in remaining.items())
    return "".join(lines).encode("utf-8")


def _atomic_write(path: Path, payload: bytes, mode: int, uid: int, gid: int) -> None:
    """Durably replace a previously safety-checked regular config file."""
    _safe_directory(path.parent, message="configuration directory is unsafe")
    existing = _regular_file(path, allow_missing=True)
    if os.name != "nt" and existing is not None and (existing.st_uid != uid or existing.st_gid != gid):
        raise SettingsPersistenceError(f"configuration file ownership is unsafe: {path.name}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, mode)
            os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except OSError as exc:
        raise SettingsPersistenceError("configuration could not be written safely") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class SettingsStore:
    """Preview and commit editable fields from a known layered config root."""

    def __init__(self, directory: str | Path, state_root: str | Path):
        self.directory = native_path(directory)
        self.state_root = native_path(state_root)
        self.backup_root = self.state_root / SETTINGS_BACKUP_DIRECTORY

    def current(self) -> CaretakerConfig:
        return load_config(self.directory)

    def _editable_directory(self) -> Path:
        """Return the only directory the web process may atomically replace."""
        editable = self.directory / _EDITABLE_DIRECTORY
        if editable.exists() or editable.is_symlink():
            _safe_directory(editable, message="editable configuration directory is unavailable")
            return editable
        # Keep direct callers and pre-v0.3 test fixtures readable.  Production
        # install/upgrade always creates the isolated child directory.
        return self.directory

    def preview(self, requested: Mapping[str, object], current: CaretakerConfig | None = None) -> tuple[CaretakerConfig, tuple[dict[str, str], ...]]:
        baseline = current or self.current()
        merged = validate_edit(requested, baseline.values)
        candidate = CaretakerConfig(merged, baseline.schema, baseline.directory)
        diff = tuple(
            {"key": key, "label": spec.label, "category": spec.category,
             "old": baseline.values[key], "new": candidate.values[key]}
            for key, spec in SETTING_SPECS.items() if baseline.values[key] != candidate.values[key]
        )
        return candidate, diff

    def _backup(self, originals: Mapping[Path, bytes | None]) -> Path:
        if self.state_root.is_symlink() or not self.state_root.is_dir():
            raise SettingsPersistenceError("caretaker state directory is unavailable")
        root = self.backup_root
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise SettingsPersistenceError("settings backup directory is unsafe")
        root.mkdir(mode=0o700, exist_ok=True)
        _sync_directory(self.state_root)
        _safe_directory(root, message="settings backup directory is unsafe")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        destination = root / f"settings-{stamp}-{secrets.token_hex(4)}"
        destination.mkdir(mode=0o700)
        try:
            # The timestamped directory name is the backup commit record.
            # Persist its parent entry before publishing configuration files.
            _sync_directory(root)
            for path, content in originals.items():
                if content is not None:
                    target = destination / path.name
                    with target.open("xb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    target.chmod(0o600)
            _sync_directory(destination)
        except OSError as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise SettingsPersistenceError("configuration backup could not be created") from exc
        return destination

    def commit(self, requested: Mapping[str, object]) -> tuple[CaretakerConfig, tuple[dict[str, str], ...], Path | None]:
        """Back up then atomically publish all changed target files.

        The caller owns the global operation lock for this entire method.
        If a later target fails, already-published targets are restored from
        their in-memory originals before reporting the failure.
        """
        _safe_directory(self.directory, message="configuration directory is unavailable")
        current = self.current()
        candidate, diff = self.preview(requested, current)
        if not diff:
            return current, diff, None
        updates: dict[Path, dict[str, str]] = {}
        editable = self._editable_directory()
        editable_info = _safe_directory(editable, message="editable configuration directory is unavailable")
        if os.name != "nt" and (editable_info.st_uid != os.geteuid() or editable_info.st_gid != os.getegid()):
            raise SettingsPersistenceError("editable configuration directory ownership is unsafe")
        for change in diff:
            spec = SETTING_SPECS[change["key"]]
            filename = f"{spec.target}.env"
            if filename not in _EDITABLE_FILES:
                raise SettingsPersistenceError("settings target is not editable")
            updates.setdefault(editable / filename, {})[spec.key] = change["new"]
        originals: dict[Path, bytes | None] = {}
        modes: dict[Path, int] = {}
        owners: dict[Path, tuple[int, int]] = {}
        for path in updates:
            info = _regular_file(path, allow_missing=True)
            if info is not None:
                # Editable files must remain owned by the web service account.
                # A root-owned target here would make the next atomic replace
                # change ownership unexpectedly and indicates a bad migration.
                if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_gid != os.getegid()):
                    raise SettingsPersistenceError(f"configuration file ownership is unsafe: {path.name}")
                originals[path] = path.read_bytes()
                modes[path] = 0o640
                owners[path] = (info.st_uid, info.st_gid)
            else:
                originals[path] = None
                modes[path] = 0o640
                owners[path] = (os.geteuid(), os.getegid()) if os.name != "nt" else (0, 0)
        backup = self._backup(originals)
        attempted: list[Path] = []
        try:
            for path, fields in updates.items():
                source = originals[path] if originals[path] is not None else b""
                uid, gid = owners[path]
                # os.replace may have happened even if the final directory
                # fsync reports an error, so include the target in rollback
                # before attempting the write.
                attempted.append(path)
                _atomic_write(path, _render_updates(source, fields), modes[path], uid, gid)
            # Reload belongs in the protected transaction.  A parse/validation
            # failure after publishing is a failed commit, not a partial save.
            reloaded = self.current()
        except (OSError, SettingsPersistenceError, ConfigError) as exc:
            rollback_error = False
            for path in reversed(attempted):
                try:
                    original = originals[path]
                    if original is None:
                        if _regular_file(path, allow_missing=True) is not None:
                            path.unlink()
                            _sync_directory(path.parent)
                    else:
                        uid, gid = owners[path]
                        _atomic_write(path, original, modes[path], uid, gid)
                except (OSError, SettingsPersistenceError):
                    rollback_error = True
            if rollback_error:
                raise SettingsPersistenceError(
                    "fatal settings commit failure: rollback could not be completed; the automatic backup was retained"
                ) from exc
            raise SettingsPersistenceError("settings were not fully applied; the automatic backup was retained") from exc
        return reloaded, diff, backup
