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
SERVICE_USER="$(config_value PALWORLD_SERVICE_USER)"
MANAGER_USER="$(config_value PALWORLD_MANAGER_USER)"
VENV_DIR="$BASE_DIR/venv"
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
install -d -o root -g "$MANAGER_USER" -m 0750 "$CONFIG_DIR"
install -d -o root -g root -m 0700 "$LOCAL_BACKUP_DIR"
install -d -o "$MANAGER_USER" -g "$MANAGER_USER" -m 0750 "$MANAGER_STATE_DIR"

copied_config=0
for config_name in palworld.env caretaker.env server.env secrets.env; do
  source_file="$CONFIG_SOURCE_DIR/$config_name"
  [[ -f "$source_file" ]] || continue
  destination="$CONFIG_DIR/$config_name"
  mode=0640; [[ "$config_name" == secrets.env ]] && mode=0600
  if [[ ! "$source_file" -ef "$destination" ]]; then
    install -o root -g "$MANAGER_USER" -m "$mode" "$source_file" "$destination"
  else
    chown root:"$MANAGER_USER" "$destination"; chmod "$mode" "$destination"
  fi
  copied_config=1
done
(( copied_config == 1 )) || die "no deployment configuration files found in $CONFIG_SOURCE_DIR"

for executable in render-settings.sh backup-palworld.sh restore-palworld.sh update-palworld.sh \
  daily-palworld-maintenance.sh graceful-stop-palworld.sh palworld-rest-firewall \
  palworld-idle-watcher.py palworld-discord-bot.py diagnose-palworld.sh; do
  install -o root -g root -m 0755 "$STAGING_DIR/scripts/$executable" "$SCRIPT_DIR/$executable"
done
install -o root -g root -m 0644 "$MANAGER" "$SCRIPT_DIR/palworld_manager.py"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-control" /usr/local/sbin/palworld-control
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-discord-configure" /usr/local/sbin/palworld-discord-configure
install -o root -g root -m 0755 "$STAGING_DIR/uninstall-palworld.sh" "$BASE_DIR/uninstall-palworld.sh"
install -o root -g root -m 0644 "$STAGING_DIR/README.md" "$BASE_DIR/README.md"

# Now verify permissions, mount safety, and destination writability before
# installing systemd definitions or changing live server data.
python3 "$SCRIPT_DIR/palworld_manager.py" --config-dir "$CONFIG_DIR" ||
  die 'filesystem preflight failed; systemd was not modified'

UPGRADE_STAMP="$(date +%Y%m%d-%H%M%S)"
UPGRADE_BACKUP_DIR="$LOCAL_BACKUP_DIR/config-upgrade-$UPGRADE_STAMP"
install -d -o root -g root -m 0700 "$UPGRADE_BACKUP_DIR"
for existing_file in /etc/systemd/system/palworld*.service /etc/systemd/system/palworld*.timer \
  /etc/sudoers.d/palworld-manager; do
  [[ -f "$existing_file" ]] && cp -a -- "$existing_file" "$UPGRADE_BACKUP_DIR/"
done

RENDER_DIR="$(mktemp -d)"
cleanup_render() { rm -rf -- "$RENDER_DIR"; }
trap cleanup_render EXIT
python3 "$MANAGER" --config-dir "$CONFIG_SOURCE_DIR" --render-units "$STAGING_DIR/units" "$RENDER_DIR"
for unit_file in "$RENDER_DIR"/*; do
  install -o root -g root -m 0644 "$unit_file" "/etc/systemd/system/$(basename -- "$unit_file")"
done
sed "s/@MANAGER_USER@/$MANAGER_USER/g" "$STAGING_DIR/config/palworld-manager.sudoers" > "$RENDER_DIR/palworld-manager.sudoers"
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
systemctl enable palworld.service palworld-rest-firewall.service palworld-idle-watcher.service
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
systemctl enable --now palworld.service palworld-idle-watcher.service palworld-backup.timer
DISCORD_TOKEN="$(config_value DISCORD_BOT_TOKEN)"
if [[ -n "$DISCORD_TOKEN" && "$DISCORD_TOKEN" != CHANGE_ME* ]]; then
  systemctl enable --now palworld-discord-bot.service
else
  log 'Discord token is not configured; bot service was installed but not started.'
fi

trap - EXIT; cleanup_render
log 'Palworld installation and service setup completed.'
log 'Configured secrets were not displayed.'
