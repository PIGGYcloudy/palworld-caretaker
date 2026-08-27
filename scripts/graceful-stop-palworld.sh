#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SERVICE='palworld.service'
SCRIPT_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${PALWORLD_CONFIG_DIR:-$(dirname -- "$SCRIPT_HOME")/config}"
MANAGER="$SCRIPT_HOME/palworld_manager.py"
[[ -r "$MANAGER" ]] || { printf 'ERROR: configuration manager is missing\n' >&2; exit 1; }
python3 "$MANAGER" --config-dir "$CONFIG_DIR" || exit $?
config_value() { python3 "$MANAGER" --config-dir "$CONFIG_DIR" --get "$1"; }
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
  maintenance_state="$(systemctl is-active palworld-maintenance.service 2>/dev/null || true)"
  case "$maintenance_state" in
    inactive|failed) ;;
    *) die 'maintenance is active or its state cannot be confirmed' ;;
  esac
fi
if ! systemctl is-active --quiet "$SERVICE"; then
  log 'Palworld is already stopped.'
  exit 0
fi

ADMIN_PASSWORD="$(config_value ADMIN_PASSWORD)"
REST_HOST="$(config_value PALWORLD_REST_API_HOST)"
REST_PORT="$(config_value PALWORLD_REST_API_PORT)"
REST_USERNAME="$(config_value PALWORLD_REST_API_USERNAME)"
SHUTDOWN_WAIT="$(config_value PALWORLD_SHUTDOWN_WAIT_SECONDS)"

command -v curl >/dev/null || die 'curl is required'

if [[ "$REST_HOST" == '::1' ]]; then
  REST_URL="http://[$REST_HOST]:$REST_PORT/v1/api"
else
  REST_URL="http://$REST_HOST:$REST_PORT/v1/api"
fi

log 'Requesting a Palworld save through the local REST API.'
curl --fail --silent --show-error --max-time 15 \
  --user "$REST_USERNAME:$ADMIN_PASSWORD" \
  --request POST "$REST_URL/save" >/dev/null || die 'Palworld save request failed; refusing to stop the server'

log 'Requesting a graceful Palworld shutdown through the local REST API.'
curl --fail --silent --show-error --max-time 15 \
  --user "$REST_USERNAME:$ADMIN_PASSWORD" \
  --header 'Content-Type: application/json' \
  --data "{\"waittime\":$SHUTDOWN_WAIT,\"message\":\"Server maintenance is starting.\"}" \
  --request POST "$REST_URL/shutdown" >/dev/null || die 'Palworld shutdown request failed; refusing to force-stop the server'

# Give the game its requested shutdown period plus a buffer.  Deliberately do
# not call `systemctl stop`: that would turn a slow graceful shutdown into a
# SIGKILL at TimeoutStopSec and risks losing unsaved world state.
deadline=$((SECONDS + SHUTDOWN_WAIT + 180))
while systemctl is-active --quiet "$SERVICE"; do
  (( SECONDS < deadline )) || die 'Palworld did not exit after the graceful shutdown request'
  sleep 2
done

log 'Palworld stopped gracefully.'
