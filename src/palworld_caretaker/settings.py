"""Typed, editable server and caretaker settings.

This is intentionally a small schema rather than a second configuration
format.  Environment files remain the deployment contract, while this module
provides the typed boundary used by the web UI and other frontends.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import ipaddress
import re
from typing import Mapping
from urllib.parse import urlsplit

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
    description: str = ""


@dataclass(frozen=True)
class WorldSettings:
    """The typed game-facing settings exposed by the visual editor."""

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
    drop_item_alive_max_hours: float
    pal_egg_default_hatching_time: float
    death_penalty: str
    pal_stamina_decreace_rate: float
    player_stamina_decreace_rate: float
    pal_auto_hp_regene_rate: float
    player_auto_hp_regene_rate: float
    pal_auto_hp_regene_rate_in_sleep: float
    player_auto_hp_regene_rate_in_sleep: float
    pal_hunger_decreace_rate: float
    player_hunger_decreace_rate: float
    build_object_hp_rate: float
    build_object_damage_rate: float
    build_object_deterioration_damage_rate: float
    base_camp_worker_max_num: int
    work_speed_rate: float
    item_weight_rate: float
    equipment_durability_damage_rate: float
    auto_reset_worker_pal_when_server_restart: bool

    # Palworld's INI calls hunger "StomachDecreace".  Retain aliases for code
    # that needs to speak in the game's terminology while presenting the
    # clearer hunger wording to caretaker consumers.
    @property
    def pal_stomach_decreace_rate(self) -> float:
        return self.pal_hunger_decreace_rate

    @property
    def player_stomach_decreace_rate(self) -> float:
        return self.player_hunger_decreace_rate


@dataclass(frozen=True)
class CaretakerOptions:
    """The non-secret runtime options safe to edit locally."""

    idle_shutdown_enabled: bool
    idle_timeout_minutes: int
    backup_retention_count: int
    backup_time: str


DEFAULT_WEB_BIND_IP = "127.0.0.1"
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)


class WebAuthorityError(ValueError):
    """A protected Web UI origin or Host authority is malformed."""


def _valid_web_authority_hostname(hostname: str) -> bool:
    """Accept only an IP literal or an ASCII DNS hostname in an authority."""
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    return (
        len(hostname) <= 253
        and bool(hostname)
        and all(_DNS_LABEL.fullmatch(label) for label in hostname.split("."))
    )


def canonical_web_origin(value: str, *, require_origin_only: bool = True) -> str | None:
    """Return a normalized HTTP(S) origin, rejecting ambiguous URL forms."""
    try:
        parsed = urlsplit(value)
        # Accessing ``port`` validates malformed port values such as ``:abc``.
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (require_origin_only and parsed.path)
        or port == 0
    ):
        return None
    hostname = parsed.hostname
    if hostname is None or not _valid_web_authority_hostname(hostname):
        return None
    # Rebuild from parsed components so case changes cannot bypass comparison.
    rendered_host = f"[{hostname.lower()}]" if ":" in hostname else hostname.lower()
    scheme = parsed.scheme.lower()
    # Browsers serialize an origin without its scheme's default port.  Treat
    # an explicitly supplied default port as the same origin so a configured
    # proxy origin and the browser's Origin header compare identically.
    rendered_port = "" if port in ({"http": 80, "https": 443}[scheme], None) else f":{port}"
    return f"{scheme}://{rendered_host}{rendered_port}"


def canonical_web_host(value: str) -> str | None:
    """Return a normalized Host authority, rejecting every other URL part."""
    if not value or value != value.strip() or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(f"http://{value}")
        port = parsed.port
    except ValueError:
        return None
    if (
        not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or port == 0
    ):
        return None
    hostname = parsed.hostname.lower()
    if not _valid_web_authority_hostname(hostname):
        return None
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    # Host headers do not identify their scheme.  Both 80 and 443 are
    # browser-default authority spellings, so canonicalize either explicit
    # spelling to the portless form.  This keeps Host comparison aligned with
    # canonical_web_origin(), whose default ports are likewise omitted.
    rendered_port = "" if port in {None, 80, 443} else f":{port}"
    return f"{rendered_host}{rendered_port}"


def _split_web_authorities(value: object, name: str) -> tuple[str, ...]:
    """Split one protected CSV authority setting without allowing empty items."""
    if not isinstance(value, str):
        raise WebAuthorityError(f"{name} must be a string")
    if not value.strip():
        return ()
    items = tuple(item.strip() for item in value.split(","))
    if any(not item for item in items):
        raise WebAuthorityError(f"{name} must not contain empty entries")
    return items


def normalize_web_authorities(values: Mapping[str, object]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate and canonicalize protected Web UI origin and Host settings.

    The result contains explicit trusted origins and explicit trusted hosts.
    Listener-derived loopback authorities remain a runtime concern for ``web``.
    """
    public_origin = values.get("PALWORLD_WEB_PUBLIC_ORIGIN", "")
    if not isinstance(public_origin, str):
        raise WebAuthorityError("PALWORLD_WEB_PUBLIC_ORIGIN must be a string")
    origin_values = (public_origin.strip(), *_split_web_authorities(
        values.get("PALWORLD_WEB_ALLOWED_ORIGINS", ""), "PALWORLD_WEB_ALLOWED_ORIGINS",
    ))
    origins: list[str] = []
    for value in origin_values:
        if not value:
            continue
        origin = canonical_web_origin(value)
        if origin is None:
            raise WebAuthorityError(
                "PALWORLD_WEB_PUBLIC_ORIGIN and PALWORLD_WEB_ALLOWED_ORIGINS "
                "must contain exact HTTP(S) origins without paths"
            )
        origins.append(origin)
    hosts: list[str] = []
    for value in _split_web_authorities(
        values.get("PALWORLD_WEB_ALLOWED_HOSTS", ""), "PALWORLD_WEB_ALLOWED_HOSTS",
    ):
        host = canonical_web_host(value)
        if host is None:
            raise WebAuthorityError(
                "PALWORLD_WEB_ALLOWED_HOSTS must contain exact host[:port] authorities"
            )
        hosts.append(host)
    return tuple(origins), tuple(hosts)


