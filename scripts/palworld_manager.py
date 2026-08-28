#!/usr/bin/env python3
"""Shared, fail-closed Palworld REST and service state helpers."""
from __future__ import annotations

import argparse
import base64
from contextlib import nullcontext
from dataclasses import asdict, dataclass
import json
import math
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
try:
    import fcntl
except ImportError:  # pragma: no cover - the deployed adapter is Linux-only
    fcntl = None

try:
    from palworld_caretaker.backup import BackupManager as _CoreBackupManager
    from palworld_caretaker.audit import AuditLog as _CoreAuditLog
    from palworld_caretaker.errors import SnapshotError as _CoreSnapshotError
    from palworld_caretaker.operations import OperationLock as _CoreOperationLock
    from palworld_caretaker.operations import OperationLockBusy as _CoreOperationLockBusy
except ImportError:
    _CoreBackupManager = None
    _CoreAuditLog = None
    class _CoreSnapshotError(RuntimeError):
        pass

    _CoreOperationLock = None
    class _CoreOperationLockBusy(RuntimeError):
        pass


class ConfigError(RuntimeError):
    pass


class ApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiagnosticCheck:
    """One secret-free deployment diagnostic result."""

    name: str
    status: str
    message: str


DEFAULT_CONFIG: dict[str, str] = {
    "PALWORLD_INSTALL_ROOT": "/srv/palworld",
    "PALWORLD_BACKUP_DIR": "/mnt/qnap-tyt/palworld-backups",
    "PALWORLD_BACKUP_MOUNT": "/mnt/qnap-tyt",
    "PALWORLD_BACKUP_REQUIRE_MOUNT": "true",
    "PALWORLD_MANAGER_STATE_DIR": "/var/lib/palworld-manager",
    "PALWORLD_SERVICE_USER": "palworld",
    "PALWORLD_MANAGER_USER": "palworld-manager",
    "MAX_PLAYERS": "10",
    "BASE_CAMP_MAX_NUM_IN_GUILD": "10",
    "DAY_TIME_SPEED_RATE": "1.0", "NIGHT_TIME_SPEED_RATE": "1.0",
    "EXP_RATE": "1.0", "PAL_CAPTURE_RATE": "1.0",
    "COLLECTION_DROP_RATE": "1.0", "ENEMY_DROP_ITEM_RATE": "1.0",
    "PAL_DAMAGE_RATE_ATTACK": "1.0", "PAL_DAMAGE_RATE_DEFENSE": "1.0",
    "PLAYER_DAMAGE_RATE_ATTACK": "1.0", "PLAYER_DAMAGE_RATE_DEFENSE": "1.0",
    "GUILD_PLAYER_MAX_NUM": "20", "PAL_SPAWN_NUM_RATE": "1.0",
    "DROP_ITEM_MAX_NUM": "3000", "PAL_EGG_DEFAULT_HATCHING_TIME": "72.0",
    "SERVER_NAME": "Palworld Dedicated Server",
    "SERVER_DESCRIPTION": "Private Palworld Dedicated Server",
    "PUBLIC_PORT": "8211",
    "PALWORLD_REST_API_HOST": "127.0.0.1",
    "PALWORLD_REST_API_PORT": "8212",
    "PALWORLD_REST_API_USERNAME": "admin",
    "PALWORLD_API_TIMEOUT_SECONDS": "5",
    "PALWORLD_IDLE_SHUTDOWN_ENABLED": "true",
    "PALWORLD_IDLE_TIMEOUT_MINUTES": "10",
    "PALWORLD_PLAYER_CHECK_INTERVAL_SECONDS": "60",
    "PALWORLD_STARTUP_GRACE_SECONDS": "600",
    "PALWORLD_SHUTDOWN_WAIT_SECONDS": "30",
    "PALWORLD_IDLE_WATCHER_DRY_RUN": "true",
    "PALWORLD_START_READY_TIMEOUT_SECONDS": "180",
    "DISCORD_PALWORLD_ALLOWED_GUILD_IDS": "",
    "DISCORD_PALWORLD_ALLOWED_ROLE_IDS": "",
    "DISCORD_PALWORLD_ADMIN_ROLE_IDS": "",
    "DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS": "",
    "PALWORLD_WEB_UI_USERNAME": "palworld-manager",
    "PALWORLD_WEB_UI_PASSWORD": "",
    "PALWORLD_SAVEGAMES_EXPORT_MAX_BYTES": str(8 * 1024 ** 3),
    "BACKUP_RETENTION_COUNT": "14",
    "BACKUP_TIME": "04:30",
    "SERVER_PASSWORD": "",
    "ADMIN_PASSWORD": "",
    "DISCORD_BOT_TOKEN": "",
}

