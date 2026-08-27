# Discord Bot 設定

Caretaker 使用 Discord slash command，並以 guild、channel、一般角色與管理員
角色的數字 ID 做 fail-closed allowlist。私訊一律拒絕；allowlist 空白時所有
指令都會被拒絕。

## 建立 Application 與 Bot

1. 登入 [Discord Developer Portal](https://discord.com/developers/applications)，
   選擇 **New Application**，建立專用 Application。
2. 在 **General Information** 複製 **Application ID**，稍後設定工具會使用。
3. 進入 **Bot** 頁面確認 Bot user（新建立的 app 通常已啟用）。此 Bot 不需要
   讀取一般訊息內容，因此不需啟用 Message Content Intent；也不需開啟
   Presence 或 Server Members Intent。
4. 在 Bot 頁面選擇 **Reset Token**／取得 token。先不要貼到命令列參數、設定
   截圖、issue 或聊天；互動式工具會用隱藏輸入讀取。

## OAuth2 scope 與最小權限

依 [Discord 官方 Guild Install 指引](https://docs.discord.com/developers/quick-start/getting-started#adding-scopes-and-bot-permissions)，
邀請時只需要 scopes：

- `bot`
- `applications.commands`

Bot permissions 只需要：

- View Channels
- Send Messages

不需 Administrator、Manage Server、Manage Roles 或讀取訊息歷史。互動式工具
完成後會產生包含 `scope=bot applications.commands` 與最小權限值 `3072` 的
邀請網址；用具備 Manage Server 權限的 Discord 帳號開啟，選擇目標 server
並授權。若頻道有 permission override，仍需在該頻道允許 Bot 查看與傳送訊息。

## 取得 guild、channel 與 role ID

依 [Discord ID 官方說明](https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID)，
在 Discord 使用者設定的 **Advanced** 開啟 **Developer Mode**，然後：

- guild ID：在伺服器圖示按右鍵，選 **Copy Server ID**。
- channel ID：在允許操作的文字頻道按右鍵，選 **Copy Channel ID**。
- role ID：到 Server Settings → Roles，在一般使用者角色與管理員角色上分別
  按右鍵並選 **Copy Role ID**。

只填角色 ID，不填使用者 ID。ID 可用逗號分隔且不要加入空白。channel 可填
`*` 代表允許指定 guild 的所有頻道，但 guild 與角色仍必須明確指定。建議建立
專用的 `Palworld Player` 與 `Palworld Admin` 角色，不要直接使用權限過廣的
既有角色。

權限模型如下：

- `/pal start`、`/pal status`、`/pal players`：一般允許角色或管理員角色。
- `/pal stop confirm:true`、`/pal update confirm:true`：僅管理員角色，並要求
  明確的 `confirm:true`。

## 執行互動式設定工具

先完成 caretaker 安裝或升級，再執行（請替換實際路徑）：

```bash
sudo /usr/local/sbin/palworld-discord-configure \
  --config-dir '<PALWORLD_INSTALL_ROOT>/config'
```

預設 `/srv/palworld` 部署可寫成：

```bash
sudo /usr/local/sbin/palworld-discord-configure \
  --config-dir /srv/palworld/config
```

工具依序要求 Application ID、token、guild ID、channel ID、一般角色 ID 與管理員
角色 ID。token 輸入不回顯，不會成為命令列參數或 shell history；工具會更新
分層部署的 `secrets.env`/`server.env`，或相容部署的 `palworld.env`，先建立
`.pre-discord-YYYYMMDD-HHMMSS` 安全副本，再以受限權限原子替換設定。通過設定
驗證後才啟用 Bot，最後顯示邀請網址。

## 驗證

開啟工具產生的邀請網址並授權後，檢查：

```bash
sudo systemctl status palworld-discord-bot.service --no-pager
sudo journalctl -u palworld-discord-bot.service -n 100 --no-pager
```

全域 slash command 初次同步可能不會立刻出現。出現後，在允許頻道以一般角色
測試 `/pal status`、`/pal players`，並確認一般角色無法執行 stop/update；再以
管理員角色測試需要確認的操作。也應從未允許頻道、未允許角色與私訊各測一次
拒絕行為。

## Token 安全與輪替

- `secrets.env` 必須為 root 擁有、群組為 `PALWORLD_MANAGER_USER`、mode `0640`；不要提交、備份到公開位置、貼入
  issue、聊天、終端截圖或 log。
- 不要把 token 放在 shell 環境變數、命令列參數或自動化輸出。設定工具的隱藏
  prompt 是建議入口。
- 若 token 可能外洩，立即在 Developer Portal 的 Bot 頁面 **Reset Token**。
  舊 token 會失效；重新執行設定工具輸入新 token，再確認服務為 active。
- 輪替後檢查 journal，但不要開啟會輸出環境內容的除錯方式。舊
  `.pre-discord-*` 安全副本可能含舊 token；確認新設定正常後，應以 root 安全
  移除不再需要的副本。

Bot 無反應時，優先檢查 service journal、邀請 scopes、頻道 permission
override，以及 guild/channel/role ID 是否填成正確類型。切勿為排錯而授予
Administrator 權限或公開 Palworld REST port。
