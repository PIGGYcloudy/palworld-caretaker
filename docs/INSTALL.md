# 安裝指南

本指南適用於全新的 Palworld Dedicated Server。安裝器會安裝 SteamCMD、建立受
限系統帳號、下載遊戲、渲染 systemd units，並啟用遊戲、REST 防火牆、閒置
監看及備份排程。Discord Bot 只有在 token 已設定時才會啟動。

## 系統需求

- Ubuntu 24.04 LTS amd64，使用 systemd 與 APT；其他發行版尚未列入 v0.1.0
  的支援範圍。
- 具 `sudo` 權限的登入帳號，以及可連線至 Ubuntu 套件庫、Steam 與 Discord
  （若啟用 Bot）的網路。
- 足以容納遊戲、世界資料及升級安全副本的本機空間；備份目的地還需至少有
  「目前 SaveGames 與 Config 合計兩倍，再加 1 GiB」的可用空間。
- 對外開放遊戲 UDP port（預設 `8211`）時需自行設定路由器或雲端防火牆。
  REST TCP port（預設 `8212`）必須維持 localhost-only，不可公開。

安裝會加入 i386 architecture，並透過 APT 安裝 `steamcmd:i386`、
`python3-venv` 與 `iptables`。Valve 授權條款仍由操作者在 APT 流程中確認。

## 準備設定

先把 `palworld-caretaker-v0.1.0.tar.gz` 與 `SHA256SUMS` 放在同一目錄，驗證並
解壓：

```bash
sha256sum --check SHA256SUMS
tar -xzf palworld-caretaker-v0.1.0.tar.gz
cd palworld-caretaker-v0.1.0
```

驗證必須顯示 `OK`。接著在專案根目錄建立不受 Git 管理的部署設定：

```bash
mkdir -p deployment-config
cp config/caretaker.env.example deployment-config/caretaker.env
cp config/server.env.example deployment-config/server.env
cp config/secrets.env.example deployment-config/secrets.env
chmod 0600 deployment-config/secrets.env
```

編輯三個檔案：

- `caretaker.env`：安裝根目錄、系統帳號、備份目的地、保留數與排程。
- `server.env`：遊戲、REST API、閒置關服與 Discord allowlist。
- `secrets.env`：伺服器密碼、管理密碼與可選的 Discord Bot token。

`SERVER_PASSWORD` 與 `ADMIN_PASSWORD` 必須替換 placeholder，且不能含
`,`、`(`、`)`、`"` 或換行。尚未設定 Discord 時可保留 token placeholder；
Bot 不會啟動。完整欄位、優先順序與安全限制見
[設定契約](CONFIGURATION.md)。

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
  palworld-idle-watcher.service palworld-backup.timer --no-pager
sudo journalctl -u palworld.service -n 100 --no-pager
sudo ss -lntp | grep ':8212'
```

確認診斷沒有 `fail`、遊戲服務可啟動、REST port 只在 loopback 監聽，並從
Palworld client 連線測試 UDP port。接著手動建立並列出第一份備份：

```bash
sudo "<PALWORLD_INSTALL_ROOT>/scripts/backup-palworld.sh"
sudo "<PALWORLD_INSTALL_ROOT>/scripts/restore-palworld.sh" list
```

閒置監看預設應先保持 `PALWORLD_IDLE_WATCHER_DRY_RUN=true`；觀察
`journalctl -u palworld-idle-watcher.service` 確認判定正確後，再改為 `false`
並重啟 watcher。Discord 設定與驗證請接續閱讀
[Discord Bot 設定](DISCORD_SETUP.md)。