CONFIG_FILES = ("caretaker.env", "server.env", "secrets.env")
LEGACY_CONFIG_FILE = "palworld.env"
EDITABLE_CONFIG_DIRECTORY = "editable"
EDITABLE_CONFIG_FILES = ("caretaker.env", "server.env")
# Keep this bootstrap-safe copy in sync with
# palworld_caretaker.settings.SETTING_SPECS.  The installer invokes this module
# before the versioned Python package has been deployed, so importing that
# module here would weaken fresh-install preflight.
EDITABLE_SETTING_KEYS = frozenset({
    "SERVER_NAME", "SERVER_DESCRIPTION", "MAX_PLAYERS",
    "DAY_TIME_SPEED_RATE", "NIGHT_TIME_SPEED_RATE", "EXP_RATE",
    "PAL_CAPTURE_RATE", "COLLECTION_DROP_RATE", "ENEMY_DROP_ITEM_RATE",
    "PAL_DAMAGE_RATE_ATTACK", "PAL_DAMAGE_RATE_DEFENSE",
    "PLAYER_DAMAGE_RATE_ATTACK", "PLAYER_DAMAGE_RATE_DEFENSE",
    "GUILD_PLAYER_MAX_NUM", "BASE_CAMP_MAX_NUM_IN_GUILD",
    "PAL_SPAWN_NUM_RATE", "DROP_ITEM_MAX_NUM",
    "PAL_EGG_DEFAULT_HATCHING_TIME", "PALWORLD_IDLE_SHUTDOWN_ENABLED",
    "PALWORLD_IDLE_TIMEOUT_MINUTES", "BACKUP_RETENTION_COUNT", "BACKUP_TIME",
})
SETTINGS_BACKUP_DIRECTORY = "settings-backups"
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SAFE_ACCOUNT_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$", re.ASCII)
_DISCORD_ID_RE = re.compile(r"^[0-9]+$")


def _parse_env_value(raw: str, path: Path, number: int) -> str:
    """Parse one dotenv value without expansion, substitution, or shell execution."""
    value = raw.lstrip(" \t")
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        output: list[str] = []
        escaped = False
        end = None
        for index, character in enumerate(value[1:], 1):
            if quote == '"' and escaped:
                if character not in {'"', "\\"}:
                    output.append("\\")
                output.append(character)
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                end = index + 1
                break
            else:
                output.append(character)
        if escaped:
            raise ConfigError(f"{path}:{number}: trailing escape in quoted value")
        if end is None:
            raise ConfigError(f"{path}:{number}: unterminated quoted value")
        trailing = value[end:].strip()
        if trailing and not trailing.startswith("#"):
            raise ConfigError(f"{path}:{number}: unexpected text after quoted value")
        parsed = "".join(output)
    else:
        # A comment starts only after whitespace, so values such as abc#123 and
        # paths containing # retain their literal meaning.
        match = re.search(r"[ \t]+#", value)
        parsed = value[:match.start()].rstrip() if match else value.rstrip()
        if any(character in parsed for character in " \t"):
            raise ConfigError(f"{path}:{number}: unquoted values may not contain whitespace")
    if "\x00" in parsed or "\r" in parsed or "\n" in parsed:
        raise ConfigError(f"{path}:{number}: value contains a forbidden control character")
    return parsed


def load_env(path: str | Path) -> dict[str, str]:
    """Load a strict dotenv file as data; values are never evaluated by a shell."""
    source = Path(path)
    values: dict[str, str] = {}
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{source}: configuration must be UTF-8") from exc
    except OSError as exc:
        raise ConfigError(f"{source}: cannot read configuration: {exc.strerror}") from exc
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{source}:{number}: expected KEY=VALUE")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            raise ConfigError(f"{source}:{number}: invalid configuration key {key!r}")
        if key in values:
            raise ConfigError(f"{source}:{number}: duplicate configuration key {key}")
        values[key] = _parse_env_value(raw_value, source, number)
    return values


def load_config(
    config_dir: str | Path = "/srv/palworld/config", *, require_file: bool = True,
) -> dict[str, str]:
    """Merge protected layers, manager-editable layers, then secrets."""
    directory = Path(config_dir)
    config = dict(DEFAULT_CONFIG)
    loaded = 0
    legacy = directory / LEGACY_CONFIG_FILE
    if legacy.is_file():
        config.update(load_env(legacy))
        loaded += 1
    for filename in EDITABLE_CONFIG_FILES:
        path = directory / filename
        if path.is_file():
            config.update(load_env(path))
            loaded += 1
    for filename in EDITABLE_CONFIG_FILES:
        path = directory / EDITABLE_CONFIG_DIRECTORY / filename
        if path.is_file():
            editable_values = load_env(path)
            forbidden = sorted(set(editable_values) - EDITABLE_SETTING_KEYS)
            if forbidden:
                raise ConfigError(
                    f"{path}: editable configuration may contain only setting keys: "
                    f"{', '.join(forbidden)}"
                )
            config.update(editable_values)
            loaded += 1
    path = directory / "secrets.env"
    if path.is_file():
        config.update(load_env(path))
        loaded += 1
    if require_file and not loaded:
        raise ConfigError(f"{directory}: no deployment configuration file found")
    return config


def load_runtime_config(source: str | Path) -> dict[str, str]:
    """Load a config directory, the legacy entrypoint, or a standalone env file."""
    path = Path(source)
    if path.is_dir() or path.name == LEGACY_CONFIG_FILE:
        directory = path if path.is_dir() else path.parent
        return load_config(directory)
    config = dict(DEFAULT_CONFIG)
    config.update(load_env(path))
    return config


