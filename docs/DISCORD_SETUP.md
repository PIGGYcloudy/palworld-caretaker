# Discord Bot 設定

v0.4.1 Caretaker 使用 Discord slash command，並以 guild、channel、一般角色與管理員
角色的數字 ID 做 fail-closed allowlist。私訊一律拒絕；allowlist 空白時所有
指令都會被拒絕。

## 建立 Application 與 Bot

## Web 面板的 4 步快速嚮導

Web 面板的「Discord 4 步嚮導」適合第一次設定：建立 Bot、貼上 Bot Token、填入 Application ID 後按「一鍵邀群」、最後貼上要使用的文字頻道 ID。Token 輸入後不會再顯示。

嚮導不會猜測 guild 或角色，也不會建立好友連線資訊。為維持 fail-closed 權限模型，仍必須依下文填入 guild ID、一般角色與管理員角色 ID，再啟動 Bot。

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

- `/pal start`、`/pal status [section]`、`/pal players`、`/pal backups`：一般允許角色或
  管理員角色。
- `/pal announce`、`/pal kick`、`/pal ban`、`/pal backup`、`/pal diagnose`：僅
  管理員角色。
- `/pal stop confirm:true`、`/pal update confirm:true`：僅管理員角色，並要求
  明確的 `confirm:true`。

`ADMIN_ROLE_IDS` 是 `DISCORD_PALWORLD_ADMIN_ROLE_IDS` 的簡稱；這個設定必須包含
可執行管理員指令的 Discord role ID。Discord 的 `Administrator` app permission
不會繞過 Caretaker 的角色檢查。`DISCORD_PALWORLD_ALLOWED_ROLE_IDS` 可包含一般
操作角色；管理員角色也會自動取得一般指令權限。

## v0.4.1 指令

| 指令 | 參數 | 說明 |
| --- | --- | --- |
| `/pal start` | 無 | 啟動服務並等待 REST API ready |
| `/pal status` | `section`（預設 `all`） | 以 rich embed 查看選定狀態區段 |
| `/pal players` | 無 | 查看目前在線玩家 |
| `/pal backups` | 無 | 列出最近最多 10 個可用快照與大小 |
| `/pal announce` | `message` | 發送遊戲內公告（管理員） |
| `/pal kick` | `player_name_or_id`、`reason`（可選） | 踢出玩家（管理員） |
| `/pal ban` | `player_name_or_id`、`reason`（可選） | 封鎖玩家（管理員） |
| `/pal backup` | 無 | 建立並驗證安全 snapshot（管理員） |
| `/pal diagnose` | 無 | 顯示 secret-masked 健康摘要（管理員） |
| `/pal stop` | `confirm:true` | 存檔後正常關服（管理員） |
| `/pal update` | `confirm:true` | 備份、更新並重啟（管理員） |

`kick` 與 `ban` 可輸入精確在線玩家名稱或 user/Steam ID；若名稱重複，請改用
ID。`announce`、`kick` 與 `ban` 會遵守 per-user cooldown、Bot 操作鎖與 audit；
`start`、`stop`、`backup` 與 `update` 另外受 maintenance guard 與全域 operation
lock 保護。診斷與快照清單不會在回覆中顯示 token 或密碼。

### `/pal status` 區段

`section` 是 Discord slash choice，值只能是 `all`、`resources`、`game` 或
`players`；不填時使用 `all`。回覆為只對執行者可見的 ephemeral rich embed：

| 區段 | 顯示內容 |
| --- | --- |
| `all` | 服務、REST API、在線玩家、idle 狀態／剩餘時間與資源總覽 |
| `resources` | 主機 RAM、Palworld RSS、1 分鐘 CPU load、存檔磁碟 |
| `game` | systemd 服務、REST API、精確匹配的 Palworld 程序 uptime |
| `players` | 在線玩家清單；REST 無法取得時顯示未知 |

主機與程序資源是 Linux procfs 的 best-effort telemetry。讀取有大小上限，且只匹配
`PalServer`、`PalServer-Linux-Test`、`PalServer-Linux-Shipping` 這些精確 executable
basename；無法取得或格式錯誤的欄位顯示未知／未偵測到。

### 主動記憶體警示

Bot 每 60 秒檢查主機 RAM。`PALWORLD_MEMORY_ALERT_PERCENT`（預設 `85`，有效範圍
10–99）設定通知門檻；`PALWORLD_MEMORY_ALERT_COOLDOWN_SECONDS`（預設 `1800`，有效
範圍 60–86400）限制恢復後再次跨越門檻的通知頻率。Bot 只在達到或超過門檻時通知，
持續達到或超過門檻不重複通知，並在恢復到門檻以下後重新啟用；hysteresis 狀態原子保存
於 `PALWORLD_MANAGER_STATE_DIR/alert-state.json`。警示必須有明確的
`DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS`；設定為 `*` 或留空時不會猜測頻道或發送
主動通知。

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
測試 `/pal status` 的四個 section、`/pal players`、`/pal backups`，並確認一般角色無法執行
`announce`、`kick`、`ban`、`backup`、`diagnose`、`stop` 或 `update`；再以管理員
角色測試管理操作。也應從未允許頻道、未允許角色、錯誤 guild 與私訊各測一次拒絕
行為。

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
