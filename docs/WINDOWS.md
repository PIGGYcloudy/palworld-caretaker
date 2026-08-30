# Windows 原生部署與維運

v0.9.0 提供不依賴 systemd 的 Windows 原生 PowerShell 維運入口。下載並解壓
release archive 後，可從專案根目錄或已部署的 `scripts/windows/` 目錄執行；建議使用
PowerShell 7（`pwsh`）。這套入口與 Python 核心共用設定格式、備份命名與操作鎖，
但不會替 Windows 主機自動註冊 PalServer service。

## 雙擊首次啟動

解壓 release archive 後，直接雙擊根目錄的「啟動伺服器與管理面板.bat」。它會自動從三個
`.env.example` 建立缺少的 `caretaker.env`、`server.env`、`secrets.env`，建立可由精靈寫入的
`config/editable/`，並在目前 Python 環境尚未安裝套件時自動執行 `pip install -e .`。面板健康檢查
通過後會開啟瀏覽器；首次精靈會要求填入伺服器名稱與自訂密碼，儲存後立即寫入可用設定。
這個首次密碼同時取代範本的 `CHANGE_ME` 本機面板密碼；之後若需要分開管理，可在
`secrets.env` 設定獨立的 `PALWORLD_WEB_UI_PASSWORD`。

批次檔會在面板已可使用後才要求 UAC 啟動 `PalServer` service。因此 service 尚未安裝或名稱不符時，
首次精靈仍可完成；請依畫面提示修正 Windows service 後再啟動。

## 進階設定

設定檔是 UTF-8 `KEY=VALUE` 純文字，不會被 shell 執行或展開。沿用 `config/` 下的
三個 `.env.example` 檔案，將正式檔案放在例如 `C:\Palworld\config`，並調整 Windows
絕對路徑：

```env
PALWORLD_INSTALL_ROOT=C:\Palworld
PALWORLD_SERVER_ROOT=C:\Palworld\server
PALWORLD_BACKUP_DIR=C:\Palworld\server\Pal\Saved\SaveGames_Backups
PALWORLD_BACKUP_MOUNT=
PALWORLD_BACKUP_REQUIRE_MOUNT=false
PALWORLD_MANAGER_STATE_DIR=C:\ProgramData\Palworld\state
PALWORLD_WEB_BIND_IP=127.0.0.1
# Only needed for non-loopback LAN/VPN access:
# PALWORLD_WEB_ALLOWED_ORIGINS=http://192.168.1.20:8765
```

預設 PALWORLD_BACKUP_DIR 是遊戲存檔同級的 Pal\Saved\SaveGames_Backups；這是唯一允許放在 server root 內的備份位置。將
`secrets.env` 與 `caretaker.env`、`server.env` 放在同一個設定目錄，並只授予需要
執行維運的 Windows 帳號存取權。若路徑包含空白，可用單引號或雙引號包住整個值。

`PALWORLD_WEB_BIND_IP` 預設為 `127.0.0.1`，並可改為 `0.0.0.0` 或指定的 IPv4
以供受信任的 LAN/VPN 維運。新設定只應放在 `caretaker.env`；loader 仍相容讀取舊
`server.env` 的值。任何 non-loopback listener 都必須同時設定精確
`PALWORLD_WEB_ALLOWED_ORIGINS`，或 TLS proxy 的
`PALWORLD_WEB_PUBLIC_ORIGIN=https://pal.example.net`；需要不同 proxy Host 才加入
`PALWORLD_WEB_ALLOWED_HOSTS=pal.example.net`。Web UI 分別白名單檢查 Host 與
Origin/Referer，不接受動態 `Origin == Host`，可防 DNS rebinding。請以 TLS、VPN 或
防火牆保護任何非 loopback listener，勿將 plaintext Web UI 直接公開到 Internet。

若要在 Windows 執行 Web UI，從含有已安裝套件的 Python 環境執行：

```powershell
python -m palworld_caretaker.web --config-dir $ConfigDir
```

