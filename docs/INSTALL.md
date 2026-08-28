# 安裝指南

本指南適用於全新的 Palworld Dedicated Server。安裝器會安裝 SteamCMD、建立受
限系統帳號、下載遊戲、渲染 systemd units，並啟用遊戲、REST 防火牆、閒置
監看、備份排程與僅限本機的 Web UI。Discord Bot 只有在 token 已設定時才會啟動。

## 系統需求

- Ubuntu 24.04 LTS amd64，使用 systemd 與 APT；其他發行版尚未列入 v0.4.0
  的支援範圍。
- 具 `sudo` 權限的登入帳號，以及可連線至 Ubuntu 套件庫、Steam 與 Discord
  （若啟用 Bot）的網路。
- 足以容納遊戲、世界資料及升級安全副本的本機空間；備份目的地還需至少有
  「目前 SaveGames 與 Config 合計兩倍，再加 1 GiB」的可用空間。
- 對外開放遊戲 UDP port（預設 `8211`）時需自行設定路由器或雲端防火牆。
  REST TCP port（預設 `8212`）必須維持 localhost-only，不可公開。
  Local Web UI 固定只監聽 `127.0.0.1:8765`，不可透過 proxy 或防火牆公開。

安裝會加入 i386 architecture，並透過 APT 安裝 `steamcmd:i386`、
`python3-venv` 與 `iptables`。Valve 授權條款仍由操作者在 APT 流程中確認。

## 準備設定

先把 `palworld-caretaker-v0.4.0.tar.gz` 與 `SHA256SUMS` 放在同一目錄，驗證並
解壓：

```bash
sha256sum --check SHA256SUMS
tar -xzf palworld-caretaker-v0.4.0.tar.gz
cd palworld-caretaker-v0.4.0
```

驗證必須顯示 `OK`。接著在專案根目錄建立不受 Git 管理的部署設定：

```bash
mkdir -p deployment-config
cp config/caretaker.env.example deployment-config/caretaker.env
cp config/server.env.example deployment-config/server.env
cp config/secrets.env.example deployment-config/secrets.env
chmod 0640 deployment-config/secrets.env
```

編輯三個檔案：

- `caretaker.env`：安裝根目錄、系統帳號、備份目的地、保留數與排程。
- `server.env`：遊戲、REST API、閒置關服與 Discord allowlist／權限矩陣。
- `secrets.env`：伺服器密碼、管理密碼、可選的獨立 Web UI 密碼與 Discord Bot token。安裝後為
  `root:<PALWORLD_MANAGER_USER>`、mode `0640`，讓本機 Web UI 與 Discord Bot 可讀取；
  不可給其他群組或使用者讀取權限。

`SERVER_PASSWORD` 與 `ADMIN_PASSWORD` 必須替換 placeholder，且不能含
`,`、`(`、`)`、`"` 或換行。尚未設定 Discord 時可保留 token placeholder；
Bot 不會啟動。完整欄位、優先順序與安全限制見
[設定契約](CONFIGURATION.md)。

Web UI 一律使用 HTTP Basic Auth。帳號由 `caretaker.env` 的
`PALWORLD_WEB_UI_USERNAME` 決定；`secrets.env` 的
`PALWORLD_WEB_UI_PASSWORD` 留空時會使用 `ADMIN_PASSWORD`。多使用者主機建議
設定獨立 Web UI 密碼。

安裝器會把 Python 核心放在 `<PALWORLD_INSTALL_ROOT>/packages` 的 root-owned
受控 release 中：release 目錄與套件目錄為 `0755`，Python 檔案為 `0644`，
`packages/current` 以原子 symlink 指向完整 release。入口腳本維持固定路徑，
因此升級不會讓執行中的程序看到半套套件。

安裝時會把 `caretaker.env` 與 `server.env` 中屬於設定 schema 的非 secret 欄位
遷移到 `<PALWORLD_INSTALL_ROOT>/config/editable/`。這個子目錄由
`PALWORLD_MANAGER_USER` 擁有、權限為 `0750`，檔案為 `0640`；root-level
配置目錄與 `secrets.env` 保持 root 保護。日後可用 Web UI 編輯遊戲設定，或
直接修改 editable layer 後重新執行驗證；不要把 token、密碼或其他受保護欄位
放入該子目錄。

### 備份目的地

一般本機磁碟可設定為：

```env
PALWORLD_BACKUP_DIR=/var/backups/palworld
PALWORLD_BACKUP_MOUNT=
PALWORLD_BACKUP_REQUIRE_MOUNT=false
```

外接磁碟、NFS 或 SMB 等獨立掛載則建議啟用 mount 防護：

```env
PALWORLD_BACKUP_DIR=/mnt/game-backups/palworld
PALWORLD_BACKUP_MOUNT=/mnt/game-backups
PALWORLD_BACKUP_REQUIRE_MOUNT=true
```

後者會在 preflight、備份與還原時確認 mount 確實存在；掛載遺失時會停止，
避免把資料誤寫到本機 mountpoint。

