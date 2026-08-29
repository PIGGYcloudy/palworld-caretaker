# Deployment configuration contract

The v0.7.0 caretaker reads UTF-8 `KEY=VALUE` files as data. It never sources them or
performs shell expansion. New deployments use these files in increasing
precedence order:

1. built-in backward-compatible defaults;
2. `palworld.env`, when present (legacy combined deployment);
3. root-level `caretaker.env` and `server.env`, when present for compatibility;
4. `editable/caretaker.env` and `editable/server.env` (non-secret settings,
   overriding root-level compatibility files);
5. `secrets.env` (passwords and tokens).

Later files override earlier files. A key may appear only once in an individual
file. Unknown keys, malformed lines, relative paths, invalid ranges, and unsafe
path relationships fail closed. `$`, backticks, command substitutions, and
other shell-looking content remain literal text and are never executed.

Copy the matching `.example` files from `config/` and set `secrets.env` to mode
`0640`（owner 為 root、group 為 `PALWORLD_MANAGER_USER`）。這讓受限的本機管理
服務可讀取 REST 密碼，但不會向其他帳號開放。The legacy `/srv/palworld/config/palworld.env` remains supported without
changes.

At install/upgrade, non-secret `caretaker.env` and `server.env` are placed in
`<install root>/config/editable`, owned by `PALWORLD_MANAGER_USER` with mode
`0640`. The config root itself remains `root:<PALWORLD_MANAGER_USER>` mode
`0750`; `secrets.env` stays at its root-protected path and cannot be renamed or
deleted by the Web UI account.

`PALWORLD_WEB_UI_USERNAME` 位於 `caretaker.env`，預設為
`palworld-manager`。本機 Web UI 的 HTTP Basic Auth 密碼優先使用
`secrets.env` 的 `PALWORLD_WEB_UI_PASSWORD`；若留空則使用既有的
`ADMIN_PASSWORD`，因此任何部署預設仍需要認證。建議在多使用者主機設定獨立的
`PALWORLD_WEB_UI_PASSWORD`。

## v0.7.0 Discord status, alerts, and SaveGames export

Discord 權限由四個 numeric ID 設定共同決定：
`DISCORD_PALWORLD_ALLOWED_GUILD_IDS`、
`DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS`、
`DISCORD_PALWORLD_ALLOWED_ROLE_IDS` 與
`DISCORD_PALWORLD_ADMIN_ROLE_IDS`。最後一項也常簡稱為 `ADMIN_ROLE_IDS`，只應
填入管理員 role ID；它不是 Discord 的 `Administrator` app permission。ID 可用逗號
分隔，只有 channel 設定可使用 `*`。空清單、未允許的 guild/channel/role 與私訊
都會 fail closed。

一般角色或管理員角色可使用 `/pal start`、`/pal status`、`/pal players` 與
`/pal backups`。只有 `DISCORD_PALWORLD_ADMIN_ROLE_IDS` 可使用
`/pal announce`、`/pal kick`、`/pal ban`、`/pal backup`、`/pal diagnose`、
`/pal stop` 與 `/pal update`；`stop` 和 `update` 另要求 `confirm:true`。完整指令
參數與 Bot 設定流程見 [Discord Bot 設定](DISCORD_SETUP.md)。

`/pal status` 的 `section` 是 Discord slash choice：`all`（預設）顯示服務、REST、
玩家、idle 與資源總覽；`resources` 顯示主機 RAM、Palworld RSS、CPU load 與存檔
磁碟；`game` 顯示服務、REST 與遊戲程序 uptime；`players` 顯示在線玩家清單。
每個 section 都以 ephemeral rich embed 回覆。

`PALWORLD_SAVEGAMES_EXPORT_MAX_BYTES` 控制 Web UI SaveGames ZIP 匯出的大小，預設
為 `8589934592`（8 GiB），有效範圍為 1 B 至 64 GiB。匯出前會先要求 REST API
存檔，並檢查 SaveGames 來源、symlink、可用空間與壓縮檔上限；成功下載或失敗後
都會移除暫存 archive。請確保 `PALWORLD_MANAGER_STATE_DIR` 所在檔案系統有足夠
空間。

`PALWORLD_MEMORY_ALERT_PERCENT` 設定 Discord Bot 發送主機 RAM proactive alert 的門檻
（預設 `85`，有效範圍 10–99）；使用率達到或超過門檻才會通知。
`PALWORLD_MEMORY_ALERT_COOLDOWN_SECONDS` 設定恢復後再次跨越門檻的最短通知間隔
（預設 `1800` 秒，有效範圍 60–86400）。Bot 每 60 秒檢查，持續達到或超過門檻時不重複
通知，只有低於門檻才會重新啟用；hysteresis 與 cooldown 的非 secret 狀態保存於
`PALWORLD_MANAGER_STATE_DIR/alert-state.json`。警示只會送到明確列出的
`DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS`；`*` 或空值不會猜測頻道。

