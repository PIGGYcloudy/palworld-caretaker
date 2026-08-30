# 從既有 `/srv/palworld` 升級至 v0.9.0

升級器只更新 caretaker 管理程式、Python 環境、sudoers 與動態渲染的 systemd
units；不下載遊戲、不改寫設定層、不刪除世界存檔，也不碰外部備份目的地。
本指南假設既有部署的設定位於 `/srv/palworld/config`，包括舊式單一
`palworld.env` 或新的分層設定檔。

v0.9.0 將 REST、備份/還原、設定 schema、maintenance、audit log、跨行程鎖與
可設定 Web UI listener 的安全決策集中在 `src/palworld_caretaker/` Python 核心；systemd、
Bash 與 Discord 只作為平台入口與 adapter。升級會保留既有設定與世界資料，並讓
所有入口共用同一套 fail-closed 契約。

## 升級前備份

1. 確認目前服務與設定位置：

   ```bash
   sudo systemctl status palworld.service --no-pager
   sudo test -x /srv/palworld/server/PalServer.sh
   sudo test -f /srv/palworld/server/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini
   ```

2. 若舊版備份工具可用，先建立一份完整版本化備份並記下名稱：

   ```bash
   sudo /srv/palworld/scripts/backup-palworld.sh
   sudo /srv/palworld/scripts/restore-palworld.sh list
   ```

3. 另行保存目前 release 原始檔或版本號。若升級後要完整回到舊管理程式，必須
   重新執行舊 release 的升級器；v0.9.0 的自動 safety copy 保存設定、
   `PalWorldSettings.ini`、systemd units 與 sudoers，但不是舊程式碼封包。

請先確認備份目的地已掛載且最新 snapshot 含有 `savegames/`、`config/` 與
`metadata/manifest.txt`，再繼續。

## 驗證既有設定

下載並解壓 v0.9.0 release，從解壓目錄執行只讀驗證：

```bash
python3 scripts/palworld_manager.py \
  --config-dir /srv/palworld/config --no-filesystem
python3 scripts/palworld_manager.py \
  --config-dir /srv/palworld/config
```

舊式 `palworld.env` 仍受支援，升級不要求拆檔；若使用 root-level
`caretaker.env`/`server.env`，v0.9.0 會將可編輯 schema 欄位遷移到
`config/editable/`。若要自行重整其他設定，應在另一個維護時段依
[設定契約](CONFIGURATION.md) 操作，避免同時升級與重整設定。
不要以 `.example` 覆蓋正式設定。

### Docker Compose：v0.7 設定 volume 升級

v0.7 建立的 Docker `config/` volume 沒有
`PALWORLD_WEB_BIND_IP`。v0.9 會在 container mode 自動將這個**未宣告**的 internal
listener 補為 `0.0.0.0`，因此既有 `8765` port mapping 在升級後仍可連線；不必為了這項
相容性覆寫或重建 volume。若要明確寫入，請在 `config/caretaker.env` 設定：

```env
PALWORLD_WEB_BIND_IP=0.0.0.0
```

這與 host-side 發布位址不同：Compose 的 host-side 變數名稱是
`PALWORLD_WEB_PUBLISH_IP`，預設為 `127.0.0.1`。要從 LAN/VPN 存取，設定它為
`0.0.0.0`（或特定 host LAN IP），並同時設定精確的
`PALWORLD_WEB_ALLOWED_ORIGINS`；完整說明見 [Docker / Compose](DOCKER.md)。

## 執行升級

```bash
sudo bash ./upgrade-palworld-manager.sh \
  --config-dir /srv/palworld/config
```

升級器會先在
`/srv/palworld/backups-local/manager-upgrade-YYYYMMDD-HHMMSS/` 建立 mode
`0700` safety copy，之後才替換管理工具與 units。若遊戲原本運行，套用新的
unit 與 REST 設定後會重新啟動；原本關閉則保持關閉。Local Web UI 會啟用並預設
綁定 `127.0.0.1:8765`。Python 核心會先完整發布到
`/srv/palworld/packages/release-*`，再以原子 symlink 切換
`/srv/palworld/packages/current`；release 由 `root:root` 擁有，目錄/檔案權限
為 `0755`/`0644`。非 secret 的 schema 欄位會遷移至
`/srv/palworld/config/editable/caretaker.env` 與 `server.env`，由 manager 擁有、
目錄/檔案權限為 `0750`/`0640`；root-level 配置與 `secrets.env` 仍由 root 保護。
Discord token 未設定時 Bot 會保持停用。

非預設安裝路徑必須改傳實際部署設定目錄；升級器會拒絕 staging 設定或推測
路徑：

