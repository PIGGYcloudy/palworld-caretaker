#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SERVICE='palworld.service'
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
BACKUP_RETENTION_COUNT="$(config_value BACKUP_RETENTION_COUNT)"
ADMIN_PASSWORD="$(config_value ADMIN_PASSWORD)"
REST_HOST="$(config_value PALWORLD_REST_API_HOST)"
REST_PORT="$(config_value PALWORLD_REST_API_PORT)"
REST_USERNAME="$(config_value PALWORLD_REST_API_USERNAME)"
LOCK_FILE="${PALWORLD_OPERATION_LOCK_FILE:-/run/palworld-caretaker/operation.lock}"

log() {
  printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

(( EUID == 0 )) || die 'run this script with sudo'
if [[ "${PALWORLD_OPERATION_LOCK_HELD:-}" != 1 ]]; then
  [[ -f "$LOCK_FILE" && ! -L "$LOCK_FILE" ]] || die "operation lock is unsafe or missing: $LOCK_FILE"
  exec 9<"$LOCK_FILE"
  flock -n 9 || die 'another Palworld operation is already running'
  export PALWORLD_OPERATION_LOCK_HELD=1
fi
if python3 "$MANAGER" --core-engine >/dev/null 2>&1; then
  # v0.2 keeps this CLI and its arguments stable while moving snapshot
  # decisions into the portable Python engine.  Old single-file deployments
  # retain the implementation below until they are upgraded.
  exec python3 "$MANAGER" --config-dir "$CONFIG_DIR" --backup "$@"
fi

# The legacy implementation remains usable for old installations, but shares
# the same deployment-wide lock as the portable core.

if [[ "$BACKUP_REQUIRE_MOUNT" == true ]]; then
  # This check intentionally happens before mkdir. A missing mount must never
  # turn the mountpoint directory into a local backup destination.
  mountpoint -q "$BACKUP_MOUNT" || die "backup filesystem is not mounted: $BACKUP_MOUNT"
  [[ -d "$BACKUP_MOUNT" ]] || die 'backup mount path is not a directory'
  [[ -w "$BACKUP_MOUNT" ]] || die 'backup mount is not writable'
  SPACE_ROOT="$BACKUP_MOUNT"
else
  SPACE_ROOT="$BACKUP_ROOT"
  while [[ ! -e "$SPACE_ROOT" ]]; do
    SPACE_ROOT="$(dirname -- "$SPACE_ROOT")"
  done
  [[ -d "$SPACE_ROOT" && -w "$SPACE_ROOT" ]] || die 'local backup destination is not writable'
fi

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
AVAILABLE_KIB="$(df -Pk "$SPACE_ROOT" | awk 'NR == 2 {print $4}')"
[[ "$AVAILABLE_KIB" =~ ^[0-9]+$ ]] || die 'could not measure backup free space'
REQUIRED_BYTES=$(( SOURCE_BYTES * 2 + 1073741824 ))
(( AVAILABLE_KIB * 1024 >= REQUIRED_BYTES )) || die 'backup free space is insufficient'

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

SERVICE_STATE="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
case "$SERVICE_STATE" in
  active) was_active=1 ;;
  inactive|failed) ;;
  *) die 'Palworld service state cannot be confirmed; refusing backup' ;;
esac
if (( was_active == 1 )); then
  command -v curl >/dev/null || die 'curl is required to save an active server before backup'
  if [[ "$REST_HOST" == '::1' ]]; then
    REST_URL="http://[$REST_HOST]:$REST_PORT/v1/api"
  else
    REST_URL="http://$REST_HOST:$REST_PORT/v1/api"
  fi
  log 'Requesting a Palworld save through the local REST API before backup.'
  curl --fail --silent --show-error --max-time 15 \
    --user "$REST_USERNAME:$ADMIN_PASSWORD" --request POST "$REST_URL/save" >/dev/null ||
    die 'Palworld save request failed; refusing to stop the server for backup'
  log 'Stopping Palworld for a consistent snapshot.'
  systemctl stop "$SERVICE"
fi

mkdir -p -- "$STAGING_DIR/savegames" "$STAGING_DIR/config" "$STAGING_DIR/metadata"
log "Syncing save data with rsync."
rsync -a --delete "$SAVE_ROOT/" "$STAGING_DIR/savegames/"
log "Syncing server configuration with rsync."
rsync -a --delete "$CONFIG_ROOT/" "$STAGING_DIR/config/"

{
  printf 'created_at=%s\n' "$(date --iso-8601=seconds)"
  printf 'source_save_dir=%s\n' "$SAVE_ROOT"
  printf 'source_config_dir=%s\n' "$CONFIG_ROOT"
  printf 'built_in_backup_dir=%s\n' "${BUILTIN_BACKUP_DIR#"$SAVE_ROOT"/}"
  printf 'retention_count=%s\n' "$BACKUP_RETENTION_COUNT"
} > "$STAGING_DIR/metadata/manifest.txt"

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
