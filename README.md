# LINE 會議自動化

FastAPI、SQLite、n8n、Playwright DSM 與 GPT 判讀的原始碼快照。
架構、流程、元件角色、擴充與維護方式見 [現階段系統報告](docs/現階段系統報告.md)。

## 開發與測試

```powershell
Copy-Item .env.example .env
# 編輯 .env，填入自己的服務設定
docker build -t linebot-dev:local .
docker run --rm --entrypoint python linebot-dev:local -m pytest -q
docker compose up -d --build
```

新助理會議由 `INTERNAL_MEETING_JOBS=true` 在核心服務內排程。
目前 `MEETING_SUBMISSION_CHECKS_ENABLED=false`，繳交與 GPT 功能待驗收後啟用。
公開 HTTPS Tunnel 應轉送至本機 8000 埠的 `/line/webhook`。
初次啟動前需設定 LINE、NAS、n8n、PostgreSQL 與 CORE_API_KEY，並完成 DSM 專用瀏覽器登入。

## 更新既有部署

以 PowerShell 7 執行，ProjectPath 指向既有 compose.yaml 與 .env 所在目錄。

```powershell
./scripts/update-linebot.ps1 -ProjectPath 'C:\path\to\existing-linebot'
```

腳本會測試新映像、備份 SQLite、保存舊映像，再更新 linebot。
產生的實際部署覆寫檔位於 `work/linebot-release/compose.release.yaml`。
後續維護需同時指定原 Compose 與覆寫檔，並保留原專案名稱與 volumes。

## 診斷工具

`scripts/diagnostics` 收錄 DSM 視窗診斷與指定檔下載測試。
容器需掛載原 `/app/data/dsm-browser-profile`。
視窗診斷另需將本儲存庫 `app` 掛到 `/tmp/nas-source`，輸出目錄掛到 `/tmp/diagnostics`。
下載測試的 FOLDER、FILENAME 和父路徑已改為範例，執行前請指定核准的測試檔案。

## GitHub 提交

```powershell
git init
git add .
git diff --cached --stat
git commit -m "Add LINE meeting automation and progress report"
git branch -M main
git remote add origin https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git
git push -u origin main
```

請將 URL 換成自己的空白儲存庫。實際 .env、資料庫、瀏覽器登入資料、NAS 檔案和備份保存在部署環境。
本交付未替使用者選擇公開授權條款。
