#!/usr/bin/env bash
# Upgrade an existing /srv/palworld installation without SteamCMD, save, or NAS changes.
set -Eeuo pipefail
IFS=$'\n\t'

(( EUID == 0 )) || { printf 'Run with sudo: sudo bash %s\n' "$0" >&2; exit 1; }

STAGING_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CONFIG="$STAGING_DIR/config/palworld.env.example"
BASE_DIR='/srv/palworld'
CONFIG_FILE="$BASE_DIR/config/palworld.env"
SETTINGS_FILE="$BASE_DIR/server/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
SCRIPT_DIR="$BASE_DIR/scripts"
STATE_DIR='/var/lib/palworld-manager'
VENV_DIR="$BASE_DIR/venv"

log() { printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

[[ -f "$CONFIG_FILE" ]] || die 'existing palworld.env was not found'
[[ -f "$SETTINGS_FILE" ]] || die 'existing PalWorldSettings.ini was not found'
[[ -x "$BASE_DIR/server/PalServer.sh" ]] || die 'existing PalServer.sh was not found'
command -v systemctl >/dev/null || die 'systemctl is required'
command -v visudo >/dev/null || die 'visudo is required'
command -v iptables >/dev/null || die 'iptables is required'
python3 -m venv --help >/dev/null || die 'python3-venv is required'

stamp="$(date +%Y%m%d-%H%M%S)"
backup="$BASE_DIR/backups-local/manager-upgrade-$stamp"
install -d -o root -g root -m 0700 "$backup"
install -o root -g root -m 0600 "$CONFIG_FILE" "$backup/palworld.env"
install -o root -g root -m 0600 "$SETTINGS_FILE" "$backup/PalWorldSettings.ini"
for path in /etc/systemd/system/palworld.service \
  /etc/systemd/system/palworld-idle-watcher.service \
  /etc/systemd/system/palworld-discord-bot.service \
  /etc/systemd/system/palworld-rest-firewall.service \
  /etc/sudoers.d/palworld-manager; do
  [[ -f "$path" ]] && cp -a -- "$path" "$backup/"
done
log "Configuration backup created at $backup"

if ! id -u palworld-manager >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin palworld-manager
fi
install -d -o palworld-manager -g palworld-manager -m 0750 "$STATE_DIR"
install -d -o root -g root -m 0755 "$SCRIPT_DIR"

install -o root -g root -m 0755 "$STAGING_DIR/scripts/render-settings.sh" "$SCRIPT_DIR/render-settings.sh"
install -o root -g root -m 0644 "$STAGING_DIR/scripts/palworld_manager.py" "$SCRIPT_DIR/palworld_manager.py"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-idle-watcher.py" "$SCRIPT_DIR/palworld-idle-watcher.py"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-discord-bot.py" "$SCRIPT_DIR/palworld-discord-bot.py"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-rest-firewall" "$SCRIPT_DIR/palworld-rest-firewall"
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-control" /usr/local/sbin/palworld-control
install -o root -g root -m 0755 "$STAGING_DIR/scripts/palworld-discord-configure" /usr/local/sbin/palworld-discord-configure

header_written=0
while IFS= read -r default_line; do
  [[ "$default_line" =~ ^([A-Z][A-Z0-9_]*)= ]] || continue
  key="${BASH_REMATCH[1]}"
  if ! grep -q "^${key}=" "$CONFIG_FILE"; then
    if (( header_written == 0 )); then
      printf '\n# Added by Palworld idle watcher / Discord Bot upgrade.\n' >> "$CONFIG_FILE"
      header_written=1
    fi
    printf '%s\n' "$default_line" >> "$CONFIG_FILE"
  fi
done < "$DEFAULT_CONFIG"
chown root:palworld-manager "$CONFIG_FILE"
chmod 0640 "$CONFIG_FILE"
chown root:palworld-manager "$BASE_DIR/config"
chmod 0750 "$BASE_DIR/config"

install -o root -g root -m 0644 "$STAGING_DIR/units/palworld.service" /etc/systemd/system/palworld.service
install -o root -g root -m 0644 "$STAGING_DIR/units/palworld-idle-watcher.service" /etc/systemd/system/palworld-idle-watcher.service
install -o root -g root -m 0644 "$STAGING_DIR/units/palworld-discord-bot.service" /etc/systemd/system/palworld-discord-bot.service
install -o root -g root -m 0644 "$STAGING_DIR/units/palworld-rest-firewall.service" /etc/systemd/system/palworld-rest-firewall.service
install -o root -g root -m 0440 "$STAGING_DIR/config/palworld-manager.sudoers" /etc/sudoers.d/palworld-manager
visudo -cf /etc/sudoers.d/palworld-manager >/dev/null

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --disable-pip-version-check --requirement "$STAGING_DIR/requirements.txt"

"$SCRIPT_DIR/render-settings.sh"
systemctl daemon-reload
systemctl enable palworld-rest-firewall.service palworld-idle-watcher.service >/dev/null

# REST configuration and MemoryMax take effect only after a controlled restart.
log 'Restarting Palworld once to enable localhost REST API and MemoryMax=48G.'
systemctl restart palworld.service
systemctl enable --now palworld-idle-watcher.service >/dev/null

if grep -q '^DISCORD_BOT_TOKEN=' "$CONFIG_FILE" &&
   ! grep -q "^DISCORD_BOT_TOKEN='\?CHANGE_ME" "$CONFIG_FILE"; then
  systemctl enable --now palworld-discord-bot.service >/dev/null
  log 'Discord Bot enabled and started.'
else
  systemctl disable --now palworld-discord-bot.service >/dev/null 2>&1 || true
  log 'Discord token is still a placeholder; Bot installed but intentionally not started.'
fi

log 'Manager upgrade complete. Idle watcher remains in dry-run until explicitly changed.'
