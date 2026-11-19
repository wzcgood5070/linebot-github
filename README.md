# LINE Bot：n8n 主導版 v3

n8n 負責指令流程、排程觸發、五種作業分流、失敗分支及通知補送。Python 提供單項功能 API，SQLite 保存業務狀態，PostgreSQL 保存 n8n 流程紀錄，NAS 保存上傳檔案。

本版本 **40 項本機測試通過，尚未部署正式環境**。真實 n8n 匯入／發布、Synology NAS 與 GPT 整合仍需驗收；正式名單保留空白，進度檢查維持停用。

## 使用與維護

詳細節點、API、架構分工及切換順序見 [n8n/README.md](n8n/README.md)。此包不含 .env、金鑰、登入設定檔、正式資料庫或上傳檔案。

```powershell
docker build -t linebot-n8n-v3:test .
docker run --rm --entrypoint python linebot-n8n-v3:test -m pytest -q
```

發布到既有專案前，先備份 n8n 工作流程／Credentials／PostgreSQL 及 Python 資料。匯入並發布 `n8n/workflows/line-meeting-orchestration.json`，再執行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\update-linebot.ps1 -ProjectPath '既有專案的完整路徑'
```

腳本會先確認 v3 n8n readiness，接著建置、跑測試、備份 SQLite、保留舊映像，再切換 Bot。失敗時恢復舊映像與原本路由設定。此腳本本次僅通過語法檢查，尚未在正式 Docker 執行。

GitHub 可直接建立儲存庫後加入本目錄內容。請勿將正式 .env、Credentials 匯出檔、資料庫或 DSM profile 加入 Git。尚未上傳 GitHub。
