#!/usr/bin/env bash
# Upgrade caretaker components without downloading Steam content or changing backups.
set -Eeuo pipefail
IFS=$'\n\t'

log() { printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

(( EUID == 0 )) || die "run with sudo: sudo bash $0 --config-dir DIRECTORY"

STAGING_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MANAGER="$STAGING_DIR/scripts/palworld_manager.py"
CONFIG_DIR="${PALWORLD_CONFIG_DIR:-}"
if (( $# > 0 )); then
  [[ $# -eq 2 && "$1" == --config-dir ]] || die "usage: $0 --config-dir DIRECTORY"
  CONFIG_DIR="$2"
fi
[[ -n "$CONFIG_DIR" ]] || die '--config-dir is required (or set PALWORLD_CONFIG_DIR)'
[[ -r "$MANAGER" ]] || die "configuration manager is missing: $MANAGER"

# Validate the complete layered value contract before creating backups, users,
# directories, or systemd state.
python3 "$MANAGER" --config-dir "$CONFIG_DIR" --no-filesystem ||
  die 'configuration validation failed; no system changes were made'
config_value() { python3 "$MANAGER" --config-dir "$CONFIG_DIR" --get "$1"; }

INSTALL_ROOT="$(config_value PALWORLD_INSTALL_ROOT)"
SERVER_ROOT="$(config_value PALWORLD_SERVER_ROOT)"
DEPLOYED_CONFIG="$(config_value PALWORLD_CONFIG_ROOT)"
SCRIPTS_ROOT="$(config_value PALWORLD_SCRIPTS_ROOT)"
LOCAL_BACKUPS="$(config_value PALWORLD_LOCAL_BACKUP_ROOT)"
STATE_DIR="$(config_value PALWORLD_MANAGER_STATE_DIR)"
MANAGER_USER="$(config_value PALWORLD_MANAGER_USER)"
VENV_DIR="$INSTALL_ROOT/venv"
SETTINGS_FILE="$SERVER_ROOT/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
SYSTEMD_UNIT_DIR="${PALWORLD_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${PALWORLD_SUDOERS_DIR:-/etc/sudoers.d}"
LOCAL_SBIN_DIR="${PALWORLD_LOCAL_SBIN_DIR:-/usr/local/sbin}"

[[ "$(realpath -m -- "$CONFIG_DIR")" == "$(realpath -m -- "$DEPLOYED_CONFIG")" ]] ||
  die "upgrade must use the deployed configuration directory: $DEPLOYED_CONFIG"
[[ "$INSTALL_ROOT" != / && "$SERVER_ROOT" == "$INSTALL_ROOT/server" &&
   "$SCRIPTS_ROOT" == "$INSTALL_ROOT/scripts" && "$LOCAL_BACKUPS" == "$INSTALL_ROOT/backups-local" ]] ||
  die 'derived path safety check failed'
[[ ! -L "$INSTALL_ROOT" && ! -L "$SERVER_ROOT" && ! -L "$DEPLOYED_CONFIG" ]] ||
  die 'installation, server, and configuration roots must not be symbolic links'
[[ -f "$SETTINGS_FILE" ]] || die "existing PalWorldSettings.ini was not found: $SETTINGS_FILE"
[[ -x "$SERVER_ROOT/PalServer.sh" ]] || die "existing PalServer.sh was not found: $SERVER_ROOT/PalServer.sh"
command -v systemctl >/dev/null || die 'systemctl is required'
command -v visudo >/dev/null || die 'visudo is required'
python3 -m venv --help >/dev/null || die 'python3-venv is required'

stamp="$(date +%Y%m%d-%H%M%S)"
backup="$LOCAL_BACKUPS/manager-upgrade-$stamp"
install -d -o root -g root -m 0700 "$backup"
for config_name in palworld.env caretaker.env server.env secrets.env; do
  [[ -f "$DEPLOYED_CONFIG/$config_name" ]] &&
    install -o root -g root -m 0600 "$DEPLOYED_CONFIG/$config_name" "$backup/$config_name"
done
install -o root -g root -m 0600 "$SETTINGS_FILE" "$backup/PalWorldSettings.ini"
for path in "$SYSTEMD_UNIT_DIR"/palworld*.service "$SYSTEMD_UNIT_DIR"/palworld*.timer \
  "$SUDOERS_DIR/palworld-manager"; do
  [[ -f "$path" ]] && cp -a -- "$path" "$backup/"
done
log "Configuration and manager backup created at $backup"

if ! id -u "$MANAGER_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$MANAGER_USER"
fi
install -d -o "$MANAGER_USER" -g "$MANAGER_USER" -m 0750 "$STATE_DIR"
install -d -o root -g root -m 0755 "$SCRIPTS_ROOT"
install -d -o root -g "$MANAGER_USER" -m 0750 "$DEPLOYED_CONFIG"
for config_name in palworld.env caretaker.env server.env; do
  if [[ -f "$DEPLOYED_CONFIG/$config_name" ]]; then
    chown root:"$MANAGER_USER" "$DEPLOYED_CONFIG/$config_name"
    chmod 0640 "$DEPLOYED_CONFIG/$config_name"
  fi
done
if [[ -f "$DEPLOYED_CONFIG/secrets.env" ]]; then
  chown root:"$MANAGER_USER" "$DEPLOYED_CONFIG/secrets.env"
  chmod 0600 "$DEPLOYED_CONFIG/secrets.env"
fi

executables=(
  render-settings.sh backup-palworld.sh restore-palworld.sh update-palworld.sh
  daily-palworld-maintenance.sh graceful-stop-palworld.sh palworld-rest-firewall
  palworld-idle-watcher.py palworld-discord-bot.py diagnose-palworld.sh
)
for executable in "${executables[@]}"; do
  install -o root -g root -m 0755 "$STAGING_DIR/scripts/$executable" "$SCRIPTS_ROOT/$executable"
done
install -o root -g root -m 0644 "$MANAGER" "$SCRIPTS_ROOT/palworld_manager.py"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-control" "$LOCAL_SBIN_DIR/palworld-control"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-discord-configure" "$LOCAL_SBIN_DIR/palworld-discord-configure"
install -o root -g root -m 0755 "$STAGING_DIR/uninstall-palworld.sh" "$INSTALL_ROOT/uninstall-palworld.sh"

# The existing filesystem must satisfy the same contract as a fresh install
# before any live systemd definition is replaced.
python3 "$SCRIPTS_ROOT/palworld_manager.py" --config-dir "$DEPLOYED_CONFIG" ||
  die "filesystem preflight failed; restore the manager backup at $backup if needed"

render_dir="$(mktemp -d)"
cleanup_render() { rm -rf -- "$render_dir"; }
trap cleanup_render EXIT
python3 "$MANAGER" --config-dir "$CONFIG_DIR" --render-units "$STAGING_DIR/units" "$render_dir"
for unit_file in "$render_dir"/*; do
  install -o root -g root -m 0644 "$unit_file" "$SYSTEMD_UNIT_DIR/$(basename -- "$unit_file")"
done
sed "s/@MANAGER_USER@/$MANAGER_USER/g" "$STAGING_DIR/config/palworld-manager.sudoers" \
  > "$render_dir/palworld-manager.sudoers"
install -o root -g root -m 0440 "$render_dir/palworld-manager.sudoers" "$SUDOERS_DIR/palworld-manager"
visudo -cf "$SUDOERS_DIR/palworld-manager" >/dev/null

[[ -x "$VENV_DIR/bin/python" ]] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --disable-pip-version-check --requirement "$STAGING_DIR/requirements.txt"

PALWORLD_CONFIG_DIR="$DEPLOYED_CONFIG" "$SCRIPTS_ROOT/render-settings.sh"
was_active=false
systemctl is-active --quiet palworld.service && was_active=true
systemctl daemon-reload
systemctl enable palworld-rest-firewall.service palworld-idle-watcher.service palworld-backup.timer >/dev/null
if [[ "$was_active" == true ]]; then
  log 'Restarting the running game service to apply the rendered service and REST settings.'
  systemctl restart palworld.service
fi
systemctl enable --now palworld-idle-watcher.service >/dev/null

discord_token="$(config_value DISCORD_BOT_TOKEN)"
if [[ -n "$discord_token" && "$discord_token" != CHANGE_ME* ]]; then
  systemctl enable --now palworld-discord-bot.service >/dev/null
  log 'Discord Bot enabled and started.'
else
  systemctl disable --now palworld-discord-bot.service >/dev/null 2>&1 || true
  log 'Discord token is empty or a placeholder; Bot was not started.'
fi

trap - EXIT
cleanup_render
log 'Caretaker upgrade complete; configuration, saved worlds, and backup destinations were preserved.'
