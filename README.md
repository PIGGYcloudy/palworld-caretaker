# Palworld Server Caretaker

以 SteamCMD、systemd 與官方 REST API 管理 Palworld Dedicated Server 的
跨平台維運工具，重點是安全關服、可恢復備份、閒置自動關服、Discord
遠端操作與受驗證的 Web 管理面板；不建議直接將管理介面暴露到 Internet。

> [!IMPORTANT]
> v0.9.0 支援 Ubuntu 24.04 LTS amd64 的原生 systemd 部署、Docker Compose
> 一鍵容器化開服，以及 Windows 原生 PowerShell 維運腳本。Docker 模式維持 container
> `0.0.0.0:8765` listener、預設只發布到 host loopback；若要 LAN/VPN 存取，需登錄
> 精確的 Host/Origin 白名單。原生 systemd 模式預設維持 loopback Web UI，並可透過
> 受驗證的 IPv4 bind 設定安全地
> 部署於可信 LAN/VPN。部署設定契約、
> 安裝器、備份/還原/更新腳本與 systemd unit 支援任意安全的絕對安裝與備份
> 路徑；設定編輯、還原與維護操作均會先驗證並留下安全紀錄。正式存檔仍建議
> 先在測試主機完成安裝、備份與還原演練。

## 目前功能

- SteamCMD 安裝、驗證與更新 Palworld Dedicated Server。
- systemd 管理、崩潰有限重啟與 localhost-only REST API 防護。
- 安全存檔、正常關服、可設定目的地的版本化備份與互動式還原。
- 型別驗證的遊戲設定管理器，使用 `config/editable/` 分層設定、差異預覽與原子寫入。
- v0.7.0 將 Web UI schema 擴充至 40 個 typed world/event settings，涵蓋
  `Survival & Penalties`、`Stamina & Health`、`Building & Decay` 與 `Pal Dynamics`；
  完整欄位與範圍見 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)。
- Windows 原生維運腳本：[`scripts/windows/`](scripts/windows/) 提供備份、還原、
  服務啟停與 `PalWorldSettings.ini` 渲染；快速流程見
  [`docs/WINDOWS.md`](docs/WINDOWS.md)。
- Web UI 與 CLI 的 pre-restore safety backup、manifest 驗證與原子還原流程。
- 無玩家逾時後再次確認，再執行存檔與正常關服。
- Discord `/pal` 指令、guild/channel/role permission matrix 與管理員確認；
  `/pal status` 支援分區 rich embeds，並可主動發送主機記憶體警示。
- Local Web UI：`PALWORLD_WEB_BIND_IP` 預設為 `127.0.0.1`；Docker 內部監聽
  `0.0.0.0`、Compose 預設只發布到 host loopback，提供遊戲內公告、玩家踢出／封鎖、SaveGames ZIP 匯出、狀態、快照、
  安全操作，以及 40 個設定欄位的說明與單欄預設值重置。
- Docker 一鍵容器化開服：`Dockerfile`、`docker-compose.yml` 與 PID 1
  `docker/docker-supervisor.py` 管理遊戲、Web UI、可選 Discord Bot、更新、備份與
  優雅停服；完整流程見 [`docs/DOCKER.md`](docs/DOCKER.md)。
- Web UI 維護觸發與即時狀態輪詢；Discord 維護前倒數及完成/失敗通知。
- Web、CLI 與 Discord 共用的 secret-masked strict JSON audit log。
- 每日維護保留原本開關服狀態，失敗時避免強制關服或無聲資料損失。

從零部署請見 [`docs/INSTALL.md`](docs/INSTALL.md)，既有 `/srv/palworld` 部署
請見 [`docs/UPGRADE.md`](docs/UPGRADE.md)，Discord Bot 的完整建立、權限與
token 安全流程見 [`docs/DISCORD_SETUP.md`](docs/DISCORD_SETUP.md)；Docker 部署請見
[`docs/DOCKER.md`](docs/DOCKER.md)。後續產品方向另列於 [`docs/ROADMAP.md`](docs/ROADMAP.md)。

## 架構

```text
palworld.service（遊戲，Restart=on-failure）
  └─ localhost:8212 官方 REST API
       ↑                         ↑
idle watcher（永久在線）      Discord Bot（永久在線）      Local Web UI（loopback）
  └─ 無人逾時→再查詢→save→shutdown  └─ /pal start|status|players|announce|kick|ban  └─ 127.0.0.1:8765
```