```bash
sudo bash ./upgrade-palworld-manager.sh \
  --config-dir '<PALWORLD_INSTALL_ROOT>/config'
```

## 升級後驗證

```bash
sudo /srv/palworld/scripts/diagnose-palworld.sh
sudo systemctl status palworld.service palworld-rest-firewall.service \
  palworld-idle-watcher.service palworld-backup.timer palworld-web-ui.service --no-pager
sudo journalctl -u palworld.service -n 100 --no-pager
sudo /srv/palworld/scripts/backup-palworld.sh
sudo /srv/palworld/scripts/restore-palworld.sh list
```

確認以下事項：設定檔內容與權限未變、世界仍可載入、REST port 只監聽
localhost、備份可建立且服務回到升級前的開關狀態。Discord 使用者另以
`/pal status` 的 `all`、`resources`、`game`、`players` 四個 section 驗證 allowlist
與狀態 embed；閒置 watcher 先維持 dry-run 觀察一個 lifecycle。

### 升級後使用 Local Web UI

確認 `palworld-web-ui.service` 為 active 後，在伺服器本機瀏覽器開啟
[`http://127.0.0.1:8765/`](http://127.0.0.1:8765/)：

```bash
sudo systemctl status palworld-web-ui.service --no-pager
```

頁面與 `/api/*` 都先要求 HTTP Basic Auth。帳號由
`PALWORLD_WEB_UI_USERNAME`（預設 `palworld-manager`）指定，密碼由
`PALWORLD_WEB_UI_PASSWORD` 指定；legacy 設定或留空時會相容地使用
`ADMIN_PASSWORD`。若要在升級後切換成獨立密碼，編輯已部署的
`/srv/palworld/config/secrets.env`，保持 `root:<PALWORLD_MANAGER_USER>`、`0640`，再執行：

```bash
sudo systemctl restart palworld-web-ui.service
```

備份、啟動、安全關閉與重啟按鈕都會受全域 operation `flock` 保護，並在實際
變更前再次確認 maintenance service 狀態；save 或狀態檢查失敗時會拒絕操作。
Web UI 預設監聽 `127.0.0.1`。遠端管理可使用 SSH tunnel 後仍開啟同一個 URL 並
輸入 Basic Auth：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@palworld-host
```

若 LAN/VPN 已由可信上游提供 TLS 與存取控制，可在 root-level `caretaker.env` 將
`PALWORLD_WEB_BIND_IP` 改為 `0.0.0.0` 或指定的 IPv4，並登錄精確來源後執行
`sudo systemctl restart palworld-web-ui.service`：

```env
PALWORLD_WEB_BIND_IP=0.0.0.0
PALWORLD_WEB_ALLOWED_ORIGINS=http://192.168.1.20:8765
# TLS reverse proxy: PALWORLD_WEB_PUBLIC_ORIGIN=https://pal.example.net
```

舊部署放在 `server.env` 的 bind 值仍相容讀取，但新範本只由 `caretaker.env` 宣告。
非 loopback listener 的 `Host` 與 mutation `Origin`／`Referer` 都必須命中明確
白名單，不能以動態 `Origin == Host` 比對，因而防範 DNS rebinding；它們仍不能取代
網路邊界，請勿直接公開 plaintext HTTP。

升級後可在「世界設定」區塊編輯型別化的遊戲與 caretaker 欄位；每個欄位都有「?」
說明及可立即填入系統預設值的「重置」按鈕。Web UI 會先顯示
差異，並在 `config/editable/` 建立設定 backup 後以 atomic write 套用；
`secrets.env` 不會出現在編輯器中。若伺服器正在運行，儲存後必須安全重啟才會
套用遊戲設定。

「SteamCMD 維護」按鈕會啟動固定的 `palworld-maintenance.service`，頁面每 10 秒
輪詢 `maintenance-state.json` 顯示關服、備份、更新、重啟及 terminal 結果。
維護中的其他變更操作會被拒絕。Web、CLI 與 Discord 操作會寫入預設的
`/var/lib/palworld-manager/audit.log`；這是 secret-masked、strict JSONL 的
manager-owned `0640` 檔案，完整命令輸出仍以 systemd journal 為準。

Discord v0.4.1 提供 `/pal announce`、`/pal kick`、`/pal ban`、`/pal backup`、
`/pal backups` 與管理員限定的 `/pal diagnose`；`/pal update confirm:true` 會在
有已知在線玩家時先發送 graceful shutdown 倒數公告，並在維護完成或失敗時通知。
`DISCORD_PALWORLD_ADMIN_ROLE_IDS`（`ADMIN_ROLE_IDS`）只授權 announce、kick、ban、
backup、diagnose、stop 與 update；一般角色或管理員角色可使用 start、status、
players 與 backups。`start`、`stop`、`backup` 與 `update` 會遵守 maintenance
guard 與全域 operation lock；announce、kick 與 ban 則受 Bot 操作鎖、冷卻與 audit
保護。

v0.4.1 的 `/pal status` 支援 `all`（預設）、`resources`、`game`、`players` slash
choices，並以 rich embeds 顯示資源與遊戲程序狀態。Bot 每 60 秒依
`PALWORLD_MEMORY_ALERT_PERCENT` 與 `PALWORLD_MEMORY_ALERT_COOLDOWN_SECONDS` 檢查
主機 RAM；新設定未指定時使用預設值，hysteresis 狀態保存於
`PALWORLD_MANAGER_STATE_DIR/alert-state.json`。主動警示需要明確的 Discord channel
ID，`*` 不會作為警示目標。

Web UI 另外提供遊戲內公告、線上玩家踢出／封鎖，以及 SaveGames ZIP 匯出。匯出會
先要求 REST API 存檔，受 `PALWORLD_SAVEGAMES_EXPORT_MAX_BYTES`（預設 8 GiB）、
symlink／regular-file 與可用空間檢查保護，下載後不保留暫存 archive；Web UI 預設
監聽 `127.0.0.1:8765`，並要求 Basic Auth 與 CSRF token。

升級後的 backup、restore、update、Web UI、Discord 與 idle watcher 會共用
`/run/palworld-caretaker/operation.lock`。若升級或另一項維護正在進行，並行
操作會拒絕；請等前一項操作完成後再重試。可先執行以下不會中斷服務的檢查：

```bash
sudo python3 /srv/palworld/scripts/palworld_manager.py \
  --config-dir /srv/palworld/config --backup-preflight
```

## 失敗復原

若升級命令失敗，先保留終端輸出並找出它列出的 `manager-upgrade-*` 目錄，不要
刪除該目錄或最新的外部 snapshot。

1. 停止管理元件，避免持續操作遊戲：

   ```bash
   sudo systemctl stop palworld-idle-watcher.service palworld-discord-bot.service
   ```

2. 從 safety copy 還原原設定、`PalWorldSettings.ini`、原 systemd units 與
   `palworld-manager` sudoers（只複製目錄中實際存在的檔案），然後執行：

   ```bash
   SAFETY=/srv/palworld/backups-local/manager-upgrade-YYYYMMDD-HHMMSS
   if [[ -f "$SAFETY/palworld.env" ]]; then
     sudo install -o root -g palworld-manager -m 0640 \
       "$SAFETY/palworld.env" /srv/palworld/config/palworld.env
   fi
   sudo install -o palworld -g palworld -m 0640 \
     "$SAFETY/PalWorldSettings.ini" \
     /srv/palworld/server/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini
   for unit in "$SAFETY"/palworld*.service "$SAFETY"/palworld*.timer; do
     [[ ! -f "$unit" ]] || sudo cp -a "$unit" /etc/systemd/system/
   done
   if [[ -f "$SAFETY/palworld-manager" ]]; then
     sudo install -o root -g root -m 0440 "$SAFETY/palworld-manager" \
       /etc/sudoers.d/palworld-manager
     sudo visudo -cf /etc/sudoers.d/palworld-manager
   fi
   sudo systemctl daemon-reload
   sudo systemctl restart palworld.service
   ```

   分層部署應以相同方式逐一還原 safety copy 中實際存在的 `caretaker.env`、
   `server.env`、`secrets.env`；`secrets.env` 使用 root:`PALWORLD_MANAGER_USER`、mode `0640`。若某個 glob 或檔案
   不存在就跳過，不要自行從其他 snapshot 猜測補入。

3. 使用先前保存的舊 release 重新執行其升級/安裝管理元件流程，恢復相符的腳本
   與 Python dependencies。不要把新 unit 與舊腳本混用。

4. 若世界資料本身無法載入，使用 v0.4.1 還原工具列出 snapshot，再以精確確認
   字串執行還原：

   ```bash
   sudo /srv/palworld/scripts/restore-palworld.sh list
   sudo /srv/palworld/scripts/restore-palworld.sh restore palworld-YYYYMMDD-HHMMSS
   ```

還原流程會先驗證 snapshot 結構、要求輸入
`RESTORE palworld-YYYYMMDD-HHMMSS`，建立新的外部 snapshot 與本機
`backups-local/pre-restore-*` safety copy，完成後才覆寫 live SaveGames/Config；
原本運行的服務會嘗試恢復運行。若備份 mount 遺失、snapshot 不完整或安全
備份失敗，live 資料不會進入覆寫階段。