def _absolute_path(config: dict[str, str], key: str) -> Path:
    raw = config.get(key, "")
    if not raw:
        raise ConfigError(f"{key} must not be empty")
    path = Path(raw)
    if not path.is_absolute():
        raise ConfigError(f"{key} must be an absolute path")
    if any(part == ".." for part in path.parts):
        raise ConfigError(f"{key} must not contain '..'")
    return Path(os.path.normpath(raw))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_config(config: dict[str, str]) -> dict[str, Path | None]:
    """Validate values and return canonical, derived deployment paths."""
    unknown = sorted(set(config) - set(DEFAULT_CONFIG))
    if unknown:
        raise ConfigError(f"unknown configuration key(s): {', '.join(unknown)}")

    install_root = _absolute_path(config, "PALWORLD_INSTALL_ROOT")
    backup_dir = _absolute_path(config, "PALWORLD_BACKUP_DIR")
    state_dir = _absolute_path(config, "PALWORLD_MANAGER_STATE_DIR")
    require_mount = env_bool(config, "PALWORLD_BACKUP_REQUIRE_MOUNT", True)
    mount_raw = config.get("PALWORLD_BACKUP_MOUNT", "")
    backup_mount = _absolute_path(config, "PALWORLD_BACKUP_MOUNT") if mount_raw else None
    if (install_root == Path("/") or backup_dir == Path("/") or
            state_dir == Path("/") or backup_mount == Path("/")):
        raise ConfigError("deployment paths must not be the filesystem root")
    if _is_relative_to(backup_dir, install_root) or _is_relative_to(install_root, backup_dir):
        raise ConfigError("PALWORLD_BACKUP_DIR and PALWORLD_INSTALL_ROOT must not overlap")
    if require_mount:
        if backup_mount is None:
            raise ConfigError("PALWORLD_BACKUP_MOUNT is required when mount checking is enabled")
        if backup_dir == backup_mount or not _is_relative_to(backup_dir, backup_mount):
            raise ConfigError("PALWORLD_BACKUP_DIR must be below PALWORLD_BACKUP_MOUNT")

    env_int(config, "MAX_PLAYERS", 10, 1, 32)
    env_int(config, "BASE_CAMP_MAX_NUM_IN_GUILD", 10, 1, 10)
    env_int(config, "GUILD_PLAYER_MAX_NUM", 20, 1, 100)
    env_int(config, "DROP_ITEM_MAX_NUM", 3000, 1, 10000)
    for key, minimum, maximum in (
        ("DAY_TIME_SPEED_RATE", .1, 5), ("NIGHT_TIME_SPEED_RATE", .1, 5),
        ("EXP_RATE", .1, 20), ("PAL_CAPTURE_RATE", .1, 5),
        ("COLLECTION_DROP_RATE", .1, 5), ("ENEMY_DROP_ITEM_RATE", .1, 5),
        ("PAL_DAMAGE_RATE_ATTACK", .1, 5), ("PAL_DAMAGE_RATE_DEFENSE", .1, 5),
        ("PLAYER_DAMAGE_RATE_ATTACK", .1, 5), ("PLAYER_DAMAGE_RATE_DEFENSE", .1, 5),
        ("PAL_SPAWN_NUM_RATE", .1, 5), ("PAL_EGG_DEFAULT_HATCHING_TIME", 0, 240),
    ):
        env_float(config, key, minimum, maximum)
    public_port = env_int(config, "PUBLIC_PORT", 8211, 1, 65535)
    rest_port = env_int(config, "PALWORLD_REST_API_PORT", 8212, 1, 65535)
    if public_port == rest_port:
        raise ConfigError("PUBLIC_PORT and PALWORLD_REST_API_PORT must be different")
    env_int(config, "PALWORLD_API_TIMEOUT_SECONDS", 5, 1, 30)
    env_int(config, "PALWORLD_SAVEGAMES_EXPORT_MAX_BYTES", 8 * 1024 ** 3, 1, 64 * 1024 ** 3)
    env_int(config, "PALWORLD_IDLE_TIMEOUT_MINUTES", 10, 1, 10080)
    env_int(config, "PALWORLD_PLAYER_CHECK_INTERVAL_SECONDS", 60, 5, 3600)
    env_int(config, "PALWORLD_STARTUP_GRACE_SECONDS", 600, 0, 86400)
    env_int(config, "PALWORLD_SHUTDOWN_WAIT_SECONDS", 30, 1, 300)
    env_int(config, "PALWORLD_START_READY_TIMEOUT_SECONDS", 180, 1, 3600)
    env_int(config, "BACKUP_RETENTION_COUNT", 14, 1, 1000)
    env_bool(config, "PALWORLD_IDLE_SHUTDOWN_ENABLED", True)
    env_bool(config, "PALWORLD_IDLE_WATCHER_DRY_RUN", True)
    if config.get("PALWORLD_REST_API_HOST", "") != "127.0.0.1":
        raise ConfigError("PALWORLD_REST_API_HOST must be 127.0.0.1")
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", config.get("BACKUP_TIME", "")):
        raise ConfigError("BACKUP_TIME must use 24-hour HH:MM format")
    for key in ("PALWORLD_SERVICE_USER", "PALWORLD_MANAGER_USER"):
        if not _SAFE_ACCOUNT_RE.fullmatch(config.get(key, "")):
            raise ConfigError(f"{key} is not a valid system account name")
    for key in (
        "DISCORD_PALWORLD_ALLOWED_GUILD_IDS", "DISCORD_PALWORLD_ALLOWED_ROLE_IDS",
        "DISCORD_PALWORLD_ADMIN_ROLE_IDS", "DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS",
    ):
        value = config.get(key, "")
        if value == "*" and key == "DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS":
            continue
        if value and any(not _DISCORD_ID_RE.fullmatch(item.strip()) for item in value.split(",")):
            raise ConfigError(f"{key} must be a comma-separated list of numeric IDs")

    return {
        "install_root": install_root,
        "server_root": install_root / "server",
        "config_root": install_root / "config",
        "scripts_root": install_root / "scripts",
        "local_backup_root": install_root / "backups-local",
        "backup_dir": backup_dir,
        "backup_mount": backup_mount,
        "state_dir": state_dir,
        "settings_backup_dir": state_dir / SETTINGS_BACKUP_DIRECTORY,
    }


