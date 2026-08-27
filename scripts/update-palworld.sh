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
SERVER_DIR="$(config_value PALWORLD_SERVER_ROOT)"
SERVICE_USER="$(config_value PALWORLD_SERVICE_USER)"
STEAM_APP_ID=2394010
GRACEFUL_STOP_SCRIPT="$SCRIPT_HOME/graceful-stop-palworld.sh"
LOCK_FILE="${PALWORLD_OPERATION_LOCK_FILE:-/run/palworld-caretaker/operation.lock}"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

(( EUID == 0 )) || die 'run this script with sudo'
STEAMCMD="$(command -v steamcmd || true)"
if [[ -z "$STEAMCMD" && -x /usr/games/steamcmd ]]; then
  STEAMCMD='/usr/games/steamcmd'
fi
[[ -n "$STEAMCMD" && -x "$STEAMCMD" ]] || die 'steamcmd is not installed'
[[ -x "$SERVER_DIR/PalServer.sh" ]] || die 'PalServer.sh is missing'
[[ -x "$GRACEFUL_STOP_SCRIPT" ]] || die 'graceful stop script is missing'

# tmpfiles pre-creates this root-owned lock in a manager-non-writable runtime
# directory.  A read-only descriptor is sufficient for flock and cannot
# truncate or create a replacement inode.
if [[ "${PALWORLD_OPERATION_LOCK_HELD:-}" != 1 ]]; then
  [[ -f "$LOCK_FILE" && ! -L "$LOCK_FILE" ]] || die "operation lock is unsafe or missing: $LOCK_FILE"
  exec 9<"$LOCK_FILE"
  flock -n 9 || die 'another Palworld operation is already running'
  export PALWORLD_OPERATION_LOCK_HELD=1
fi

was_active=0
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
  printf '[%s] Saving and gracefully stopping Palworld before update.\n' "$(date --iso-8601=seconds)"
  "$GRACEFUL_STOP_SCRIPT"
  printf '[%s] Creating a safety backup before update.\n' "$(date --iso-8601=seconds)"
  "$SCRIPT_HOME/backup-palworld.sh" --pre-update
fi

printf '[%s] Updating Palworld Dedicated Server with SteamCMD.\n' "$(date --iso-8601=seconds)"
runuser -u "$SERVICE_USER" -- env HOME="$SERVER_DIR" "$STEAMCMD" \
  +@sSteamCmdForcePlatformType linux \
  +login anonymous \
  +app_info_update 1 \
  +app_info_print "$STEAM_APP_ID" \
  +quit || true
runuser -u "$SERVICE_USER" -- env HOME="$SERVER_DIR" "$STEAMCMD" \
  +@sSteamCmdForcePlatformType linux \
  +force_install_dir "$SERVER_DIR" \
  +login anonymous \
  +app_update "$STEAM_APP_ID" validate \
  +quit

printf '[%s] Palworld update completed.\n' "$(date --iso-8601=seconds)"
