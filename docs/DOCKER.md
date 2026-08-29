# Docker / Compose

Palworld Caretaker v0.6.0 runs the dedicated server, caretaker Web UI, optional
Discord bot, SteamCMD update, and graceful shutdown in one container. Docker
is not a systemd deployment: the container supervisor owns its child
processes and sends the Palworld REST `save` and `shutdown` commands on
`SIGTERM`/`SIGINT` before falling back to `SIGINT` for the game process.

## Quick start

1. Create the persistent directories and first configuration files:

   ```bash
   mkdir -p data/server data/backups config
   docker compose run --rm palworld-caretaker true
   ```

   The first normal start also creates `config/caretaker.env`, `server.env`,
   and `secrets.env`; it then stops deliberately until placeholders are
   replaced. If you prefer to inspect them without starting the image, copy
   the files in `docker/default-config/`.

2. Edit `config/secrets.env`. Set strong `SERVER_PASSWORD`, `ADMIN_PASSWORD`,
   and `PALWORLD_WEB_UI_PASSWORD` values. The Web UI username is set by
   `PALWORLD_WEB_UI_USERNAME` in `config/caretaker.env`.

3. Start it:

   ```bash
   docker compose up -d
   docker compose logs -f palworld-caretaker
   ```

   The initial SteamCMD install is large and can take several minutes.
   Open `http://localhost:8765` or `http://<server-LAN-IP>:8765` and use the
   Web UI credentials. The default `0.0.0.0` listener also supports a private
   Hamachi, Tailscale, or ZeroTier address. By default the UI validates each
   browser Origin (or supplied Referer) against its request Host, so direct
   LAN IP, DNS, and private VPN access work without a fixed
   `PALWORLD_WEB_PUBLIC_ORIGIN`. Set that variable to an exact origin (for
   example `https://pal.example.net`) when using a reverse proxy.

## Persistent data and ports

| Host path | Container path | Purpose |
| --- | --- | --- |
| `./data/server` | `/srv/palworld` | Palworld installation, world saves, and caretaker state |
| `./data/backups` | `/srv/palworld-backups` | External, versioned caretaker snapshots |
| `./config` | `/etc/palworld-caretaker` | `caretaker.env`, `server.env`, and `secrets.env` |

`8211/udp` is the game port. `25575/tcp` is Palworld REST and is bound to
loopback by default (`PALWORLD_REST_BIND_IP` changes that only when explicitly
needed). `8765/tcp` is the authenticated Web UI and is bound to `0.0.0.0` by
default so a host administrator can use it from the LAN. It uses HTTP Basic
authentication over plaintext HTTP: never expose it directly to the Internet.
The dynamic Origin/Host check is for trusted LAN/VPN paths, not an Internet
access-control boundary.
For public remote access, keep
`PALWORLD_WEB_BIND_IP=127.0.0.1` and terminate TLS in a trusted reverse proxy
on the same host. Set `PALWORLD_WEB_PUBLIC_ORIGIN=https://pal.example.net` to
the exact HTTPS origin served by that proxy. Only set `PALWORLD_WEB_BIND_IP`
to a non-loopback address when an upstream network boundary provides TLS and
access control. When the public-origin variable is unset, the proxy must
preserve the public `Host` header for dynamic same-origin validation.

The entrypoint runs as root only long enough to align those volume inode
owners with `PUID`/`PGID`, then uses the non-root `steam` account. It only
repairs a volume recursively when that volume's top-level owner changed, so
normal restarts do not scan or chown every world file. Do not mount a symlink
at any of the three container paths. Compose also enables
`no-new-privileges:true`; the image removes unneeded set-id bits.

The Web UI and Discord bot use a private Unix socket to ask the container PID
1 supervisor to start, stop, restart, back up, restore, or update the game.
They do not invoke `sudo` or `systemctl` in container mode. The supervisor owns
the PalServer process group, and its `operation_lock` serializes those
operations with scheduled backup and idle shutdown. On `SIGTERM`/`SIGINT` it
drains an active operation, saves through REST before graceful shutdown, and
only then falls back to process-group signals; it never removes the live save
or config trees. A crashed child is reaped into a restartable failed state.

## Configuration precedence

Configuration is parsed as data, never shell-sourced. Defaults are followed
by `caretaker.env`, then `server.env`, optional `editable/*.env`, and finally
`secrets.env`. A later layer wins. Docker environment variables deliberately
control only the container runtime (`PUID`, `PGID`, update behavior, browser
origin, and development placeholder opt-in); they do not silently overwrite
server or secret settings. This keeps a mounted configuration reproducible.

`STEAMCMD_UPDATE_ON_START=true` updates app 2394010 before launch. Set it to
`false` to restart an already installed server without checking Steam.
`PALWORLD_DISCORD_ENABLED=false` leaves a configured Discord bot disabled.

## Backup and restoration

Backups live exclusively in `./data/backups`; never place that directory below
`./data/server`. Use the Web UI to create and restore snapshots. For an
offline restore, stop the container first, then run the repository restore
tool in a one-off image command after selecting a snapshot:

```bash
docker compose stop palworld-caretaker
docker compose run --rm palworld-caretaker restore palworld-YYYYMMDD-HHMMSS
docker compose up -d
```

Restoration takes a safety copy first and refuses unsafe snapshot paths. Keep
the container stopped for the full restore; never copy live SaveGames by hand.

## Shutdown and updates

Use `docker compose stop`, not `docker kill`. Compose grants three minutes so
the supervisor can request REST save/shutdown and allow Palworld's configured
shutdown timer to complete. `docker compose up -d --build` updates the
caretaker image; game files and backups remain in their mapped volumes.
