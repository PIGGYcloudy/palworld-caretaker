"""Fail-closed persistence for visual-editor configuration changes."""
from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
from typing import Mapping
from urllib.parse import urlsplit

from .config import CaretakerConfig, SETTINGS_BACKUP_DIRECTORY, load_config
from .errors import ConfigError
from .settings import canonical_web_origin, normalize_web_bind_ip
from .paths import native_path
from .settings import SETTING_SPECS, validate_edit, validate_ini_password
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

    def _publish_editable(self, updates: Mapping[str, Mapping[str, str]], *, directory: Path | None = None) -> Path:
        """Back up and atomically publish a group of editable files.

        This is the transaction primitive shared by the normal editor and
        small-purpose wizards.  Every target is first captured in one backup;
        a failure while replacing any later file restores all earlier files,
        including targets created for this attempt.
        """
        _safe_directory(self.directory, message="configuration directory is unavailable")
        editable = directory or self.directory / _EDITABLE_DIRECTORY
        editable_info = _safe_directory(editable, message="editable configuration directory is unavailable")
        if os.name != "nt" and (editable_info.st_uid != os.geteuid() or editable_info.st_gid != os.getegid()):
            raise SettingsPersistenceError("editable configuration directory ownership is unsafe")
        targets: dict[Path, Mapping[str, str]] = {}
        for filename, fields in updates.items():
            if filename not in {*_EDITABLE_FILES, "secrets.env"}:
                raise SettingsPersistenceError("settings target is not editable")
            if not fields or any(not isinstance(key, str) or not isinstance(value, str)
                                 for key, value in fields.items()):
                raise SettingsPersistenceError("settings update is invalid")
            targets[editable / filename] = fields
        originals: dict[Path, bytes | None] = {}
        owners: dict[Path, tuple[int, int]] = {}
        for path in targets:
            info = _regular_file(path, allow_missing=True)
            if info is not None:
                if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_gid != os.getegid()):
                    raise SettingsPersistenceError(f"configuration file ownership is unsafe: {path.name}")
                originals[path] = path.read_bytes()
                owners[path] = (info.st_uid, info.st_gid)
            else:
                originals[path] = None
                owners[path] = (os.geteuid(), os.getegid()) if os.name != "nt" else (0, 0)
        backup = self._backup(originals)
        attempted: list[Path] = []
        try:
            for path, fields in targets.items():
                attempted.append(path)
                uid, gid = owners[path]
                _atomic_write(path, _render_updates(originals[path] or b"", fields), 0o640, uid, gid)
            # A post-write config parse is part of the transaction, not an
            # optional follow-up; invalid layered configuration is rolled back.
            self.current()
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
                        _atomic_write(path, original, 0o640, uid, gid)
                except (OSError, SettingsPersistenceError):
                    rollback_error = True
            if rollback_error:
                raise SettingsPersistenceError(
                    "fatal settings commit failure: rollback could not be completed; the automatic backup was retained"
                ) from exc
            raise SettingsPersistenceError("settings were not fully applied; the automatic backup was retained") from exc
        return backup

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
        updates: dict[str, dict[str, str]] = {}
        editable = self._editable_directory()
        for change in diff:
            spec = SETTING_SPECS[change["key"]]
            filename = f"{spec.target}.env"
            if filename not in _EDITABLE_FILES:
                raise SettingsPersistenceError("settings target is not editable")
            updates.setdefault(filename, {})[spec.key] = change["new"]
        backup = self._publish_editable(updates, directory=editable)
        return self.current(), diff, backup

    def complete_onboarding(self, *, server_name: object, server_password: object, backup_time: object,
                            bind_mode: object, lan_origin: object = "") -> CaretakerConfig:
        """Persist the deliberately small first-run-only configuration surface.

        The manager account never writes root's secrets file.  Instead its
        server password is stored in ``editable/secrets.env``; its loader
        allowlist is deliberately tiny. A placeholder panel credential is
        replaced by the same first-run password, so a copied release template
        never leaves the local management UI protected by CHANGE_ME text.
        """
        server_password = validate_ini_password(server_password)
        if bind_mode not in {"local", "lan"}:
            raise ConfigError("bind mode must be local or lan")
        if backup_time == "off":
            schedule_enabled, rendered_time = "false", None
        elif isinstance(backup_time, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", backup_time):
            schedule_enabled, rendered_time = "true", backup_time
        else:
            raise ConfigError("backup time must be HH:MM or off")
        origin = ""
        if bind_mode == "lan":
            origin = canonical_web_origin(lan_origin) if isinstance(lan_origin, str) else None
            hostname = urlsplit(origin).hostname if origin is not None else None
            try:
                address = ipaddress.ip_address(hostname) if hostname is not None else None
            except ValueError:
                address = None
            # A LAN listener accepts a literal RFC1918/ULA-style private
            # address only.  DNS, public, loopback, link-local and IPv6
            # authorities cannot express the bound IPv4 LAN control plane.
            if (origin is None or not origin.startswith("http://") or not isinstance(address, ipaddress.IPv4Address)
                    or not address.is_private or address.is_loopback or address.is_link_local
                    or address.is_unspecified or address.is_multicast):
                raise ConfigError("a valid private IPv4 LAN http:// address is required")
            normalize_web_bind_ip("0.0.0.0")

        current = self.current()
        server_name = validate_edit({"SERVER_NAME": server_name}, current.values)["SERVER_NAME"]
        values = dict(current.values)
        panel_password = values.get("PALWORLD_WEB_UI_PASSWORD") or values.get("ADMIN_PASSWORD", "")
        replace_placeholder_panel_password = not panel_password or panel_password.startswith("CHANGE_ME")
        values.update({
            "SERVER_NAME": server_name,
            "SERVER_PASSWORD": server_password,
            "PALWORLD_BACKUP_SCHEDULE_ENABLED": schedule_enabled,
            "PALWORLD_WEB_BIND_IP": "0.0.0.0" if bind_mode == "lan" else "127.0.0.1",
            "PALWORLD_WEB_ALLOWED_ORIGINS": origin,
            "PALWORLD_WEB_ALLOWED_HOSTS": "",
        })
        if rendered_time is not None:
            values["BACKUP_TIME"] = rendered_time
        # Exercise the full config schema before any write.
        candidate = CaretakerConfig(values, current.schema, current.directory)
        editable = self.directory / _EDITABLE_DIRECTORY
        # First-run provisioning must never fall back to the protected root
        # used by older deployments.  Upgrade once so the installer creates
        # the manager-owned editable directory.
        if not editable.is_dir() or editable.is_symlink():
            raise SettingsPersistenceError("first-run setup requires the editable configuration directory")
        info = _safe_directory(editable, message="editable configuration directory is unavailable")
        if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_gid != os.getegid()):
            raise SettingsPersistenceError("editable configuration directory ownership is unsafe")

        caretaker_updates = {
            "PALWORLD_BACKUP_SCHEDULE_ENABLED": schedule_enabled,
            "PALWORLD_WEB_BIND_IP": values["PALWORLD_WEB_BIND_IP"],
            "PALWORLD_WEB_ALLOWED_ORIGINS": origin,
            "PALWORLD_WEB_ALLOWED_HOSTS": "",
        }
        if rendered_time is not None:
            caretaker_updates["BACKUP_TIME"] = rendered_time
        secrets_updates = {"SERVER_PASSWORD": server_password}
        if replace_placeholder_panel_password:
            values["PALWORLD_WEB_UI_PASSWORD"] = server_password
            secrets_updates["PALWORLD_WEB_UI_PASSWORD"] = server_password
        self._publish_editable({
            "caretaker.env": caretaker_updates,
            "server.env": {"SERVER_NAME": server_name},
            "secrets.env": secrets_updates,
        }, directory=editable)
        return self.current()

    def configure_discord(self, *, token: str, channel_id: str) -> CaretakerConfig:
        """Atomically commit the two files used by the Discord mini-wizard."""
        editable = self.directory / _EDITABLE_DIRECTORY
        if not editable.is_dir() or editable.is_symlink():
            raise SettingsPersistenceError("Discord setup requires the editable configuration directory")
        self._publish_editable({
            "secrets.env": {"DISCORD_BOT_TOKEN": token},
            "server.env": {"DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS": channel_id},
        }, directory=editable)
        return self.current()
