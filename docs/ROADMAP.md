# Roadmap

本專案維持 **Linux-first、可靠維運工具** 的定位：先完成可安全公開使用的
Linux 版本，再逐步加入統一設定核心、Web 管理面板、Docker 與 Windows。

Roadmap 是方向與優先順序，不是固定承諾；實際版本內容會依測試結果、
Palworld Dedicated Server 變更與社群回饋調整。

## 發展原則

- 存檔安全、可還原性與失敗保護優先於新增功能。
- secrets、一般設定與執行資料必須分離。
- 任何會改動正式設定、更新遊戲或還原存檔的操作，都應先驗證並留下備份。
- 共用邏輯逐步移至可測試的 Python 核心；平台差異留在 adapter。
- Web UI 預設只監聽 localhost，不預設暴露到公網。
- Docker 是可選部署 backend，不取代原生 Linux。
- Windows 支援建立在穩定的共用核心與明確的平台抽象之上。

## 階段總覽

| 階段 | 預估範圍 | 主要目標 | 里程碑 |
| --- | --- | --- | --- |
| 短期 | 約 2–4 週 | 從個人部署整理成通用 Linux 工具 | v0.1.0 |
| 中期 | 約 1–3 個月 | 建立產品化核心、診斷能力與本機 Web UI | v0.2.0–v0.4 |
| 長期 | 約 3–9 個月 | Docker、Windows、發佈與升級生態 | v1.0+ |

時間僅供安排工作量，不代表發布保證。

## v0.2.0 狀態

v0.2.0 已完成中期里程碑的第一批核心工作：

- `src/palworld_caretaker` 可測試的 Python 核心，將 REST、生命週期、設定、
  SteamCMD 與 Backup/Restore 決策從 Linux shell 入口分離。
- `OperationLock` 與 `/run/palworld-caretaker/operation.lock` 的跨行程互斥，
  已整合 Web UI、Discord、idle watcher、timer 與 maintenance，並修復
  restart 子程序重入造成的死鎖。
- loopback-only Web UI、HTTP Basic Auth、CSRF、同源與瀏覽器安全 headers。
- Backup/Restore 原子發布、manifest/容量/mount preflight，以及
  `--backup-preflight` 維護前檢查。
- 82 個測試全數通過，包含核心、整合流程與可重現 release artifact 驗證。

後續 v0.3–v0.4 聚焦於設定 schema/migration、診斷與 audit 可觀察性、Web UI
的設定與 log 體驗，以及更多 Discord 操作；Docker 與 Windows 仍屬長期目標。

## v0.3.0 狀態

v0.3.0 完成了上述下一批的核心操作體驗：

- Web UI 提供型別化的 in-game settings manager，使用
  `config/editable/caretaker.env` 與 `server.env` 的分層配置、差異預覽、
  settings backup 與 atomic write；secret layer 維持 root 保護。
- CLI 與 Web UI 共用 restore preflight、外部 pre-restore snapshot、
  `backups-local/pre-restore-*` safety copy、manifest 驗證、容量檢查與 atomic
  restore/rollback；完成後維持原本的服務開關狀態。
- Web UI 可觸發固定的 `palworld-maintenance.service`，每 10 秒輪詢並呈現
  preflight、關服、備份、更新、重啟與 terminal 狀態；Discord 在維護前公告
  倒數，並發送完成/失敗通知。
- Web、CLI 與 Discord 共用 `/var/lib/palworld-manager/audit.log` 的 strict
  JSONL 操作紀錄，寫入與顯示前都遮蔽 secrets。
- 關鍵檔案與目錄採 regular-file、owner/mode、mount、`O_NOFOLLOW` 與 atomic
  rename 檢查；Web service 的可寫範圍限縮在 manager-owned editable/settings
  backup 路徑。

後續 v0.4 聚焦於更多遊戲設定欄位、audit rotation/匯出、診斷報告、migration
工具與實機升級驗證；Docker 與 Windows 仍屬長期目標。

## 短期：Linux v0.1

### 1. 移除主機硬編碼

將以下假設改為安裝參數或設定值：

- `/srv/palworld` 安裝位置。
- `/mnt/qnap-tyt` 與固定 NAS 備份路徑。
- SteamCMD 位置。
- REST API 與遊戲 port。
- 備份排程與保留數量。
- Ubuntu 套件名稱及支援版本。

