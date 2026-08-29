#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${PALWORLD_TEST_BASE_DIR:-}" ]]; then
  BASE_DIR="$PALWORLD_TEST_BASE_DIR"
  [[ "$BASE_DIR" == /tmp/* ]] || { printf 'ERROR: test base must be under /tmp\n' >&2; exit 1; }
  TEST_MODE=1
  CONFIG_DIR="$BASE_DIR/config"
else
  TEST_MODE=0
  CONFIG_DIR="${PALWORLD_CONFIG_DIR:-$(dirname -- "$SCRIPT_HOME")/config}"
fi
MANAGER="$SCRIPT_HOME/palworld_manager.py"
[[ -r "$MANAGER" ]] || { printf 'ERROR: configuration manager is missing\n' >&2; exit 1; }
python3 "$MANAGER" --config-dir "$CONFIG_DIR" --no-filesystem || exit $?
config_value() { python3 "$MANAGER" --config-dir "$CONFIG_DIR" --get "$1"; }
if (( TEST_MODE == 0 )); then BASE_DIR="$(config_value PALWORLD_INSTALL_ROOT)"; fi
SETTINGS_FILE="$BASE_DIR/server/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

if (( EUID != 0 && TEST_MODE != 1 )) && [[ "${PALWORLD_CONTAINER_MODE:-}" != 1 ]]; then
  die 'run this script with sudo'
fi
[[ -f "$SETTINGS_FILE" ]] || die 'PalWorldSettings.ini is missing; start the server once first'

MAX_PLAYERS="$(config_value MAX_PLAYERS)"
BASE_CAMP_MAX_NUM_IN_GUILD="$(config_value BASE_CAMP_MAX_NUM_IN_GUILD)"
SERVER_PASSWORD="$(config_value SERVER_PASSWORD)"
ADMIN_PASSWORD="$(config_value ADMIN_PASSWORD)"
SERVER_NAME="$(config_value SERVER_NAME)"
SERVER_DESCRIPTION="$(config_value SERVER_DESCRIPTION)"
PUBLIC_PORT="$(config_value PUBLIC_PORT)"
PALWORLD_REST_API_PORT="$(config_value PALWORLD_REST_API_PORT)"
SERVICE_USER="$(config_value PALWORLD_SERVICE_USER)"
DAY_TIME_SPEED_RATE="$(config_value DAY_TIME_SPEED_RATE)"
NIGHT_TIME_SPEED_RATE="$(config_value NIGHT_TIME_SPEED_RATE)"
EXP_RATE="$(config_value EXP_RATE)"
PAL_CAPTURE_RATE="$(config_value PAL_CAPTURE_RATE)"
COLLECTION_DROP_RATE="$(config_value COLLECTION_DROP_RATE)"
ENEMY_DROP_ITEM_RATE="$(config_value ENEMY_DROP_ITEM_RATE)"
PAL_DAMAGE_RATE_ATTACK="$(config_value PAL_DAMAGE_RATE_ATTACK)"
PAL_DAMAGE_RATE_DEFENSE="$(config_value PAL_DAMAGE_RATE_DEFENSE)"
PLAYER_DAMAGE_RATE_ATTACK="$(config_value PLAYER_DAMAGE_RATE_ATTACK)"
PLAYER_DAMAGE_RATE_DEFENSE="$(config_value PLAYER_DAMAGE_RATE_DEFENSE)"
PAL_STAMINA_DECREACE_RATE="$(config_value PAL_STAMINA_DECREACE_RATE)"
PLAYER_STAMINA_DECREACE_RATE="$(config_value PLAYER_STAMINA_DECREACE_RATE)"
PAL_AUTO_HP_REGENE_RATE="$(config_value PAL_AUTO_HP_REGENE_RATE)"
PLAYER_AUTO_HP_REGENE_RATE="$(config_value PLAYER_AUTO_HP_REGENE_RATE)"
PAL_AUTO_HP_REGENE_RATE_IN_SLEEP="$(config_value PAL_AUTO_HP_REGENE_RATE_IN_SLEEP)"
PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP="$(config_value PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP)"
PAL_STOMACH_DECREACE_RATE="$(config_value PAL_STOMACH_DECREACE_RATE)"
PLAYER_STOMACH_DECREACE_RATE="$(config_value PLAYER_STOMACH_DECREACE_RATE)"
GUILD_PLAYER_MAX_NUM="$(config_value GUILD_PLAYER_MAX_NUM)"
PAL_SPAWN_NUM_RATE="$(config_value PAL_SPAWN_NUM_RATE)"
DROP_ITEM_MAX_NUM="$(config_value DROP_ITEM_MAX_NUM)"
DROP_ITEM_ALIVE_MAX_HOURS="$(config_value DROP_ITEM_ALIVE_MAX_HOURS)"
PAL_EGG_DEFAULT_HATCHING_TIME="$(config_value PAL_EGG_DEFAULT_HATCHING_TIME)"
BASE_CAMP_WORKER_MAX_NUM="$(config_value BASE_CAMP_WORKER_MAX_NUM)"
WORK_SPEED_RATE="$(config_value WORK_SPEED_RATE)"
ITEM_WEIGHT_RATE="$(config_value ITEM_WEIGHT_RATE)"
EQUIPMENT_DURABILITY_DAMAGE_RATE="$(config_value EQUIPMENT_DURABILITY_DAMAGE_RATE)"
DEATH_PENALTY="$(config_value DEATH_PENALTY)"
BUILD_OBJECT_HP_RATE="$(config_value BUILD_OBJECT_HP_RATE)"
BUILD_OBJECT_DAMAGE_RATE="$(config_value BUILD_OBJECT_DAMAGE_RATE)"
BUILD_OBJECT_DETERIORATION_DAMAGE_RATE="$(config_value BUILD_OBJECT_DETERIORATION_DAMAGE_RATE)"
AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART="$(config_value AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART)"

ini_boolean() {
  case "$1" in
    true) printf 'True' ;;
    false) printf 'False' ;;
    *) die "invalid boolean value: $1" ;;
  esac
}
AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART="$(ini_boolean "$AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART")"

validate_ini_value() {
  local name="$1"
  local value="$2"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || die "$name contains a newline"
  [[ "$value" != *','* && "$value" != *'('* && "$value" != *')'* && "$value" != *'"'* ]] || die "$name contains an INI-reserved character"
  [[ "$value" != *\\ ]] || die "$name must not end with a backslash"
  [[ -n "$value" ]] || die "$name must not be empty"
}

validate_ini_value SERVER_PASSWORD "$SERVER_PASSWORD"
validate_ini_value ADMIN_PASSWORD "$ADMIN_PASSWORD"
validate_ini_value SERVER_NAME "$SERVER_NAME"
validate_ini_value SERVER_DESCRIPTION "$SERVER_DESCRIPTION"

tmp_file="$(mktemp "${SETTINGS_FILE}.tmp.XXXXXX")"
cleanup() {
  rm -f -- "${tmp_file:-}"
}
trap cleanup EXIT

PW_MAX_PLAYERS="$MAX_PLAYERS" \
PW_BASE_CAMP_MAX_NUM_IN_GUILD="$BASE_CAMP_MAX_NUM_IN_GUILD" \
PW_SERVER_PASSWORD="$SERVER_PASSWORD" \
PW_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
PW_SERVER_NAME="$SERVER_NAME" \
PW_SERVER_DESCRIPTION="$SERVER_DESCRIPTION" \
PW_PUBLIC_PORT="$PUBLIC_PORT" \
PW_REST_API_PORT="$PALWORLD_REST_API_PORT" \
PW_DAY_TIME_SPEED_RATE="$DAY_TIME_SPEED_RATE" \
PW_NIGHT_TIME_SPEED_RATE="$NIGHT_TIME_SPEED_RATE" \
PW_EXP_RATE="$EXP_RATE" \
PW_PAL_CAPTURE_RATE="$PAL_CAPTURE_RATE" \
PW_COLLECTION_DROP_RATE="$COLLECTION_DROP_RATE" \
PW_ENEMY_DROP_ITEM_RATE="$ENEMY_DROP_ITEM_RATE" \
PW_PAL_DAMAGE_RATE_ATTACK="$PAL_DAMAGE_RATE_ATTACK" \
PW_PAL_DAMAGE_RATE_DEFENSE="$PAL_DAMAGE_RATE_DEFENSE" \
PW_PLAYER_DAMAGE_RATE_ATTACK="$PLAYER_DAMAGE_RATE_ATTACK" \
PW_PLAYER_DAMAGE_RATE_DEFENSE="$PLAYER_DAMAGE_RATE_DEFENSE" \
PW_PAL_STAMINA_DECREACE_RATE="$PAL_STAMINA_DECREACE_RATE" \
PW_PLAYER_STAMINA_DECREACE_RATE="$PLAYER_STAMINA_DECREACE_RATE" \
PW_PAL_AUTO_HP_REGENE_RATE="$PAL_AUTO_HP_REGENE_RATE" \
PW_PLAYER_AUTO_HP_REGENE_RATE="$PLAYER_AUTO_HP_REGENE_RATE" \
PW_PAL_AUTO_HP_REGENE_RATE_IN_SLEEP="$PAL_AUTO_HP_REGENE_RATE_IN_SLEEP" \
PW_PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP="$PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP" \
PW_PAL_STOMACH_DECREACE_RATE="$PAL_STOMACH_DECREACE_RATE" \
PW_PLAYER_STOMACH_DECREACE_RATE="$PLAYER_STOMACH_DECREACE_RATE" \
PW_GUILD_PLAYER_MAX_NUM="$GUILD_PLAYER_MAX_NUM" \
PW_PAL_SPAWN_NUM_RATE="$PAL_SPAWN_NUM_RATE" \
PW_DROP_ITEM_MAX_NUM="$DROP_ITEM_MAX_NUM" \
PW_PAL_EGG_DEFAULT_HATCHING_TIME="$PAL_EGG_DEFAULT_HATCHING_TIME" \
PW_DROP_ITEM_ALIVE_MAX_HOURS="$DROP_ITEM_ALIVE_MAX_HOURS" \
PW_BASE_CAMP_WORKER_MAX_NUM="$BASE_CAMP_WORKER_MAX_NUM" \
PW_WORK_SPEED_RATE="$WORK_SPEED_RATE" \
PW_ITEM_WEIGHT_RATE="$ITEM_WEIGHT_RATE" \
PW_EQUIPMENT_DURABILITY_DAMAGE_RATE="$EQUIPMENT_DURABILITY_DAMAGE_RATE" \
PW_DEATH_PENALTY="$DEATH_PENALTY" \
PW_BUILD_OBJECT_HP_RATE="$BUILD_OBJECT_HP_RATE" \
PW_BUILD_OBJECT_DAMAGE_RATE="$BUILD_OBJECT_DAMAGE_RATE" \
PW_BUILD_OBJECT_DETERIORATION_DAMAGE_RATE="$BUILD_OBJECT_DETERIORATION_DAMAGE_RATE" \
PW_AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART="$AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART" \
python3 - "$SETTINGS_FILE" "$tmp_file" <<'PY'
import os
import re
import sys

TARGET_SECTION = "/Script/Pal.PalWorldSettings"
SECTION = re.compile(r"^\s*\[([^]\r\n]+)\]\s*$")
OPTION = re.compile(r"^(\s*OptionSettings\s*=\s*)\((.*)\)(\s*)$")
FIELD = re.compile(r"^(\s*([A-Za-z_][A-Za-z0-9_]*)\s*=)")


def fail(message):
    raise ValueError(message)


def split_fields(value):
    """Split a tuple without treating commas inside quoted values as fields."""
    fields, start, quote, escaped = [], 0, None, False
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"\"", "'"}:
            quote = char
        elif char == ",":
            fields.append(value[start:index])
            start = index + 1
    if quote or escaped:
        fail("OptionSettings has an unterminated quoted value")
    fields.append(value[start:])
    if any(not field.strip() for field in fields):
        fail("OptionSettings contains an empty field")
    return fields


def validate_ini(text):
    """Perform the INI/tuple checks required before replacing the live file."""
    section_seen = False
    depth = 0
    quote = None
    escaped = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if SECTION.fullmatch(line):
            section_seen = True
        elif "=" not in line:
            fail(f"INI syntax error on line {number}: expected section or key=value")
        for char in raw:
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in {"\"", "'"}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    fail(f"INI syntax error on line {number}: unmatched closing parenthesis")
    if quote or escaped:
        fail("INI syntax error: unterminated quoted value")
    if depth:
        fail("INI syntax error: unbalanced parentheses")
    if not section_seen:
        fail(f"INI syntax error: missing [{TARGET_SECTION}] section")


def target_option_line(lines):
    starts = [index for index, line in enumerate(lines) if SECTION.fullmatch(line.rstrip("\r\n")) and SECTION.fullmatch(line.rstrip("\r\n")).group(1) == TARGET_SECTION]
    if len(starts) != 1:
        fail(f"expected exactly one [{TARGET_SECTION}] section")
    start = starts[0]
    end = next((index for index in range(start + 1, len(lines)) if SECTION.fullmatch(lines[index].rstrip("\r\n"))), len(lines))
    matches = [index for index in range(start + 1, end) if lines[index].lstrip().startswith("OptionSettings")]
    if len(matches) != 1:
        fail(f"expected exactly one OptionSettings tuple in [{TARGET_SECTION}]")
    return matches[0]


def quoted(value):
    return f'"{value}"'


source, destination = map(os.fspath, sys.argv[1:3])
try:
    with open(source, encoding="utf-8", newline="") as handle:
        lines = handle.readlines()
    original = "".join(lines)
    validate_ini(original)
    option_index = target_option_line(lines)
    newline = "\r\n" if lines[option_index].endswith("\r\n") else "\n" if lines[option_index].endswith("\n") else ""
    match = OPTION.fullmatch(lines[option_index].rstrip("\r\n"))
    if not match:
        fail("OptionSettings must be a complete tuple on one line")
    prefix, body, suffix = match.groups()
    fields = split_fields(body)
    required = {
        "ServerPlayerMaxNum": os.environ["PW_MAX_PLAYERS"],
        "PublicPort": os.environ["PW_PUBLIC_PORT"],
        "ServerPassword": quoted(os.environ["PW_SERVER_PASSWORD"]),
        "AdminPassword": quoted(os.environ["PW_ADMIN_PASSWORD"]),
        "ServerName": quoted(os.environ["PW_SERVER_NAME"]),
        "ServerDescription": quoted(os.environ["PW_SERVER_DESCRIPTION"]),
    }
    options = {
        "DayTimeSpeedRate": os.environ["PW_DAY_TIME_SPEED_RATE"],
        "NightTimeSpeedRate": os.environ["PW_NIGHT_TIME_SPEED_RATE"],
        "ExpRate": os.environ["PW_EXP_RATE"],
        "PalCaptureRate": os.environ["PW_PAL_CAPTURE_RATE"],
        "CollectionDropRate": os.environ["PW_COLLECTION_DROP_RATE"],
        "EnemyDropItemRate": os.environ["PW_ENEMY_DROP_ITEM_RATE"],
        "PalDamageRateAttack": os.environ["PW_PAL_DAMAGE_RATE_ATTACK"],
        "PalDamageRateDefense": os.environ["PW_PAL_DAMAGE_RATE_DEFENSE"],
        "PlayerDamageRateAttack": os.environ["PW_PLAYER_DAMAGE_RATE_ATTACK"],
        "PlayerDamageRateDefense": os.environ["PW_PLAYER_DAMAGE_RATE_DEFENSE"],
        "PalStaminaDecreaceRate": os.environ["PW_PAL_STAMINA_DECREACE_RATE"],
        "PlayerStaminaDecreaceRate": os.environ["PW_PLAYER_STAMINA_DECREACE_RATE"],
        "PalAutoHPRegeneRate": os.environ["PW_PAL_AUTO_HP_REGENE_RATE"],
        "PlayerAutoHPRegeneRate": os.environ["PW_PLAYER_AUTO_HP_REGENE_RATE"],
        "PalAutoHpRegeneRateInSleep": os.environ["PW_PAL_AUTO_HP_REGENE_RATE_IN_SLEEP"],
        "PlayerAutoHpRegeneRateInSleep": os.environ["PW_PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP"],
        "PalStomachDecreaceRate": os.environ["PW_PAL_STOMACH_DECREACE_RATE"],
        "PlayerStomachDecreaceRate": os.environ["PW_PLAYER_STOMACH_DECREACE_RATE"],
        "GuildPlayerMaxNum": os.environ["PW_GUILD_PLAYER_MAX_NUM"],
        "PalSpawnNumRate": os.environ["PW_PAL_SPAWN_NUM_RATE"],
        "DropItemMaxNum": os.environ["PW_DROP_ITEM_MAX_NUM"],
        "DropItemAliveMaxHours": os.environ["PW_DROP_ITEM_ALIVE_MAX_HOURS"],
        "PalEggDefaultHatchingTime": os.environ["PW_PAL_EGG_DEFAULT_HATCHING_TIME"],
        "BaseCampWorkerMaxNum": os.environ["PW_BASE_CAMP_WORKER_MAX_NUM"],
        "WorkSpeedRate": os.environ["PW_WORK_SPEED_RATE"],
        "ItemWeightRate": os.environ["PW_ITEM_WEIGHT_RATE"],
        "EquipmentDurabilityDamageRate": os.environ["PW_EQUIPMENT_DURABILITY_DAMAGE_RATE"],
        "DeathPenalty": os.environ["PW_DEATH_PENALTY"],
        "BuildObjectHpRate": os.environ["PW_BUILD_OBJECT_HP_RATE"],
        "BuildObjectDamageRate": os.environ["PW_BUILD_OBJECT_DAMAGE_RATE"],
        "BuildObjectDeteriorationDamageRate": os.environ["PW_BUILD_OBJECT_DETERIORATION_DAMAGE_RATE"],
        "AutoResetWorkerPalWhenServerRestart": os.environ["PW_AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART"],
        "bIsUseBackupSaveData": "True",
        "RESTAPIEnabled": "True",
        "RESTAPIPort": os.environ["PW_REST_API_PORT"],
        "BaseCampMaxNumInGuild": os.environ["PW_BASE_CAMP_MAX_NUM_IN_GUILD"],
    }
    replacements = {**required, **options}
    present, rendered = set(), []
    for field in fields:
        field_match = FIELD.match(field)
        if not field_match:
            fail(f"invalid OptionSettings field: {field!r}")
        key = field_match.group(2)
        if key in present:
            fail(f"duplicate OptionSettings field: {key}")
        present.add(key)
        rendered.append(field_match.group(1) + replacements[key] if key in replacements else field)
    missing_required = sorted(set(required) - present)
    if missing_required:
        fail("required OptionSettings field(s) missing: " + ", ".join(missing_required))
    rendered.extend(f"{key}={value}" for key, value in options.items() if key not in present)
    lines[option_index] = prefix + "(" + ",".join(rendered) + ")" + suffix + newline
    result = "".join(lines)
    validate_ini(result)
    result_fields = split_fields(OPTION.fullmatch(lines[option_index].rstrip("\r\n")).group(2))
    result_values = {FIELD.match(field).group(2): field.split("=", 1)[1].strip() for field in result_fields}
    for key, value in replacements.items():
        if result_values.get(key) != value:
            fail(f"rendered OptionSettings field is invalid: {key}")
    with open(destination, "w", encoding="utf-8", newline="") as handle:
        handle.write(result)
except (OSError, UnicodeError, ValueError) as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY

if (( TEST_MODE == 0 && "${PALWORLD_CONTAINER_MODE:-}" != 1 )); then
  chown "$SERVICE_USER:$SERVICE_USER" "$tmp_file"
fi
chmod 0640 "$tmp_file"
mv -f -- "$tmp_file" "$SETTINGS_FILE"
trap - EXIT
printf 'PalWorldSettings.ini rendered from %s (password values were not displayed).\n' "$CONFIG_DIR"
