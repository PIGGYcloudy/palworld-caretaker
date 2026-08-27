#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${PALWORLD_CONFIG_DIR:-$(dirname -- "$SCRIPT_HOME")/config}"
MANAGER="$SCRIPT_HOME/palworld_manager.py"
[[ -r "$MANAGER" ]] || { printf 'ERROR: configuration manager is missing: %s\n' "$MANAGER" >&2; exit 1; }
python3 "$MANAGER" --config-dir "$CONFIG_DIR" || exit $?
config_value() {
  python3 "$MANAGER" --config-dir "$CONFIG_DIR" --get "$1"
}
SERVER_ROOT="$(config_value PALWORLD_SERVER_ROOT)"
SAVE_ROOT="$SERVER_ROOT/Pal/Saved/SaveGames"
CONFIG_ROOT="$SERVER_ROOT/Pal/Saved/Config"
BACKUP_ROOT="$(config_value PALWORLD_BACKUP_DIR)"
BACKUP_MOUNT="$(config_value PALWORLD_BACKUP_MOUNT)"
BACKUP_REQUIRE_MOUNT="$(config_value PALWORLD_BACKUP_REQUIRE_MOUNT)"
LOCAL_ROOT="$(config_value PALWORLD_LOCAL_BACKUP_ROOT)"
SERVICE_USER="$(config_value PALWORLD_SERVICE_USER)"
LOCK_FILE="${PALWORLD_OPERATION_LOCK_FILE:-/run/palworld-caretaker/operation.lock}"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

(( EUID == 0 )) || die 'run this script with sudo'
if [[ "$BACKUP_REQUIRE_MOUNT" == true ]]; then
  mountpoint -q "$BACKUP_MOUNT" || die "backup filesystem is not mounted: $BACKUP_MOUNT"
fi

list_backups() {
  if [[ ! -d "$BACKUP_ROOT" ]]; then
    printf 'No Palworld backup directory exists yet: %s\n' "$BACKUP_ROOT"
    return 0
  fi
  find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
    -name 'palworld-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]' \
    -printf '%f\n' | sort -r
}

if [[ $# -eq 0 || "${1:-}" == 'list' ]]; then
  list_backups
  exit 0
fi

[[ $# -eq 2 && "$1" == 'restore' ]] || {
  printf 'Usage:\n  %s\n  %s restore palworld-YYYYMMDD-HHMMSS\n' "$0" "$0" >&2
  exit 2
}

VERSION="$2"
[[ "$VERSION" =~ ^palworld-[0-9]{8}-[0-9]{6}$ ]] || die 'backup version name is invalid'
BACKUP_DIR="$BACKUP_ROOT/$VERSION"
[[ "$BACKUP_DIR" == "$BACKUP_ROOT"/palworld-* ]] || die 'backup path safety check failed'
[[ -d "$BACKUP_DIR" ]] || die "backup version does not exist: $VERSION"
[[ -d "$BACKUP_DIR/savegames" ]] || die 'backup savegames directory is missing'
[[ -d "$BACKUP_DIR/config" ]] || die 'backup config directory is missing'
[[ -f "$BACKUP_DIR/config/LinuxServer/PalWorldSettings.ini" ]] || die 'backup settings file is missing'
find "$BACKUP_DIR/savegames" -type d -name backup -print -quit | grep -q . || die 'backup built-in backup directory is missing'

# The portable core checks the verified snapshot inventory, required mount, and
# every restore filesystem before service interruption.  It repeats the check
# immediately before copying to close the normal prompt-to-restore race.
if python3 "$MANAGER" --core-engine >/dev/null 2>&1; then
  python3 "$MANAGER" --config-dir "$CONFIG_DIR" --restore-preflight "$VERSION" || exit $?
fi

printf 'This will stop Palworld, create a fresh pre-restore backup, and overwrite the live save/config.\n'
printf 'Type exactly "RESTORE %s" to continue: ' "$VERSION"
read -r confirmation
[[ "$confirmation" == "RESTORE $VERSION" ]] || die 'restore confirmation did not match'

# The confirmation is deliberately outside the lock; the destructive state
# check and every subsequent filesystem/service action are inside it.
[[ -f "$LOCK_FILE" && ! -L "$LOCK_FILE" ]] || die "operation lock is unsafe or missing: $LOCK_FILE"
exec 9<"$LOCK_FILE"
flock -n 9 || die 'another Palworld operation is already running'
export PALWORLD_OPERATION_LOCK_HELD=1

was_active=0
stamp="$(date +%Y%m%d-%H%M%S)"
local_snapshot="$LOCAL_ROOT/pre-restore-$stamp"
cleanup() {
  local rc=$?
  if (( was_active == 1 )); then
    if ! systemctl is-active --quiet palworld.service; then
      systemctl start palworld.service || true
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT

if systemctl is-active --quiet palworld.service; then
  was_active=1
  systemctl stop palworld.service
fi

# The backup script sees the service stopped and therefore leaves it stopped.
"$SCRIPT_HOME/backup-palworld.sh" --pre-restore

if python3 "$MANAGER" --core-engine >/dev/null 2>&1; then
  python3 "$MANAGER" --config-dir "$CONFIG_DIR" --restore "$VERSION" || exit $?
  chown -R "$SERVICE_USER:$SERVICE_USER" "$SAVE_ROOT" "$CONFIG_ROOT"
  find "$SAVE_ROOT" "$CONFIG_ROOT" -type d -exec chmod 0750 {} +
  find "$SAVE_ROOT" "$CONFIG_ROOT" -type f -exec chmod 0640 {} +
  printf 'Restore completed from %s.\n' "$VERSION"
  exit 0
fi

install -d -o root -g root -m 0700 "$local_snapshot"
rsync -a "$SAVE_ROOT/" "$local_snapshot/savegames/"
rsync -a "$CONFIG_ROOT/" "$local_snapshot/config/"

rsync -a --delete "$BACKUP_DIR/savegames/" "$SAVE_ROOT/"
rsync -a --delete "$BACKUP_DIR/config/" "$CONFIG_ROOT/"

chown -R "$SERVICE_USER:$SERVICE_USER" "$SAVE_ROOT" "$CONFIG_ROOT"
find "$SAVE_ROOT" "$CONFIG_ROOT" -type d -exec chmod 0750 {} +
find "$SAVE_ROOT" "$CONFIG_ROOT" -type f -exec chmod 0640 {} +

printf 'Restore completed from %s.\n' "$VERSION"
printf 'Current pre-restore safety copy: %s\n' "$local_snapshot"
