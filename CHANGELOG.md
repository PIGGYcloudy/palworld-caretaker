# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## Unreleased

- 尚無變更。

## [0.3.0] - 2026-08-28

這個版本把遊戲內設定、還原、維護與管理操作紀錄整合到同一套可驗證的
安全流程；Web UI、CLI 與 Discord 入口仍共用相同的鎖定、驗證與路徑信任邊界。

### 遊戲設定管理

- 新增 schema-backed 的 in-game settings manager，以型別化 schema 驗證整數、
  數值範圍、布林值、時間格式與嵌入 `PalWorldSettings.ini` 前必須避開的字元。
  Web UI 依 General、Multipliers、Pal Dynamics、Player & Guild、Drops & Spawns
  與 Caretaker 分類呈現欄位，儲存前會顯示精確差異；執行中的伺服器會明確標示
  需要安全重啟才會套用。
- 非 secret 的可編輯設定改用分層配置：`config/editable/caretaker.env` 與
  `config/editable/server.env` 覆蓋受保護的相容層，`secrets.env` 仍留在 root-only
  配置目錄，Web UI 絕不讀取或修改它。舊式 `palworld.env` 與 root-level 設定仍
  可相容讀取。
- 設定變更會先在 `settings-backups/` 建立受保護的目前版本副本，再以同一檔案系統
  的 temporary file、`fsync` 與 atomic rename 寫入；任何 reload 或 rollback
  失敗都會拒絕發布或回復原內容。

### Pre-restore safety backup 與原子還原

- CLI `restore-palworld.sh restore <snapshot>` 與 Web UI 還原按鈕共用 restore
  preflight：在提示、停止服務或建立副作用前驗證 snapshot manifest、檔案清單、
  mount policy、容量與 live path。CLI 仍要求精確輸入
  `RESTORE palworld-YYYYMMDD-HHMMSS`。
- 覆寫 SaveGames/Config 前一定建立新的外部 snapshot 與安裝根目錄內
  `backups-local/pre-restore-*` safety copy；快照以 staging、驗證與 atomic
  publish 取代 live trees，部分提交失敗時會 rollback。Web UI 會驗證 safety
  backup 與最終服務狀態，並只回報已確認的 restarted/stopped 結果。
- 還原完成後保留還原前的服務開關狀態；CLI 與 Web 操作都寫入共用 audit log。

### 維護、Web UI 與 Discord

- Web UI 新增 SteamCMD maintenance trigger。按鈕只會啟動固定的
  `palworld-maintenance.service`，前端每 10 秒輪詢安全的 maintenance state，
  即時顯示 preflight、關服、備份、更新、重啟與完成/失敗階段；維護進行中會
  拒絕其他變更操作。
- Discord `/pal update confirm:true` 在維護啟動前，若能確認有在線玩家，會先
  發送包含 graceful-shutdown 秒數的倒數公告；之後以同一則進度訊息追蹤維護，
  並在 terminal state 發送完成或失敗通知。無法確認玩家時採保守流程，不把未知
  當成無人。
- Web、CLI、Discord、timer、idle watcher 與 maintenance 共同使用
  `/run/palworld-caretaker/operation.lock`，避免還原、備份、更新與啟停交錯。

### 多通道 audit log

- 新增 Web、CLI 與 Discord 共用的多通道管理操作紀錄，預設寫入
  `/var/lib/palworld-manager/audit.log`（`PALWORLD_MANAGER_STATE_DIR` 可改變根目錄）。
- 每筆紀錄是單行、UTF-8、strict JSON；禁止 NaN/Infinity，並限制單行大小與讀取
  筆數。欄位名稱與文字內容都會遮蔽 token、password、secret、key、auth、cookie
  等 credential，讀取歷史紀錄時還會再次 sanitize。檔案由 manager 擁有、權限為
  `0640`，Web UI 只提供受限的最近紀錄檢視。

### Symlink 防護與路徑信任邊界

- 配置、audit、maintenance state、settings backup、lock、snapshot 與 live
  data 都要求 regular file/real directory、受控 owner/mode 與不重疊的絕對路徑；
  重要開啟使用 `O_NOFOLLOW`，temporary inode 建立後才以 descriptor 操作並
  `fsync`，拒絕 symlink、hard-link 異常與 mountpoint 失效。
- install/upgrade 將 Web 可寫範圍限縮到 manager-owned `config/editable/` 和
  `settings-backups/`；root-owned 配置根目錄、`secrets.env`、manager state
  父目錄與 systemd trust boundary 不可由 Web UI 帳號替換、刪除或重新導向。
- Python package 以 root-owned 完整 release 發布，再以 atomic symlink 切換
  `packages/current`，避免半套程式或不受信任的操作路徑進入執行流程。

### 測試與發布

- 新增 settings schema/persistence、audit log、Web restore/maintenance polling、
  Discord countdown/terminal notification 與 symlink/path boundary coverage；
  release package test 改以 v0.3.0 驗證 archive 命名、內容潔淨度、可重現性與
  `SHA256SUMS`。

