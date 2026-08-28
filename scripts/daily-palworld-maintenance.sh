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
LOCK_FILE="${PALWORLD_OPERATION_LOCK_FILE:-/run/palworld-caretaker/operation.lock}"
STATE_FILE="$STATE_ROOT/maintenance-state.json"
# This identifies this invocation independently of the second-resolution
# display timestamp.  The Discord bot uses it to avoid confusing a terminal
# state from an earlier maintenance run with this one.
RUN_ID="$(cat /proc/sys/kernel/random/uuid)"

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
  # STATE_ROOT is manager-owned, so a PID-derived name would let that account
  # pre-create or replace a file that this root workflow later chmods.  mktemp
  # creates a fresh, non-predictable inode in the same filesystem for rename.
  if [[ ! -d "$STATE_ROOT" || -L "$STATE_ROOT" ]]; then
    log "ERROR: maintenance state directory is unsafe: $STATE_ROOT"
    return 1
  fi
  local temporary
  if ! temporary="$(mktemp -p "$STATE_ROOT" '.maintenance-state.XXXXXX')"; then
    log 'ERROR: could not create maintenance state file'
    return 1
  fi
  if [[ ! -f "$temporary" || -L "$temporary" ]]; then
    rm -f -- "$temporary"
    log 'ERROR: maintenance state temporary file is unsafe'
    return 1
  fi
  printf '{"run_id":"%s","phase":"%s","message":"%s","updated_at":"%s"}\n' \
    "$RUN_ID" "$phase" "$message" "$(date --iso-8601=seconds)" > "$temporary"
  chmod 0644 "$temporary"
  mv -f -- "$temporary" "$STATE_FILE"
}

(( EUID == 0 )) || die 'run this script with sudo'
[[ -x "$BACKUP_SCRIPT" ]] || die "backup script is missing: $BACKUP_SCRIPT"
[[ -x "$UPDATE_SCRIPT" ]] || die "update script is missing: $UPDATE_SCRIPT"
[[ -x "$GRACEFUL_STOP_SCRIPT" ]] || die "graceful stop script is missing: $GRACEFUL_STOP_SCRIPT"

was_active=0

restore_service_state() {
  local rc=$?
  # This handler also covers failures before the lock is acquired and before
  # preflight starts.  Disable itself and make state reporting best-effort so
  # an unsafe/unavailable state directory cannot obscure the original error.
  trap - EXIT
  set +e
  if (( was_active == 1 )) && ! systemctl is-active --quiet "$SERVICE"; then
    write_state 'restarting' '維護中斷，正在恢復伺服器。'
    log 'Restoring the previously running Palworld service.'
    systemctl start "$SERVICE" || log 'ERROR: Palworld could not be restarted automatically.'
  fi
  # A recovery start is transitional, never the final outcome of a failed
  # maintenance run.  Consumers poll this file and require a terminal state.
  if (( rc != 0 )); then
    write_state 'failed' '維護失敗，請查看伺服器紀錄。'
  fi
  exit "$rc"
}
trap restore_service_state EXIT

[[ -f "$LOCK_FILE" && ! -L "$LOCK_FILE" ]] || die "operation lock is unsafe or missing: $LOCK_FILE"
exec 9<"$LOCK_FILE"
flock -n 9 || die 'another Palworld operation is already running'
export PALWORLD_OPERATION_LOCK_HELD=1
write_state 'starting' '已接受更新要求，正在檢查伺服器狀態。'

if systemctl is-active --quiet "$SERVICE"; then
  was_active=1
fi

write_state 'starting' '正在檢查備份空間與來源資料。'
log 'Validating backup storage and sources before stopping Palworld.'
"$MANAGER" --config-dir "$CONFIG_DIR" --backup-preflight

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