未指定 `--bind` 時，程式使用 `load_config` 合併後的 `PALWORLD_WEB_BIND_IP`；只有
`--bind` 會覆寫該值。

### Windows Web UI 支援範圍

Windows Web UI 可安全提供 REST 狀態、玩家公告／管理與設定編輯。
但原生 Windows 部署目前沒有等效的 systemd privileged workflow，因此「啟動／停止／
重啟」、「建立 backup」、「還原 snapshot」與「maintenance update」按鈕會明確回報不
支援；程式不會嘗試執行 `sudo`、`/usr/bin/systemctl` 或
`/usr/local/sbin/palworld-control`。請改用本文件的 PowerShell
`palworld-service.ps1`、`backup-palworld.ps1` 與 `restore-palworld.ps1` 完成這些
操作。

Palworld 的 live tree 預期位於：

```text
<PALWORLD_SERVER_ROOT>\Pal\Saved\SaveGames
<PALWORLD_SERVER_ROOT>\Pal\Saved\Config\LinuxServer\PalWorldSettings.ini
```

## 快速操作

解壓 release archive 後可以直接雙擊根目錄的「啟動伺服器與管理面板.bat」。首次雙擊會建立設定、檢查 Python、啟動 Web UI 並開啟本機面板，然後請求 UAC 啟動 PalServer Windows service。首次開啟時依 Web 的首次開服精靈設定伺服器名稱與密碼；系統不會自動生成或顯示密碼。

```powershell
$Repository = 'C:\palworld-caretaker'
$ConfigDir = 'C:\Palworld\config'

# 查詢既有 Windows service
pwsh -NoProfile -File "$Repository\scripts\windows\palworld-service.ps1" `
  -Action status -ServiceName PalServer

# 建立並驗證 snapshot
pwsh -NoProfile -File "$Repository\scripts\windows\backup-palworld.ps1" `
  -ConfigDir $ConfigDir

# 列出 snapshot，或依精確名稱互動式還原
pwsh -NoProfile -File "$Repository\scripts\windows\restore-palworld.ps1" `
  -ConfigDir $ConfigDir -List
pwsh -NoProfile -File "$Repository\scripts\windows\restore-palworld.ps1" `
  -ConfigDir $ConfigDir -Version palworld-YYYYMMDD-HHMMSS

# 將設定渲染至 PalWorldSettings.ini
pwsh -NoProfile -File "$Repository\scripts\windows\render-settings.ps1" `
  -ConfigDir $ConfigDir
```

還原省略 `-Force` 時會要求輸入 `RESTORE palworld-YYYYMMDD-HHMMSS`。在沒有服務
控制需求的測試環境可對備份／還原傳入 `-NoServiceControl`；
`palworld-service.ps1 -WhatIf` 可演練 start、stop、restart 而不觸碰 service。
生產維運應保留預設的 service control，並先確認 PalServer 服務名稱正確。

## 還原與安全邊界

還原先驗證 snapshot manifest 與所有 payload 的 SHA-256，再將資料複製到 staging
並重新比對檔案大小與 SHA-256；兩階段都通過後才會替換 live SaveGames/Config。
替換前會建立唯一的 `pre-restore-YYYYMMDD-HHMMSS-fff-GUID` Safety Copy。若 live
發布失敗，工具會自動從 Safety Copy rollback；即使 rollback 也失敗，Safety Copy
仍會保留供人工復原。

所有 Windows 路徑都必須是絕對路徑，不得包含 `..`、filesystem root、危險重疊或
symlink、junction、其他 reparse point。預設 operation lock 位於
`C:\ProgramData\Palworld\operation.lock`；PowerShell 與 Python 入口共用它，
不要刪除、替換或改成 junction/symlink。鎖定期間會持有父目錄的 Win32
No-Delete-Share handle，並以 128-bit `FILE_ID_INFO` 驗證目錄物件身分，避免同名
目錄替換或換鎖繞過互斥保護。