def normalize_web_bind_ip(value: object) -> str:
    """Validate the IPv4 listener address accepted by the Web UI."""
    if not isinstance(value, str):
        raise ConfigError("PALWORLD_WEB_BIND_IP must be an IPv4 address")
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ConfigError("PALWORLD_WEB_BIND_IP must be a valid IPv4 address") from exc
    if address.version != 4:
        raise ConfigError("PALWORLD_WEB_BIND_IP must be an IPv4 address")
    return str(address)


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
    SettingSpec("PAL_STAMINA_DECREACE_RATE", "Pal stamina depletion", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PAL_AUTO_HP_REGENE_RATE", "Pal natural health regeneration", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PAL_AUTO_HP_REGENE_RATE_IN_SLEEP", "Pal sleeping health regeneration", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PAL_STOMACH_DECREACE_RATE", "Pal hunger depletion", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_DAMAGE_RATE_ATTACK", "Player damage dealt", "Player & Guild", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_DAMAGE_RATE_DEFENSE", "Player damage received", "Player & Guild", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_STAMINA_DECREACE_RATE", "Player stamina depletion", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_AUTO_HP_REGENE_RATE", "Player natural health regeneration", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP", "Player sleeping health regeneration", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_STOMACH_DECREACE_RATE", "Player hunger depletion", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("GUILD_PLAYER_MAX_NUM", "Guild player limit", "Player & Guild", "server", "integer", "20", 1, 100),
    SettingSpec("BASE_CAMP_MAX_NUM_IN_GUILD", "Base camp limit", "Player & Guild", "server", "integer", "10", 1, 10),
    SettingSpec("BASE_CAMP_WORKER_MAX_NUM", "Pals per base camp", "Pal Dynamics", "server", "integer", "15", 1, 50),
    SettingSpec("AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART", "Reset working Pals on server restart", "Pal Dynamics", "server", "boolean", "false"),
    SettingSpec("PAL_SPAWN_NUM_RATE", "Pal spawn rate", "Drops & Spawns", "server", "number", "1.0", 0.1, 5),
    SettingSpec("DROP_ITEM_MAX_NUM", "Maximum dropped items", "Drops & Spawns", "server", "integer", "3000", 1, 10000),
    SettingSpec("DROP_ITEM_ALIVE_MAX_HOURS", "Dropped item lifetime (hours)", "Drops & Spawns", "server", "number", "1.0", 0.1, 8760),
    SettingSpec("PAL_EGG_DEFAULT_HATCHING_TIME", "Egg hatching time", "Drops & Spawns", "server", "number", "72.0", 0, 240),
    SettingSpec("WORK_SPEED_RATE", "Pal work speed", "Pal Dynamics", "server", "number", "1.0", 0.1, 5),
    SettingSpec("ITEM_WEIGHT_RATE", "Item weight", "Survival & Penalties", "server", "number", "1.0", 0.1, 5),
    SettingSpec("EQUIPMENT_DURABILITY_DAMAGE_RATE", "Equipment durability loss", "Survival & Penalties", "server", "number", "1.0", 0.1, 5),
    SettingSpec("DEATH_PENALTY", "Death penalty", "Survival & Penalties", "server", "choice", "Item", choices=("None", "Item", "ItemAndEquipment", "All")),
    SettingSpec("BUILD_OBJECT_HP_RATE", "Structure health", "Building & Decay", "server", "number", "1.0", 0.1, 5),
    SettingSpec("BUILD_OBJECT_DAMAGE_RATE", "Structure damage received", "Building & Decay", "server", "number", "1.0", 0.1, 5),
    SettingSpec("BUILD_OBJECT_DETERIORATION_DAMAGE_RATE", "Structure deterioration", "Building & Decay", "server", "number", "1.0", 0, 5),
    SettingSpec("PALWORLD_IDLE_SHUTDOWN_ENABLED", "Idle shutdown enabled", "Caretaker", "server", "boolean", "true"),
    SettingSpec("PALWORLD_IDLE_TIMEOUT_MINUTES", "Idle shutdown timeout (minutes)", "Caretaker", "server", "integer", "10", 1, 1440),
    SettingSpec("BACKUP_RETENTION_COUNT", "Backup retention count", "Caretaker", "caretaker", "integer", "14", 1, 1000),
    SettingSpec("BACKUP_TIME", "Backup schedule", "Caretaker", "caretaker", "string", "daily-04:30"),
)

