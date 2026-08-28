#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

log() { printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

(( EUID == 0 )) || die "run this installer with sudo: sudo bash $0 --config-dir /path/to/config"

STAGING_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MANAGER="$STAGING_DIR/scripts/palworld_manager.py"
CONFIG_SOURCE_DIR="${PALWORLD_CONFIG_DIR:-$STAGING_DIR/config}"
if [[ $# -gt 0 ]]; then
  [[ $# -eq 2 && "$1" == --config-dir ]] || die "usage: $0 [--config-dir DIRECTORY]"
  CONFIG_SOURCE_DIR="$2"
fi
[[ -r "$MANAGER" ]] || die "configuration manager is missing: $MANAGER"
SAFE_STATE_SETUP="$STAGING_DIR/scripts/safe-manager-state.py"
[[ -r "$SAFE_STATE_SETUP" ]] || die "safe manager state helper is missing: $SAFE_STATE_SETUP"
EDITABLE_MIGRATION="$STAGING_DIR/scripts/migrate-editable-config.py"
[[ -r "$EDITABLE_MIGRATION" ]] || die "editable configuration migration helper is missing: $EDITABLE_MIGRATION"

# This must remain before dpkg, apt, useradd, install, mkdir, or writes to /etc.
# A fresh deployment has no target directories, so this gate validates values.
log 'Validating deployment configuration before making system changes.'
python3 "$MANAGER" --config-dir "$CONFIG_SOURCE_DIR" --no-filesystem ||
  die 'configuration preflight failed; no system changes were made'

config_value() { python3 "$MANAGER" --config-dir "$CONFIG_SOURCE_DIR" --get "$1"; }
BASE_DIR="$(config_value PALWORLD_INSTALL_ROOT)"
SERVER_DIR="$(config_value PALWORLD_SERVER_ROOT)"
CONFIG_DIR="$(config_value PALWORLD_CONFIG_ROOT)"
SCRIPT_DIR="$(config_value PALWORLD_SCRIPTS_ROOT)"
LOCAL_BACKUP_DIR="$(config_value PALWORLD_LOCAL_BACKUP_ROOT)"
BACKUP_DIR="$(config_value PALWORLD_BACKUP_DIR)"
BACKUP_MOUNT="$(config_value PALWORLD_BACKUP_MOUNT)"
BACKUP_REQUIRE_MOUNT="$(config_value PALWORLD_BACKUP_REQUIRE_MOUNT)"
MANAGER_STATE_DIR="$(config_value PALWORLD_MANAGER_STATE_DIR)"
SETTINGS_BACKUP_DIR="$(config_value PALWORLD_SETTINGS_BACKUP_DIR)"
SERVICE_USER="$(config_value PALWORLD_SERVICE_USER)"
MANAGER_USER="$(config_value PALWORLD_MANAGER_USER)"
VENV_DIR="$BASE_DIR/venv"
PACKAGE_ROOT="$BASE_DIR/packages"
TMPFILES_DIR="${PALWORLD_TMPFILES_DIR:-/etc/tmpfiles.d}"
SETTINGS_FILE="$SERVER_DIR/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
STEAM_APP_ID=2394010
[[ -f "$SETTINGS_FILE" ]] && HAD_EXISTING_SETTINGS=1 || HAD_EXISTING_SETTINGS=0

log 'Enabling the i386 architecture required by Ubuntu SteamCMD.'
if ! dpkg --print-foreign-architectures | grep -qx i386; then dpkg --add-architecture i386; fi
log 'Installing SteamCMD package.'
apt-get update
# Keep debconf interactive so the operator explicitly accepts Valve's license.
apt-get install -y steamcmd:i386 python3-venv iptables

STEAMCMD="$(command -v steamcmd || true)"
[[ -n "$STEAMCMD" ]] || STEAMCMD='/usr/games/steamcmd'
[[ -x "$STEAMCMD" ]] || die 'steamcmd executable was not found after installation'

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  log "Creating system user $SERVICE_USER."
  useradd --system --home-dir "$BASE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
if ! id -u "$MANAGER_USER" >/dev/null 2>&1; then
  log "Creating restricted manager user $MANAGER_USER."
  useradd --system --home-dir "$MANAGER_STATE_DIR" --shell /usr/sbin/nologin "$MANAGER_USER"
fi

install -d -o root -g root -m 0755 "$BASE_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$SERVER_DIR"
install -d -o root -g root -m 0755 "$SCRIPT_DIR"
install -d -o root -g root -m 0755 "$PACKAGE_ROOT"
install -d -o root -g "$MANAGER_USER" -m 0750 "$CONFIG_DIR"
EDITABLE_CONFIG_DIR="$CONFIG_DIR/editable"
install -d -o "$MANAGER_USER" -g "$MANAGER_USER" -m 0750 "$EDITABLE_CONFIG_DIR"
install -d -o root -g root -m 0700 "$LOCAL_BACKUP_DIR"
# The manager owns these directories.  Validate and repair their actual
# inodes through O_NOFOLLOW descriptors; never chown/chmod a path supplied by
# that account after a shell-level existence check.
python3 "$SAFE_STATE_SETUP" "$MANAGER_STATE_DIR" --manager-user "$MANAGER_USER" \
  --directory-mode 0750 --file audit.log --file-mode 0640 || die 'manager state setup failed'
python3 "$SAFE_STATE_SETUP" "$SETTINGS_BACKUP_DIR" --manager-user "$MANAGER_USER" \
  --directory-mode 0700 || die 'settings backup state setup failed'

copied_config=0
for config_name in palworld.env secrets.env; do
  source_file="$CONFIG_SOURCE_DIR/$config_name"
  [[ -f "$source_file" ]] || continue
  destination="$CONFIG_DIR/$config_name"
  mode=0640
  if [[ ! "$source_file" -ef "$destination" ]]; then
    install -o root -g "$MANAGER_USER" -m "$mode" "$source_file" "$destination"
  else
    chown root:"$MANAGER_USER" "$destination"
    chmod "$mode" "$destination"
  fi
  copied_config=1
done
for config_name in caretaker.env server.env; do
  source_file="$CONFIG_SOURCE_DIR/$config_name"
  [[ -f "$source_file" ]] || continue
  python3 "$EDITABLE_MIGRATION" --manager "$MANAGER" --manager-user "$MANAGER_USER" \
    --protected-destination "$CONFIG_DIR/$config_name" \
    "$source_file" "$EDITABLE_CONFIG_DIR/$config_name" || die "could not migrate $config_name into the editable layer"
  copied_config=1
done
(( copied_config == 1 )) || die "no deployment configuration files found in $CONFIG_SOURCE_DIR"

for executable in render-settings.sh backup-palworld.sh restore-palworld.sh update-palworld.sh \
  daily-palworld-maintenance.sh graceful-stop-palworld.sh palworld-rest-firewall \
  palworld-idle-watcher.py palworld-discord-bot.py palworld-web-ui.py diagnose-palworld.sh; do
  install -o root -g root -m 0755 "$STAGING_DIR/scripts/$executable" "$SCRIPT_DIR/$executable"
done
install -o root -g root -m 0644 "$MANAGER" "$SCRIPT_DIR/palworld_manager.py"
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
  script_tmp="$SCRIPT_DIR/.palworld_caretaker.$$"
  ln -s -- "$PACKAGE_ROOT/current/palworld_caretaker" "$script_tmp"
  if [[ -e "$SCRIPT_DIR/palworld_caretaker" && ! -L "$SCRIPT_DIR/palworld_caretaker" ]]; then
    mv -- "$SCRIPT_DIR/palworld_caretaker" "$PACKAGE_ROOT/legacy-package-$(date +%Y%m%d-%H%M%S)-$$"
  fi
  mv -Tf -- "$script_tmp" "$SCRIPT_DIR/palworld_caretaker"
}
deploy_python_package
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-control" /usr/local/sbin/palworld-control
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-discord-configure" /usr/local/sbin/palworld-discord-configure
install -o root -g root -m 0755 "$STAGING_DIR/uninstall-palworld.sh" "$BASE_DIR/uninstall-palworld.sh"
install -o root -g root -m 0644 "$STAGING_DIR/README.md" "$BASE_DIR/README.md"
sed "s/@MANAGER_USER@/$MANAGER_USER/g" "$STAGING_DIR/config/palworld-caretaker.tmpfiles.conf" > "$PACKAGE_ROOT/.tmpfiles.$$"
install -o root -g root -m 0644 "$PACKAGE_ROOT/.tmpfiles.$$" "$TMPFILES_DIR/palworld-caretaker.conf"
rm -f -- "$PACKAGE_ROOT/.tmpfiles.$$"
if [[ "$TMPFILES_DIR" == /etc/tmpfiles.d ]]; then
  systemd-tmpfiles --create /etc/tmpfiles.d/palworld-caretaker.conf
fi

# Now verify permissions, mount safety, and destination writability before
# installing systemd definitions or changing live server data.
python3 "$SCRIPT_DIR/palworld_manager.py" --config-dir "$CONFIG_DIR" ||
  die 'filesystem preflight failed; systemd was not modified'

UPGRADE_STAMP="$(date +%Y%m%d-%H%M%S)"
UPGRADE_BACKUP_DIR="$LOCAL_BACKUP_DIR/config-upgrade-$UPGRADE_STAMP"
install -d -o root -g root -m 0700 "$UPGRADE_BACKUP_DIR"
for existing_file in /etc/systemd/system/palworld*.service /etc/systemd/system/palworld*.timer \
  /etc/sudoers.d/palworld-manager; do
  [[ -f "$existing_file" ]] && install -o root -g root -m 0644 "$existing_file" "$UPGRADE_BACKUP_DIR/$(basename -- "$existing_file")"
done

RENDER_DIR="$(mktemp -d)"
cleanup_render() { rm -rf -- "$RENDER_DIR"; }
trap cleanup_render EXIT
python3 "$MANAGER" --config-dir "$CONFIG_SOURCE_DIR" --render-units "$STAGING_DIR/units" "$RENDER_DIR"
for unit_file in "$RENDER_DIR"/*; do
  install -o root -g root -m 0644 "$unit_file" "/etc/systemd/system/$(basename -- "$unit_file")"
done
sed_replacement() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }
SUDOERS_GRACEFUL_STOP="$(sed_replacement "${SCRIPT_DIR// /\\ }/graceful-stop-palworld.sh")"
SUDOERS_RESTORE="$(sed_replacement "${SCRIPT_DIR// /\\ }/restore-palworld.sh")"
sed -e "s|@MANAGER_USER@|$(sed_replacement "$MANAGER_USER")|g" -e "s|@GRACEFUL_STOP_SCRIPT@|$SUDOERS_GRACEFUL_STOP|g" -e "s|@RESTORE_SCRIPT@|$SUDOERS_RESTORE|g" \
  "$STAGING_DIR/config/palworld-manager.sudoers" > "$RENDER_DIR/palworld-manager.sudoers"
install -o root -g root -m 0440 "$RENDER_DIR/palworld-manager.sudoers" /etc/sudoers.d/palworld-manager
visudo -cf /etc/sudoers.d/palworld-manager

[[ -x "$VENV_DIR/bin/python" ]] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --disable-pip-version-check --requirement "$STAGING_DIR/requirements.txt"

refresh_steam_metadata() {
  log "Refreshing Steam app metadata as $SERVICE_USER."
  runuser -u "$SERVICE_USER" -- env HOME="$SERVER_DIR" "$STEAMCMD" \
    +@sSteamCmdForcePlatformType linux +login anonymous +app_info_update 1 \
    +app_info_print "$STEAM_APP_ID" +quit ||
    log 'Steam metadata refresh returned non-zero; continuing to app update.'
}
install_palworld_app() {
  log 'Downloading/updating Palworld Dedicated Server.'
  runuser -u "$SERVICE_USER" -- env HOME="$SERVER_DIR" "$STEAMCMD" \
    +@sSteamCmdForcePlatformType linux +force_install_dir "$SERVER_DIR" \
    +login anonymous +app_update "$STEAM_APP_ID" validate +quit
}

refresh_steam_metadata
install_palworld_app || log 'Initial Steam app update returned a non-zero status.'
if [[ ! -x "$SERVER_DIR/PalServer.sh" ]]; then
  log 'PalServer.sh is missing; clearing only Palworld SteamCMD metadata and retrying.'
  CACHE_DIR="$SERVER_DIR/.local/share/Steam/appcache"
  [[ "$CACHE_DIR" == "$SERVER_DIR/.local/share/Steam/appcache" ]] || die 'Steam cache safety check failed'
  rm -f -- "$CACHE_DIR/appinfo.vdf" "$CACHE_DIR/packageinfo.vdf"
  refresh_steam_metadata; install_palworld_app
fi
[[ -x "$SERVER_DIR/PalServer.sh" ]] || die 'PalServer.sh was not installed'

systemctl daemon-reload
systemctl enable palworld.service palworld-rest-firewall.service palworld-idle-watcher.service palworld-web-ui.service
log 'Starting Palworld once to create its generated configuration directories.'
if ! systemctl start palworld.service; then
  journalctl -u palworld.service -n 80 --no-pager >&2 || true
  die 'Palworld service failed to start during initial setup'
fi
for _ in $(seq 1 120); do [[ -f "$SETTINGS_FILE" ]] && break; sleep 1; done
if [[ ! -f "$SETTINGS_FILE" ]]; then
  journalctl -u palworld.service -n 80 --no-pager >&2 || true
  systemctl stop palworld.service || true
  die 'PalWorldSettings.ini was not generated within 120 seconds'
fi
systemctl stop palworld.service

if (( HAD_EXISTING_SETTINGS == 1 )); then
  install -o root -g root -m 0600 "$SETTINGS_FILE" "$UPGRADE_BACKUP_DIR/PalWorldSettings.ini"
  log 'Preserving existing PalWorldSettings.ini and enabling REST API in place.'
else
  log 'Copying the official default settings for a new installation.'
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0640 \
    "$SERVER_DIR/DefaultPalWorldSettings.ini" "$SETTINGS_FILE"
fi
PALWORLD_CONFIG_DIR="$CONFIG_DIR" "$SCRIPT_DIR/render-settings.sh"

if [[ "$BACKUP_REQUIRE_MOUNT" == true ]]; then
  mountpoint -q "$BACKUP_MOUNT" || die "backup filesystem is not mounted: $BACKUP_MOUNT"
fi
mkdir -p -- "$BACKUP_DIR"
systemctl enable --now palworld.service palworld-idle-watcher.service palworld-backup.timer palworld-web-ui.service
DISCORD_TOKEN="$(config_value DISCORD_BOT_TOKEN)"
if [[ -n "$DISCORD_TOKEN" && "$DISCORD_TOKEN" != CHANGE_ME* ]]; then
  systemctl enable --now palworld-discord-bot.service
else
  log 'Discord token is not configured; bot service was installed but not started.'
fi

trap - EXIT; cleanup_render
log 'Palworld installation and service setup completed.'
log 'Configured secrets were not displayed.'