## 執行全新安裝

先做不寫入系統的值驗證：

```bash
python3 scripts/palworld_manager.py \
  --config-dir "$PWD/deployment-config" --no-filesystem
```

再執行安裝：

```bash
sudo bash ./install-palworld.sh --config-dir "$PWD/deployment-config"
```

安裝器在任何套件、帳號、目錄或 systemd 變更前執行值驗證；建立目標目錄後，
還會執行完整 filesystem preflight。任一步驟失敗都會以非零狀態結束，應先依
錯誤訊息修正，不要跳過驗證。

## 非預設安裝路徑

`PALWORLD_INSTALL_ROOT` 可設為任意絕對路徑，包含空白或非 ASCII 字元，例如：

```env
PALWORLD_INSTALL_ROOT='/opt/game servers/palworld'
PALWORLD_MANAGER_STATE_DIR=/var/lib/palworld-manager
```

以下位置會自動衍生，不需也不能另行設定：

```text
<PALWORLD_INSTALL_ROOT>/server
<PALWORLD_INSTALL_ROOT>/config
<PALWORLD_INSTALL_ROOT>/scripts
<PALWORLD_INSTALL_ROOT>/backups-local
```

備份目錄必須位於安裝根目錄之外，所有部署路徑不得為 `/`、相互危險重疊或
使用 symbolic link。後續命令請使用實際絕對路徑；升級與 Discord 設定尤其要
傳入 `<PALWORLD_INSTALL_ROOT>/config`。

## 安裝後驗證

依序執行：

```bash
sudo "<PALWORLD_INSTALL_ROOT>/scripts/diagnose-palworld.sh"
sudo systemctl status palworld.service palworld-rest-firewall.service \
  palworld-idle-watcher.service palworld-backup.timer palworld-web-ui.service --no-pager
sudo journalctl -u palworld.service -n 100 --no-pager
sudo ss -lntp | grep ':8212'
```

確認診斷沒有 `fail`、遊戲服務可啟動、REST port 只在 loopback 監聽，並從
Palworld client 連線測試 UDP port。接著手動建立並列出第一份備份：

```bash
sudo "<PALWORLD_INSTALL_ROOT>/scripts/backup-palworld.sh"
sudo "<PALWORLD_INSTALL_ROOT>/scripts/restore-palworld.sh" list
```

### 開啟 Local Web UI

