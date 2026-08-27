#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

(( EUID == 0 )) || { printf 'Run with sudo: sudo bash %s\n' "$0" >&2; exit 1; }

BASE_DIR='/srv/palworld'
ENV_FILE="$BASE_DIR/config/palworld.env"
SETTINGS_FILE="$BASE_DIR/server/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
BACKUP_DIR="$BASE_DIR/backups-local/config-change-$(date +%Y%m%d-%H%M%S)"

[[ -f "$ENV_FILE" ]] || { printf 'palworld.env was not found\n' >&2; exit 1; }
[[ -f "$SETTINGS_FILE" ]] || { printf 'PalWorldSettings.ini was not found\n' >&2; exit 1; }

install -d -o root -g root -m 0700 "$BACKUP_DIR"
install -o root -g root -m 0600 "$ENV_FILE" "$BACKUP_DIR/palworld.env"
install -o root -g root -m 0600 "$SETTINGS_FILE" "$BACKUP_DIR/PalWorldSettings.ini"

env_tmp="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
settings_tmp="$(mktemp "${SETTINGS_FILE}.tmp.XXXXXX")"
cleanup() { rm -f -- "${env_tmp:-}" "${settings_tmp:-}"; }
trap cleanup EXIT

awk '
  BEGIN { found=0 }
  /^BASE_CAMP_MAX_NUM_IN_GUILD=/ { print "BASE_CAMP_MAX_NUM_IN_GUILD=10"; found=1; next }
  { print }
  END { if (!found) print "BASE_CAMP_MAX_NUM_IN_GUILD=10" }
' "$ENV_FILE" > "$env_tmp"

perl -0pe '
  if (/\bBaseCampMaxNumInGuild=/) {
    my $count = s/(BaseCampMaxNumInGuild=)(?:"[^"]*"|[^,)]*)/$1 . "10"/ge;
    die "duplicate or invalid BaseCampMaxNumInGuild\n" if $count != 1;
  } else {
    s/(OptionSettings=\(.*)\)/$1 . ",BaseCampMaxNumInGuild=10)"/se
      or die "OptionSettings block missing\n";
  }
' "$SETTINGS_FILE" > "$settings_tmp"

grep -q 'BaseCampMaxNumInGuild=10' "$settings_tmp" || {
  printf 'rendered guild base limit validation failed\n' >&2; exit 1;
}

chown root:palworld-manager "$env_tmp"
chmod 0640 "$env_tmp"
chown palworld:palworld "$settings_tmp"
chmod 0640 "$settings_tmp"
mv -f -- "$env_tmp" "$ENV_FILE"
mv -f -- "$settings_tmp" "$SETTINGS_FILE"
trap - EXIT

printf 'BaseCampMaxNumInGuild=10 is staged for the next Palworld restart.\n'
printf 'No service was restarted. Backup: %s\n' "$BACKUP_DIR"