_DESCRIPTIONS = {
    "SERVER_NAME": "The public server name shown to players in the game browser.",
    "SERVER_DESCRIPTION": "A short server description shown with the server name.",
    "MAX_PLAYERS": "Maximum number of players that may connect at the same time.",
    "DAY_TIME_SPEED_RATE": "Multiplier for the speed of the in-game day; 1.0 is vanilla.",
    "NIGHT_TIME_SPEED_RATE": "Multiplier for the speed of the in-game night; 1.0 is vanilla.",
    "EXP_RATE": "Multiplier for experience gained by players and Pals.",
    "PAL_CAPTURE_RATE": "Multiplier for Pal capture probability; 1.0 is vanilla.",
    "COLLECTION_DROP_RATE": "Multiplier for resources collected from the world.",
    "ENEMY_DROP_ITEM_RATE": "Multiplier for items dropped by defeated enemies.",
    "PAL_DAMAGE_RATE_ATTACK": "Multiplier for damage dealt by Pals.",
    "PAL_DAMAGE_RATE_DEFENSE": "Multiplier for damage received by Pals; lower values reduce damage.",
    "PAL_STAMINA_DECREACE_RATE": "Multiplier for how quickly Pal stamina is consumed.",
    "PAL_AUTO_HP_REGENE_RATE": "Multiplier for Pal health regeneration while awake.",
    "PAL_AUTO_HP_REGENE_RATE_IN_SLEEP": "Multiplier for Pal health regeneration while sleeping.",
    "PAL_STOMACH_DECREACE_RATE": "Multiplier for how quickly Pal hunger decreases.",
    "PLAYER_DAMAGE_RATE_ATTACK": "Multiplier for damage dealt by players.",
    "PLAYER_DAMAGE_RATE_DEFENSE": "Multiplier for damage received by players; lower values reduce damage.",
    "PLAYER_STAMINA_DECREACE_RATE": "Multiplier for how quickly player stamina is consumed.",
    "PLAYER_AUTO_HP_REGENE_RATE": "Multiplier for player health regeneration while awake.",
    "PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP": "Multiplier for player health regeneration while sleeping.",
    "PLAYER_STOMACH_DECREACE_RATE": "Multiplier for how quickly player hunger decreases.",
    "GUILD_PLAYER_MAX_NUM": "Maximum number of players that can join one guild.",
    "BASE_CAMP_MAX_NUM_IN_GUILD": "Maximum number of base camps a guild can build.",
    "BASE_CAMP_WORKER_MAX_NUM": "Maximum number of working Pals assigned to each base camp.",
    "AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART": "Whether working Pals are reset when the server restarts.",
    "PAL_SPAWN_NUM_RATE": "Multiplier for the number of Pals that spawn in the world.",
    "DROP_ITEM_MAX_NUM": "Maximum number of dropped items allowed in the world.",
    "DROP_ITEM_ALIVE_MAX_HOURS": "How many hours dropped items remain before they expire.",
    "PAL_EGG_DEFAULT_HATCHING_TIME": "Default time in hours required to hatch a Pal egg.",
    "WORK_SPEED_RATE": "Multiplier for Pal work speed at bases.",
    "ITEM_WEIGHT_RATE": "Multiplier for item weight; lower values let players carry more.",
    "EQUIPMENT_DURABILITY_DAMAGE_RATE": "Multiplier for equipment durability loss; lower values preserve durability.",
    "DEATH_PENALTY": "Items and equipment lost when a player dies.",
    "BUILD_OBJECT_HP_RATE": "Multiplier for the health of built structures.",
    "BUILD_OBJECT_DAMAGE_RATE": "Multiplier for damage received by built structures.",
    "BUILD_OBJECT_DETERIORATION_DAMAGE_RATE": "Multiplier for passive structure deterioration; 0 disables it.",
    "PALWORLD_IDLE_SHUTDOWN_ENABLED": "Automatically stop the server after it has been empty for the configured period.",
    "PALWORLD_IDLE_TIMEOUT_MINUTES": "Minutes with no players before the idle shutdown watcher stops the server.",
    "BACKUP_RETENTION_COUNT": "Number of completed backups to retain; older backups are pruned safely.",
    "BACKUP_TIME": "Schedule: daily-HH:MM, every-2h, every-4h, every-6h, every-12h, or off. Legacy HH:MM values remain supported.",
}
if set(_DESCRIPTIONS) != {spec.key for spec in _SPECS}:  # pragma: no cover - schema authoring guard
    raise AssertionError("every editable setting requires a description")
