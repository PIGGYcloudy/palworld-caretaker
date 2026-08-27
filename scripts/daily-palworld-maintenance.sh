#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SERVICE='palworld.service'
SCRIPT_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${PALWORLD_CONFIG_DIR:-$(dirname -- "$SCRIPT_HOME")/config}"
MANAGER="$SCRIPT_HOME/palworld_manager.py"
[[ -r "$MANAGER" ]] || { printf 'ERROR: configuration manager is missing\n' >&2; exit 1; }
python3 "$MANAGER" --config-dir "$CONFIG_DIR" || exit $?
STATE_ROOT="$(python3 "$MANAGER" --config-dir "$CONFIG_DIR" --get PALWORLD_MANAGER_STATE_DIR)"
BACKUP_SCRIPT="$SCRIPT_HOME/backup-palworld.sh"
UPDATE_SCRIPT="$SCRIPT_HOME/update-palworld.sh"
GRACEFUL_STOP_SCRIPT="$SCRIPT_HOME/graceful-stop-palworld.sh"
LOCK_FILE='/run/lock/palworld-maintenance.lock'
STATE_FILE="$STATE_ROOT/maintenance-state.json"

log() {
  printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

write_state() {
  local phase=$1
  local message=$2
  local temporary="$STATE_FILE.$$"
  printf '{"phase":"%s","message":"%s","updated_at":"%s"}\n' \
    "$phase" "$message" "$(date --iso-8601=seconds)" > "$temporary"
  chmod 0644 "$temporary"
  mv -f -- "$temporary" "$STATE_FILE"
}

(( EUID == 0 )) || die 'run this script with sudo'
[[ -x "$BACKUP_SCRIPT" ]] || die "backup script is missing: $BACKUP_SCRIPT"
[[ -x "$UPDATE_SCRIPT" ]] || die "update script is missing: $UPDATE_SCRIPT"
[[ -x "$GRACEFUL_STOP_SCRIPT" ]] || die "graceful stop script is missing: $GRACEFUL_STOP_SCRIPT"

exec 9>"$LOCK_FILE"
flock -n 9 || die 'another Palworld maintenance operation is already running'
write_state 'starting' '已接受更新要求，正在檢查伺服器狀態。'

was_active=0
if systemctl is-active --quiet "$SERVICE"; then
  was_active=1
fi

restore_service_state() {
  local rc=$?
  if (( rc != 0 )); then
    write_state 'failed' '維護失敗，請查看伺服器紀錄。'
  fi
  if (( was_active == 1 )) && ! systemctl is-active --quiet "$SERVICE"; then
    write_state 'restarting' '維護中斷，正在恢復伺服器。'
    log 'Restoring the previously running Palworld service.'
    systemctl start "$SERVICE" || log 'ERROR: Palworld could not be restarted automatically.'
  fi
  exit "$rc"
}
trap restore_service_state EXIT

if (( was_active == 1 )); then
  write_state 'stopping' '正在存檔並正常關閉伺服器。'
  log 'Palworld was running; stopping it for backup and update.'
  "$GRACEFUL_STOP_SCRIPT"
else
  write_state 'backup' '伺服器原本關閉，正在建立備份。'
  log 'Palworld was already stopped; it will remain stopped after maintenance.'
fi

write_state 'backup' '正在建立 NAS 備份。'
log 'Creating NAS backup.'
"$BACKUP_SCRIPT"

write_state 'updating' '正在透過 SteamCMD 驗證並更新遊戲檔案。'
log 'Updating Palworld Dedicated Server with SteamCMD.'
"$UPDATE_SCRIPT"

if (( was_active == 1 )); then
  write_state 'restarting' '更新完成，正在重新啟動伺服器。'
  log 'Starting Palworld after maintenance.'
  systemctl start "$SERVICE"
fi

trap - EXIT
write_state 'completed' '備份與更新已完成。'
log 'Daily Palworld maintenance completed.'
