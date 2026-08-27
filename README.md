# Palworld Server Caretaker

以 SteamCMD、systemd 與官方 REST API 管理 Palworld Dedicated Server 的
Linux 維運工具，重點是安全關服、可恢復備份、閒置自動關服與 Discord
遠端操作，不需要常駐公開的 Web 管理面板。

> [!IMPORTANT]
> v0.1.0 支援 Ubuntu 24.04 LTS amd64 的原生 systemd 部署。部署設定契約、
> 安裝器、備份/還原/更新腳本與 systemd unit 支援任意安全的絕對安裝與備份
> 路徑。正式存檔仍建議先在測試主機完成安裝、備份與還原演練。

## 目前功能

- SteamCMD 安裝、驗證與更新 Palworld Dedicated Server。
- systemd 管理、崩潰有限重啟與 localhost-only REST API 防護。
- 安全存檔、正常關服、可設定目的地的版本化備份與互動式還原。
- 無玩家逾時後再次確認，再執行存檔與正常關服。
- Discord `/pal` 指令、guild/channel/role allowlist 與管理員確認。
- 每日維護保留原本開關服狀態，失敗時避免強制關服或無聲資料損失。

從零部署請見 [`docs/INSTALL.md`](docs/INSTALL.md)，既有 `/srv/palworld` 部署
請見 [`docs/UPGRADE.md`](docs/UPGRADE.md)，Discord Bot 的完整建立、權限與
token 安全流程見 [`docs/DISCORD_SETUP.md`](docs/DISCORD_SETUP.md)。後續產品
方向另列於 [`docs/ROADMAP.md`](docs/ROADMAP.md)。

## 架構

```text
palworld.service（遊戲，Restart=on-failure）
  └─ localhost:8212 官方 REST API
       ↑                         ↑
idle watcher（永久在線）      Discord Bot（永久在線）
  └─ 無人逾時→再查詢→save→shutdown  └─ /pal start|status|players|stop
```

三個服務彼此獨立。遊戲正常關閉不會停止 Bot 或 watcher；Bot 的 `/pal start` 只會透過 root 擁有、參數固定的 `/usr/local/sbin/palworld-control start` 啟動 `palworld.service`，不會執行 Discord 訊息或任意 shell 指令。

## 安全與設定

新設定範本依用途拆成 `config/caretaker.env.example`、
`config/server.env.example` 與 `config/secrets.env.example`，規範與 preflight
用法見 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)。既有
`/srv/palworld/config/palworld.env` 仍是完整支援的相容來源。正式設定檔已由
Git 排除，不可提交。
升級器會先在 `<PALWORLD_INSTALL_ROOT>/backups-local/manager-upgrade-*` 建立受保護
的設定與 manager safety copy。它不會改寫或追加任何設定層，也不會碰觸存檔與
外部備份。

```env
PALWORLD_REST_API_HOST=127.0.0.1
PALWORLD_REST_API_PORT=8212
PALWORLD_REST_API_USERNAME=admin
PALWORLD_API_TIMEOUT_SECONDS=5
PALWORLD_IDLE_SHUTDOWN_ENABLED=true
PALWORLD_IDLE_TIMEOUT_MINUTES=10
PALWORLD_PLAYER_CHECK_INTERVAL_SECONDS=60
PALWORLD_STARTUP_GRACE_SECONDS=600
PALWORLD_SHUTDOWN_WAIT_SECONDS=30
PALWORLD_IDLE_WATCHER_DRY_RUN=true
PALWORLD_START_READY_TIMEOUT_SECONDS=180
DISCORD_BOT_TOKEN='...'
DISCORD_PALWORLD_ALLOWED_GUILD_IDS=123
DISCORD_PALWORLD_ALLOWED_ROLE_IDS=456
DISCORD_PALWORLD_ADMIN_ROLE_IDS=789
DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS=101112
```

ID 清單可用逗號分隔。`DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS=*` 表示指定 guild 內所有頻道；guild 與角色仍必須明確指定。allow list 為空時採 fail-closed，所有指令都拒絕；私訊一律拒絕。`start`、`status`、`players` 需要允許角色或管理員角色，`stop` 只接受管理員角色且必須 `confirm:true`。權限只比較 Discord guild/channel/role 的數字 ID。

官方 REST API 會由 renderer 設為 `RESTAPIEnabled=True` 和指定連接埠。`palworld-rest-firewall.service` 同時以 IPv4/IPv6 firewall 阻擋非 loopback 對 REST TCP port 的連線；切勿在路由器、雲端防火牆或其他 proxy 公開 8212。遊戲 UDP 8211 的既有映射不變。

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
- `/pal status`：顯示服務/API、玩家數、uptime、idle 開關與剩餘時間。
- `/pal players`：顯示玩家數與名稱；API 異常顯示未知。
- `/pal stop confirm:true`：管理員限定，成功 save 後才送出 graceful shutdown。
- `/pal update confirm:true`：啟動備份與 SteamCMD 更新。Bot 會以同一則嵌入式訊息顯示安全關服、備份、更新與重啟進度；會保留更新開始前的開關服狀態。

Bot 採 slash command，不會重複註冊文字指令。全域 slash command 初次同步可能需要 Discord 一段時間顯示。

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
chmod 0600 ./deployment-config/secrets.env
# 編輯 deployment-config/*.env 後：
sudo bash ./install-palworld.sh --config-dir "$PWD/deployment-config"
sudo "<PALWORLD_INSTALL_ROOT>/scripts/render-settings.sh"
sudo systemctl restart palworld.service palworld-idle-watcher.service
sudo systemctl enable --now palworld-discord-bot.service
```

升級既有管理元件而不重新下載遊戲時，必須明確提供已部署的設定目錄；安裝
根目錄、帳號、state 與所有衍生路徑都從三層設定契約解析：

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
```

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

```bash
sudo "<PALWORLD_INSTALL_ROOT>/scripts/restore-palworld.sh"
sudo "<PALWORLD_INSTALL_ROOT>/scripts/restore-palworld.sh" restore palworld-YYYYMMDD-HHMMSS
```

`palworld.service` 維持 `Restart=on-failure` 並將正常結束碼 0、130、143 視為成功。官方 graceful shutdown 正常退出時不會被拉起；crash 才有限度重啟。若實機版本的 API shutdown 產生其他 exit code，先保持 dry-run 並以 journal 驗證，必要時將該已確認的正常碼加入 `SuccessExitStatus`。

## 開發與安全

提交變更前請閱讀 [`CONTRIBUTING.md`](CONTRIBUTING.md)，安全邊界與漏洞回報
方式見 [`SECURITY.md`](SECURITY.md)。GitHub Actions 會執行 Python 測試、Python
編譯、Bash 語法檢查、ShellCheck 與 release 產物驗證。從乾淨且已提交的
v0.1.0 source tree 建立 tarball 與 checksum：

```bash
scripts/package-release.sh --version 0.1.0 --output-dir dist
(cd dist && sha256sum --check SHA256SUMS)
```

本專案採用 [GNU General Public License v3.0](LICENSE) 授權。你可以依照
GPL-3.0 的條款使用、修改及散布本專案；散布衍生作品時也必須保留相同授權。
