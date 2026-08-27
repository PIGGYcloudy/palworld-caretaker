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
| 中期 | 約 1–3 個月 | 建立產品化核心、診斷能力與本機 Web UI | v0.2–v0.4 |
| 長期 | 約 3–9 個月 | Docker、Windows、發佈與升級生態 | v1.0+ |

時間僅供安排工作量，不代表發布保證。

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

## 中期：產品化核心與操作體驗

### 1. 建立 Python 共用核心

逐步將可跨平台的邏輯從 Bash 移入可測試的 Python 模組：

- 設定讀取、驗證與 migration。
- SteamCMD 安裝及更新。
- 備份、還原與保留策略。
- 伺服器狀態與 Palworld REST API 操作。
- 診斷報告與排程任務定義。

Bash 保留為 Linux 安裝、權限及 systemd adapter。

### 2. Schema-backed 設定模型

採用 YAML 或 TOML，搭配 Pydantic 等 schema 驗證。CLI、Web API、面板、
Discord Bot 與平台 adapter 必須共用同一設定模型，不直接各自修改 env 或
`PalWorldSettings.ini`。

### 3. 本機 Web 管理面板

初期使用 FastAPI 搭配簡單的 server-rendered UI／HTMX，預設只監聽
`127.0.0.1`。第一版功能包含：

- 伺服器啟動、停止、更新及狀態。
- 在線玩家清單。
- 世界參數分類表單、搜尋、預設值與輸入驗證。
- 安裝及備份路徑設定。
- 自動更新、閒置關服與 Discord Bot 功能開關。
- 備份清單、立即備份及還原。
- Log 與診斷結果。
- 套用前差異預覽及自動備份。

### 4. 可觀察性與安全性

- 結構化 log 與管理操作 audit log，不加入遙測。
- secrets 遮蔽及診斷資料匯出預覽。
- 設定修改歷史。
- snapshot checksum 與定期還原驗證。
- Web UI session、CSRF 與權限防護。
- 遠端存取另行提供 HTTPS、認證與防火牆指引，不預設開放公網。

### 5. Discord 操作體驗

- 加入 `/pal backup`、`/pal backups` 與 `/pal diagnose`。
- 維護開始、完成與失敗通知。
- 更新前在線玩家警告。
- 指令冷卻、操作鎖與重複請求保護。
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
5. 擴充測試並發布 `v0.1.0`。

Web UI、Docker 與 Windows 先以 milestone 追蹤，避免 v0.1 範圍失控。

目前 repository 已完成前三項與第 4 項的首版：通用路徑／三層設定契約、可
設定且 fail-closed 的備份目的地、可重複執行的安裝／升級流程，以及唯讀
diagnose 與保護備份的分級 uninstall。下一批以實機 Ubuntu 升級／解除安裝
驗證、失敗 rollback 與 v0.1.0 發佈文件為主。