def config_value(config: dict[str, str], key: str) -> str:
    """Return a configured or derived value for non-shell consumers."""
    paths = validate_config(config)
    derived = {
        "PALWORLD_SERVER_ROOT": paths["server_root"],
        "PALWORLD_CONFIG_ROOT": paths["config_root"],
        "PALWORLD_SCRIPTS_ROOT": paths["scripts_root"],
        "PALWORLD_LOCAL_BACKUP_ROOT": paths["local_backup_root"],
        "PALWORLD_SETTINGS_BACKUP_DIR": paths["settings_backup_dir"],
    }
    if key in config:
        return config[key]
    if key in derived:
        return str(derived[key])
    raise ConfigError(f"unknown configuration value: {key}")


def _systemd_quote(value: str) -> str:
    """Quote a scalar and suppress systemd specifier expansion."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _systemd_path(value: str) -> str:
    """Escape an absolute path used by a non-command systemd directive."""
    output: list[str] = []
    for character in value:
        if character == "%":
            output.append("%%")
        elif character in {"\\", '"', "'"}:
            output.extend(f"\\x{byte:02x}" for byte in character.encode("utf-8"))
        elif character.isspace():
            encoded = character.encode("utf-8")
            output.extend(f"\\x{byte:02x}" for byte in encoded)
        else:
            output.append(character)
    return "".join(output)


def render_systemd_units(
    config: dict[str, str], source_dir: str | Path, output_dir: str | Path,
) -> list[Path]:
    """Render unit templates using only validated deployment configuration."""
    paths = validate_config(config)
    source = Path(source_dir)
    output = Path(output_dir)
    if not source.is_dir():
        raise ConfigError(f"unit template directory does not exist: {source}")
    output.mkdir(parents=True, exist_ok=True)
    replacements = {
        "@SERVICE_USER@": config["PALWORLD_SERVICE_USER"],
        "@MANAGER_USER@": config["PALWORLD_MANAGER_USER"],
        "@SERVER_ROOT@": _systemd_path(str(paths["server_root"])),
        "@CONFIG_ROOT@": _systemd_path(str(paths["config_root"])),
        "@SCRIPTS_ROOT@": _systemd_path(str(paths["scripts_root"])),
        "@STATE_DIR@": _systemd_path(str(paths["state_dir"])),
        "@SETTINGS_BACKUP_DIR@": _systemd_path(str(paths["settings_backup_dir"])),
        "@HOME_ENV@": _systemd_quote(f"HOME={paths['server_root']}"),
        "@CONFIG_ENV@": _systemd_quote(f"PALWORLD_CONFIG={paths['config_root']}"),
        "@CONFIG_DIR_ENV@": _systemd_quote(f"PALWORLD_CONFIG_DIR={paths['config_root']}"),
        "@IDLE_STATE_ENV@": _systemd_quote(f"PALWORLD_STATE={paths['state_dir']}/idle-state.json"),
        "@MAINTENANCE_STATE_ENV@": _systemd_quote(
            f"PALWORLD_MAINTENANCE_STATE={paths['state_dir']}/maintenance-state.json"
        ),
        "@PALSERVER@": _systemd_quote(str(paths["server_root"] / "PalServer.sh")),
        "@BACKUP_SCRIPT@": _systemd_quote(str(paths["scripts_root"] / "backup-palworld.sh")),
        "@RESTORE_SCRIPT@": _systemd_quote(str(paths["scripts_root"] / "restore-palworld.sh")),
        "@MAINTENANCE_SCRIPT@": _systemd_quote(
            str(paths["scripts_root"] / "daily-palworld-maintenance.sh")
        ),
        "@FIREWALL_SCRIPT@": _systemd_quote(str(paths["scripts_root"] / "palworld-rest-firewall")),
        "@IDLE_SCRIPT@": _systemd_quote(str(paths["scripts_root"] / "palworld-idle-watcher.py")),
        "@DISCORD_SCRIPT@": _systemd_quote(str(paths["scripts_root"] / "palworld-discord-bot.py")),
        "@WEB_UI_SCRIPT@": _systemd_quote(str(paths["scripts_root"] / "palworld-web-ui.py")),
        "@VENV_PYTHON@": _systemd_quote(str(paths["install_root"] / "venv/bin/python")),
        "@BACKUP_TIME@": f"*-*-* {config['BACKUP_TIME']}:00",
    }
    rendered: list[Path] = []
    for template in sorted(source.glob("palworld*.service")) + sorted(source.glob("palworld*.timer")):
        text = template.read_text(encoding="utf-8")
        for marker, value in replacements.items():
            text = text.replace(marker, value)
        remaining = sorted(set(re.findall(r"@[A-Z][A-Z0-9_]*@", text)))
        if remaining:
            raise ConfigError(f"{template}: unknown template marker(s): {', '.join(remaining)}")
        destination = output / template.name
        destination.write_text(text, encoding="utf-8")
        rendered.append(destination)
    if not rendered:
        raise ConfigError(f"no systemd unit templates found in {source}")
    return rendered


class PreflightReport:
    """Collected preflight failures; falsey means the deployment is safe to use."""

    def __init__(self, errors: list[str] | None = None):
        self.errors = errors or []

    @property
    def ok(self) -> bool:
        return not self.errors

    def __bool__(self) -> bool:
        return self.ok

    def raise_for_errors(self) -> None:
        if self.errors:
            raise ConfigError("preflight failed:\n- " + "\n- ".join(self.errors))


def preflight_values(
    config: dict[str, str], *, config_dir: str | Path | None = None,
    deployed: bool = True,
) -> PreflightReport:
    """Validate the full value/secret contract without deployment path checks."""
    validate_config(config)
    errors: list[str] = []
    if config_dir is not None:
        secrets = Path(config_dir) / "secrets.env"
        if secrets.exists() or secrets.is_symlink():
            if secrets.is_symlink() or not secrets.is_file():
                errors.append(f"secrets.env must be a regular file, not a symlink: {secrets}")
            else:
                info = secrets.stat()
                mode = stat.S_IMODE(info.st_mode)
                if mode != 0o640:
                    errors.append(f"secrets.env permissions must be exactly 0640 (found {mode:04o})")
                # Install/upgrade preflight runs as root.  Development and
                # unprivileged value checks cannot meaningfully assert root
                # ownership, but a deployed configuration must have it.
                if deployed and os.geteuid() == 0:
                    try:
                        manager_gid = pwd.getpwnam(config["PALWORLD_MANAGER_USER"]).pw_gid
                    except KeyError:
                        errors.append(
                            "PALWORLD_MANAGER_USER does not resolve to a local account "
                            "for secrets.env ownership validation"
                        )
                    else:
                        if info.st_uid != 0 or info.st_gid != manager_gid:
                            errors.append(
                                "secrets.env owner/group must be root:"
                                f"{config['PALWORLD_MANAGER_USER']}"
                            )
    for key in ("SERVER_PASSWORD", "ADMIN_PASSWORD"):
        value = config.get(key, "")
        if not value or value.startswith("CHANGE_ME"):
            errors.append(f"{key} must be configured")
        elif any(character in value for character in ',()"\r\n'):
            errors.append(f"{key} contains a Palworld INI-reserved character")
    for key in ("SERVER_NAME", "SERVER_DESCRIPTION"):
        value = config.get(key, "")
        if not value:
            errors.append(f"{key} must not be empty")
        elif any(character in value for character in ',()"\r\n'):
            errors.append(f"{key} contains a Palworld INI-reserved character")
    web_password = config.get("PALWORLD_WEB_UI_PASSWORD", "")
    if web_password and any(character in web_password for character in "\r\n"):
        errors.append("PALWORLD_WEB_UI_PASSWORD contains a forbidden control character")
    return PreflightReport(errors)


def preflight_config(
    config: dict[str, str], *, config_dir: str | Path | None = None,
    mount_checker=os.path.ismount,
) -> PreflightReport:
    """Check deployment paths, permissions, symlinks, and backup mount safety."""
    paths = validate_config(config)
    errors = list(preflight_values(config, config_dir=config_dir, deployed=True).errors)

    def has_mode(path: Path, mask: int) -> bool:
        return bool(stat.S_IMODE(path.stat().st_mode) & mask)

    for name in ("install_root", "server_root", "config_root", "scripts_root", "state_dir"):
        path = paths[name]
        if path.is_symlink():
            errors.append(f"{name} must not be a symbolic link: {path}")
        elif not path.exists():
            errors.append(f"{name} does not exist: {path}")
        elif not path.is_dir():
            errors.append(f"{name} is not a directory: {path}")
        elif (not has_mode(path, 0o444) or not has_mode(path, 0o111) or
              not os.access(path, os.R_OK | os.X_OK)):
            errors.append(f"{name} is not readable/searchable: {path}")

    server_root = paths["server_root"]
    if server_root.is_dir() and (not has_mode(server_root, 0o222) or not os.access(server_root, os.W_OK)):
        errors.append(f"server_root is not writable: {server_root}")
    state_dir = paths["state_dir"]
    if state_dir.is_dir() and (not has_mode(state_dir, 0o222) or not os.access(state_dir, os.W_OK)):
        errors.append(f"state_dir is not writable: {state_dir}")

    # The web editor may replace only the isolated non-secret layer.  Check
    # the ownership contract when this root-owned deployment preflight runs;
    # unprivileged development checks intentionally cannot assert account IDs.
    config_root = paths["config_root"]
    if os.geteuid() == 0 and config_root.is_dir():
        try:
            manager = pwd.getpwnam(config["PALWORLD_MANAGER_USER"])
        except KeyError:
            errors.append("PALWORLD_MANAGER_USER does not resolve for editable configuration validation")
        else:
            info = config_root.stat()
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, manager.pw_gid, 0o750):
                errors.append("config_root must be root:<PALWORLD_MANAGER_USER> with mode 0750")
            editable = config_root / EDITABLE_CONFIG_DIRECTORY
            if editable.is_symlink() or not editable.is_dir():
                errors.append("editable configuration directory must be a real directory")
            else:
                editable_info = editable.stat()
                if (editable_info.st_uid, editable_info.st_gid, stat.S_IMODE(editable_info.st_mode)) != (
                    manager.pw_uid, manager.pw_gid, 0o750,
                ):
                    errors.append("editable configuration directory must be manager-owned with mode 0750")
                for name in EDITABLE_CONFIG_FILES:
                    path = editable / name
                    if path.exists() or path.is_symlink():
                        if path.is_symlink() or not path.is_file():
                            errors.append(f"editable {name} must be a regular file")
                        else:
                            file_info = path.stat()
                            if (file_info.st_uid, file_info.st_gid, stat.S_IMODE(file_info.st_mode)) != (
                                manager.pw_uid, manager.pw_gid, 0o640,
                            ):
                                errors.append(f"editable {name} must be manager-owned with mode 0640")

    backup_dir = paths["backup_dir"]
    mount = paths["backup_mount"]
    if env_bool(config, "PALWORLD_BACKUP_REQUIRE_MOUNT", True):
        assert mount is not None
        if mount.is_symlink():
            errors.append(f"backup_mount must not be a symbolic link: {mount}")
        elif not mount.is_dir():
            errors.append(f"backup_mount is not a directory: {mount}")
        elif not mount_checker(mount):
            errors.append(f"backup_mount is not mounted: {mount}")
    target = backup_dir if backup_dir.exists() else backup_dir.parent
    if backup_dir.exists() and (backup_dir.is_symlink() or not backup_dir.is_dir()):
        errors.append(f"backup_dir must be a real directory: {backup_dir}")
    elif (not target.is_dir() or not has_mode(target, 0o222) or
          not has_mode(target, 0o111) or not os.access(target, os.W_OK | os.X_OK)):
        errors.append(f"backup destination is not writable: {target}")

    return PreflightReport(errors)


def diagnose_deployment(
    config: dict[str, str], *, config_dir: str | Path,
    mount_checker=os.path.ismount, command_runner=subprocess.run,
) -> list[DiagnosticCheck]:
    """Inspect a deployed caretaker without changing services or files."""
    checks: list[DiagnosticCheck] = []
    paths = validate_config(config)
    report = preflight_config(
        config, config_dir=config_dir, mount_checker=mount_checker,
    )
    if report.ok:
        checks.append(DiagnosticCheck(
            "preflight", "pass", "configuration, paths, permissions, and backup policy are valid",
        ))
    else:
        checks.extend(DiagnosticCheck("preflight", "fail", error) for error in report.errors)

    required_files = {
        "PalServer": paths["server_root"] / "PalServer.sh",
        "settings": paths["server_root"] / "Pal/Saved/Config/LinuxServer/PalWorldSettings.ini",
        "configuration manager": paths["scripts_root"] / "palworld_manager.py",
        "backup tool": paths["scripts_root"] / "backup-palworld.sh",
        "restore tool": paths["scripts_root"] / "restore-palworld.sh",
    }
    for name, path in required_files.items():
        if path.is_file():
            checks.append(DiagnosticCheck(name, "pass", f"present: {path}"))
        else:
            checks.append(DiagnosticCheck(name, "fail", f"missing: {path}"))

    systemctl = shutil.which("systemctl")
    if systemctl is None:
        checks.append(DiagnosticCheck("systemd", "warn", "systemctl is not available"))
        return checks

    service_names = (
        "palworld.service", "palworld-rest-firewall.service",
        "palworld-idle-watcher.service", "palworld-backup.timer",
        "palworld-discord-bot.service",
    )
    active_game = False
    for service in service_names:
        try:
            result = command_runner(
                [systemctl, "show", service, "--property=LoadState,ActiveState,UnitFileState",
                 "--value"],
                text=True, capture_output=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append(DiagnosticCheck(service, "warn", f"systemd query failed: {exc}"))
            continue
        values = result.stdout.splitlines()
        load = values[0].strip() if len(values) > 0 else "unknown"
        active = values[1].strip() if len(values) > 1 else "unknown"
        enabled = values[2].strip() if len(values) > 2 else "unknown"
        if service == "palworld.service":
            active_game = active == "active"
        if load == "not-found" or result.returncode != 0:
            checks.append(DiagnosticCheck(service, "fail", "systemd unit is not installed"))
        elif active == "failed":
            checks.append(DiagnosticCheck(service, "fail", f"failed (enablement: {enabled})"))
        elif active in {"active", "activating"}:
            checks.append(DiagnosticCheck(service, "pass", f"{active} (enablement: {enabled})"))
        else:
            checks.append(DiagnosticCheck(service, "warn", f"{active} (enablement: {enabled})"))

    if active_game:
        try:
            players = PalworldAPI(config).players()
            checks.append(DiagnosticCheck(
                "REST API", "pass", f"reachable on localhost; {len(players)} player(s) online",
            ))
        except (ConfigError, ApiError):
            checks.append(DiagnosticCheck(
                "REST API", "fail", "game service is active but the localhost API is unavailable",
            ))
    else:
        checks.append(DiagnosticCheck(
            "REST API", "warn", "not queried because the game service is not active",
        ))
    return checks


def diagnostic_exit_code(checks: list[DiagnosticCheck]) -> int:
    """Use 2 for actionable failures; warnings alone remain automation-friendly."""
    return 2 if any(check.status == "fail" for check in checks) else 0


def env_bool(config: dict[str, str], key: str, default: bool = False) -> bool:
    value = config.get(key, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ConfigError(f"{key} must be true or false")
    return value == "true"


def env_int(config: dict[str, str], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(config.get(key, str(default)))
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


def env_float(config: dict[str, str], key: str, minimum: float, maximum: float) -> float:
    try:
        value = float(config[key])
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"{key} must be a number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum:g} and {maximum:g}")
    return value


class _LegacyNoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the legacy transport from forwarding credentials after a redirect."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class _LegacyPalworldAPI:
    def __init__(self, config: dict[str, str]):
        host = config.get("PALWORLD_REST_API_HOST", "127.0.0.1")
        if host != "127.0.0.1":
            raise ConfigError("PALWORLD_REST_API_HOST must be 127.0.0.1")
        port = env_int(config, "PALWORLD_REST_API_PORT", 8212, 1, 65535)
        url_host = f"[{host}]" if ":" in host else host
        self.base = f"http://{url_host}:{port}/v1/api"
        self.username = config.get("PALWORLD_REST_API_USERNAME", "admin")
        self.password = config.get("ADMIN_PASSWORD", "")
        if not self.password:
            raise ConfigError("ADMIN_PASSWORD is required")
        self.timeout = env_int(config, "PALWORLD_API_TIMEOUT_SECONDS", 5, 1, 30)
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _LegacyNoRedirect())

    def request(self, method: str, endpoint: str, body: dict | None = None, expect_json: bool = False):
        data = None if body is None else json.dumps(body).encode("utf-8")
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
        request = urllib.request.Request(
            self.base + endpoint,
            data=data,
            method=method,
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
                if response.status != 200:
                    raise ApiError(f"HTTP {response.status}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError("API request failed") from exc
        if not expect_json:
            return None
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("API returned invalid JSON") from exc

    def players(self) -> list[str]:
        payload = self.request("GET", "/players", expect_json=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("players"), list):
            raise ApiError("API players schema is invalid")
        names: list[str] = []
        for player in payload["players"]:
            if not isinstance(player, dict) or not isinstance(player.get("name"), str):
                raise ApiError("API player entry schema is invalid")
            names.append(player["name"])
        return names

    def ready(self) -> bool:
        self.players()
        return True

    def save(self) -> None:
        self.request("POST", "/save")

    def shutdown(self, wait_seconds: int, message: str) -> None:
        self.request("POST", "/shutdown", {"waittime": wait_seconds, "message": message})


# A deployed v0.2 installation carries the portable package beside this
# compatibility entrypoint.  Old single-file installations keep the original
# implementation until upgraded, so no shell or Python caller breaks.
try:
    from palworld_caretaker.errors import ApiError as _CoreApiError, ConfigError as _CoreConfigError
    from palworld_caretaker.rest import PalworldRESTClient as _CorePalworldAPI
except ImportError:
    PalworldAPI = _LegacyPalworldAPI
else:
    class PalworldAPI(_CorePalworldAPI):
        """Keep the historical manager exception types at its public boundary."""
        def __init__(self, *args, **kwargs):
            try:
                super().__init__(*args, **kwargs)
            except _CoreConfigError as exc:
                raise ConfigError(str(exc)) from exc

        def request(self, *args, **kwargs):
            try:
                return super().request(*args, **kwargs)
            except _CoreApiError as exc:
                raise ApiError(str(exc)) from exc

        def player_records(self):
            try:
                return super().player_records()
            except _CoreApiError as exc:
                raise ApiError(str(exc)) from exc

        def metrics(self):
            try:
                return super().metrics()
            except _CoreApiError as exc:
                raise ApiError(str(exc)) from exc

        def broadcast(self, message):
            try:
                return super().broadcast(message)
            except _CoreApiError as exc:
                raise ApiError(str(exc)) from exc

        def shutdown(self, wait_seconds, message):
            try:
                return super().shutdown(wait_seconds, message)
            except _CoreApiError as exc:
                raise ApiError(str(exc)) from exc


def service_property(name: str) -> str:
    result = subprocess.run(
        ["systemctl", "show", "palworld.service", f"--property={name}", "--value"],
        text=True, capture_output=True, timeout=5, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def service_active() -> bool:
    return service_state() == "active"


def service_state() -> str:
    result = subprocess.run(
        ["systemctl", "is-active", "palworld.service"], text=True,
        capture_output=True, timeout=5, check=False
    )
    state = result.stdout.strip()
    return state if state in {"active", "inactive", "failed", "activating", "deactivating"} else "unknown"


def service_lifecycle() -> str:
    return service_property("InvocationID")


def service_uptime_seconds() -> int | None:
    value = service_property("ActiveEnterTimestampMonotonic")
    if not value.isdigit() or int(value) <= 0:
        return None
    return max(0, int(time.monotonic() - int(value) / 1_000_000))


def read_state(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def write_state(path: str | Path, state: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".state-", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _core_backup_engine(config: dict[str, str]):
    if _CoreBackupManager is None:
        raise ConfigError("Python backup core is not installed beside palworld_manager.py")
    paths = validate_config(config)
    return _CoreBackupManager(
        save_root=paths["server_root"] / "Pal/Saved/SaveGames",
        config_root=paths["server_root"] / "Pal/Saved/Config",
        backup_root=paths["backup_dir"], local_backup_root=paths["local_backup_root"],
        retention_count=env_int(config, "BACKUP_RETENTION_COUNT", 14, 1, 1000),
        backup_mount=paths["backup_mount"],
        require_mount=env_bool(config, "PALWORLD_BACKUP_REQUIRE_MOUNT", True),
    )


def _core_backup(config: dict[str, str]) -> Path:
    """Linux adapter around the portable snapshot engine for the old shell CLI."""
    if _CoreBackupManager is None:
        raise ConfigError("Python backup core is not installed beside palworld_manager.py")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise ConfigError("run this script with sudo")
    if (_CoreOperationLock is None and
            os.environ.get("PALWORLD_OPERATION_LOCK_HELD") != "1"):
        raise ConfigError("backup locking is unavailable on this platform")
    try:
        lock = nullcontext() if os.environ.get("PALWORLD_OPERATION_LOCK_HELD") == "1" else _CoreOperationLock(
            manager_user=config["PALWORLD_MANAGER_USER"]
        )
        with lock:
            # Maintenance has already acquired this lock and explicitly marks
            # its child backup.  A standalone backup checks maintenance only
            # after taking the shared lock, eliminating the TOCTOU window.
            if os.environ.get("PALWORLD_OPERATION_LOCK_HELD") != "1":
                result = subprocess.run(
                    ["systemctl", "is-active", "palworld-maintenance.service"],
                    text=True, capture_output=True, timeout=15, check=False,
                )
                state = result.stdout.strip()
                if result.returncode not in {0, 3} or state not in {"inactive", "failed"}:
                    raise ConfigError("maintenance is active or its state cannot be confirmed")
            engine = _core_backup_engine(config)
            # Validate storage and source safety before touching a running
            # server.  In particular, a missing mount or insufficient space
            # must never result in an otherwise avoidable service stop.
            engine.preflight_snapshot()
            initial_state = service_state()
            if initial_state not in {"active", "inactive", "failed"}:
                raise ConfigError("Palworld service state cannot be confirmed")
            was_active = initial_state == "active"
            try:
                if was_active:
                    # A backup may never force-stop an active server until the
                    # REST API has acknowledged a synchronous save.
                    PalworldAPI(config).save()
                    subprocess.run(["systemctl", "stop", "palworld.service"], check=True, timeout=60)
                return engine.create_snapshot().snapshot
            finally:
                final_state = service_state()
                if final_state == "unknown":
                    raise ConfigError("Palworld service state cannot be confirmed after backup")
                if was_active and final_state != "active":
                    subprocess.run(["systemctl", "start", "palworld.service"], check=True, timeout=60)
    except _CoreOperationLockBusy as exc:
        raise ConfigError("another Palworld operation is already running") from exc
    except _CoreSnapshotError as exc:
        raise ConfigError(str(exc)) from exc
    except (ApiError, OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(f"backup service operation failed: {exc}") from exc


def _core_backup_preflight(config: dict[str, str]) -> int:
    """Validate backup inputs without saving, stopping, or starting Palworld."""
    if _CoreBackupManager is None:
        raise ConfigError("Python backup core is not installed beside palworld_manager.py")
    try:
        return _core_backup_engine(config).preflight_snapshot()
    except _CoreSnapshotError as exc:
        raise ConfigError(str(exc)) from exc


def _core_restore(config: dict[str, str], version: str) -> Path:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise ConfigError("run this script with sudo")
    try:
        return _core_backup_engine(config).restore(version).safety_copy
    except _CoreSnapshotError as exc:
        raise ConfigError(str(exc)) from exc


def _audit_management(config: dict[str, str], *, action: str, status: str,
                      details: dict[str, object] | None = None) -> None:
    """Record root CLI outcomes into the manager-owned common audit log."""
    if _CoreAuditLog is None:
        return
    try:
        manager = pwd.getpwnam(config["PALWORLD_MANAGER_USER"])
        _CoreAuditLog(
            validate_config(config)["state_dir"], expected_uid=manager.pw_uid, expected_gid=manager.pw_gid,
            secrets=tuple(config.get(key, "") for key in ("DISCORD_BOT_TOKEN", "ADMIN_PASSWORD", "SERVER_PASSWORD")),
        ).record(source="CLI", who="CLI", action=action, status=status, details=details)
    except (KeyError, OSError, ValueError, ConfigError):
        # Management work must not be masked by an observability filesystem
        # failure; service logs remain the operational fallback.
        pass
def _core_restore_preflight(config: dict[str, str], version: str) -> None:
    try:
        _core_backup_engine(config).preflight_restore(version)
    except _CoreSnapshotError as exc:
        raise ConfigError(str(exc)) from exc


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Palworld caretaker deployment")
    parser.add_argument("--config-dir", default="/srv/palworld/config")
    parser.add_argument("--no-filesystem", action="store_true", help="validate values only")
    parser.add_argument("--get", metavar="KEY", help="print one validated config or derived value")
    parser.add_argument("--render-units", nargs=2, metavar=("SOURCE", "OUTPUT"),
                        help="render systemd unit templates into OUTPUT")
    parser.add_argument("--diagnose", action="store_true", help="run read-only deployment diagnostics")
    parser.add_argument("--json", action="store_true", help="emit diagnose results as JSON")
    parser.add_argument(
        "--backup-preflight", action="store_true",
        help="validate backup storage and sources without changing the server",
    )
    parser.add_argument("--core-engine", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--backup", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pre-restore", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--restore-preflight", metavar="VERSION", help=argparse.SUPPRESS)
    parser.add_argument("--restore", metavar="VERSION", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.core_engine:
            return 0 if _CoreBackupManager is not None else 1
        config = load_config(args.config_dir)
        if args.json and not args.diagnose:
            raise ConfigError("--json requires --diagnose")
        if args.diagnose:
            checks = diagnose_deployment(config, config_dir=args.config_dir)
            if args.json:
                print(json.dumps([asdict(check) for check in checks], ensure_ascii=False, indent=2))
            else:
                for check in checks:
                    print(f"[{check.status.upper()}] {check.name}: {check.message}")
                totals = {status: sum(check.status == status for check in checks)
                          for status in ("pass", "warn", "fail")}
                print(f"Summary: {totals['pass']} passed, {totals['warn']} warnings, "
                      f"{totals['fail']} failed.")
            return diagnostic_exit_code(checks)
        if args.backup_preflight:
            _core_backup_preflight(config)
            return 0
        if args.backup:
            snapshot = _core_backup(config)
            _audit_management(config, action="backup", status="success", details={"snapshot": snapshot.name})
            print(f"Created backup version: {snapshot.name}")
            return 0
        if args.restore_preflight:
            _core_restore_preflight(config, args.restore_preflight)
            return 0
        if args.restore:
            safety_copy = _core_restore(config, args.restore)
            _audit_management(config, action="restore", status="success", details={
                "snapshot": args.restore, "safety_backup": safety_copy.name,
            })
            print(f"Current pre-restore safety copy: {safety_copy}")
            return 0
        if args.get:
            print(config_value(config, args.get))
            return 0
        if args.render_units:
            render_systemd_units(config, *args.render_units)
            return 0
        if args.no_filesystem:
            # The installer performs this gate before it creates the manager
            # account or copies configuration into the deployed root.  The
            # owner/group contract is checked by the later filesystem pass.
            report = preflight_values(config, config_dir=args.config_dir, deployed=False)
            report.raise_for_errors()
        else:
            report = preflight_config(config, config_dir=args.config_dir)
            report.raise_for_errors()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("Palworld configuration preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
