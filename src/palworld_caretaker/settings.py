"""Typed, editable server and caretaker settings.

This is intentionally a small schema rather than a second configuration
format.  Environment files remain the deployment contract, while this module
provides the typed boundary used by the web UI and other frontends.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from .errors import ConfigError


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    category: str
    target: str
    kind: str  # ``integer``, ``number``, ``boolean``, ``string``, or ``choice``.
    default: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    secret: bool = False


@dataclass(frozen=True)
class WorldSettings:
    """The game-facing settings exposed by the first visual editor."""

    server_name: str
    server_description: str
    max_players: int
    day_time_speed_rate: float
    night_time_speed_rate: float
    exp_rate: float
    pal_capture_rate: float
    collection_drop_rate: float
    enemy_drop_item_rate: float
    pal_damage_rate_attack: float
    pal_damage_rate_defense: float
    player_damage_rate_attack: float
    player_damage_rate_defense: float
    guild_player_max_num: int
    base_camp_max_num_in_guild: int
    pal_spawn_num_rate: float
    drop_item_max_num: int
    pal_egg_default_hatching_time: float


@dataclass(frozen=True)
class CaretakerOptions:
    """The non-secret runtime options safe to edit locally."""

    idle_shutdown_enabled: bool
    idle_timeout_minutes: int
    backup_retention_count: int
    backup_time: str


_SPECS = (
    SettingSpec("SERVER_NAME", "Server name", "General", "server", "string", "Palworld Dedicated Server"),
    SettingSpec("SERVER_DESCRIPTION", "Server description", "General", "server", "string", "Private Palworld Dedicated Server"),
    SettingSpec("MAX_PLAYERS", "Maximum players", "General", "server", "integer", "10", 1, 32),
    SettingSpec("DAY_TIME_SPEED_RATE", "Day speed", "Multipliers", "server", "number", "1.0", 0.1, 5),
    SettingSpec("NIGHT_TIME_SPEED_RATE", "Night speed", "Multipliers", "server", "number", "1.0", 0.1, 5),
    SettingSpec("EXP_RATE", "Experience rate", "Multipliers", "server", "number", "1.0", 0.1, 20),
    SettingSpec("PAL_CAPTURE_RATE", "Pal capture rate", "Multipliers", "server", "number", "1.0", 0.1, 5),
    SettingSpec("COLLECTION_DROP_RATE", "Collection drop rate", "Multipliers", "server", "number", "1.0", 0.1, 5),
    SettingSpec("ENEMY_DROP_ITEM_RATE", "Enemy drop rate", "Multipliers", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PAL_DAMAGE_RATE_ATTACK", "Pal damage dealt", "Pal Dynamics", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PAL_DAMAGE_RATE_DEFENSE", "Pal damage received", "Pal Dynamics", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_DAMAGE_RATE_ATTACK", "Player damage dealt", "Player & Guild", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_DAMAGE_RATE_DEFENSE", "Player damage received", "Player & Guild", "server", "number", "1.0", 0.1, 5),
    SettingSpec("GUILD_PLAYER_MAX_NUM", "Guild player limit", "Player & Guild", "server", "integer", "20", 1, 100),
    SettingSpec("BASE_CAMP_MAX_NUM_IN_GUILD", "Base camp limit", "Player & Guild", "server", "integer", "10", 1, 10),
    SettingSpec("PAL_SPAWN_NUM_RATE", "Pal spawn rate", "Drops & Spawns", "server", "number", "1.0", 0.1, 5),
    SettingSpec("DROP_ITEM_MAX_NUM", "Maximum dropped items", "Drops & Spawns", "server", "integer", "3000", 1, 10000),
    SettingSpec("PAL_EGG_DEFAULT_HATCHING_TIME", "Egg hatching time", "Drops & Spawns", "server", "number", "72.0", 0, 240),
    SettingSpec("PALWORLD_IDLE_SHUTDOWN_ENABLED", "Idle shutdown enabled", "Caretaker", "server", "boolean", "true"),
    SettingSpec("PALWORLD_IDLE_TIMEOUT_MINUTES", "Idle shutdown timeout (minutes)", "Caretaker", "server", "integer", "10", 1, 1440),
    SettingSpec("BACKUP_RETENTION_COUNT", "Backup retention count", "Caretaker", "caretaker", "integer", "14", 1, 1000),
    SettingSpec("BACKUP_TIME", "Daily backup time", "Caretaker", "caretaker", "string", "04:30"),
)

SETTING_SPECS: dict[str, SettingSpec] = {spec.key: spec for spec in _SPECS}
EDITABLE_DEFAULTS: dict[str, str] = {spec.key: spec.default for spec in _SPECS}
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_INI_RESERVED = frozenset(',()"')


def _string(value: object, spec: SettingSpec) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{spec.key} must be a string")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ConfigError(f"{spec.key} contains a forbidden control character")
    if len(value) > 256:
        raise ConfigError(f"{spec.key} must be at most 256 characters")
    # These values are embedded in PalWorld's comma-delimited OptionSettings
    # tuple by render-settings.sh.  Keep this schema at the same boundary so a
    # browser save cannot publish a value which the renderer must reject.
    if spec.key in {"SERVER_NAME", "SERVER_DESCRIPTION"}:
        if not value:
            raise ConfigError(f"{spec.key} must not be empty")
        if any(character in value for character in _INI_RESERVED):
            raise ConfigError(f"{spec.key} contains an INI-reserved character")
    if spec.key == "BACKUP_TIME" and not _TIME.fullmatch(value):
        raise ConfigError("BACKUP_TIME must use 24-hour HH:MM format")
    return value


def normalize_value(value: object, spec: SettingSpec) -> str:
    """Validate one JSON/env value and return canonical dotenv text."""
    if spec.kind == "string":
        return _string(value, spec)
    if spec.kind == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower()
        raise ConfigError(f"{spec.key} must be true or false")
    if spec.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ConfigError(f"{spec.key} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{spec.key} must be an integer") from exc
        if str(parsed) != str(value).strip() and not isinstance(value, int):
            raise ConfigError(f"{spec.key} must be an integer")
        canonical = str(parsed)
    elif spec.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ConfigError(f"{spec.key} must be a number")
        try:
            parsed_float = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{spec.key} must be a number") from exc
        if parsed_float != parsed_float or parsed_float in {float("inf"), float("-inf")}:
            raise ConfigError(f"{spec.key} must be a finite number")
        canonical = format(parsed_float, ".15g")
        parsed = parsed_float
    elif spec.kind == "choice":
        if not isinstance(value, str) or value not in spec.choices:
            raise ConfigError(f"{spec.key} must be one of: {', '.join(spec.choices)}")
        return value
    else:  # pragma: no cover - schema authoring error
        raise AssertionError(f"unsupported setting kind: {spec.kind}")
    if spec.minimum is not None and parsed < spec.minimum or spec.maximum is not None and parsed > spec.maximum:
        raise ConfigError(f"{spec.key} must be between {spec.minimum:g} and {spec.maximum:g}")
    return canonical


def validate_settings_values(values: Mapping[str, object]) -> dict[str, str]:
    """Validate all typed settings present in a full configuration mapping."""
    return {key: normalize_value(values[key], spec) for key, spec in SETTING_SPECS.items() if key in values}


def validate_edit(values: Mapping[str, object], current: Mapping[str, str]) -> dict[str, str]:
    """Validate an untrusted editor payload and merge it with current values."""
    if not isinstance(values, Mapping):
        raise ConfigError("settings values must be an object")
    unknown = sorted(set(values) - set(SETTING_SPECS))
    if unknown:
        raise ConfigError(f"unknown editable setting(s): {', '.join(unknown)}")
    merged = dict(current)
    for key, value in values.items():
        merged[key] = normalize_value(value, SETTING_SPECS[key])
    # This also validates values omitted from a partial browser submission.
    validate_settings_values(merged)
    return merged


def categories() -> tuple[tuple[str, tuple[SettingSpec, ...]], ...]:
    names = ("General", "Multipliers", "Pal Dynamics", "Player & Guild", "Drops & Spawns", "Caretaker")
    return tuple((name, tuple(spec for spec in _SPECS if spec.category == name)) for name in names)


def world_settings_from(values: Mapping[str, str]) -> WorldSettings:
    """Construct a typed world value object after schema validation."""
    normalized = validate_settings_values(values)
    return WorldSettings(
        normalized["SERVER_NAME"], normalized["SERVER_DESCRIPTION"], int(normalized["MAX_PLAYERS"]),
        float(normalized["DAY_TIME_SPEED_RATE"]), float(normalized["NIGHT_TIME_SPEED_RATE"]),
        float(normalized["EXP_RATE"]), float(normalized["PAL_CAPTURE_RATE"]), float(normalized["COLLECTION_DROP_RATE"]),
        float(normalized["ENEMY_DROP_ITEM_RATE"]), float(normalized["PAL_DAMAGE_RATE_ATTACK"]),
        float(normalized["PAL_DAMAGE_RATE_DEFENSE"]), float(normalized["PLAYER_DAMAGE_RATE_ATTACK"]),
        float(normalized["PLAYER_DAMAGE_RATE_DEFENSE"]), int(normalized["GUILD_PLAYER_MAX_NUM"]),
        int(normalized["BASE_CAMP_MAX_NUM_IN_GUILD"]), float(normalized["PAL_SPAWN_NUM_RATE"]),
        int(normalized["DROP_ITEM_MAX_NUM"]), float(normalized["PAL_EGG_DEFAULT_HATCHING_TIME"]),
    )


def caretaker_options_from(values: Mapping[str, str]) -> CaretakerOptions:
    normalized = validate_settings_values(values)
    return CaretakerOptions(
        normalized["PALWORLD_IDLE_SHUTDOWN_ENABLED"] == "true",
        int(normalized["PALWORLD_IDLE_TIMEOUT_MINUTES"]),
        int(normalized["BACKUP_RETENTION_COUNT"]), normalized["BACKUP_TIME"],
    )
