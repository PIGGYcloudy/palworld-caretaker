# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## Unreleased

- 尚無變更。

## [0.2.0] - 2026-08-28

這個版本把既有 Linux 維運腳本背後的安全決策整理成可測試、可重用的
Python 核心，並加入僅限本機的 Web 管理介面。systemd、Bash、Discord 與
idle watcher 仍是平台 adapter／操作入口，但共用相同的 REST、生命週期、
備份與互斥保護契約。

### Python 核心與跨平台邊界

- 新增 `src/palworld_caretaker` 套件，提供設定解析與驗證、Palworld REST
  API client、服務生命週期/診斷、SteamCMD adapter，以及 Backup/Restore
  engine；核心邏輯不依賴 systemd、shell 或第三方套件，平台差異留在入口
  adapter。
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

- Discord Bot 改用共用 Python 核心與生命週期流程，`/pal start|status|players|
  stop|update` 的安全關服、備份、更新與重啟共用操作鎖，並保留 allowlist、
  管理員確認與 per-user/command cooldown。
- idle watcher 在最後一次玩家檢查、save 與 shutdown 之間持有全域鎖；鎖忙碌
  時延後重試，不會誤觸其他備份、更新或 Web 操作。
- 安裝器與升級器部署 operation lock/tmpfiles、Web UI 與新的核心檔案；設定
  preflight、diagnose、服務狀態 fail-closed，並保留非預設路徑與 legacy
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

[0.1.0]: https://github.com/PIGGYcloudy/palworld-caretaker/releases/tag/v0.1.0
