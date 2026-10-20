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
    SettingSpec("SERVER_NAME", "伺服器名稱", "General", "server", "string", "Palworld Dedicated Server"),
    SettingSpec("SERVER_DESCRIPTION", "伺服器描述", "General", "server", "string", "Private Palworld Dedicated Server"),
    SettingSpec("MAX_PLAYERS", "最大玩家數", "General", "server", "integer", "10", 1, 32),
    SettingSpec("DAY_TIME_SPEED_RATE", "白天流逝速度", "Multipliers", "server", "number", "1.0", 0.1, 5),
    SettingSpec("NIGHT_TIME_SPEED_RATE", "夜晚流逝速度", "Multipliers", "server", "number", "1.0", 0.1, 5),
    SettingSpec("EXP_RATE", "經驗值倍率", "Multipliers", "server", "number", "1.0", 0.1, 20),
    SettingSpec("PAL_CAPTURE_RATE", "帕魯捕獲率", "Multipliers", "server", "number", "1.0", 0.1, 5),
    SettingSpec("COLLECTION_DROP_RATE", "採集掉落倍率", "Multipliers", "server", "number", "1.0", 0.1, 5),
    SettingSpec("ENEMY_DROP_ITEM_RATE", "敵人掉落倍率", "Multipliers", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PAL_DAMAGE_RATE_ATTACK", "帕魯造成傷害", "Pal Dynamics", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PAL_DAMAGE_RATE_DEFENSE", "帕魯承受傷害", "Pal Dynamics", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PAL_STAMINA_DECREACE_RATE", "帕魯耐力消耗", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PAL_AUTO_HP_REGENE_RATE", "帕魯自然回血", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PAL_AUTO_HP_REGENE_RATE_IN_SLEEP", "帕魯睡眠回血", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PAL_STOMACH_DECREACE_RATE", "帕魯飢餓速度", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_DAMAGE_RATE_ATTACK", "玩家造成傷害", "Player & Guild", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_DAMAGE_RATE_DEFENSE", "玩家承受傷害", "Player & Guild", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_STAMINA_DECREACE_RATE", "玩家耐力消耗", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_AUTO_HP_REGENE_RATE", "玩家自然回血", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP", "玩家睡眠回血", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("PLAYER_STOMACH_DECREACE_RATE", "玩家飢餓速度", "Stamina & Health", "server", "number", "1.0", 0.1, 5),
    SettingSpec("GUILD_PLAYER_MAX_NUM", "公會人數上限", "Player & Guild", "server", "integer", "20", 1, 100),
    SettingSpec("BASE_CAMP_MAX_NUM_IN_GUILD", "公會據點上限", "Player & Guild", "server", "integer", "10", 1, 10),
    SettingSpec("BASE_CAMP_WORKER_MAX_NUM", "每個據點的帕魯上限", "Pal Dynamics", "server", "integer", "15", 1, 50),
    SettingSpec("AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART", "重啟時重置工作中的帕魯", "Pal Dynamics", "server", "boolean", "false"),
    SettingSpec("PAL_SPAWN_NUM_RATE", "帕魯出現數量", "Drops & Spawns", "server", "number", "1.0", 0.1, 5),
    SettingSpec("DROP_ITEM_MAX_NUM", "地上掉落物上限", "Drops & Spawns", "server", "integer", "3000", 1, 10000),
    SettingSpec("DROP_ITEM_ALIVE_MAX_HOURS", "掉落物保留時間（小時）", "Drops & Spawns", "server", "number", "1.0", 0.1, 8760),
    SettingSpec("PAL_EGG_DEFAULT_HATCHING_TIME", "帕魯蛋孵化時間（小時）", "Drops & Spawns", "server", "number", "72.0", 0, 240),
    SettingSpec("WORK_SPEED_RATE", "帕魯工作速度", "Pal Dynamics", "server", "number", "1.0", 0.1, 5),
    SettingSpec("ITEM_WEIGHT_RATE", "物品重量倍率", "Survival & Penalties", "server", "number", "1.0", 0.1, 5),
    SettingSpec("EQUIPMENT_DURABILITY_DAMAGE_RATE", "裝備耐久消耗", "Survival & Penalties", "server", "number", "1.0", 0.1, 5),
    SettingSpec("DEATH_PENALTY", "死亡懲罰", "Survival & Penalties", "server", "choice", "Item", choices=("None", "Item", "ItemAndEquipment", "All")),
    SettingSpec("BUILD_OBJECT_HP_RATE", "建築耐久度", "Building & Decay", "server", "number", "1.0", 0.1, 5),
    SettingSpec("BUILD_OBJECT_DAMAGE_RATE", "建築承受傷害", "Building & Decay", "server", "number", "1.0", 0.1, 5),
    SettingSpec("BUILD_OBJECT_DETERIORATION_DAMAGE_RATE", "建築自然劣化", "Building & Decay", "server", "number", "1.0", 0, 5),
    SettingSpec("PALWORLD_IDLE_SHUTDOWN_ENABLED", "無人時自動關服", "Caretaker", "server", "boolean", "true"),
    SettingSpec("PALWORLD_IDLE_TIMEOUT_MINUTES", "無人自動關服等待（分鐘）", "Caretaker", "server", "integer", "10", 1, 1440),
    SettingSpec("BACKUP_RETENTION_COUNT", "備份保留份數", "Caretaker", "caretaker", "integer", "14", 1, 1000),
    SettingSpec("BACKUP_TIME", "備份排程", "Caretaker", "caretaker", "string", "daily-04:30"),
    SettingSpec("UPDATE_TIME", "自動更新檢查排程", "Caretaker", "caretaker", "string", "off"),
)