第一版可以使用 CLI 參數或設定檔，不必等待 Web 面板。

### 2. 建立清楚的設定邊界

逐步將設定拆分為：

```text
config/
├── caretaker.env       # 路徑、排程與功能開關
├── server.env          # 世界與伺服器參數
└── secrets.env         # 密碼與 Discord Token
```

必要行為：

- secrets 永不提交至 Git，也不出現在一般診斷輸出。
- install 與 upgrade 不覆蓋現有 secrets、世界設定或存檔。
- 新增設定鍵時可安全補上預設值。
- 設定無效時，在改動正式環境前停止。

### 3. 備份目的地不再依賴 NAS

備份目的地可以是本機磁碟、外接硬碟，或由使用者自行掛載的 NFS／SMB／NAS。

必須保留目前的安全特性：

- 指定為 mount 的目的地消失時，不得改寫到本機 mountpoint 目錄。
- 先寫入 staging，完整驗證後才原子化發布 snapshot。
- 寫入前檢查來源與可用空間。
- 失敗後清理不完整資料並恢復原本服務狀態。
- 還原前自動建立安全備份。

### 4. 完整安裝生命週期

提供四個清楚入口：

```text
install
upgrade
diagnose
uninstall
```

安裝及升級必須可重複執行。解除安裝至少區分：

- 只移除管理工具，保留遊戲、設定、存檔與備份。
- 移除遊戲程式，保留存檔與備份。
- 完整移除必須額外確認並明示資料影響。

### 5. 擴充測試

在現有 API 與設定渲染測試之外，加入：

- 備份成功、失敗與 staging 清理。
- 空間不足與備份目的地消失。
- 不存在或不完整 snapshot 的還原拒絕。
- 設定合併與 secrets 保留。
- install／upgrade 重複執行。
- systemd 或 SteamCMD 失敗。
- Discord guild、channel、role allowlist 與管理員權限。
- 路徑含空白或特殊字元。

### 6. 文件與發佈

v0.1 文件至少涵蓋：

- 全新安裝與系統需求。
- 從既有 `/srv/palworld` 升級。
- 設定、啟停、更新、備份與還原。
- Discord Bot 完整設定教學。
- 診斷與常見問題。
- 安全解除安裝。

### v0.1.0 完成條件

- 在乾淨 Ubuntu 24.04 完成全新安裝。
- 重複安裝不破壞設定與存檔。
- 既有部署升級成功。
- 任意本機路徑的備份與還原實機驗證成功。
- 所有 CI、ShellCheck 與單元測試通過。
- repository 不含 secrets、真實存檔或私人備份。
- 文件足以讓未參與開發的人獨立完成基本部署。
- 建立 `v0.1.0` tag、GitHub Release、checksum 與 release notes。

## 中期：產品化核心與操作體驗（v0.2.0–v0.3.0 已完成）

### 1. 建立 Python 共用核心（v0.2.0–v0.3.0 已完成）

v0.2.0 完成第一批移轉，v0.3.0 再把設定、還原與操作紀錄納入共用核心；後續
持續擴充核心與 adapter 邊界：

- 已完成：設定讀取與驗證、SteamCMD adapter、備份/還原與保留策略、
  伺服器狀態、Palworld REST API client、診斷資料模型、typed settings schema、
  atomic restore、audit log 與 maintenance state。
- 後續：設定 migration、診斷報告與排程任務的通用模型，以及更多平台 adapter。

Bash 保留為 Linux 安裝、權限及 systemd adapter。

### 2. Schema-backed 設定模型（v0.3.0 已完成第一版）

v0.3.0 先以無第三方依賴的 Python schema 建立共用型別邊界；Web UI、CLI、
Discord Bot 與平台 adapter 讀取同一套分層 env 契約，不直接各自修改
`PalWorldSettings.ini`。下一步再處理向前 migration、schema 版本與更多可編輯欄位。

### 3. 本機 Web 管理面板（v0.3.0 已完成核心操作）

目前版本使用無第三方依賴的 server-rendered UI，預設只監聽
`127.0.0.1:8765`。v0.2.0 提供狀態、玩家、metrics、snapshot、備份、安全停止
與重啟；v0.3.0 再加入設定編輯、restore、maintenance trigger、進度輪詢與
audit log 檢視；後續面板工作包括：

