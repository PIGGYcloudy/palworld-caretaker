# AGENTS.md

給在這個 repo 工作的 AI 代理（Claude、Codex 等）與新貢獻者的工作守則。
README 與 `docs/` 寫給使用者；這份寫給「要改程式碼的人」。內容過時時請一併更新。

## 專案一句話

Palworld Dedicated Server 的跨平台維運工具：安全關服、可還原備份、閒置關服、
Discord 遠端操作與本機 Web 管理面板。支援 Ubuntu 24.04 systemd、Docker Compose、
Windows 原生三種部署。存檔安全永遠優先於新功能。

## 目錄地圖

| 位置 | 內容 |
| --- | --- |
| `src/palworld_caretaker/` | 共用 Python 核心（零第三方依賴，Python ≥ 3.10）。 |
| `src/palworld_caretaker/web.py` | Web 面板的 HTTP server、API、驗證、CSRF、CSP、操作鎖。 |
| `src/palworld_caretaker/web_page.py` | 面板的單頁 HTML/CSS/JS（純字串，`render_page` 只替換 nonce 與 CSRF token）。 |
| `src/palworld_caretaker/settings.py` | 41 個可編輯設定的 schema（標籤與說明為繁體中文）、排程語法驗證。 |
| `src/palworld_caretaker/settings_store.py` | 分層設定的預覽、差異與原子寫入。 |
| `src/palworld_caretaker/scheduling.py` | 備份／更新排程 gate（systemd timer、Docker supervisor、Windows worker 共用）。 |
| `src/palworld_caretaker/worlds.py` | 多世界登錄與每個世界的設定建立。 |
| `src/palworld_caretaker/backup.py`、`operations.py` | 備份 manifest／原子 commit、跨行程 `flock` 操作鎖。 |
| `src/palworld_caretaker/windows*.py` | Windows 服務控制與雙擊啟動器流程。 |
| `scripts/` | Bash 入口（備份、還原、更新、每日維護）、Discord bot、idle watcher、`palworld_manager.py` preflight。 |
| `scripts/windows/` | PowerShell 入口與 `Caretaker.Common.psm1`。 |
| `docker/` | `docker-supervisor.py`（容器 PID 1）、entrypoint 與預設設定。 |
| `units/` | systemd unit 範本（`@PLACEHOLDER@` 由安裝器替換）。 |
| `config/` | 設定範本；真正的 `*.env` 不進 Git。 |
| `tests/` | `unittest` 測試，含用假 `systemctl`／`curl`／`rsync` 的 Bash 整合測試。 |
| `docs/` | 安裝、升級、設定、Docker、Windows、Discord、Roadmap。 |

## 必跑的檢查

送出前在 Linux 上跑（與 CI 相同）：

```bash
python3 -m pip install "discord.py>=2.3.0,<3"   # Discord 測試需要；rsync、node 也要在 PATH 上
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -W error -m py_compile src/palworld_caretaker/*.py scripts/*.py tests/*.py docker/*.py
bash -n install-palworld.sh upgrade-palworld-manager.sh scripts/*.sh
shellcheck install-palworld.sh upgrade-palworld-manager.sh scripts/*.sh \
  scripts/palworld-control scripts/palworld-rest-firewall scripts/palworld-discord-configure
```

注意事項：

- **不要用 root 跑測試。** preflight 在 `euid == 0` 時會檢查真實帳號與擁有者，
  約 15 個備份／preflight 測試在 root 下會失敗，這不是程式錯誤。容器裡請建立一般使用者再跑。
- `test_frontend_auth_unicode_handoff_and_download` 會用 Node 執行面板 JS 中
  `const text=` 之前的那段 helper。**那段只能放純函式**，不能直接碰 `document`；
  啟動時要操作 DOM 的程式碼放在 script 後段。
- Windows／PowerShell 專用測試在 Linux 會 skip，CI 另有 `windows-latest` matrix。
- 排程相關的 Bash 測試用假的 `date`；`daily-palworld-maintenance.sh` 會把
  `date +%Y-%m-%dT%H:%M` 傳給 `python -m palworld_caretaker.scheduling`，改動時兩邊要一致。

## 修改原則

- **安全邊界不放寬。** 關服前一定先 REST save；備份／還原失敗時保持原本服務狀態；
  還原前留 safety backup；所有寫入走 operation lock 與 audit log。改動 shutdown、
  update、backup、restore、firewall 或權限邊界時要附測試並描述失敗行為（見 CONTRIBUTING.md）。
- **Web 面板**：資料只用 `textContent` 插入，不用 `innerHTML`；CSP 只允許帶 nonce 的
  inline script/style，不能載入外部 CDN 或字型。所有 mutation API 需要 CSRF header。
  REST API（8212）只能 loopback，不要提供任何把它公開的選項。
- **面板 UI 文案用繁體中文。** 伺服器回傳的英文訊息由 `web_page.py` 的 `messageText`
  對照表在瀏覽器翻譯；新增 API 訊息時請一起加對照，或直接回傳中文。
- 測試會檢查面板 JS 的某些字串（例如 `loadSettings().then(loadCommon)`、
  `input.dispatchEvent(new Event('input',{bubbles:true}))`），重構時請保留或同步更新測試。
- 平台差異留在 adapter（systemd / Docker supervisor / Windows），共用決策放在 `src/` 核心，
  不要在各入口重複安全判斷。
- 不要提交真實的 `*.env`、Discord token、密碼、存檔或備份。

## 文件同步

- 使用者可見的行為變更：更新 `README.md` 與相關 `docs/*.md`，並在 `CHANGELOG.md`
  的 `[Unreleased]` 加一行。版本號同時存在 `pyproject.toml` 與 README。
- 新增設定鍵：更新 `config/*.env.example`、`docs/CONFIGURATION.md` 與 `settings.py` schema。
- 本檔案描述的指令、路徑或陷阱失效時，直接改這份檔案。

## 目前已知的待決事項

- 面板內的 Hamachi 說明是固定文案；若要支援其他連線方式需另行設計。
- 伺服器端錯誤訊息仍是英文（前端已對照翻譯）；若改成直接回傳中文，需同步更新相關測試。
