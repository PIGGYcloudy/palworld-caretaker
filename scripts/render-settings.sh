#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

BASE_DIR="${PALWORLD_TEST_BASE_DIR:-/srv/palworld}"
if [[ -n "${PALWORLD_TEST_BASE_DIR:-}" ]]; then
  [[ "$BASE_DIR" == /tmp/* ]] || { printf 'ERROR: test base must be under /tmp\n' >&2; exit 1; }
  TEST_MODE=1
else
  TEST_MODE=0
fi
ENV_FILE="$BASE_DIR/config/palworld.env"
SETTINGS_FILE="$BASE_DIR/server/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

(( EUID == 0 || TEST_MODE == 1 )) || die 'run this script with sudo'
[[ -r "$ENV_FILE" ]] || die 'configuration file is missing'
[[ -f "$SETTINGS_FILE" ]] || die 'PalWorldSettings.ini is missing; start the server once first'

# shellcheck disable=SC1090
source "$ENV_FILE"
: "${MAX_PLAYERS:?MAX_PLAYERS is required}"
: "${BASE_CAMP_MAX_NUM_IN_GUILD:?BASE_CAMP_MAX_NUM_IN_GUILD is required}"
: "${SERVER_PASSWORD:?SERVER_PASSWORD is required}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required}"
: "${SERVER_NAME:?SERVER_NAME is required}"
: "${SERVER_DESCRIPTION:?SERVER_DESCRIPTION is required}"
: "${PUBLIC_PORT:?PUBLIC_PORT is required}"
: "${PALWORLD_REST_API_PORT:?PALWORLD_REST_API_PORT is required}"

[[ "$MAX_PLAYERS" =~ ^[1-9][0-9]*$ ]] || die 'MAX_PLAYERS must be a positive integer'
(( MAX_PLAYERS <= 32 )) || die 'MAX_PLAYERS must be 32 or less'
[[ "$BASE_CAMP_MAX_NUM_IN_GUILD" =~ ^[1-9][0-9]*$ ]] || die 'BASE_CAMP_MAX_NUM_IN_GUILD must be a positive integer'
(( BASE_CAMP_MAX_NUM_IN_GUILD <= 10 )) || die 'BASE_CAMP_MAX_NUM_IN_GUILD must be 10 or less'
[[ "$PUBLIC_PORT" =~ ^[1-9][0-9]*$ ]] || die 'PUBLIC_PORT must be a positive integer'
(( PUBLIC_PORT <= 65535 )) || die 'PUBLIC_PORT is out of range'
[[ "$PALWORLD_REST_API_PORT" =~ ^[1-9][0-9]*$ ]] || die 'PALWORLD_REST_API_PORT must be a positive integer'
(( PALWORLD_REST_API_PORT <= 65535 )) || die 'PALWORLD_REST_API_PORT is out of range'

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
  chown palworld:palworld "$tmp_file"
fi
chmod 0640 "$tmp_file"
mv -f -- "$tmp_file" "$SETTINGS_FILE"
trap - EXIT
printf 'PalWorldSettings.ini rendered from %s (password values were not displayed).\n' "$ENV_FILE"
