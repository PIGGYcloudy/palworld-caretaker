#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

BASE_DIR='/srv/palworld'
SERVICE='palworld.service'
ENV_FILE="$BASE_DIR/config/palworld.env"

log() {
  printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

(( EUID == 0 )) || die 'run this script with sudo'
if ! systemctl is-active --quiet "$SERVICE"; then
  log 'Palworld is already stopped.'
  exit 0
fi

[[ -r "$ENV_FILE" ]] || die 'configuration file is missing'
# This root-owned configuration is also used by the existing backup tooling.
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required}"
REST_HOST="${PALWORLD_REST_API_HOST:-127.0.0.1}"
REST_PORT="${PALWORLD_REST_API_PORT:-8212}"
REST_USERNAME="${PALWORLD_REST_API_USERNAME:-admin}"
SHUTDOWN_WAIT="${PALWORLD_SHUTDOWN_WAIT_SECONDS:-30}"

[[ "$REST_HOST" == '127.0.0.1' || "$REST_HOST" == 'localhost' || "$REST_HOST" == '::1' ]] || die 'REST API host must be localhost'
if [[ ! "$REST_PORT" =~ ^[1-9][0-9]*$ ]] || (( REST_PORT > 65535 )); then
  die 'REST API port is invalid'
fi
if [[ ! "$SHUTDOWN_WAIT" =~ ^[1-9][0-9]*$ ]] || (( SHUTDOWN_WAIT > 300 )); then
  die 'shutdown wait is invalid'
fi
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