- 更完整的維護進度、診斷結果與 audit 篩選/匯出。
- 世界參數搜尋、預設值、版本化 schema 與更多欄位。
- 安裝及備份路徑設定。
- 自動更新、閒置關服與 Discord Bot 功能開關。
- 更完整的備份管理與還原演練。

### 4. 可觀察性與安全性（v0.3.0 已完成核心）

- Web、CLI、Discord 的 strict JSON audit log 與 secrets 遮蔽已完成，不加入遙測。
- `O_NOFOLLOW`、real-path/owner/mode、mount trust boundary 與 manager writable
  path isolation 已完成。
- 後續加入 audit rotation、診斷資料匯出預覽與不可否認性更強的保存策略。
- 設定修改歷史。
- snapshot checksum 與定期還原驗證。
- Web UI Basic Auth、CSRF、同源、no-cache 與禁止嵌入防護；後續再補 session
  與更細緻的權限模型。
- 遠端存取另行提供 HTTPS、認證與防火牆指引，不預設開放公網。

### 5. Discord 操作體驗（v0.3.0 已完成維護流程）

- 加入 `/pal backup`、`/pal backups` 與 `/pal diagnose`。
- 維護前在線玩家倒數公告，以及完成與失敗通知。
- 更新前在線玩家警告。
- 指令冷卻、全域操作鎖、重複請求保護與維護進度輪詢已完成；後續補充更多
  診斷與 audit 查詢指令。
- Discord、CLI 與 Web UI 共用相同的操作核心與授權判斷。

## 長期：跨平台與 v1.0

### 1. Docker backend

Docker 作為原生 Linux 以外的可選部署方式：

- 提供 Compose 範例、health check 與 volume 路徑驗證。
- 處理 UID／GID 與存檔權限。
- 備份不得依賴 container filesystem。
- 原生部署與 Docker 共用設定模型、備份格式與管理 API。
- 明確定義 image 更新、回滾與版本相容策略。

Docker 可以作為 Windows 的過渡選項，但不等同完整 Windows 原生支援。

### 2. Windows 原生 adapter

| 功能 | Linux | Windows |
| --- | --- | --- |
| 服務 | systemd | Windows Service |
| 排程 | systemd timer | Task Scheduler |
| 防火牆 | nftables／iptables | Windows Firewall |
| 權限 | user／group | ACL |
| 安裝 | shell／deb | PowerShell／installer |
| Log | journald | 檔案／Event Log |

備份、REST API、設定、Discord 與 SteamCMD 邏輯由 Python 核心共用。

### 3. 正式發佈體系

- Semantic Versioning 與自動產生 changelog。
- Release artifact checksum，後續評估簽章。
- Linux tarball／deb 與 Windows zip／installer。
- 設定 migration、升級前備份及失敗 rollback。
- 支援版本矩陣、安全公告與維護週期。

### v1.0 完成條件

- Linux 原生部署穩定且具備升級及 rollback 流程。
- Docker backend 穩定。
- Windows 至少有一條正式支援的安裝路徑。
- 設定可向前 migration。
- 備份與還原經過多種目的地實機驗證。
- Web 面板具備清楚的認證、授權與網路安全邊界。
- 文件足以讓新使用者部署、維護、升級與復原。

## 下一個工作批次

下一輪依序處理，完成後才進入 Web UI、Docker 或 Windows：

1. 定義通用路徑與設定規格。
2. 將 NAS 專用備份改成可設定目的地。
3. 讓 install 與 upgrade 可安全重複執行。
4. 加入 diagnose 與分級 uninstall。
5. 發布 `v0.3.0` 後，依實機回饋進入 schema migration、audit rotation、診斷
   與 adapter 測試。

Web UI、Docker 與 Windows 先以 milestone 追蹤，避免 v0.1 範圍失控。

目前 repository 已完成 v0.3.0 發布所需的通用路徑／分層設定契約、typed
settings manager、可設定且 fail-closed 的備份目的地、pre-restore safety
backup、atomic restore、可重複執行的安裝／升級流程、唯讀 diagnose、分級
uninstall、Web maintenance、Discord 維護通知、audit log、全域操作鎖與
loopback Web UI。下一批以實機 Ubuntu 升級／解除安裝驗證、schema migration、
audit rotation 與更完整的跨平台 adapter 為主。
