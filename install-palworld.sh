#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

(( EUID == 0 )) || {
  printf 'Run this installer with sudo, for example: sudo bash %s\n' "$0" >&2
  exit 1
}

STAGING_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CONFIG="$STAGING_DIR/config/palworld.env.example"
BASE_DIR='/srv/palworld'
SERVER_DIR="$BASE_DIR/server"
CONFIG_DIR="$BASE_DIR/config"
SCRIPT_DIR="$BASE_DIR/scripts"
LOCAL_BACKUP_DIR="$BASE_DIR/backups-local"
MANAGER_STATE_DIR='/var/lib/palworld-manager'
VENV_DIR="$BASE_DIR/venv"
NAS_MOUNT='/mnt/qnap-tyt'
NAS_BACKUP_DIR="$NAS_MOUNT/palworld-backups"
SETTINGS_FILE="$SERVER_DIR/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
STEAM_APP_ID=2394010
if [[ -f "$SETTINGS_FILE" ]]; then
  HAD_EXISTING_SETTINGS=1
else
  HAD_EXISTING_SETTINGS=0
fi

log() {
  printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

log 'Enabling the i386 architecture required by Ubuntu SteamCMD.'
if ! dpkg --print-foreign-architectures | grep -qx i386; then
  dpkg --add-architecture i386
fi
log 'Installing SteamCMD package.'
apt-get update
# Keep debconf interactive so the operator can explicitly accept Valve's
# Steam License Agreement instead of silently accepting it here.
apt-get install -y steamcmd:i386 python3-venv iptables

STEAMCMD="$(command -v steamcmd || true)"
if [[ -z "$STEAMCMD" && -x /usr/games/steamcmd ]]; then
  STEAMCMD='/usr/games/steamcmd'
fi
[[ -x "$STEAMCMD" ]] || die 'steamcmd executable was not found after installation'

if ! id -u palworld >/dev/null 2>&1; then
  log 'Creating system user palworld.'
  useradd --system --home-dir "$BASE_DIR" --shell /usr/sbin/nologin palworld
fi
if ! id -u palworld-manager >/dev/null 2>&1; then
  log 'Creating restricted Palworld manager user.'
  useradd --system --home-dir "$MANAGER_STATE_DIR" --shell /usr/sbin/nologin palworld-manager
fi

install -d -o root -g root -m 0755 "$BASE_DIR"
install -d -o palworld -g palworld -m 0750 "$SERVER_DIR"
install -d -o root -g root -m 0755 "$SCRIPT_DIR"
install -d -o root -g palworld-manager -m 0750 "$CONFIG_DIR"
install -d -o root -g root -m 0700 "$LOCAL_BACKUP_DIR"
install -d -o palworld-manager -g palworld-manager -m 0750 "$MANAGER_STATE_DIR"

UPGRADE_STAMP="$(date +%Y%m%d-%H%M%S)"
UPGRADE_BACKUP_DIR="$LOCAL_BACKUP_DIR/config-upgrade-$UPGRADE_STAMP"
install -d -o root -g root -m 0700 "$UPGRADE_BACKUP_DIR"
for existing_file in \
  /etc/systemd/system/palworld.service \
  /etc/systemd/system/palworld-backup.service \
  /etc/systemd/system/palworld-backup.timer \
  /etc/systemd/system/palworld-maintenance.service \
  /etc/systemd/system/palworld-idle-watcher.service \
  /etc/systemd/system/palworld-discord-bot.service \
  /etc/systemd/system/palworld-rest-firewall.service \
  /etc/sudoers.d/palworld-manager; do
  if [[ -f "$existing_file" ]]; then
    cp -a -- "$existing_file" "$UPGRADE_BACKUP_DIR/"
  fi
done

install -o root -g root -m 0755 "$STAGING_DIR/scripts/render-settings.sh" "$SCRIPT_DIR/render-settings.sh"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/backup-palworld.sh" "$SCRIPT_DIR/backup-palworld.sh"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/restore-palworld.sh" "$SCRIPT_DIR/restore-palworld.sh"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/update-palworld.sh" "$SCRIPT_DIR/update-palworld.sh"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/daily-palworld-maintenance.sh" "$SCRIPT_DIR/daily-palworld-maintenance.sh"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/graceful-stop-palworld.sh" "$SCRIPT_DIR/graceful-stop-palworld.sh"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-control" /usr/local/sbin/palworld-control
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-discord-configure" /usr/local/sbin/palworld-discord-configure
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-rest-firewall" "$SCRIPT_DIR/palworld-rest-firewall"
install -o root -g root -m 0644 "$STAGING_DIR/scripts/palworld_manager.py" "$SCRIPT_DIR/palworld_manager.py"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-idle-watcher.py" "$SCRIPT_DIR/palworld-idle-watcher.py"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-discord-bot.py" "$SCRIPT_DIR/palworld-discord-bot.py"
if [[ -f "$CONFIG_DIR/palworld.env" ]]; then
  config_backup="$CONFIG_DIR/palworld.env.pre-idle-discord-$(date +%Y%m%d-%H%M%S)"
  install -o root -g root -m 0640 "$CONFIG_DIR/palworld.env" "$config_backup"
  log "Preserved existing configuration and created $(basename "$config_backup")."
  upgrade_header_written=0
  while IFS= read -r default_line; do
    [[ "$default_line" =~ ^([A-Z][A-Z0-9_]*)= ]] || continue
    default_key="${BASH_REMATCH[1]}"
    if ! grep -q "^${default_key}=" "$CONFIG_DIR/palworld.env"; then
      if (( upgrade_header_written == 0 )); then
        printf '\n# Added by idle watcher / Discord Bot upgrade.\n' >> "$CONFIG_DIR/palworld.env"
        upgrade_header_written=1
      fi
      printf '%s\n' "$default_line" >> "$CONFIG_DIR/palworld.env"
    fi
done < "$DEFAULT_CONFIG"
else
  install -o root -g palworld-manager -m 0640 "$DEFAULT_CONFIG" "$CONFIG_DIR/palworld.env"
fi
chown root:palworld-manager "$CONFIG_DIR/palworld.env"
chmod 0640 "$CONFIG_DIR/palworld.env"
chown root:palworld-manager "$CONFIG_DIR"
chmod 0750 "$CONFIG_DIR"
install -o root -g root -m 0644 "$STAGING_DIR/README.md" "$BASE_DIR/README.md"
install -o root -g root -m 0644 "$STAGING_DIR/units/palworld.service" /etc/systemd/system/palworld.service
install -o root -g root -m 0644 "$STAGING_DIR/units/palworld-backup.service" /etc/systemd/system/palworld-backup.service
install -o root -g root -m 0644 "$STAGING_DIR/units/palworld-backup.timer" /etc/systemd/system/palworld-backup.timer
install -o root -g root -m 0644 "$STAGING_DIR/units/palworld-maintenance.service" /etc/systemd/system/palworld-maintenance.service
install -o root -g root -m 0644 "$STAGING_DIR/units/palworld-idle-watcher.service" /etc/systemd/system/palworld-idle-watcher.service
install -o root -g root -m 0644 "$STAGING_DIR/units/palworld-discord-bot.service" /etc/systemd/system/palworld-discord-bot.service
install -o root -g root -m 0644 "$STAGING_DIR/units/palworld-rest-firewall.service" /etc/systemd/system/palworld-rest-firewall.service
install -o root -g root -m 0440 "$STAGING_DIR/config/palworld-manager.sudoers" /etc/sudoers.d/palworld-manager
visudo -cf /etc/sudoers.d/palworld-manager

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --disable-pip-version-check --requirement "$STAGING_DIR/requirements.txt"

refresh_steam_metadata() {
  log 'Refreshing Steam app metadata as palworld.'
  runuser -u palworld -- env HOME="$SERVER_DIR" "$STEAMCMD" \
    +@sSteamCmdForcePlatformType linux \
    +login anonymous \
    +app_info_update 1 \
    +app_info_print "$STEAM_APP_ID" \
    +quit || log 'Steam metadata refresh returned a non-zero status; continuing to app update.'
}

install_palworld_app() {
  log 'Downloading/updating Palworld Dedicated Server as palworld.'
  runuser -u palworld -- env HOME="$SERVER_DIR" "$STEAMCMD" \
    +@sSteamCmdForcePlatformType linux \
    +force_install_dir "$SERVER_DIR" \
    +login anonymous \
    +app_update "$STEAM_APP_ID" validate \
    +quit
}

refresh_steam_metadata
install_palworld_app || log 'Initial Steam app update returned a non-zero status.'

if [[ ! -x "$SERVER_DIR/PalServer.sh" ]]; then
  log 'PalServer.sh is still missing; clearing only Palworld SteamCMD metadata cache and retrying.'
  CACHE_DIR="$SERVER_DIR/.local/share/Steam/appcache"
  [[ "$CACHE_DIR" == "$SERVER_DIR/.local/share/Steam/appcache" ]] || die 'Steam cache safety check failed'
  rm -f -- "$CACHE_DIR/appinfo.vdf" "$CACHE_DIR/packageinfo.vdf"
  refresh_steam_metadata
  install_palworld_app
fi

[[ -x "$SERVER_DIR/PalServer.sh" ]] || die 'PalServer.sh was not installed'

systemctl daemon-reload
systemctl enable palworld.service
systemctl enable palworld-rest-firewall.service palworld-idle-watcher.service

log 'Starting Palworld once to create its generated configuration directories.'
if ! systemctl start palworld.service; then
  journalctl -u palworld.service -n 80 --no-pager >&2 || true
  die 'Palworld service failed to start during initial setup'
fi
for _ in $(seq 1 120); do
  [[ -f "$SETTINGS_FILE" ]] && break
  sleep 1
done
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
  install -o palworld -g palworld -m 0640 "$SERVER_DIR/DefaultPalWorldSettings.ini" "$SETTINGS_FILE"
fi
"$SCRIPT_DIR/render-settings.sh"

if ! mountpoint -q "$NAS_MOUNT"; then
  die "NAS is not mounted; refusing to create $NAS_BACKUP_DIR"
fi
[[ -w "$NAS_MOUNT" ]] || die 'NAS mount is not writable'
[[ "$NAS_BACKUP_DIR" == "$NAS_MOUNT/palworld-backups" ]] || die 'NAS backup path safety check failed'
mkdir -p -- "$NAS_BACKUP_DIR"

systemctl enable --now palworld.service
systemctl enable --now palworld-idle-watcher.service
systemctl enable --now palworld-backup.timer

if grep -q '^DISCORD_BOT_TOKEN=' "$CONFIG_DIR/palworld.env" &&
   ! grep -q "^DISCORD_BOT_TOKEN='\?CHANGE_ME" "$CONFIG_DIR/palworld.env"; then
  systemctl enable --now palworld-discord-bot.service
else
  log 'Discord token is not configured; bot service was installed but not started.'
fi

log 'Palworld installation and service setup completed.'
log 'The configured passwords are placeholders and were not displayed.'
