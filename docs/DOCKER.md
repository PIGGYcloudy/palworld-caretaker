# Docker / Compose

Palworld Caretaker v0.9.0 runs the dedicated server, caretaker Web UI, optional
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
   Open `http://localhost:8765` and use the Web UI credentials. Docker keeps
   the process bound to container `0.0.0.0`, but Compose publishes it only to
   host loopback by default. For private LAN, Hamachi, Tailscale, or ZeroTier
   access, set both `PALWORLD_WEB_PUBLISH_IP=0.0.0.0` (or one host LAN IP) and
   an exact `PALWORLD_WEB_ALLOWED_ORIGINS`, for example
   `http://192.168.1.20:8765`. A TLS reverse proxy instead uses exact
   `PALWORLD_WEB_PUBLIC_ORIGIN=https://pal.example.net`.

## Persistent data and ports

| Host path | Container path | Purpose |
| --- | --- | --- |
| `./data/server` | `/srv/palworld` | Palworld installation, world saves, and caretaker state |
| `./data/backups` | `/srv/palworld-backups` | External, versioned caretaker snapshots |
| `./config` | `/etc/palworld-caretaker` | `caretaker.env`, `server.env`, and `secrets.env` |

`8211/udp` is the game port. `25575/tcp` is Palworld REST and is bound to
loopback by default (`PALWORLD_REST_BIND_IP` changes that only when explicitly
needed). `PALWORLD_WEB_BIND_IP` is the **container internal** listener and is
`0.0.0.0` in Docker's `caretaker.env`; that value lets Docker forward the host
port into the container. v0.7 persisted volumes that omit it receive the same
container-mode default on upgrade. In contrast,
`PALWORLD_WEB_PUBLISH_IP` controls the **host-side Compose publication** and
defaults to `127.0.0.1`. It is deliberately a separate variable.

Before exposing `8765/tcp` beyond localhost, configure exact browser origins:

```env
PALWORLD_WEB_PUBLISH_IP=0.0.0.0
PALWORLD_WEB_ALLOWED_ORIGINS=http://192.168.1.20:8765
# TLS reverse proxy alternative:
# PALWORLD_WEB_PUBLIC_ORIGIN=https://pal.example.net
# PALWORLD_WEB_ALLOWED_HOSTS=pal.example.net
```

The Web UI strictly whitelists both `Host` and mutation `Origin`/`Referer`; it
never trusts a browser-supplied `Origin == Host`, preventing DNS rebinding.
`PALWORLD_WEB_ALLOWED_HOSTS` is only for a trusted proxy that sends a Host
different from the public origin authority. It uses HTTP Basic authentication
over plaintext HTTP: never expose it directly to the Internet. These browser
checks do not replace TLS, a VPN, or firewall access control.

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
control only the container runtime (`PUID`, `PGID`, update behavior, explicit
browser origin/host allowlists, and development placeholder opt-in); they do
not silently overwrite server or secret settings. This keeps a mounted
configuration reproducible. The three Web authority variables are the explicit
exception: Compose passes them to the Web process so an operator can set a
deployment-specific public address without editing the mounted file.

`PALWORLD_WEB_PUBLISH_IP` is not a container-process environment variable. It
is evaluated by Compose on the **host** when it builds the `ports` mapping, and
both supplied Compose files default it to `127.0.0.1`.

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