## [0.2.0] - 2026-08-28

這個版本把既有 Linux 維運腳本背後的安全決策整理成可測試、可重用的
Python 核心，並加入僅限本機的 Web 管理介面。systemd、Bash、Discord 與
idle watcher 仍是平台 adapter／操作入口，但共用相同的 REST、生命週期、
備份與互斥保護契約。

### Python 核心與跨平台邊界

- 新增 `src/palworld_caretaker` 套件，提供設定解析與驗證、Palworld REST
  API client、服務生命週期、診斷、SteamCMD adapter，以及 Backup/Restore
  engine；`config.py`、`rest.py`、`backup.py`、`diagnostics.py`、
  `operations.py` 與 `web.py` 都不依賴第三方套件，systemd、shell 與 Discord
  差異留在入口 adapter。
- REST client 提供型別化的玩家、metrics、save、shutdown 與 announce 操作，
  嚴格驗證 loopback、redirect、transport error、HTTP 回應與 JSON schema；
  不確定的玩家狀態不會被當成「無人」。
- 保留既有 `scripts/palworld_manager.py` 與 Bash CLI 相容入口，讓既有部署
  可逐步切換至核心而不改變操作參數。

### 全域操作鎖與並行安全

- 新增跨行程 `OperationLock`，所有變更操作共用由 tmpfiles 預建的
  `/run/palworld-caretaker/operation.lock`。鎖檔以 root ownership、manager
  group、`0640`、regular-file 與 `O_NOFOLLOW` 契約驗證，再以 non-blocking
  `flock` 拒絕並行操作。
- backup、restore、update、graceful stop、maintenance、Web UI、Discord
  與 idle watcher 都在狀態檢查到實際變更的區段使用同一把鎖，避免 check-then-act
  race、重複關服或備份/更新交錯。
- 修復 Web UI restart 與子程序重入同一把 `flock` 所造成的死鎖：停止階段
  完成後先釋放 Python lock，再交由 root-owned control entry point 取得鎖；
  maintenance 的子腳本則明確沿用父程序已持有的鎖。

### Web UI 與安全性

- 新增 `scripts/palworld-web-ui.py` 與 `palworld-web-ui.service`，提供
  `127.0.0.1:8765` 的 service/REST/玩家/metrics 狀態、snapshot 清單、立即
  備份、正常停止與重啟操作；拒絕非 loopback bind，絕不設計為公開管理入口。
- 所有頁面與 API 先要求 HTTP Basic Auth；帳號由
  `PALWORLD_WEB_UI_USERNAME` 指定，密碼支援獨立的
  `PALWORLD_WEB_UI_PASSWORD`，未設定時相容地使用 `ADMIN_PASSWORD`。
- 變更請求要求 JSON、同源 `Origin` 與行程內 CSRF token，並加入 no-cache、
  CSP、`X-Frame-Options` 等瀏覽器防護；錯誤回應不洩漏 secrets 或內部命令。
- `secrets.env` 在安裝/升級時標準化為 `root:<PALWORLD_MANAGER_USER>`、
  `0640`；Web UI 帳密仍必須透過 Basic Auth 驗證，無密碼時拒絕啟動。

### 備份、還原與 preflight

- Backup/Restore 移入 Python engine：備份會先驗證來源、symlink、mount、
  寫入權限與保守可用空間需求，再寫入 `.incomplete-*` staging、fsync 內容，
  驗證 manifest 後以 atomic rename 發布 snapshot；失敗會清理 staging 並
  維持原服務狀態。
- 還原在停止服務或建立 safety copy 前驗證 snapshot 結構、檔案清單、大小、
  mount 與容量；覆寫 live SaveGames/Config 前建立最新外部 snapshot 及
  `backups-local/pre-restore-*` safety copy，部分提交失敗時可 rollback。
- 新增 `python3 scripts/palworld_manager.py --backup-preflight`，可在停止
  伺服器前獨立檢查備份來源、目的地與容量；daily maintenance 將其置於
  save/stop 之前，避免 preflight 失敗造成不必要的服務中斷。

### Discord、idle watcher 與維運流程

- Discord Bot 改用共用 Python 核心與生命週期流程，新增 `/pal backup`、
  `/pal backups` 與 `/pal diagnose`；`/pal start|status|players|stop|update`
  的安全關服、備份、更新與重啟共用操作鎖，並保留 allowlist、管理員確認與
  per-user/command cooldown。每個變更指令同時以全域 `flock` 與
  `systemd is-active` 維護狀態檢查保護。
- idle watcher 在最後一次玩家檢查、save 與 shutdown 之間持有全域鎖；鎖忙碌
  時延後重試，不會誤觸其他備份、更新或 Web 操作。