主機與程序 metrics 使用有大小上限的 Linux procfs 讀取，不依賴 shell utilities；
Palworld 程序只匹配精確 executable basename `PalServer`、`PalServer-Linux-Test` 或
`PalServer-Linux-Shipping`。無法取得、超限或格式錯誤的欄位在 status 顯示未知／未偵測到，
不會把失敗探測當成零值。

## Local visual world settings

The loopback-only Web UI includes a schema-validated World Settings editor for
server name/player limits, rate multipliers, Pal and player dynamics, guild
limits, drops/spawns, and non-secret caretaker options. It shows an exact diff
before saving. Each save creates a private copy of the current `server.env`
and `caretaker.env` below `PALWORLD_MANAGER_STATE_DIR/settings-backups`, then
atomically writes only changed fields while holding the global operation lock.

In v0.7.0 the schema contains 40 typed world/event settings. The expanded
groups are:

- `Survival & Penalties` (3): item weight, equipment durability loss, and death
  penalty (`None`, `Item`, `ItemAndEquipment`, or `All`).
- `Stamina & Health` (8): Pal/player stamina depletion, natural and sleeping
  health regeneration, and Pal/player hunger depletion.
- `Building & Decay` (3): structure health, structure damage received, and
  deterioration damage.
- `Pal Dynamics` (5): Pal damage dealt/received, Pals per base camp, work
  speed, and reset-working-Pals-on-restart.

The remaining 21 schema fields are in General, Multipliers, Player & Guild,
Drops & Spawns, and Caretaker. Every field uses the same typed boundary for
Web JSON, editable environment files, and `PalWorldSettings.ini` rendering;
the exact environment keys and ranges are defined by the editor schema.

The editor never exposes or edits `secrets.env`. If the game is active it
marks the change as requiring a restart. The service renders the saved values
into `PalWorldSettings.ini` in its root-only start pre-step, so a normal safe
restart is the boundary at which world settings take effect.

The settings commit is also the trust boundary for browser input: unknown keys,
wrong types, values outside the schema range, invalid `HH:MM` values, and
`PalWorldSettings.ini` reserved characters are rejected before a backup or file
write. The old file contents remain available in the timestamped settings
backup if an operator needs to inspect or recover a change.

## v0.4 operational records and maintenance

The default manager state directory is `/var/lib/palworld-manager`. It contains
the multi-channel audit log at `/var/lib/palworld-manager/audit.log` and the
short-lived `maintenance-state.json` progress record. Web, CLI, and Discord
operations append one compact JSON object per line to the audit log. Records use
strict JSON (including rejecting non-finite numbers), are capped in size, and
mask credential-shaped keys and configured secret values before both writing and
displaying them. The log is a manager-owned regular file with mode `0640`; it is
not a replacement for the systemd journal.

The Web UI includes in-game announcement broadcasting, online-player kick/ban
controls, and SaveGames ZIP export in addition to its existing controls. Its
SteamCMD maintenance button starts only the fixed
`palworld-maintenance.service` unit. The browser polls the persisted maintenance
state every 10 seconds and shows the safe phase summary. A maintenance request
is rejected when another maintenance or operation lock is active. Discord
`/pal update confirm:true` performs the same guarded workflow, announces a
graceful-shutdown countdown when online players are known, and sends a terminal
success or failure notification.

Restore is available from the Web UI and the CLI. The CLI form is:

```bash
sudo /path/to/scripts/restore-palworld.sh list
sudo /path/to/scripts/restore-palworld.sh restore palworld-YYYYMMDD-HHMMSS
```

The CLI requires the exact `RESTORE palworld-YYYYMMDD-HHMMSS` confirmation. Both
frontends validate the snapshot, mount, manifest, and capacity before stopping
the service; they create an external pre-restore snapshot and a local
`backups-local/pre-restore-*` safety copy before atomically publishing the
restore. A failed preflight, safety backup, or publish leaves live data
untouched, and a previously active service is restored to its prior state when
possible.

## Operational path trust boundaries

All configured deployment paths must be absolute, must not be `/`, must not
overlap in unsafe ways, and must be real directories rather than symbolic links.
The same checks apply to the lock, snapshots, audit/state files, editable
configuration, and settings backups. Privileged workflows open critical files
with `O_NOFOLLOW` and use temporary files plus `fsync`/atomic rename; a missing
required mount is never treated as a writable local directory.

The installed configuration root, `secrets.env`, manager-state parent, live
server data, and systemd-controlled entry points remain outside the Web UI
write set. Only manager-owned `config/editable/` and the dedicated
`settings-backups/` directory are writable by the Web service. Do not replace
the operation lock or any trusted directory with a symlink, even temporarily.

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
