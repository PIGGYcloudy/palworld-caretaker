"""Strict layered configuration shared by all Caretaker frontends."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Callable, Mapping

from .errors import ConfigError
from .paths import has_parent_reference, is_filesystem_root, native_path, physical_path
from .settings import (
    DEFAULT_WEB_BIND_IP,
    EDITABLE_DEFAULTS,
    SETTING_SPECS,
    normalize_backup_schedule,
    normalize_web_bind_ip,
    normalize_web_authorities,
    validate_settings_values,
)


def _platform_defaults() -> dict[str, str]:
    """Use native absolute defaults so an unmodified config validates on Windows."""
    if os.name == "nt":
        program_data = native_path(os.environ.get("ProgramData", r"C:\ProgramData"))
        root = program_data / "Palworld"
        return {
            "PALWORLD_INSTALL_ROOT": str(root),
            # Keep snapshots beside (not inside) the live SaveGames tree.
            # This is intentionally a very narrow in-server exception: the
            # validator accepts only this exact sibling, never arbitrary
            # directories below the installation.
            "PALWORLD_BACKUP_DIR": str(root / "server/Pal/Saved/SaveGames_Backups"),
            "PALWORLD_BACKUP_MOUNT": "",
            "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
            "PALWORLD_MANAGER_STATE_DIR": str(root / "state"),
        }
    return {
        "PALWORLD_INSTALL_ROOT": "/srv/palworld",
        "PALWORLD_BACKUP_DIR": "/srv/palworld/server/Pal/Saved/SaveGames_Backups",
        "PALWORLD_BACKUP_MOUNT": "",
        "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
        "PALWORLD_MANAGER_STATE_DIR": "/var/lib/palworld-manager",
    }


DEFAULTS: dict[str, str] = {
    **_platform_defaults(),
    # Empty retains the host deployment layout of INSTALL_ROOT/server.  Docker
    # mounts the game data directly at /srv/palworld and sets this explicitly.
    "PALWORLD_SERVER_ROOT": "",
    "PALWORLD_SERVICE_USER": "palworld", "PALWORLD_MANAGER_USER": "palworld-manager",
    "MAX_PLAYERS": "10", "BASE_CAMP_MAX_NUM_IN_GUILD": "10",
    "SERVER_NAME": "Palworld Dedicated Server", "SERVER_DESCRIPTION": "Private Palworld Dedicated Server",
    "PUBLIC_PORT": "8211", "PALWORLD_REST_API_HOST": "127.0.0.1",
    "PALWORLD_REST_API_PORT": "8212", "PALWORLD_REST_API_USERNAME": "admin",
    "PALWORLD_API_TIMEOUT_SECONDS": "5", "PALWORLD_IDLE_SHUTDOWN_ENABLED": "true",
    "PALWORLD_IDLE_TIMEOUT_MINUTES": "10", "PALWORLD_PLAYER_CHECK_INTERVAL_SECONDS": "60",
    "PALWORLD_STARTUP_GRACE_SECONDS": "600", "PALWORLD_SHUTDOWN_WAIT_SECONDS": "30",
    "PALWORLD_IDLE_WATCHER_DRY_RUN": "true", "PALWORLD_START_READY_TIMEOUT_SECONDS": "180",
    "PALWORLD_MEMORY_ALERT_PERCENT": "85", "PALWORLD_MEMORY_ALERT_COOLDOWN_SECONDS": "1800",
    "DISCORD_PALWORLD_ALLOWED_GUILD_IDS": "", "DISCORD_PALWORLD_ALLOWED_ROLE_IDS": "",
    "DISCORD_PALWORLD_ADMIN_ROLE_IDS": "", "DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS": "",
    "BACKUP_RETENTION_COUNT": "14", "BACKUP_TIME": "daily-04:30",
    "PALWORLD_BACKUP_SCHEDULE_ENABLED": "true",
    # This is deliberately independent of SERVER_PASSWORD: public servers
    # may use an empty game password and still have completed first-run setup.
    "PALWORLD_ONBOARDING_COMPLETED": "false",
    "SERVER_PASSWORD": "", "ADMIN_PASSWORD": "", "DISCORD_BOT_TOKEN": "",
    "PALWORLD_WEB_UI_USERNAME": "palworld-manager", "PALWORLD_WEB_UI_PASSWORD": "",
    "PALWORLD_WEB_BIND_IP": DEFAULT_WEB_BIND_IP,
    # Web request authorities are intentionally part of the protected
    # deployment configuration, never editable through the browser.
    "PALWORLD_WEB_PUBLIC_ORIGIN": "", "PALWORLD_WEB_ALLOWED_ORIGINS": "",
    "PALWORLD_WEB_ALLOWED_HOSTS": "",
    # The archive is assembled in the manager state directory.  This cap is
    # deliberately independent from backup retention so a browser request
    # cannot consume arbitrary local disk.
    "PALWORLD_SAVEGAMES_EXPORT_MAX_BYTES": str(8 * 1024 ** 3),
    **EDITABLE_DEFAULTS,
}
CONFIG_FILES = ("caretaker.env", "server.env", "secrets.env")
LEGACY_CONFIG_FILE = "palworld.env"
EDITABLE_CONFIG_DIRECTORY = "editable"
EDITABLE_CONFIG_FILES = ("caretaker.env", "server.env")
EDITABLE_SECRET_FILE = "secrets.env"
EDITABLE_SETTING_KEYS = frozenset(SETTING_SPECS)
WIZARD_EDITABLE_KEYS = frozenset({
    "PALWORLD_WEB_BIND_IP", "PALWORLD_WEB_ALLOWED_ORIGINS",
    "PALWORLD_WEB_ALLOWED_HOSTS", "PALWORLD_BACKUP_SCHEDULE_ENABLED",
    "PALWORLD_ONBOARDING_COMPLETED",
    "DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS",
})
# This child is deliberately separate from the general manager state files so
# the web UI can be granted the narrowest possible writable systemd path.
SETTINGS_BACKUP_DIRECTORY = "settings-backups"
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ACCOUNT_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$", re.ASCII)
_DISCORD_ID_RE = re.compile(r"^[0-9]+$")


def _parse_value(raw: str, path: Path, line: int) -> str:
    value = raw.lstrip(" \t")
    if not value:
        return ""
    if value[0] not in {"'", '"'}:
        comment = re.search(r"[ \t]+#", value)
        parsed = value[:comment.start()].rstrip() if comment else value.rstrip()
        if any(c in parsed for c in " \t"):
            raise ConfigError(f"{path}:{line}: unquoted values may not contain whitespace")
    else:
        quote, escaped, output, end = value[0], False, [], None
        for index, char in enumerate(value[1:], 1):
            if quote == '"' and escaped:
                output.append(char if char in {'"', "\\"} else "\\" + char)
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                end = index + 1
                break
            else:
                output.append(char)
        if escaped:
            raise ConfigError(f"{path}:{line}: trailing escape in quoted value")
        if end is None:
            raise ConfigError(f"{path}:{line}: unterminated quoted value")
        trailing = value[end:].strip()
        if trailing and not trailing.startswith("#"):
            raise ConfigError(f"{path}:{line}: unexpected text after quoted value")
        parsed = "".join(output)
    if any(c in parsed for c in ("\x00", "\r", "\n")):
        raise ConfigError(f"{path}:{line}: value contains a forbidden control character")
    return parsed


def load_env(path: str | Path) -> dict[str, str]:
    """Read dotenv syntax as data; never execute, expand, or substitute it."""
    source, result = native_path(path), {}
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{source}: configuration must be UTF-8") from exc
    except OSError as exc:
        raise ConfigError(f"{source}: cannot read configuration: {exc.strerror}") from exc
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{source}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            raise ConfigError(f"{source}:{number}: invalid configuration key {key!r}")
        if key in result:
            raise ConfigError(f"{source}:{number}: duplicate configuration key {key}")
        result[key] = _parse_value(value, source, number)
    return result


@dataclass(frozen=True)
class ConfigSchema:
    """Extensible schema boundary; later TOML/YAML loaders can use it unchanged."""
    defaults: Mapping[str, str]
    validators: tuple[Callable[[Mapping[str, str]], None], ...] = ()

    def validate(self, values: Mapping[str, str]) -> None:
        unknown = sorted(set(values) - set(self.defaults))
        if unknown:
            raise ConfigError(f"unknown configuration key(s): {', '.join(unknown)}")
        _validate_core(values)
        for validator in self.validators:
            validator(values)


DEFAULT_SCHEMA = ConfigSchema(DEFAULTS)


def load_config(directory: str | Path, *, schema: ConfigSchema = DEFAULT_SCHEMA, require_file: bool = True) -> "CaretakerConfig":
    """Merge defaults, protected layers, editable layers, then secrets.

    New deployments place the two web-editable files in ``editable/``.  The
    protected config root remains searchable but non-writable to the manager,
    so that account can never rename or remove ``secrets.env``.
    """
    root, values, count = native_path(directory), dict(schema.defaults), 0
    # Docker v0.7 volumes predate this setting.  Do not let their absence
    # inherit the host-safe loopback default: a listener inside a container
    # must accept traffic from Docker's port-forwarding interface.  An
    # explicitly declared value in any configuration layer always wins.
    web_bind_ip_declared = False
    for name in (LEGACY_CONFIG_FILE, *EDITABLE_CONFIG_FILES):
        source = root / name
        if source.is_file():
            layer = load_env(source)
            values.update(layer)
            web_bind_ip_declared = web_bind_ip_declared or "PALWORLD_WEB_BIND_IP" in layer
            count += 1
    editable = root / EDITABLE_CONFIG_DIRECTORY
    for name in EDITABLE_CONFIG_FILES:
        source = editable / name
        if source.is_file():
            editable_values = load_env(source)
            forbidden = sorted(set(editable_values) - (EDITABLE_SETTING_KEYS | WIZARD_EDITABLE_KEYS))
            if forbidden:
                raise ConfigError(
                    f"{source}: editable configuration may contain only setting keys: "
                    f"{', '.join(forbidden)}"
                )
            values.update(editable_values)
            web_bind_ip_declared = web_bind_ip_declared or "PALWORLD_WEB_BIND_IP" in editable_values
            count += 1
    source = root / "secrets.env"
    if source.is_file():
        layer = load_env(source)
        values.update(layer)
        web_bind_ip_declared = web_bind_ip_declared or "PALWORLD_WEB_BIND_IP" in layer
        count += 1
    # The first-run and Discord wizards may write their two narrowly scoped
    # secrets into the manager-owned editable directory.  They are applied
    # after the protected template so a fresh CHANGE_ME placeholder cannot
    # mask the user's deliberate first-run value.
    editable_secret = editable / EDITABLE_SECRET_FILE
    if editable_secret.is_file():
        editable_values = load_env(editable_secret)
        forbidden = sorted(set(editable_values) - {"SERVER_PASSWORD", "DISCORD_BOT_TOKEN", "PALWORLD_WEB_UI_PASSWORD"})
        if forbidden:
            raise ConfigError(f"{editable_secret}: editable secrets may contain only SERVER_PASSWORD, "
                              f"PALWORLD_WEB_UI_PASSWORD or DISCORD_BOT_TOKEN: "
                              f"{', '.join(forbidden)}")
        values.update(editable_values)
        count += 1
    if require_file and not count:
        raise ConfigError(f"{root}: no deployment configuration file found")
    if os.environ.get("PALWORLD_CONTAINER_MODE") == "1" and not web_bind_ip_declared:
        values["PALWORLD_WEB_BIND_IP"] = "0.0.0.0"
    schema.validate(values)
    return CaretakerConfig(values, schema, root)


def _absolute(values: Mapping[str, str], key: str) -> Path:
    raw = values.get(key, "")
    path = native_path(raw)
    if not raw or not path.is_absolute() or has_parent_reference(raw):
        raise ConfigError(f"{key} must be an absolute path without '..'")
    return path


def _below(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _bool(values: Mapping[str, str], key: str) -> bool:
    value = values.get(key, "").strip().lower()
    if value not in {"true", "false"}:
        raise ConfigError(f"{key} must be true or false")
    return value == "true"


def _integer(values: Mapping[str, str], key: str, low: int, high: int) -> int:
    try:
        value = int(values[key])
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if not low <= value <= high:
        raise ConfigError(f"{key} must be between {low} and {high}")
    return value


def _validate_core(values: Mapping[str, str]) -> None:
    install, backup, state = (_absolute(values, key) for key in (
        "PALWORLD_INSTALL_ROOT", "PALWORLD_BACKUP_DIR", "PALWORLD_MANAGER_STATE_DIR"))
    server = _absolute(values, "PALWORLD_SERVER_ROOT") if values.get("PALWORLD_SERVER_ROOT") else install / "server"
    mount_raw = values.get("PALWORLD_BACKUP_MOUNT", "")
    mount = _absolute(values, "PALWORLD_BACKUP_MOUNT") if mount_raw else None
    if any(is_filesystem_root(path) for path in (install, server, backup, state, mount) if path is not None):
        raise ConfigError("deployment paths must not be the filesystem root")
    try:
        physical_install, physical_server, physical_backup = (
            physical_path(path) for path in (install, server, backup)
        )
    except OSError as exc:
        raise ConfigError(f"deployment path cannot be physically resolved: {exc}") from exc
    # Compare both the configured spelling and the target of every existing
    # ancestor.  In particular, ``D:\outside\junction\backup`` must not
    # evade this guard when ``junction`` targets the installation tree.
    allowed_sibling = server / "Pal/Saved/SaveGames_Backups"
    physical_allowed_sibling = physical_server / "Pal/Saved/SaveGames_Backups"
    backup_is_allowed_sibling = backup == allowed_sibling and physical_backup == physical_allowed_sibling
    if ((_below(backup, install) or _below(install, backup)
            or _below(backup, server) or _below(server, backup)
            or _below(physical_backup, physical_install) or _below(physical_install, physical_backup)
            or _below(physical_backup, physical_server) or _below(physical_server, physical_backup))
            and not backup_is_allowed_sibling):
        raise ConfigError("PALWORLD_BACKUP_DIR must not overlap the Palworld installation or server root")
    if _bool(values, "PALWORLD_BACKUP_REQUIRE_MOUNT") and (mount is None or backup == mount or not _below(backup, mount)):
        raise ConfigError("PALWORLD_BACKUP_DIR must be below PALWORLD_BACKUP_MOUNT when mount checking is enabled")
    for key, low, high in (("MAX_PLAYERS", 1, 32), ("BASE_CAMP_MAX_NUM_IN_GUILD", 1, 10),
                           ("PUBLIC_PORT", 1, 65535), ("PALWORLD_REST_API_PORT", 1, 65535),
                           ("PALWORLD_API_TIMEOUT_SECONDS", 1, 30), ("BACKUP_RETENTION_COUNT", 1, 1000),
                           ("PALWORLD_MEMORY_ALERT_PERCENT", 10, 99), ("PALWORLD_MEMORY_ALERT_COOLDOWN_SECONDS", 60, 86400),
                           ("PALWORLD_SAVEGAMES_EXPORT_MAX_BYTES", 1, 64 * 1024 ** 3)):
        _integer(values, key, low, high)
    if _integer(values, "PUBLIC_PORT", 1, 65535) == _integer(values, "PALWORLD_REST_API_PORT", 1, 65535):
        raise ConfigError("PUBLIC_PORT and PALWORLD_REST_API_PORT must be different")
    if values.get("PALWORLD_REST_API_HOST") != "127.0.0.1":
        raise ConfigError("PALWORLD_REST_API_HOST must be 127.0.0.1")
    normalize_web_bind_ip(values.get("PALWORLD_WEB_BIND_IP", ""))
    try:
        normalize_web_authorities(values)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    normalize_backup_schedule(values.get("BACKUP_TIME", ""))
    _bool(values, "PALWORLD_BACKUP_SCHEDULE_ENABLED")
    _bool(values, "PALWORLD_ONBOARDING_COMPLETED")
    for key in ("PALWORLD_SERVICE_USER", "PALWORLD_MANAGER_USER"):
        if not _ACCOUNT_RE.fullmatch(values.get(key, "")):
            raise ConfigError(f"{key} is not a valid system account name")
    for key in ("DISCORD_PALWORLD_ALLOWED_GUILD_IDS", "DISCORD_PALWORLD_ALLOWED_ROLE_IDS", "DISCORD_PALWORLD_ADMIN_ROLE_IDS", "DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS"):
        value = values.get(key, "")
        if value == "*" and key != "DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS":
            raise ConfigError(f"{key} may not use a wildcard")
        if value != "*" and value and any(not _DISCORD_ID_RE.fullmatch(item.strip()) for item in value.split(",")):
            raise ConfigError(f"{key} must be a comma-separated list of numeric IDs")
    validate_settings_values(values)


@dataclass(frozen=True)
class CaretakerConfig:
    values: Mapping[str, str]
    schema: ConfigSchema = DEFAULT_SCHEMA
    directory: Path | None = None

    def __post_init__(self) -> None:
        self.schema.validate(self.values)

    def get(self, key: str) -> str:
        try:
            return self.values[key]
        except KeyError as exc:
            raise ConfigError(f"unknown configuration value: {key}") from exc

    @property
    def install_root(self) -> Path: return _absolute(self.values, "PALWORLD_INSTALL_ROOT")
    @property
    def server_root(self) -> Path:
        configured = self.values.get("PALWORLD_SERVER_ROOT", "")
        return _absolute(self.values, "PALWORLD_SERVER_ROOT") if configured else self.install_root / "server"
    @property
    def config_root(self) -> Path: return self.install_root / "config"
    @property
    def scripts_root(self) -> Path: return self.install_root / "scripts"
    @property
    def local_backup_root(self) -> Path: return self.install_root / "backups-local"
    @property
    def backup_root(self) -> Path: return _absolute(self.values, "PALWORLD_BACKUP_DIR")
    @property
    def backup_mount(self) -> Path | None:
        return _absolute(self.values, "PALWORLD_BACKUP_MOUNT") if self.values.get("PALWORLD_BACKUP_MOUNT") else None
    @property
    def state_root(self) -> Path: return _absolute(self.values, "PALWORLD_MANAGER_STATE_DIR")
    @property
    def settings_backup_root(self) -> Path: return self.state_root / SETTINGS_BACKUP_DIRECTORY
    @property
    def rest_port(self) -> int: return _integer(self.values, "PALWORLD_REST_API_PORT", 1, 65535)
    @property
    def rest_timeout(self) -> int: return _integer(self.values, "PALWORLD_API_TIMEOUT_SECONDS", 1, 30)
    @property
    def backup_retention(self) -> int: return _integer(self.values, "BACKUP_RETENTION_COUNT", 1, 1000)
    @property
    def memory_alert_percent(self) -> int: return _integer(self.values, "PALWORLD_MEMORY_ALERT_PERCENT", 10, 99)
    @property
    def memory_alert_cooldown_seconds(self) -> int: return _integer(self.values, "PALWORLD_MEMORY_ALERT_COOLDOWN_SECONDS", 60, 86400)
    @property
    def require_backup_mount(self) -> bool: return _bool(self.values, "PALWORLD_BACKUP_REQUIRE_MOUNT")