_SPECS = tuple(replace(spec, description=_DESCRIPTIONS[spec.key]) for spec in _SPECS)

SETTING_SPECS: dict[str, SettingSpec] = {spec.key: spec for spec in _SPECS}
EDITABLE_DEFAULTS: dict[str, str] = {spec.key: spec.default for spec in _SPECS}
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_BACKUP_INTERVALS = frozenset({"every-2h", "every-4h", "every-6h", "every-12h"})


def normalize_backup_schedule(value: object, *, key: str = "BACKUP_TIME") -> str:
    """Validate backup schedule syntax and canonicalize legacy daily times.

    ``HH:MM`` was the original configuration contract.  Continue accepting it
    when loading old deployments, but write new daily schedules as
    ``daily-HH:MM`` so the cadence is unambiguous next to interval schedules.
    """
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a backup schedule string")
    if value in {"off", *_BACKUP_INTERVALS}:
        return value
    if value.startswith("daily-") and _TIME.fullmatch(value[6:]):
        return value
    if _TIME.fullmatch(value):
        return f"daily-{value}"
    raise ConfigError(
        f"{key} must be daily-HH:MM, every-2h, every-4h, every-6h, every-12h, or off"
    )
_INI_RESERVED = frozenset(",()\"'")


def validate_ini_password(value: object, *, key: str = "SERVER_PASSWORD") -> str:
    """Validate a password that will be embedded in Palworld's INI tuple.

    First-run setup is the only browser path that writes a game password, so
    it must use the exact same grammar boundary as the renderers.  Keeping the
    check here avoids a permissive wizard publishing a value which a later
    renderer rejects (or which alters the comma-delimited OptionSettings
    tuple).
    """
    if not isinstance(value, str) or len(value) > 256:
        raise ConfigError(f"{key} must be 0–256 characters")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ConfigError(f"{key} contains a forbidden control character")
    if value.endswith("\\"):
        raise ConfigError(f"{key} must not end with a backslash")
    if any(character in value for character in _INI_RESERVED):
        raise ConfigError(f"{key} contains an INI-reserved character")
    return value


