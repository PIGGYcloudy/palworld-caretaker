#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

BASE_DIR='/srv/palworld'
SERVER_DIR="$BASE_DIR/server"
STEAM_APP_ID=2394010
GRACEFUL_STOP_SCRIPT="$BASE_DIR/scripts/graceful-stop-palworld.sh"

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
  "$BASE_DIR/scripts/backup-palworld.sh" --pre-update
fi

printf '[%s] Updating Palworld Dedicated Server with SteamCMD.\n' "$(date --iso-8601=seconds)"
runuser -u palworld -- env HOME="$SERVER_DIR" "$STEAMCMD" \
  +@sSteamCmdForcePlatformType linux \
  +login anonymous \
  +app_info_update 1 \
  +app_info_print "$STEAM_APP_ID" \
  +quit || true
runuser -u palworld -- env HOME="$SERVER_DIR" "$STEAMCMD" \
  +@sSteamCmdForcePlatformType linux \
  +force_install_dir "$SERVER_DIR" \
  +login anonymous \
  +app_update "$STEAM_APP_ID" validate \
  +quit

printf '[%s] Palworld update completed.\n' "$(date --iso-8601=seconds)"