_DESCRIPTIONS = {
    "SERVER_NAME": "玩家在遊戲伺服器列表看到的名稱。",
    "SERVER_DESCRIPTION": "顯示在伺服器名稱旁的簡短說明。",
    "MAX_PLAYERS": "同時可連線的玩家人數上限。",
    "DAY_TIME_SPEED_RATE": "遊戲內白天的流逝速度倍率；1.0 為原版。",
    "NIGHT_TIME_SPEED_RATE": "遊戲內夜晚的流逝速度倍率；1.0 為原版。",
    "EXP_RATE": "玩家與帕魯獲得經驗值的倍率。",
    "PAL_CAPTURE_RATE": "捕獲帕魯成功率的倍率；1.0 為原版。",
    "COLLECTION_DROP_RATE": "從世界採集資源時的取得量倍率。",
    "ENEMY_DROP_ITEM_RATE": "擊敗敵人後掉落物品的倍率。",
    "PAL_DAMAGE_RATE_ATTACK": "帕魯造成傷害的倍率。",
    "PAL_DAMAGE_RATE_DEFENSE": "帕魯承受傷害的倍率；數值越低受到的傷害越少。",
    "PAL_STAMINA_DECREACE_RATE": "帕魯耐力消耗速度的倍率。",
    "PAL_AUTO_HP_REGENE_RATE": "帕魯清醒時自然回復生命的倍率。",
    "PAL_AUTO_HP_REGENE_RATE_IN_SLEEP": "帕魯睡眠時回復生命的倍率。",
    "PAL_STOMACH_DECREACE_RATE": "帕魯飽食度下降速度的倍率。",
    "PLAYER_DAMAGE_RATE_ATTACK": "玩家造成傷害的倍率。",
    "PLAYER_DAMAGE_RATE_DEFENSE": "玩家承受傷害的倍率；數值越低受到的傷害越少。",
    "PLAYER_STAMINA_DECREACE_RATE": "玩家耐力消耗速度的倍率。",
    "PLAYER_AUTO_HP_REGENE_RATE": "玩家清醒時自然回復生命的倍率。",
    "PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP": "玩家睡眠時回復生命的倍率。",
    "PLAYER_STOMACH_DECREACE_RATE": "玩家飽食度下降速度的倍率。",
    "GUILD_PLAYER_MAX_NUM": "單一公會可加入的玩家上限。",
    "BASE_CAMP_MAX_NUM_IN_GUILD": "單一公會可建立的據點數上限。",
    "BASE_CAMP_WORKER_MAX_NUM": "每個據點可指派工作的帕魯數上限。",
    "AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART": "伺服器重新啟動時，是否重置正在工作的帕魯。",
    "PAL_SPAWN_NUM_RATE": "世界中帕魯生成數量的倍率。",
    "DROP_ITEM_MAX_NUM": "世界中同時存在的掉落物數量上限。",
    "DROP_ITEM_ALIVE_MAX_HOURS": "掉落物在消失前保留的小時數。",
    "PAL_EGG_DEFAULT_HATCHING_TIME": "孵化一顆帕魯蛋預設需要的小時數。",
    "WORK_SPEED_RATE": "帕魯在據點工作速度的倍率。",
    "ITEM_WEIGHT_RATE": "物品重量的倍率；數值越低可攜帶越多。",
    "EQUIPMENT_DURABILITY_DAMAGE_RATE": "裝備耐久消耗的倍率；數值越低越耐用。",
    "DEATH_PENALTY": "玩家死亡時會掉落的東西：None 不掉落、Item 掉落物品、ItemAndEquipment 物品與裝備、All 全部（含帕魯）。",
    "BUILD_OBJECT_HP_RATE": "建築物耐久度的倍率。",
    "BUILD_OBJECT_DAMAGE_RATE": "建築物承受傷害的倍率。",
    "BUILD_OBJECT_DETERIORATION_DAMAGE_RATE": "建築物自然劣化的倍率；0 代表不會劣化。",
    "PALWORLD_IDLE_SHUTDOWN_ENABLED": "伺服器沒有玩家一段時間後，自動存檔並關服以節省資源。",
    "PALWORLD_IDLE_TIMEOUT_MINUTES": "沒有玩家多少分鐘後，閒置監控會關閉伺服器。",
    "BACKUP_RETENTION_COUNT": "保留多少份完成的備份；較舊的備份會被安全清除。",
    "BACKUP_TIME": "每天指定時間，或每 1–365 小時／天執行一次；off 關閉。",
    "UPDATE_TIME": "自動更新檢查時間：daily-HH:MM、every-Nh、every-Nd 或 off。更新前備份，完成後恢復原本的啟動狀態。",
}
if set(_DESCRIPTIONS) != {spec.key for spec in _SPECS}:  # pragma: no cover - schema authoring guard
    raise AssertionError("every editable setting requires a description")
