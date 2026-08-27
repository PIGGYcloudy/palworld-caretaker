#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

log() { printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

(( EUID == 0 )) || die "run with sudo: sudo bash $0 --config-dir DIRECTORY --level LEVEL"

REPOSITORY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MANAGER="$REPOSITORY_DIR/scripts/palworld_manager.py"
CONFIG_DIR="${PALWORLD_CONFIG_DIR:-}"
LEVEL=''
CONFIRM=''
while (( $# > 0 )); do
  case "$1" in
    --config-dir) [[ $# -ge 2 ]] || die '--config-dir requires a directory'; CONFIG_DIR="$2"; shift 2 ;;
    --level) [[ $# -ge 2 ]] || die '--level requires manager, game, or all'; LEVEL="$2"; shift 2 ;;
    --confirm) [[ $# -ge 2 ]] || die '--confirm requires a value'; CONFIRM="$2"; shift 2 ;;
    *) die "usage: $0 --config-dir DIRECTORY --level manager|game|all [--confirm 'DELETE PALWORLD DATA']" ;;
  esac
done

[[ -n "$CONFIG_DIR" ]] || die '--config-dir is required (or set PALWORLD_CONFIG_DIR)'
[[ "$LEVEL" == manager || "$LEVEL" == game || "$LEVEL" == all ]] ||
  die '--level must be manager, game, or all'
[[ -r "$MANAGER" ]] || die "configuration manager is missing: $MANAGER"
config_value() { python3 "$MANAGER" --config-dir "$CONFIG_DIR" --get "$1"; }

INSTALL_ROOT="$(config_value PALWORLD_INSTALL_ROOT)" || die 'configuration validation failed; nothing was removed'
EXPECTED_CONFIG="$(config_value PALWORLD_CONFIG_ROOT)" || die 'configuration validation failed; nothing was removed'
SERVER_ROOT="$(config_value PALWORLD_SERVER_ROOT)" || die 'configuration validation failed; nothing was removed'
SCRIPTS_ROOT="$(config_value PALWORLD_SCRIPTS_ROOT)" || die 'configuration validation failed; nothing was removed'
LOCAL_BACKUPS="$(config_value PALWORLD_LOCAL_BACKUP_ROOT)" || die 'configuration validation failed; nothing was removed'
EXTERNAL_BACKUPS="$(config_value PALWORLD_BACKUP_DIR)" || die 'configuration validation failed; nothing was removed'
STATE_DIR="$(config_value PALWORLD_MANAGER_STATE_DIR)" || die 'configuration validation failed; nothing was removed'
SYSTEMD_UNIT_DIR="${PALWORLD_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${PALWORLD_SUDOERS_DIR:-/etc/sudoers.d}"
LOCAL_SBIN_DIR="${PALWORLD_LOCAL_SBIN_DIR:-/usr/local/sbin}"

[[ "$(realpath -m -- "$CONFIG_DIR")" == "$(realpath -m -- "$EXPECTED_CONFIG")" ]] ||
  die "configuration must be loaded from the deployed directory: $EXPECTED_CONFIG"
[[ "$INSTALL_ROOT" != / && "$SERVER_ROOT" == "$INSTALL_ROOT/server" &&
   "$SCRIPTS_ROOT" == "$INSTALL_ROOT/scripts" && "$LOCAL_BACKUPS" == "$INSTALL_ROOT/backups-local" ]] ||
  die 'derived path safety check failed'
for protected_path in "$INSTALL_ROOT" "$SERVER_ROOT" "$SCRIPTS_ROOT" "$STATE_DIR"; do
  [[ ! -L "$protected_path" ]] || die "refusing to remove through symbolic link: $protected_path"
done
if [[ "$LEVEL" == all && "$CONFIRM" != 'DELETE PALWORLD DATA' ]]; then
  die "full removal deletes configuration and saved worlds; repeat with --confirm 'DELETE PALWORLD DATA'"
fi

units=(
  palworld-discord-bot.service palworld-idle-watcher.service
  palworld-backup.timer palworld-maintenance.service palworld-backup.service
  palworld.service palworld-rest-firewall.service
)
if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now "${units[@]}" >/dev/null 2>&1 || true
fi
if [[ -x "$SCRIPTS_ROOT/palworld-rest-firewall" ]]; then
  PALWORLD_CONFIG_DIR="$CONFIG_DIR" "$SCRIPTS_ROOT/palworld-rest-firewall" stop >/dev/null 2>&1 || true
fi
for unit in "${units[@]}"; do rm -f -- "$SYSTEMD_UNIT_DIR/$unit"; done
rm -f -- "$SUDOERS_DIR/palworld-manager" \
  "$LOCAL_SBIN_DIR/palworld-control" "$LOCAL_SBIN_DIR/palworld-discord-configure"
rm -rf -- "$SCRIPTS_ROOT" "$INSTALL_ROOT/venv" "$STATE_DIR"

if [[ "$LEVEL" == game || "$LEVEL" == all ]]; then
  if [[ -d "$SERVER_ROOT" ]]; then
    find "$SERVER_ROOT" -mindepth 1 -maxdepth 1 ! -name Pal -exec rm -rf -- {} +
    if [[ -d "$SERVER_ROOT/Pal" ]]; then
      find "$SERVER_ROOT/Pal" -mindepth 1 -maxdepth 1 ! -name Saved -exec rm -rf -- {} +
    fi
  fi
fi

if [[ "$LEVEL" == all ]]; then
  rm -rf -- "$SERVER_ROOT/Pal/Saved" "$EXPECTED_CONFIG"
  find "$SERVER_ROOT" -depth -type d -empty -delete 2>/dev/null || true
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
log "Uninstall level '$LEVEL' completed."
log "External backups were preserved at $EXTERNAL_BACKUPS"
log "Local safety backups were preserved at $LOCAL_BACKUPS"
if [[ "$LEVEL" != all ]]; then
  log "Configuration was preserved at $EXPECTED_CONFIG"
fi
if [[ "$LEVEL" == manager ]]; then
  log "Game files and saved worlds were preserved at $SERVER_ROOT"
elif [[ "$LEVEL" == game ]]; then
  log "Saved worlds were preserved at $SERVER_ROOT/Pal/Saved"
fi
