#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SERVICE='palworld.service'
BASE_DIR='/srv/palworld'
SERVER_ROOT="$BASE_DIR/server"
SAVE_ROOT="$SERVER_ROOT/Pal/Saved/SaveGames"
CONFIG_ROOT="$SERVER_ROOT/Pal/Saved/Config"
ENV_FILE="$BASE_DIR/config/palworld.env"
NAS_MOUNT='/mnt/qnap-tyt'
BACKUP_ROOT="$NAS_MOUNT/palworld-backups"
LOCK_FILE='/run/lock/palworld-backup.lock'

log() {
  printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

(( EUID == 0 )) || die 'run this script with sudo'
[[ -r "$ENV_FILE" ]] || die 'configuration file is missing'
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${BACKUP_RETENTION_COUNT:?BACKUP_RETENTION_COUNT is required}"
[[ "$BACKUP_RETENTION_COUNT" =~ ^[1-9][0-9]*$ ]] || die 'BACKUP_RETENTION_COUNT must be a positive integer'
(( BACKUP_RETENTION_COUNT <= 1000 )) || die 'BACKUP_RETENTION_COUNT is unreasonably large'

[[ "$BACKUP_ROOT" == "$NAS_MOUNT/palworld-backups" ]] || die 'backup path safety check failed'
exec 9>"$LOCK_FILE"
flock -n 9 || die 'another Palworld backup is already running'

# This check intentionally happens before mkdir. A missing NAS mount must never
# turn the mountpoint directory into a local backup destination.
mountpoint -q "$NAS_MOUNT" || die "NAS is not mounted: $NAS_MOUNT"
[[ -d "$NAS_MOUNT" ]] || die 'NAS mount path is not a directory'
[[ -w "$NAS_MOUNT" ]] || die 'NAS mount is not writable'

[[ -d "$SERVER_ROOT" ]] || die 'Palworld server directory is missing'
[[ -d "$SAVE_ROOT" ]] || die 'Palworld save directory is missing'
[[ -d "$CONFIG_ROOT" ]] || die 'Palworld config directory is missing'
SETTINGS_FILE="$CONFIG_ROOT/LinuxServer/PalWorldSettings.ini"
[[ -f "$SETTINGS_FILE" ]] || die 'PalWorldSettings.ini is missing'

BUILTIN_BACKUP_DIR="$(find "$SAVE_ROOT" -type d -name backup -print -quit)"
[[ -n "$BUILTIN_BACKUP_DIR" ]] || die 'Palworld built-in backup directory was not found'

mapfile -t source_sizes < <(du -sx --bytes "$SAVE_ROOT" "$CONFIG_ROOT" | awk '{print $1}')
(( ${#source_sizes[@]} == 2 )) || die 'could not measure Palworld source size'
SOURCE_BYTES=$(( source_sizes[0] + source_sizes[1] ))
AVAILABLE_KIB="$(df -Pk "$NAS_MOUNT" | awk 'NR == 2 {print $4}')"
[[ "$AVAILABLE_KIB" =~ ^[0-9]+$ ]] || die 'could not measure NAS free space'
REQUIRED_BYTES=$(( SOURCE_BYTES * 2 + 1073741824 ))
(( AVAILABLE_KIB * 1024 >= REQUIRED_BYTES )) || die 'NAS free space is insufficient'

mkdir -p -- "$BACKUP_ROOT"

STAMP="$(date +%Y%m%d-%H%M%S)"
FINAL_DIR="$BACKUP_ROOT/palworld-$STAMP"
STAGING_DIR="$BACKUP_ROOT/.incomplete-$STAMP-$$"
[[ ! -e "$FINAL_DIR" ]] || die "backup version already exists: $STAMP"

was_active=0
cleanup() {
  local rc=$?
  if [[ -n "${STAGING_DIR:-}" && -d "$STAGING_DIR" && "$STAGING_DIR" == "$BACKUP_ROOT"/.incomplete-* ]]; then
    rm -rf -- "$STAGING_DIR"
  fi
  if (( was_active == 1 )); then
    if ! systemctl is-active --quiet "$SERVICE"; then
      log 'Restarting Palworld after backup.'
      systemctl start "$SERVICE" || log 'ERROR: Palworld could not be restarted automatically.'
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT

if systemctl is-active --quiet "$SERVICE"; then
  was_active=1
  log 'Stopping Palworld for a consistent snapshot.'
  systemctl stop "$SERVICE"
fi

mkdir -p -- "$STAGING_DIR/savegames" "$STAGING_DIR/config" "$STAGING_DIR/metadata"
log "Syncing save data with rsync."
rsync -a --delete "$SAVE_ROOT/" "$STAGING_DIR/savegames/"
log "Syncing server configuration with rsync."
rsync -a --delete "$CONFIG_ROOT/" "$STAGING_DIR/config/"

printf 'created_at=%s\n' "$(date --iso-8601=seconds)" > "$STAGING_DIR/metadata/manifest.txt"
printf 'source_save_dir=%s\n' "$SAVE_ROOT" >> "$STAGING_DIR/metadata/manifest.txt"
printf 'source_config_dir=%s\n' "$CONFIG_ROOT" >> "$STAGING_DIR/metadata/manifest.txt"
printf 'built_in_backup_dir=%s\n' "${BUILTIN_BACKUP_DIR#"$SAVE_ROOT"/}" >> "$STAGING_DIR/metadata/manifest.txt"
printf 'retention_count=%s\n' "$BACKUP_RETENTION_COUNT" >> "$STAGING_DIR/metadata/manifest.txt"

[[ -f "$STAGING_DIR/config/LinuxServer/PalWorldSettings.ini" ]] || die 'staged settings file is missing'
find "$STAGING_DIR/savegames" -type d -name backup -print -quit | grep -q . || die 'staged built-in backup directory is missing'

mv -- "$STAGING_DIR" "$FINAL_DIR"
log "Created backup version: $(basename "$FINAL_DIR")"

mapfile -t snapshots < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'palworld-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]' -printf '%f\n' | sort)
while (( ${#snapshots[@]} > BACKUP_RETENTION_COUNT )); do
  old_snapshot="${snapshots[0]}"
  [[ "$old_snapshot" =~ ^palworld-[0-9]{8}-[0-9]{6}$ ]] || die 'old snapshot name failed safety validation'
  old_path="$BACKUP_ROOT/$old_snapshot"
  [[ "$old_path" == "$BACKUP_ROOT/palworld-"* ]] || die 'old snapshot path failed safety validation'
  log "Removing oldest Palworld snapshot: $old_snapshot"
  rm -rf -- "$old_path"
  snapshots=("${snapshots[@]:1}")
done

log "Backup complete; retained ${#snapshots[@]} version(s)."
