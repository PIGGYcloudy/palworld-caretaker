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
SAFE_STATE_SETUP="$STAGING_DIR/scripts/safe-manager-state.py"
[[ -r "$SAFE_STATE_SETUP" ]] || die "safe manager state helper is missing: $SAFE_STATE_SETUP"
EDITABLE_MIGRATION="$STAGING_DIR/scripts/migrate-editable-config.py"
[[ -r "$EDITABLE_MIGRATION" ]] || die "editable configuration migration helper is missing: $EDITABLE_MIGRATION"

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
SETTINGS_BACKUP_DIR="$(config_value PALWORLD_SETTINGS_BACKUP_DIR)"
MANAGER_USER="$(config_value PALWORLD_MANAGER_USER)"
VENV_DIR="$INSTALL_ROOT/venv"
PACKAGE_ROOT="$INSTALL_ROOT/packages"
TMPFILES_DIR="${PALWORLD_TMPFILES_DIR:-/etc/tmpfiles.d}"
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
  [[ -f "$path" ]] && install -o root -g root -m 0644 "$path" "$backup/$(basename -- "$path")"
done
log "Configuration and manager backup created at $backup"

if ! id -u "$MANAGER_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$MANAGER_USER"
fi
python3 "$SAFE_STATE_SETUP" "$STATE_DIR" --manager-user "$MANAGER_USER" \
  --directory-mode 0750 --file audit.log --file-mode 0640 || die 'manager state setup failed'
python3 "$SAFE_STATE_SETUP" "$SETTINGS_BACKUP_DIR" --manager-user "$MANAGER_USER" \
  --directory-mode 0700 || die 'settings backup state setup failed'
install -d -o root -g root -m 0755 "$SCRIPTS_ROOT"
install -d -o root -g root -m 0755 "$PACKAGE_ROOT"
install -d -o root -g "$MANAGER_USER" -m 0750 "$DEPLOYED_CONFIG"
EDITABLE_CONFIG_DIR="$DEPLOYED_CONFIG/editable"
install -d -o "$MANAGER_USER" -g "$MANAGER_USER" -m 0750 "$EDITABLE_CONFIG_DIR"
# Move only the non-secret layers below the manager-owned child directory.
# The root remains non-writable to this account, which protects secrets.env
# from replacement or deletion while still allowing atomic editor writes.
for config_name in caretaker.env server.env; do
  source_file="$DEPLOYED_CONFIG/$config_name"
  destination="$EDITABLE_CONFIG_DIR/$config_name"
  if [[ -f "$source_file" ]]; then
    # Old deployments mixed operational fields with UI settings.  Preserve
    # protected fields in the root-owned base layer and move only keys from
    # SETTING_SPECS into the manager-writable child.
    python3 "$EDITABLE_MIGRATION" --manager "$MANAGER" --manager-user "$MANAGER_USER" \
      "$source_file" "$destination" || die "could not migrate $config_name into the editable layer"
  elif [[ -f "$destination" ]]; then
    python3 "$SAFE_STATE_SETUP" "$EDITABLE_CONFIG_DIR" --manager-user "$MANAGER_USER" \
      --directory-mode 0750 --file "$config_name" --file-mode 0640 || die "editable $config_name setup failed"
  fi
done
if [[ -f "$DEPLOYED_CONFIG/palworld.env" ]]; then
  chown root:"$MANAGER_USER" "$DEPLOYED_CONFIG/palworld.env"
  chmod 0640 "$DEPLOYED_CONFIG/palworld.env"
fi
if [[ -f "$DEPLOYED_CONFIG/secrets.env" ]]; then
  chown root:"$MANAGER_USER" "$DEPLOYED_CONFIG/secrets.env"
  chmod 0640 "$DEPLOYED_CONFIG/secrets.env"
fi

executables=(
  render-settings.sh backup-palworld.sh restore-palworld.sh update-palworld.sh
  daily-palworld-maintenance.sh graceful-stop-palworld.sh palworld-rest-firewall
  palworld-idle-watcher.py palworld-discord-bot.py palworld-web-ui.py diagnose-palworld.sh
)
for executable in "${executables[@]}"; do
  install -o root -g root -m 0755 "$STAGING_DIR/scripts/$executable" "$SCRIPTS_ROOT/$executable"
