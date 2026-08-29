#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

CONFIG_DIR=/etc/palworld-caretaker
SERVER_DIR=/srv/palworld
BACKUP_DIR=/srv/palworld-backups
DEFAULT_CONFIG_DIR=/opt/palworld-caretaker/docker/default-config

log() { printf '[docker-entrypoint] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
valid_id() { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 > 0 && $1 <= 2147483647 )); }

initialise_config() {
  install -d -m 0750 "$CONFIG_DIR"
  local name
  for name in caretaker.env server.env secrets.env; do
    if [[ ! -e "$CONFIG_DIR/$name" ]]; then
      # A bind-mounted config directory can already have the requested owner.
      # In that case repair_ownership deliberately avoids a recursive chown,
      # so the newly copied inode must be created for the runtime user here.
      install -o "$PUID" -g "$PGID" -m 0640 "$DEFAULT_CONFIG_DIR/$name" "$CONFIG_DIR/$name"
      log "created $CONFIG_DIR/$name; review it before production use"
    fi
  done
}

repair_ownership() {
  local target owner
  for target in "$SERVER_DIR" "$BACKUP_DIR" "$CONFIG_DIR"; do
    [[ -d "$target" && ! -L "$target" ]] || die "required volume is not a real directory: $target"
    owner="$(stat -c '%u:%g' -- "$target")"
    # A recursive walk is expensive on a large world and must not happen at
    # every boot.  The volume root is the ownership boundary: only a host
    # remap causes a no-follow, same-filesystem repair of that one volume.
    if [[ "$owner" != "$PUID:$PGID" ]]; then
      find -P "$target" -xdev -exec chown -h "$PUID:$PGID" {} +
    fi
  done
  chmod 0750 "$CONFIG_DIR"
  # Also repair files bootstrapped by an older image.  The directory may
  # already be PUID:PGID, which means the volume-root repair above correctly
  # skips its expensive recursive walk.
  chown "$PUID:$PGID" "$CONFIG_DIR/caretaker.env" "$CONFIG_DIR/server.env" "$CONFIG_DIR/secrets.env"
  chmod 0640 "$CONFIG_DIR/caretaker.env" "$CONFIG_DIR/server.env"
  # The non-root supervisor owns this group; 0640 preserves the documented
  # split-layer contract while keeping secrets unreadable to other users.
  chmod 0640 "$CONFIG_DIR/secrets.env"
}

validate_config() {
  python3 -c 'from palworld_caretaker.config import load_config; import sys; load_config(sys.argv[1])' \
    "$CONFIG_DIR" >/dev/null
  if [[ "${PALWORLD_DOCKER_ALLOW_INSECURE_DEFAULTS:-false}" != true ]] \
    && grep -Eq "^.*='?CHANGE_ME_" "$CONFIG_DIR/secrets.env"; then
    die "replace CHANGE_ME values in $CONFIG_DIR/secrets.env (or set PALWORLD_DOCKER_ALLOW_INSECURE_DEFAULTS=true for development)"
  fi
}

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
valid_id "$PUID" || die "PUID must be a positive 32-bit integer"
valid_id "$PGID" || die "PGID must be a positive 32-bit integer"

if (( EUID != 0 )); then
  die "entrypoint must start as root so it can align mounted-volume ownership"
fi

# Numeric ownership is what mounted volumes and gosu enforce.  Duplicate
# numeric IDs can legitimately exist in a base image, so never mutate a
# colliding account/group; map only the dedicated steam identity.
getent group steam >/dev/null || die "container steam group is missing"
id steam >/dev/null 2>&1 || die "container steam user is missing"
if [[ "$(getent group steam | cut -d: -f3)" != "$PGID" ]]; then
  groupmod -o -g "$PGID" steam
fi
if [[ "$(id -u steam)" != "$PUID" || "$(id -g steam)" != "$PGID" ]]; then
  usermod -o -u "$PUID" -g "$PGID" steam
fi

install -d -m 0755 "$SERVER_DIR" "$BACKUP_DIR" "$CONFIG_DIR" /run/palworld-caretaker
# Mark the mode before validation too: persisted v0.7 Docker volumes did not
# contain PALWORLD_WEB_BIND_IP, and their safe container default is 0.0.0.0.
export PALWORLD_CONTAINER_MODE=1
initialise_config
repair_ownership
validate_config

export PALWORLD_CONFIG="$CONFIG_DIR"
export PALWORLD_CONFIG_DIR="$CONFIG_DIR"
export HOME="$SERVER_DIR"

case "${1:-run}" in
  run) shift || true; exec gosu steam:steam python3 /usr/local/bin/docker-supervisor.py "$@" ;;
  restore) shift || true; exec gosu steam:steam python3 /usr/local/bin/docker-supervisor.py --restore "$@" ;;
  shell) shift || true; exec gosu steam:steam /bin/bash "$@" ;;
  *) exec gosu steam:steam "$@" ;;
esac