各管理服務彼此獨立。遊戲正常關閉不會停止 Bot、watcher 或 Web UI；啟動只會透過 root 擁有、參數固定的 `/usr/local/sbin/palworld-control start` 執行，絕不執行瀏覽器或 Discord 訊息中的任意 shell 指令。

Docker Compose 模式改由 `docker/docker-supervisor.py` 作為 PID 1 管理
PalServer process group、Web UI、可選 Bot、排程備份與 idle watcher；容器內不呼叫
systemd 或 sudo，前端透過私有 Unix socket 請求受序列化的生命週期操作。

v0.7.0 的共用核心位於 `src/palworld_caretaker/`：`rest.py` 負責
loopback-only、禁止 proxy/redirect 的強型別 REST；`backup.py` 負責 mount、
容量、manifest SHA-256 與原子兩階段 commit；`diagnostics.py` 收集服務、程序、
REST 與玩家狀態；`operations.py` 提供跨行程 `flock`；`settings.py` 與
`settings_store.py` 提供型別 schema、差異預覽與分層原子設定；`audit.py` 提供
strict JSON 操作紀錄；`web.py` 提供零第三方依賴的本機面板、維護輪詢與還原入口。
`service.py`、`config.py` 與 `steamcmd.py` 提供生命週期、設定與外部 adapter 邊界。
Bash、systemd、Discord 與 Web 入口只組合這些契約，不在各入口重複安全決策。

Windows 原生入口位於 `scripts/windows/`，使用相同的 UTF-8 `KEY=VALUE` 設定層與
跨程序 operation lock；Windows lock 的預設位置為
`C:\\ProgramData\\Palworld\\operation.lock`。PowerShell 入口會拒絕相對路徑、
`..`、root、重疊的備份樹，以及 symlink、junction 或其他 reparse point。

## 安全與設定

新設定範本依用途拆成 `config/caretaker.env.example`、
`config/server.env.example` 與 `config/secrets.env.example`，規範與 preflight
用法見 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)。既有
`/srv/palworld/config/palworld.env` 仍是完整支援的相容來源。正式設定檔已由
Git 排除，不可提交。
升級器會先在 `<PALWORLD_INSTALL_ROOT>/backups-local/manager-upgrade-*` 建立受保護
的設定與 manager safety copy。它不會改寫或追加任何設定層，也不會碰觸存檔與
外部備份。安裝或升級後，非 secret 設定位於
`<PALWORLD_INSTALL_ROOT>/config/editable/caretaker.env` 與 `server.env`；
配置根目錄與 `secrets.env` 仍由 root 保護。預設狀態目錄
`/var/lib/palworld-manager` 另存 `audit.log` 與維護狀態。

```env
PALWORLD_REST_API_HOST=127.0.0.1
PALWORLD_REST_API_PORT=8212
PALWORLD_REST_API_USERNAME=admin
PALWORLD_API_TIMEOUT_SECONDS=5
PALWORLD_SAVEGAMES_EXPORT_MAX_BYTES=8589934592
PALWORLD_IDLE_SHUTDOWN_ENABLED=true
PALWORLD_IDLE_TIMEOUT_MINUTES=10
PALWORLD_PLAYER_CHECK_INTERVAL_SECONDS=60
PALWORLD_STARTUP_GRACE_SECONDS=600
PALWORLD_SHUTDOWN_WAIT_SECONDS=30
PALWORLD_IDLE_WATCHER_DRY_RUN=true
PALWORLD_START_READY_TIMEOUT_SECONDS=180
PALWORLD_MEMORY_ALERT_PERCENT=85
PALWORLD_MEMORY_ALERT_COOLDOWN_SECONDS=1800
PALWORLD_WEB_UI_USERNAME=palworld-manager
PALWORLD_WEB_BIND_IP=127.0.0.1
PALWORLD_WEB_UI_PASSWORD=''
DISCORD_BOT_TOKEN='...'
DISCORD_PALWORLD_ALLOWED_GUILD_IDS=123
DISCORD_PALWORLD_ALLOWED_ROLE_IDS=456
DISCORD_PALWORLD_ADMIN_ROLE_IDS=789
DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS=101112
```

