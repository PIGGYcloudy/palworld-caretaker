# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## Unreleased

- 尚無變更。

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