_SPECS = tuple(replace(spec, description=_DESCRIPTIONS[spec.key]) for spec in _SPECS)

SETTING_SPECS: dict[str, SettingSpec] = {spec.key: spec for spec in _SPECS}
EDITABLE_DEFAULTS: dict[str, str] = {spec.key: spec.default for spec in _SPECS}
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


def normalize_backup_schedule(value: object, *, key: str = "BACKUP_TIME") -> str:
    """Validate backup schedule syntax and canonicalize legacy daily times.

    ``HH:MM`` was the original configuration contract.  Continue accepting it
    when loading old deployments, but write new daily schedules as
    ``daily-HH:MM`` so the cadence is unambiguous next to interval schedules.
    """
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a backup schedule string")
    if value == "off" or re.fullmatch(r"every-([1-9]|[1-9][0-9]|[12][0-9]{2}|3[0-5][0-9]|36[0-5])[hd]", value):
        return value
    if value.startswith("daily-") and _TIME.fullmatch(value[6:]):
        return value
    if _TIME.fullmatch(value):
        return f"daily-{value}"
    raise ConfigError(
        f"{key} must be daily-HH:MM, every-Nh, every-Nd (N: 1–365), or off"
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
    if spec.key in {"BACKUP_TIME", "UPDATE_TIME"}:
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