- 安裝器與升級器部署 operation lock/tmpfiles、Web UI 與新的核心檔案；Python
  核心以 root:root 的受控 release 發布到 `<install>/packages`，目錄/套件檔案
  維持 `0755`/`0644`，再以原子 symlink 切換 `packages/current`，避免半套更新。
  設定 preflight、diagnose、服務狀態 fail-closed，並保留非預設路徑與 legacy
  `palworld.env` 相容性。

### 測試與發布

- 測試套件擴充至 82 個 unit/integration/release tests，涵蓋核心 API、鎖、
  Web UI、Discord、idle watcher、備份/還原邊界、生命週期、安裝/升級/解除
  安裝與 reproducible release artifact；`82/82 tests PASS`。
- `pyproject.toml` 版本更新為 `0.2.0`，release packager 預設版本、文件與
  archive 命名同步至 `palworld-caretaker-v0.2.0.tar.gz`。

## [0.1.0] - 2026-08-27

首個公開 Linux release，將既有單機維運腳本整理成可驗證、可升級與可復原的
Ubuntu 24.04 systemd 部署工具鏈。

### 設定契約

- 新增 `caretaker.env`、`server.env`、`secrets.env` 三層設定；後層覆蓋前層，
  同時完整相容既有 `/srv/palworld/config/palworld.env`。
- 設定以資料解析，不執行 shell expansion。未知/重複鍵、相對或危險重疊路徑、
  非法數值與保留字元一律 fail closed；正式 secrets 不受 Git 追蹤並要求 mode
  `0600`。
- 安裝、服務、state 與備份位置皆由設定解析；安裝根目錄支援含空白及非 ASCII
  字元的任意安全絕對路徑，備份目的地可為本機或受 mount policy 保護的外部
  filesystem。

### Preflight 與動態 systemd

- 安裝器在第一次系統 mutation 前執行 value-only preflight，建立目標後再檢查
  路徑、symlink、權限、可寫性、secrets mode 與備份 mount。
- 升級器要求明確的已部署設定目錄，先驗證完整契約及安全路徑，再更換任何 live
  manager 元件；不推測或追加正式設定。
- 所有 systemd service/timer 從模板依部署路徑、帳號、REST port 與排程動態
  渲染，並以 systemd escaping 支援特殊路徑。
- REST API 固定為 localhost，搭配 IPv4/IPv6 firewall service 阻擋非 loopback
  TCP 存取；遊戲正常 shutdown 不會觸發 crash restart。

### 備份與還原保護

- 備份先驗證 mount、來源結構、目的地可寫性與保守空間需求；先寫入
  `.incomplete-*` staging，內容驗證完成後才原子發布具時間戳的 snapshot。
- 失敗時清除 incomplete staging 並恢復備份前的服務狀態；保留策略只刪除符合
  嚴格命名格式的最舊 snapshot。
- 還原在停止服務或覆寫 live data 前驗證 snapshot 結構與精確確認字串；覆寫前
  另建立最新外部 snapshot 與 `backups-local/pre-restore-*` 本機 safety copy。
- 每日維護在安全關服後依序備份、SteamCMD validate/update，並保留開始前的服務
  開關狀態；任何安全關服或備份失敗都會中止後續更新。

### 維運工具鏈

- 提供安裝、manager-only 升級、唯讀文字/JSON 診斷，以及 manager/game/all
  三級解除安裝；所有解除安裝層級都保留外部備份與 `backups-local`。
- 加入 REST 設定 renderer、graceful stop、update、daily maintenance、CLI 控制
  與 invocation-aware 閒置 watcher；API timeout、認證或資料格式異常都視為未知
  玩家狀態，不會誤觸自動關服。
- Discord `/pal start|status|players|stop|update` 採 guild/channel/role allowlist、
  管理員角色與破壞性操作確認；互動式設定工具隱藏 token 輸入、建立設定安全
  副本，並支援分層設定與非預設安裝路徑。
- 補齊安裝、舊版升級/失敗復原與 Discord Bot 最小權限/token 輪替文件。

### Release 與 CI

- 新增 deterministic release 打包工具，從指定 Git commit 建立
  `palworld-caretaker-v0.1.0.tar.gz` 與 `SHA256SUMS`，排除 `.git`、local env、
  cache、存檔、備份及其他未追蹤檔案，並在發布前自我驗證內容與 checksum。
- CI 執行 Python unit/integration tests、release artifact 驗證、Python compile、
  Bash syntax 與 ShellCheck；涵蓋設定/路徑契約、systemd renderer、生命週期、
  備份/還原失敗邊界、升級、解除安裝及 release 封包潔淨度。

[0.3.0]: https://github.com/PIGGYcloudy/palworld-caretaker/releases/tag/v0.3.0
[0.2.0]: https://github.com/PIGGYcloudy/palworld-caretaker/releases/tag/v0.2.0
[0.1.0]: https://github.com/PIGGYcloudy/palworld-caretaker/releases/tag/v0.1.0