def _string(value: object, spec: SettingSpec) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{spec.key} must be a string")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ConfigError(f"{spec.key} contains a forbidden control character")
    if len(value) > 256:
        raise ConfigError(f"{spec.key} must be at most 256 characters")
    # A final backslash would escape the closing quote when this value is
    # rendered into Palworld's quoted INI tuple values.
    if value.endswith("\\"):
        raise ConfigError(f"{spec.key} must not end with a backslash")
    # These values are embedded in PalWorld's comma-delimited OptionSettings
    # tuple by render-settings.sh.  Keep this schema at the same boundary so a
    # browser save cannot publish a value which the renderer must reject.
    if spec.key in {"SERVER_NAME", "SERVER_DESCRIPTION"}:
        if not value:
            raise ConfigError(f"{spec.key} must not be empty")
        if any(character in value for character in _INI_RESERVED):
            raise ConfigError(f"{spec.key} contains an INI-reserved character")
    if spec.key == "BACKUP_TIME":
        return normalize_backup_schedule(value, key=spec.key)
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
    names = (
        "General", "Multipliers", "Survival & Penalties", "Stamina & Health",
        "Building & Decay", "Pal Dynamics", "Player & Guild", "Drops & Spawns", "Caretaker",
    )
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
        int(normalized["DROP_ITEM_MAX_NUM"]), float(normalized["DROP_ITEM_ALIVE_MAX_HOURS"]),
        float(normalized["PAL_EGG_DEFAULT_HATCHING_TIME"]), normalized["DEATH_PENALTY"],
        float(normalized["PAL_STAMINA_DECREACE_RATE"]), float(normalized["PLAYER_STAMINA_DECREACE_RATE"]),
        float(normalized["PAL_AUTO_HP_REGENE_RATE"]), float(normalized["PLAYER_AUTO_HP_REGENE_RATE"]),
        float(normalized["PAL_AUTO_HP_REGENE_RATE_IN_SLEEP"]), float(normalized["PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP"]),
        float(normalized["PAL_STOMACH_DECREACE_RATE"]), float(normalized["PLAYER_STOMACH_DECREACE_RATE"]),
        float(normalized["BUILD_OBJECT_HP_RATE"]), float(normalized["BUILD_OBJECT_DAMAGE_RATE"]),
        float(normalized["BUILD_OBJECT_DETERIORATION_DAMAGE_RATE"]), int(normalized["BASE_CAMP_WORKER_MAX_NUM"]),
        float(normalized["WORK_SPEED_RATE"]), float(normalized["ITEM_WEIGHT_RATE"]),
        float(normalized["EQUIPMENT_DURABILITY_DAMAGE_RATE"]),
        normalized["AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART"] == "true",
    )


def caretaker_options_from(values: Mapping[str, str]) -> CaretakerOptions:
    normalized = validate_settings_values(values)
    return CaretakerOptions(
        normalized["PALWORLD_IDLE_SHUTDOWN_ENABLED"] == "true",
        int(normalized["PALWORLD_IDLE_TIMEOUT_MINUTES"]),
        int(normalized["BACKUP_RETENTION_COUNT"]), normalized["BACKUP_TIME"],
    )
