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

(( EUID == 0 || TEST_MODE == 1 )) || die 'run this script with sudo'
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
GUILD_PLAYER_MAX_NUM="$(config_value GUILD_PLAYER_MAX_NUM)"
PAL_SPAWN_NUM_RATE="$(config_value PAL_SPAWN_NUM_RATE)"
DROP_ITEM_MAX_NUM="$(config_value DROP_ITEM_MAX_NUM)"
PAL_EGG_DEFAULT_HATCHING_TIME="$(config_value PAL_EGG_DEFAULT_HATCHING_TIME)"

validate_ini_value() {
  local name="$1"
  local value="$2"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || die "$name contains a newline"
  [[ "$value" != *','* && "$value" != *'('* && "$value" != *')'* && "$value" != *'"'* ]] || die "$name contains an INI-reserved character"
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
PW_GUILD_PLAYER_MAX_NUM="$GUILD_PLAYER_MAX_NUM" \
PW_PAL_SPAWN_NUM_RATE="$PAL_SPAWN_NUM_RATE" \
PW_DROP_ITEM_MAX_NUM="$DROP_ITEM_MAX_NUM" \
PW_PAL_EGG_DEFAULT_HATCHING_TIME="$PAL_EGG_DEFAULT_HATCHING_TIME" \
perl -0pe '
  my %numeric = (
    ServerPlayerMaxNum => $ENV{PW_MAX_PLAYERS},
    PublicPort => $ENV{PW_PUBLIC_PORT},
  );
  my %string = (
    ServerPassword => $ENV{PW_SERVER_PASSWORD},
    AdminPassword => $ENV{PW_ADMIN_PASSWORD},
    ServerName => $ENV{PW_SERVER_NAME},
    ServerDescription => $ENV{PW_SERVER_DESCRIPTION},
  );
  for my $key (keys %numeric) {
    my $value = $numeric{$key};
    my $count = s/(\Q$key\E=)(?:"[^"]*"|[^,)]*)/$1 . $value/ge;
    die "required setting missing\n" if $count == 0;
  }
  for my $key (keys %string) {
    my $value = $string{$key};
    my $count = s/(\Q$key\E=)(?:"[^"]*"|[^,)]*)/$1 . chr(34) . $value . chr(34)/ge;
    die "required setting missing\n" if $count == 0;
  }
  my %option = (
    DayTimeSpeedRate => $ENV{PW_DAY_TIME_SPEED_RATE},
    NightTimeSpeedRate => $ENV{PW_NIGHT_TIME_SPEED_RATE},
    ExpRate => $ENV{PW_EXP_RATE},
    PalCaptureRate => $ENV{PW_PAL_CAPTURE_RATE},
    CollectionDropRate => $ENV{PW_COLLECTION_DROP_RATE},
    EnemyDropItemRate => $ENV{PW_ENEMY_DROP_ITEM_RATE},
    PalDamageRateAttack => $ENV{PW_PAL_DAMAGE_RATE_ATTACK},
    PalDamageRateDefense => $ENV{PW_PAL_DAMAGE_RATE_DEFENSE},
    PlayerDamageRateAttack => $ENV{PW_PLAYER_DAMAGE_RATE_ATTACK},
    PlayerDamageRateDefense => $ENV{PW_PLAYER_DAMAGE_RATE_DEFENSE},
    GuildPlayerMaxNum => $ENV{PW_GUILD_PLAYER_MAX_NUM},
    PalSpawnNumRate => $ENV{PW_PAL_SPAWN_NUM_RATE},
    DropItemMaxNum => $ENV{PW_DROP_ITEM_MAX_NUM},
    PalEggDefaultHatchingTime => $ENV{PW_PAL_EGG_DEFAULT_HATCHING_TIME},
  );
  for my $key (keys %option) {
    my $value = $option{$key};
    if (!/\b\Q$key\E=/) {
      s/(OptionSettings=\(.*)\)/$1 . "," . $key . "=" . $value . ")"/se
        or die "OptionSettings block missing\n";
    } else {
      s/(\Q$key\E=)(?:"[^"]*"|[^,)])*/$1 . $value/ge
        or die "world setting missing\n";
    }
  }
  if (!/\bbIsUseBackupSaveData=/) {
    s/(OptionSettings=\(.*)\)/$1 . ",bIsUseBackupSaveData=True)"/se
      or die "OptionSettings block missing\n";
  } else {
    s/(bIsUseBackupSaveData=)(?:"[^"]*"|[^,)]*)/$1 . "True"/ge
      or die "backup setting missing\n";
  }
  if (!/\bRESTAPIEnabled=/) {
    s/(OptionSettings=\(.*)\)/$1 . ",RESTAPIEnabled=True)"/se
      or die "OptionSettings block missing\n";
  } else {
    s/(RESTAPIEnabled=)(?:"[^"]*"|[^,)]*)/$1 . "True"/ge;
  }
  if (!/\bRESTAPIPort=/) {
    s/(OptionSettings=\(.*)\)/$1 . ",RESTAPIPort=" . $ENV{PW_REST_API_PORT} . ")"/se
      or die "OptionSettings block missing\n";
  } else {
    s/(RESTAPIPort=)(?:"[^"]*"|[^,)]*)/$1 . $ENV{PW_REST_API_PORT}/ge
      or die "REST API port setting missing\n";
  }
  if (!/\bBaseCampMaxNumInGuild=/) {
    s/(OptionSettings=\(.*)\)/$1 . ",BaseCampMaxNumInGuild=" . $ENV{PW_BASE_CAMP_MAX_NUM_IN_GUILD} . ")"/se
      or die "OptionSettings block missing\n";
  } else {
    s/(BaseCampMaxNumInGuild=)(?:"[^"]*"|[^,)]*)/$1 . $ENV{PW_BASE_CAMP_MAX_NUM_IN_GUILD}/ge
      or die "guild base camp setting missing\n";
  }
' "$SETTINGS_FILE" > "$tmp_file"

grep -q 'ServerPlayerMaxNum=' "$tmp_file" || die 'rendered settings missing ServerPlayerMaxNum'
grep -q 'ServerPassword=' "$tmp_file" || die 'rendered settings missing ServerPassword'
grep -q 'AdminPassword=' "$tmp_file" || die 'rendered settings missing AdminPassword'
grep -q 'bIsUseBackupSaveData=True' "$tmp_file" || die 'rendered settings missing backup setting'
grep -q 'RESTAPIEnabled=True' "$tmp_file" || die 'rendered settings missing REST API setting'
grep -q "RESTAPIPort=$PALWORLD_REST_API_PORT" "$tmp_file" || die 'rendered settings missing REST API port'
grep -q "BaseCampMaxNumInGuild=$BASE_CAMP_MAX_NUM_IN_GUILD" "$tmp_file" || die 'rendered settings missing guild base camp limit'

if (( TEST_MODE == 0 )); then
  chown "$SERVICE_USER:$SERVICE_USER" "$tmp_file"
fi
chmod 0640 "$tmp_file"
mv -f -- "$tmp_file" "$SETTINGS_FILE"
trap - EXIT
printf 'PalWorldSettings.ini rendered from %s (password values were not displayed).\n' "$CONFIG_DIR"
