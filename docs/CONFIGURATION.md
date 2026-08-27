# Deployment configuration contract

The caretaker reads UTF-8 `KEY=VALUE` files as data. It never sources them or
performs shell expansion. New deployments use these files in increasing
precedence order:

1. built-in backward-compatible defaults;
2. `palworld.env`, when present (legacy combined deployment);
3. `caretaker.env` (paths, accounts, backup policy);
4. `server.env` (non-secret game and runtime settings);
5. `secrets.env` (passwords and tokens).

Later files override earlier files. A key may appear only once in an individual
file. Unknown keys, malformed lines, relative paths, invalid ranges, and unsafe
path relationships fail closed. `$`, backticks, command substitutions, and
other shell-looking content remain literal text and are never executed.

Copy the matching `.example` files from `config/` and set `secrets.env` to mode
`0640`（owner 為 root、group 為 `PALWORLD_MANAGER_USER`）。這讓受限的本機管理
服務可讀取 REST 密碼，但不會向其他帳號開放。The legacy `/srv/palworld/config/palworld.env` remains supported without
changes.

`PALWORLD_WEB_UI_USERNAME` 位於 `caretaker.env`，預設為
`palworld-manager`。本機 Web UI 的 HTTP Basic Auth 密碼優先使用
`secrets.env` 的 `PALWORLD_WEB_UI_PASSWORD`；若留空則使用既有的
`ADMIN_PASSWORD`，因此任何部署預設仍需要認證。建議在多使用者主機設定獨立的
`PALWORLD_WEB_UI_PASSWORD`。

## Paths and backups

`PALWORLD_INSTALL_ROOT` may be any absolute path, including paths containing
spaces or non-ASCII characters. The following paths are derived from it:

```text
server            <install root>/server
config            <install root>/config
scripts           <install root>/scripts
local safety copy <install root>/backups-local
```

`PALWORLD_BACKUP_DIR` is independent and must not overlap the installation
root. For a NAS or removable disk, set its exact mountpoint in
`PALWORLD_BACKUP_MOUNT` and leave `PALWORLD_BACKUP_REQUIRE_MOUNT=true`. Preflight
then refuses to operate unless that path is an actual mounted filesystem. This
prevents a missing NAS from turning into an unnoticed local backup.

For a deliberately local backup destination, set
`PALWORLD_BACKUP_REQUIRE_MOUNT=false`; `PALWORLD_BACKUP_MOUNT` may then be empty.

## Validation

Validate configuration values without touching the live filesystem:

```bash
python3 scripts/palworld_manager.py --config-dir /path/to/config --no-filesystem
```

Run the complete preflight (directories, symlinks, access, secret-file mode,
backup destination, and mountpoint):

```bash
python3 scripts/palworld_manager.py --config-dir /path/to/config
```

Both commands exit with status 2 and a non-secret diagnostic when validation
fails. Preflight does not create directories, mount filesystems, or modify
configuration. The deployed filesystem preflight additionally requires
`secrets.env` to be a regular `0640` file owned by
`root:<PALWORLD_MANAGER_USER>`.

The installer requires a directory containing the deployment files and runs
value-only preflight before its first package, account, directory, or systemd
change:

```bash
sudo bash ./install-palworld.sh --config-dir /absolute/path/to/deployment-config
```

After creating the configured directories it runs complete filesystem
preflight before rendering and installing the unit templates. The installed
Bash tools discover `<PALWORLD_INSTALL_ROOT>/config` relative to their own
location; `PALWORLD_CONFIG_DIR` can explicitly override that location.

Upgrades intentionally require the deployed configuration directory. They do
not infer an installation root, append defaults to a live file, or rewrite any
of the four supported configuration layers:

```bash
sudo bash ./upgrade-palworld-manager.sh \
  --config-dir "<PALWORLD_INSTALL_ROOT>/config"
```

The upgrader resolves every destination through the merged contract, creates a
mode-`0700` manager/configuration safety copy below `backups-local`, renders all
systemd units for the configured paths, and leaves saved worlds and the
configured backup destination untouched.