確認服務為 `active (running)` 後，在遊戲主機本機瀏覽器開啟
[`http://127.0.0.1:8765/`](http://127.0.0.1:8765/)：

```bash
sudo systemctl enable --now palworld-web-ui.service
sudo systemctl status palworld-web-ui.service --no-pager
```

瀏覽器會要求 HTTP Basic Auth。帳號是 `PALWORLD_WEB_UI_USERNAME`（預設
`palworld-manager`），密碼是 `PALWORLD_WEB_UI_PASSWORD`；若該欄位留空，
才會 fallback 到 `ADMIN_PASSWORD`。Basic Auth 失敗時不會洩漏頁面或 CSRF
token。建議使用獨立的 `PALWORLD_WEB_UI_PASSWORD`，修改 `secrets.env` 後以
`root:<PALWORLD_MANAGER_USER>`、`0640` 保存並重啟 Web UI。

Web UI 永遠只監聽 IPv4 loopback，不能直接從其他主機連線，也不可用 reverse
proxy 或防火牆公開。若需要遠端維運，先從管理者電腦建立 SSH tunnel，再在
管理者瀏覽器開啟同一個 URL，並照常輸入 Basic Auth：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@palworld-host
```

正式維護前可先只檢查備份來源、mount 與可用空間，不會存檔、停止或啟動服務：

```bash
sudo python3 "<PALWORLD_INSTALL_ROOT>/scripts/palworld_manager.py" \
  --config-dir "<PALWORLD_INSTALL_ROOT>/config" --backup-preflight
```

所有備份、還原、更新與安全啟停都會共用
`/run/palworld-caretaker/operation.lock`；若已有另一個操作進行中，命令會立即
拒絕並等待下一次維護窗口。不要刪除、替換或以 symbolic link 取代這個鎖檔。

## v0.4.0 管理功能、設定、維護與還原

### Discord 指令與權限

`ADMIN_ROLE_IDS` 是設定鍵 `DISCORD_PALWORLD_ADMIN_ROLE_IDS` 的簡稱；它必須填入
管理員 Discord role ID。`DISCORD_PALWORLD_ALLOWED_ROLE_IDS` 是一般操作角色。
所有請求仍須通過指定 guild 與 channel；空 allowlist、未允許的 guild/channel、
沒有符合角色或私訊一律拒絕。`DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS=*` 只可放寬
頻道邊界，不會放寬 guild 或角色邊界。

| 指令 | 權限 | 用途 |
| --- | --- | --- |
| `/pal start`、`/pal status`、`/pal players`、`/pal backups` | 一般角色或管理員角色 | 查看狀態、玩家與最近快照；`start` 也會啟動服務 |
| `/pal announce message:"..."` | 管理員角色 | 發送遊戲內公告 |
| `/pal kick player_name_or_id:"..." reason:"..."` | 管理員角色 | 踢出指定玩家 |
| `/pal ban player_name_or_id:"..." reason:"..."` | 管理員角色 | 封鎖指定玩家 |
| `/pal backup`、`/pal diagnose` | 管理員角色 | 建立安全備份或查看健康診斷 |
| `/pal stop confirm:true`、`/pal update confirm:true` | 管理員角色 | 安全關服或備份後更新；必須明確確認 |

踢出／封鎖請使用精確玩家名稱或 user/Steam ID；名稱重複時改用 ID。Bot 使用
slash command，不需要 Discord `Administrator` app permission，且管理員角色仍由
`DISCORD_PALWORLD_ADMIN_ROLE_IDS` 控制。完整建立、邀請與驗證流程見
[Discord Bot 設定](DISCORD_SETUP.md)。

### Web UI 公告、玩家管理與 SaveGames 匯出

登入僅限 loopback 的 Web UI 後：

- 「遊戲內公告」表單會將訊息廣播給遊戲內玩家。
- 線上玩家清單提供「踢出」與「封鎖」按鈕；每次操作先確認，並可輸入原因。
- 「SaveGames 匯出」會先要求 REST API `POST /save` 成功，再把目前使用中的
  `Pal/Saved/SaveGames` 建成 ZIP 下載。`PALWORLD_SAVEGAMES_EXPORT_MAX_BYTES`
  預設為 8 GiB；超過大小、symlink／非 regular file 或暫存空間不足時會拒絕，
  完成或失敗後清理暫存檔。

這些功能與其他 Web 操作一樣要求 HTTP Basic Auth、同源 CSRF token 與 JSON request；
SaveGames 匯出另外受 maintenance guard 與操作鎖保護。REST port `8212` 與 Web port
`8765` 都不可公開到網際網路。

### Web UI 設定管理

在本機 Web UI 的「世界設定」區塊可編輯伺服器名稱、玩家上限、時間/經驗/掉落
倍率、Pal 與玩家傷害、guild/base 限制、spawn/drop 參數，以及 idle shutdown
與備份排程等非 secret caretaker 選項。欄位會執行型別與範圍驗證，按「預覽變更」
可先查看差異；按「儲存設定」時會建立 settings backup，再以 atomic write
更新 `config/editable/`。伺服器執行中儲存仍會成功，但必須安全重啟才會套用遊戲
設定；Web UI 不會顯示或修改 `secrets.env`。

### Pre-restore safety backup

Web UI 的 snapshot 下拉選單與 CLI 使用相同的安全還原流程。先列出可用 snapshot：

```bash
sudo "<PALWORLD_INSTALL_ROOT>/scripts/restore-palworld.sh" list
```

CLI 還原必須提供完整名稱並輸入精確確認字串：

```bash
sudo "<PALWORLD_INSTALL_ROOT>/scripts/restore-palworld.sh" \
  restore palworld-YYYYMMDD-HHMMSS
# prompt: RESTORE palworld-YYYYMMDD-HHMMSS
```

流程會在停止服務前驗證 snapshot manifest、檔案、mount 與容量；接著先建立新的
外部 snapshot 以及 `<PALWORLD_INSTALL_ROOT>/backups-local/pre-restore-*` 本機
safety copy，才以 staging 與 atomic publish 覆寫 SaveGames/Config。驗證、備份或
發布任一步驟失敗，都不會進入 live data 覆寫階段；原本運行中的服務完成後會嘗試
恢復運行。Web UI 只在能驗證 safety copy 與最終服務狀態時回報成功。

### Web 維護按鈕與 audit log

「SteamCMD 維護」按鈕會背景啟動固定的
`palworld-maintenance.service`，不在 Web request 中執行任意命令。頁面每 10 秒
輪詢 `/api/maintenance/status`，顯示 preflight、關服、備份、更新、重啟與
terminal 狀態；維護期間其他變更操作會被拒絕。Discord 的
`/pal update confirm:true` 會在已知有玩家時先公告 graceful-shutdown 倒數，並
在維護完成或失敗時發送通知。

Web、CLI 與 Discord 的管理操作共用多通道 strict JSON audit log，預設位於
`/var/lib/palworld-manager/audit.log`。每行禁止 NaN/Infinity，寫入及讀取時都會
遮蔽 token、password、secret、key、auth 與 cookie 等 credential；檔案為 manager
擁有的 `0640` regular file。此紀錄不是備份，仍應保留 systemd journal 作為完整
維護輸出。

閒置監看預設應先保持 `PALWORLD_IDLE_WATCHER_DRY_RUN=true`；觀察
`journalctl -u palworld-idle-watcher.service` 確認判定正確後，再改為 `false`
並重啟 watcher。Discord 設定與驗證請接續閱讀
[Discord Bot 設定](DISCORD_SETUP.md)。