done
install -o root -g root -m 0644 "$MANAGER" "$SCRIPTS_ROOT/palworld_manager.py"
deploy_python_package() {
  [[ -d "$STAGING_DIR/src/palworld_caretaker" ]] || return 0
  local staging release current_tmp script_tmp
  staging="$(mktemp -d "$PACKAGE_ROOT/.release.XXXXXX")"
  install -d -o root -g root -m 0755 "$staging/palworld_caretaker"
  cp -R --no-preserve=mode,ownership -- "$STAGING_DIR/src/palworld_caretaker/." "$staging/palworld_caretaker/"
  find "$staging" -type d -name __pycache__ -prune -exec rm -rf -- {} +
  find "$staging" -type f -name '*.pyc' -delete
  chown -R root:root "$staging"
  find "$staging" -type d -exec chmod 0755 {} +
  find "$staging" -type f -exec chmod 0644 {} +
  release="$PACKAGE_ROOT/release-$(date +%Y%m%d-%H%M%S)-$$"
  mv -T -- "$staging" "$release"
  current_tmp="$PACKAGE_ROOT/.current.$$"
  ln -s -- "$(basename -- "$release")" "$current_tmp"
  mv -Tf -- "$current_tmp" "$PACKAGE_ROOT/current"
  script_tmp="$SCRIPTS_ROOT/.palworld_caretaker.$$"
  ln -s -- "$PACKAGE_ROOT/current/palworld_caretaker" "$script_tmp"
  if [[ -e "$SCRIPTS_ROOT/palworld_caretaker" && ! -L "$SCRIPTS_ROOT/palworld_caretaker" ]]; then
    mv -- "$SCRIPTS_ROOT/palworld_caretaker" "$PACKAGE_ROOT/legacy-package-$(date +%Y%m%d-%H%M%S)-$$"
  fi
  mv -Tf -- "$script_tmp" "$SCRIPTS_ROOT/palworld_caretaker"
}
deploy_python_package
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-control" "$LOCAL_SBIN_DIR/palworld-control"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-discord-configure" "$LOCAL_SBIN_DIR/palworld-discord-configure"
install -o root -g root -m 0755 "$STAGING_DIR/uninstall-palworld.sh" "$INSTALL_ROOT/uninstall-palworld.sh"
sed "s/@MANAGER_USER@/$MANAGER_USER/g" "$STAGING_DIR/config/palworld-caretaker.tmpfiles.conf" > "$PACKAGE_ROOT/.tmpfiles.$$"
install -o root -g root -m 0644 "$PACKAGE_ROOT/.tmpfiles.$$" "$TMPFILES_DIR/palworld-caretaker.conf"
rm -f -- "$PACKAGE_ROOT/.tmpfiles.$$"
if [[ "$TMPFILES_DIR" == /etc/tmpfiles.d ]]; then
  systemd-tmpfiles --create /etc/tmpfiles.d/palworld-caretaker.conf
fi

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
sed_replacement() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }
SUDOERS_GRACEFUL_STOP="$(sed_replacement "${SCRIPTS_ROOT// /\\ }/graceful-stop-palworld.sh")"
SUDOERS_RESTORE="$(sed_replacement "${SCRIPTS_ROOT// /\\ }/restore-palworld.sh")"
sed -e "s|@MANAGER_USER@|$(sed_replacement "$MANAGER_USER")|g" -e "s|@GRACEFUL_STOP_SCRIPT@|$SUDOERS_GRACEFUL_STOP|g" -e "s|@RESTORE_SCRIPT@|$SUDOERS_RESTORE|g" "$STAGING_DIR/config/palworld-manager.sudoers" \
  > "$render_dir/palworld-manager.sudoers"
install -o root -g root -m 0440 "$render_dir/palworld-manager.sudoers" "$SUDOERS_DIR/palworld-manager"
visudo -cf "$SUDOERS_DIR/palworld-manager" >/dev/null

[[ -x "$VENV_DIR/bin/python" ]] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --disable-pip-version-check --requirement "$STAGING_DIR/requirements.txt"

PALWORLD_CONFIG_DIR="$DEPLOYED_CONFIG" "$SCRIPTS_ROOT/render-settings.sh"
was_active=false
systemctl is-active --quiet palworld.service && was_active=true
systemctl daemon-reload
systemctl enable palworld-rest-firewall.service palworld-idle-watcher.service palworld-backup.timer palworld-web-ui.service >/dev/null
if [[ "$was_active" == true ]]; then
  log 'Restarting the running game service to apply the rendered service and REST settings.'
  systemctl restart palworld.service
fi
systemctl enable --now palworld-idle-watcher.service >/dev/null
systemctl enable --now palworld-web-ui.service >/dev/null

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