ID 清單可用逗號分隔。只有 `DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS=*` 可表示指定 guild 內所有頻道；guild 與角色仍必須明確指定。allow list 為空時採 fail-closed，所有指令都拒絕；私訊一律拒絕。`start`、`status`、`players`、`backups` 需要 `DISCORD_PALWORLD_ALLOWED_ROLE_IDS` 或管理員角色；`announce`、`kick`、`ban`、`stop`、`backup`、`diagnose` 與 `update` 只接受 `DISCORD_PALWORLD_ADMIN_ROLE_IDS`（簡稱 `ADMIN_ROLE_IDS`）。`stop` 與 `update` 仍必須 `confirm:true`。權限只比較 Discord guild/channel/role 的數字 ID。

官方 REST API 會由 renderer 設為 `RESTAPIEnabled=True` 和指定連接埠。`palworld-rest-firewall.service` 同時以 IPv4/IPv6 firewall 阻擋非 loopback 對 REST TCP port 的連線；切勿在路由器、雲端防火牆或其他 proxy 公開 8212。遊戲 UDP 8211 的既有映射不變。

## Local Web UI

原生 systemd 安裝或升級後 `palworld-web-ui.service` 預設常駐於
`127.0.0.1:8765`；在伺服器本機瀏覽器開啟
[`http://127.0.0.1:8765/`](http://127.0.0.1:8765/)。將受保護設定中的
`PALWORLD_WEB_BIND_IP` 改為 `0.0.0.0` 或指定私有 IPv4、填入允許的 browser origin，
並重啟服務，即可在已受 TLS、VPN 與防火牆保護的 LAN/VPN 使用。Docker 預設僅發布
`127.0.0.1:8765`；LAN/VPN 需設定 `PALWORLD_WEB_PUBLISH_IP` 與允許的 origin，詳見
[`docs/DOCKER.md`](docs/DOCKER.md)。瀏覽器跳出的 HTTP Basic Auth
帳號是 `PALWORLD_WEB_UI_USERNAME`（預設 `palworld-manager`），密碼是
`PALWORLD_WEB_UI_PASSWORD`；後者留空時相容地使用 `ADMIN_PASSWORD`。建議正式部署
設定獨立的 `PALWORLD_WEB_UI_PASSWORD`，不要把密碼放進 URL、shell history 或聊天。
通過 Basic Auth 後，頁面才會提供行程內 CSRF token。

對非 loopback 使用，必須明確設定完整且精確的
`PALWORLD_WEB_ALLOWED_ORIGINS`（LAN/VPN）或
`PALWORLD_WEB_PUBLIC_ORIGIN=https://pal.example.net`（TLS reverse proxy）。Host 與
Origin/Referer 分別經過白名單驗證，不接受由瀏覽器控制的動態 `Origin == Host`，以防
DNS rebinding。只有 proxy 的 upstream Host 不同時才加入
`PALWORLD_WEB_ALLOWED_HOSTS`。這不會取代 Basic Auth、CSRF token 或 TLS；不要直接把
plaintext `8765` 暴露在 Internet。

面板顯示 service/REST/玩家與 REST 可提供的 CPU、記憶體摘要，以及安全驗證過的 snapshot 清單。遊戲內公告表單會直接廣播訊息；玩家清單提供確認後的踢出／封鎖按鈕與可選原因。SaveGames 匯出會先由 REST API 存檔，再建立目前 SaveGames 的 ZIP 下載；`PALWORLD_SAVEGAMES_EXPORT_MAX_BYTES` 預設限制為 8 GiB，symlink、非 regular file、容量或空間檢查失敗時會拒絕，暫存檔不會留在下載目錄。立即備份會先發送固定公告，再啟動既有的 systemd 備份服務；安全關閉與重啟必先由 REST 成功存檔。若從管理者電腦操作，可先建立 SSH tunnel，再仍以相同 URL 與 Basic Auth 登入：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@palworld-host
```

世界設定頁面提供型別與範圍驗證、每個欄位的 `?` 說明及立即重設為系統預設值的
按鈕、套用前差異預覽，以及只寫入
`config/editable/` 的原子更新；每次修改前會保存設定副本，遊戲運行中則標示需
安全重啟。備份清單也可從 Web UI 觸發還原：流程會先建立外部 snapshot 與
`backups-local/pre-restore-*`，完成 preflight 後才停止服務並原子替換 live
SaveGames/Config。SteamCMD 維護按鈕會以背景方式啟動固定的
`palworld-maintenance.service`，並每 10 秒輪詢 preflight、關服、備份、更新、
重啟及完成/失敗狀態；維護中不接受其他變更操作。

原生 systemd 接受 `PALWORLD_WEB_BIND_IP` 的有效 IPv4 值；Docker 的該值是 container
internal bind，host publication 則使用 `PALWORLD_WEB_PUBLISH_IP`。兩者都應限制在可信
區網／VPN 或受 TLS 與存取控制保護的 proxy 後方，切勿直接公開 plaintext HTTP。

原生 systemd 的所有變更操作共用 `/run/palworld-caretaker/operation.lock`；它由 tmpfiles
以 `root:<manager>` 預建，runtime 目錄為 manager 不可寫入的 `0750`。Docker 則由
PID 1 Supervisor 的 `operation_lock` 序列化 Web、Discord、排程與維護操作；收到
`SIGTERM`／`SIGINT` 時先等待進行中的 backup／restore／update，再 save、graceful
shutdown，必要時才逐級終止完整遊戲 process group，避免操作中斷造成壞檔。無法判定
maintenance 或服務狀態時一律拒絕。備份若伺服器運行，必須先收到 REST `POST /save` 的
成功回應，否則絕不停止服務。所有頁面與 API 都先要求 HTTP Basic Auth，再要求 JSON +
行程內 CSRF token，並設有同源、無快取與禁止嵌入的瀏覽器防護。

操作紀錄由 Web、CLI 與 Discord 寫入預設的
`/var/lib/palworld-manager/audit.log`。每行都是禁止 NaN/Infinity 的 strict JSON，
寫入與 Web 顯示前都會遮蔽 token、password、secret、key、auth 與 cookie 等
credential；檔案預設為 manager 擁有的 `0640` regular file。

## Windows 原生 PowerShell 維運（v0.9.0）

Windows 主機可直接使用 `scripts/windows/` 下的 PowerShell 入口，不需要 systemd。
請使用 PowerShell 7（`pwsh`），並先將同一套 UTF-8 `KEY=VALUE` 設定檔放在部署
設定目錄；至少指定安裝根目錄、遊戲存檔同級的備份目錄，以及 Windows 上的
`PALWORLD_BACKUP_REQUIRE_MOUNT=false`：

```env
PALWORLD_INSTALL_ROOT=C:\Palworld
PALWORLD_SERVER_ROOT=C:\Palworld\server
PALWORLD_BACKUP_DIR=C:\Palworld\server\Pal\Saved\SaveGames_Backups
PALWORLD_BACKUP_MOUNT=
PALWORLD_BACKUP_REQUIRE_MOUNT=false
PALWORLD_MANAGER_STATE_DIR=C:\ProgramData\Palworld\state
```

從專案根目錄執行快速操作：

```powershell
$ConfigDir = 'C:\Palworld\config'
pwsh -NoProfile -File .\scripts\windows\palworld-service.ps1 -Action status
pwsh -NoProfile -File .\scripts\windows\backup-palworld.ps1 -ConfigDir $ConfigDir
pwsh -NoProfile -File .\scripts\windows\restore-palworld.ps1 -ConfigDir $ConfigDir -List
pwsh -NoProfile -File .\scripts\windows\render-settings.ps1 -ConfigDir $ConfigDir
```

還原時省略 `-Force` 會要求輸入精確的 `RESTORE palworld-YYYYMMDD-HHMMSS` 確認；
`palworld-service.ps1` 的服務名稱預設為 `PalServer`，可用 `-ServiceName` 覆寫。
測試或尚未註冊 Windows service 時，備份／還原可使用 `-NoServiceControl`，服務腳本
則可使用 `-WhatIf` 做不改動的演練。備份、還原與服務操作共用
`C:\ProgramData\Palworld\operation.lock`，不要刪除、替換或改成 junction/symlink。

Windows 邊界會拒絕相對路徑、`..`、filesystem root、備份與 live tree 重疊，以及
symlink、junction 或其他 reparse point；還原會先建立毫秒時間戳加 GUID 的 Safety
Copy，並在 live publish 失敗時自動 rollback。完整設定與安全注意事項見
[`docs/WINDOWS.md`](docs/WINDOWS.md)。

## 無人自動關服

watcher 對每個 systemd `InvocationID` 視為一次新 lifecycle。新 lifecycle 先等待 startup grace，之後按間隔查詢 `GET /v1/api/players`。有玩家立即清除 timer；連續無人達 timeout 時再次查詢，接著執行：

1. `POST /v1/api/save`，必須收到 HTTP 200；
2. 短暫緩衝後再查一次玩家；
3. 仍無人時 `POST /v1/api/shutdown`，傳入等待秒數；
4. 標記該 lifecycle 已送出關服，絕不重複觸發。

timeout、認證失敗、連線錯誤、非 200、JSON 無法解析、缺少 `players` 陣列或玩家項目格式錯誤，都視為「未知」而不是 0 人，並抑制關服。狀態原子寫入 `/var/lib/palworld-manager/idle-state.json`；watcher 重啟會沿用同一 lifecycle 的 timer，缺少可信狀態時則重新給予 grace period。

第一次正式測試保持 `PALWORLD_IDLE_WATCHER_DRY_RUN=true`。確認 log 正確後才改為 `false` 並重啟 watcher。修改 timeout 後也需重啟 watcher。

## Discord 指令

- `/pal start`：鎖定後啟動服務，等待 REST API ready；已在線時不重複啟動。
- `/pal status [all|resources|game|players]`：Discord 會以 slash choices 提供區段，
  預設 `all`，並以 ephemeral rich embed 回覆。`all` 顯示服務、REST、玩家、idle
  與資源總覽；`resources` 顯示主機 RAM、Palworld RSS、CPU load、存檔磁碟；`game`
  顯示服務、REST、遊戲程序 uptime；`players` 顯示在線清單。
- `/pal players`：顯示玩家數與名稱；API 異常顯示未知。
- `/pal backups`：列出最近可用的備份快照與大小。
- `/pal announce message:"..."`：管理員限定，發送遊戲內公告。
- `/pal kick player_name_or_id:"..." reason:"..."`：管理員限定，踢出指定在線玩家；
  可用精確名稱或 user/Steam ID。
- `/pal ban player_name_or_id:"..." reason:"..."`：管理員限定，封鎖指定玩家；
  可用精確名稱或 user/Steam ID。
- `/pal stop confirm:true`：管理員限定，成功 save 後才送出 graceful shutdown。
- `/pal backup`：管理員限定，透過 systemd 備份服務建立並驗證一份新 snapshot。
- `/pal diagnose`：管理員限定，顯示不含 secrets 的服務、REST 與玩家健康摘要。
- `/pal update confirm:true`：啟動備份與 SteamCMD 更新。若能確認有在線玩家，Bot 會在維護前先發送包含 graceful shutdown 秒數的倒數公告，再以同一則嵌入式訊息顯示安全關服、備份、更新與重啟進度，最後發送完成或失敗通知；會保留更新開始前的開關服狀態。

Bot 採 slash command，不會重複註冊文字指令。`DISCORD_PALWORLD_ADMIN_ROLE_IDS`（常簡稱
`ADMIN_ROLE_IDS`）必須填入管理員 Discord role ID；Discord 的 `Administrator` app
permission 不會取代這項設定。`start`、`stop`、`backup` 與 `update` 會取得
`/run/palworld-caretaker/operation.lock`，並確認 `palworld-maintenance.service`
不是 active/activating/deactivating；任一狀態無法確認就拒絕操作。`announce`、
`kick` 與 `ban` 另受 Bot 內的操作鎖、冷卻與 audit 保護。全域 slash command 初次
同步可能需要 Discord 一段時間顯示。

Bot 每 60 秒檢查主機 RAM。`PALWORLD_MEMORY_ALERT_PERCENT`（預設 `85`，10–99）
與 `PALWORLD_MEMORY_ALERT_COOLDOWN_SECONDS`（預設 `1800`，60–86400）控制主動警示；
使用率達到或超過門檻時只通知一次，必須先恢復到門檻以下才重新啟用，冷卻時間會
抑制恢復後立即再次跨越門檻的通知。hysteresis/cooldown 狀態安全保存在
`PALWORLD_MANAGER_STATE_DIR/alert-state.json`。警示會送至明確設定的
`DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS` channel；channel 設為 `*` 或未設定時不會
猜測目標或傳送主動通知。

Linux 資源 metrics 以有大小上限的 procfs 讀取取得，不呼叫 shell utilities；只會
匹配精確的 Palworld executable basename（`PalServer`、`PalServer-Linux-Test`、
`PalServer-Linux-Shipping`）。無法取得或資料不合法的欄位在 status embed 顯示
`未知`／`未偵測到`，不會把探測失敗當成零值。

互動式設定（token 輸入不顯示，也不會出現在 shell history）：

```bash
sudo /usr/local/sbin/palworld-discord-configure \
  --config-dir '<PALWORLD_INSTALL_ROOT>/config'
```

工具會要求 Application ID、guild ID、channel ID、一般角色 ID 與管理員角色 ID，成功後啟動 Bot 並顯示只包含 View Channel、Send Messages 和 slash-command scope 的邀請網址。

## 安裝、啟停與紀錄

安裝器以 Ubuntu 24.04、systemd 與 SteamCMD 套件為前提。完整系統需求、備份
目的地選擇、非預設路徑和驗證流程見 [`docs/INSTALL.md`](docs/INSTALL.md)。快速
流程如下：

```bash
mkdir -p ./deployment-config
cp config/{caretaker,server,secrets}.env.example ./deployment-config/
mv ./deployment-config/caretaker.env.example ./deployment-config/caretaker.env
mv ./deployment-config/server.env.example ./deployment-config/server.env
mv ./deployment-config/secrets.env.example ./deployment-config/secrets.env
chmod 0640 ./deployment-config/secrets.env
# 編輯 deployment-config/*.env 後：
sudo bash ./install-palworld.sh --config-dir "$PWD/deployment-config"
sudo "<PALWORLD_INSTALL_ROOT>/scripts/render-settings.sh"
sudo systemctl restart palworld.service palworld-idle-watcher.service
sudo systemctl enable --now palworld-discord-bot.service
```

升級既有管理元件而不重新下載遊戲時，必須明確提供已部署的設定目錄；安裝
根目錄、帳號、state 與所有衍生路徑都從分層設定契約解析：

```bash
sudo bash ./upgrade-palworld-manager.sh \
  --config-dir "<PALWORLD_INSTALL_ROOT>/config"
```

常用操作：

```bash
sudo systemctl start palworld.service
sudo systemctl stop palworld.service
sudo systemctl restart palworld-idle-watcher.service palworld-discord-bot.service
sudo journalctl -u palworld.service -f
sudo journalctl -u palworld-idle-watcher.service -f
sudo journalctl -u palworld-discord-bot.service -f
sudo "<PALWORLD_INSTALL_ROOT>/scripts/backup-palworld.sh"
sudo systemctl status palworld-web-ui.service --no-pager
```

## Docker / Compose 部署

v0.9.0 提供 [`Dockerfile`](Dockerfile)、[`docker-compose.yml`](docker-compose.yml)、
PID 1 [`docker/docker-supervisor.py`](docker/docker-supervisor.py) 與完整的
[`docs/DOCKER.md`](docs/DOCKER.md)。快速建立一次性設定並啟動：

```bash
mkdir -p data/server data/backups config
docker compose run --rm palworld-caretaker true
# 編輯 config/secrets.env，替換所有 CHANGE_ME 密碼
docker compose up -d
docker compose logs -f palworld-caretaker
```

容器內的 Web UI 監聽 `0.0.0.0:8765`，但 Compose 預設僅發布至 host
`127.0.0.1:8765`；遊戲發布為 `8211/udp`，並將遊戲、備份與分層設定保存到上述三個
目錄。LAN/VPN 發布請設定 host-side `PALWORLD_WEB_PUBLISH_IP`，並登錄精確的
`PALWORLD_WEB_ALLOWED_ORIGINS`。entrypoint 以 root 完成 `PUID`／`PGID` bootstrap 後
即降權為 non-root `steam`；詳細的來源掛載、反向 proxy、備份還原與停服流程請先閱讀
[`docs/DOCKER.md`](docs/DOCKER.md)。

## 診斷與安全解除安裝

診斷是唯讀操作，會檢查設定／權限／備份 mount policy、必要檔案、systemd
unit 狀態，並在遊戲運行時測試 localhost REST API。輸出不包含密碼或 token：

```bash
sudo "<PALWORLD_INSTALL_ROOT>/scripts/diagnose-palworld.sh"
sudo "<PALWORLD_INSTALL_ROOT>/scripts/diagnose-palworld.sh" --json
```

解除安裝必須指定已部署的設定目錄與層級：

```bash
# 移除 caretaker、systemd units、venv 與管理腳本；保留遊戲、設定、世界與備份
sudo "<PALWORLD_INSTALL_ROOT>/uninstall-palworld.sh" \
  --config-dir "<PALWORLD_INSTALL_ROOT>/config" --level manager

# 再移除遊戲程式；仍保留 Pal/Saved、設定與所有備份
sudo "<PALWORLD_INSTALL_ROOT>/uninstall-palworld.sh" \
  --config-dir "<PALWORLD_INSTALL_ROOT>/config" --level game

# 移除設定與世界資料；精確確認字串為必要條件
sudo "<PALWORLD_INSTALL_ROOT>/uninstall-palworld.sh" \
  --config-dir "<PALWORLD_INSTALL_ROOT>/config" --level all \
  --confirm 'DELETE PALWORLD DATA'
```

三個層級都不刪除 `PALWORLD_BACKUP_DIR`，也保留安裝根目錄內的
`backups-local`。解除安裝不會自行刪除設定中命名的系統帳號，避免誤刪原本就
存在或仍由其他服務共用的帳號。

暫停自動關服：將 `PALWORLD_IDLE_SHUTDOWN_ENABLED=false`，再重啟 watcher。只觀察不動作：改為 `PALWORLD_IDLE_WATCHER_DRY_RUN=true`。

API 不通時，先查看遊戲 log、確認 `RESTAPIEnabled=True`、8212 loopback listener、管理密碼一致，以及 firewall service 正常。Discord 無反應時，查看 Bot log，確認 token、guild/channel/role ID、Bot 的 `applications.commands` scope 與頻道權限；不要把 token 或管理密碼貼進 log 或聊天。

## 備份與還原

維護時間、通用備份目的地與保留版本數分別由 `BACKUP_TIME`、
`PALWORLD_BACKUP_DIR` 與 `BACKUP_RETENTION_COUNT` 決定。維護會先透過本機
REST API 存檔並要求正常關服，才取得一致 snapshot，再以 SteamCMD 驗證並
更新伺服器。若正常關服失敗，維護會中止，不會以強制終止取代。若維護開始
前伺服器正在運行，完成後才會重新啟動；開始前已關閉則維持關閉。還原使用：

新安裝預設將備份放在遊戲存檔同級的
`<PALWORLD_INSTALL_ROOT>/server/Pal/Saved/SaveGames_Backups`。Docker Compose
則維持獨立的 `./data/backups` volume，以隔離容器中的遊戲資料與備份。

```bash
sudo "<PALWORLD_INSTALL_ROOT>/scripts/restore-palworld.sh"
sudo "<PALWORLD_INSTALL_ROOT>/scripts/restore-palworld.sh" restore palworld-YYYYMMDD-HHMMSS
```

CLI 還原會在停止服務前再次驗證 snapshot，要求輸入精確確認字串
`RESTORE palworld-YYYYMMDD-HHMMSS`，並建立外部 snapshot 與
`backups-local/pre-restore-*` safety copy；任何 preflight、容量、manifest 或
原子發布失敗都不會覆寫 live 資料。Web UI 會回報已驗證的 safety backup 與
最終服務狀態，避免把未完成的還原誤報為成功。

`palworld.service` 維持 `Restart=on-failure` 並將正常結束碼 0、130、143 視為成功。官方 graceful shutdown 正常退出時不會被拉起；crash 才有限度重啟。若實機版本的 API shutdown 產生其他 exit code，先保持 dry-run 並以 journal 驗證，必要時將該已確認的正常碼加入 `SuccessExitStatus`。

## 開發與安全

提交變更前請閱讀 [`CONTRIBUTING.md`](CONTRIBUTING.md)，安全邊界與漏洞回報
方式見 [`SECURITY.md`](SECURITY.md)。GitHub Actions 會執行 Python 測試、Python
編譯、PowerShell parser 檢查、Bash 語法檢查、ShellCheck 與 release 產物驗證，
並在 Linux 與 `windows-latest` matrix 上執行跨平台驗證。從乾淨且已提交的
v0.9.0 source tree 建立 tarball 與 checksum：

```bash
scripts/package-release.sh --output dist/palworld-caretaker-v0.9.0.tar.gz
(cd dist && sha256sum --check SHA256SUMS)
```

本專案採用 [GNU General Public License v3.0](LICENSE) 授權。你可以依照
GPL-3.0 的條款使用、修改及散布本專案；散布衍生作品時也必須保留相同授權。
