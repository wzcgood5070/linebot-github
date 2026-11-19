# n8n 主導版 v3

這版把新版 `/助理` 指令、定時觸發、五種作業分流、失敗處理與通知補送放回 n8n。Python 提供單項 API，負責簽章驗證、解析、資料交易、NAS 自動操作、轉檔與 GPT 呼叫。

**狀態：程式與 40 項本機測試通過；本次尚未匯入／發布正式 n8n，也未切換正式容器。** GPT、名單與 NAS 問題不會因架構切換自動解決。

## 三條主要流程

1. **群組指令**：LINE → Python 驗簽 → `LINE Intake` → `Normalize Intake` → `Process Group Command` → `Command Handled?`。已處理則回覆；新增會議則進 `Prepare Meeting` → 回覆登記結果。改期、取消、查詢、交付及複核也經過這裡。
2. **會前工作**：`Meeting Job Schedule` 每 15 秒 → `Claim Due Job` → `Only Available Job` → `Route Meeting Operation` → 建立上傳連結／準備上傳通知／未繳提醒／GPT 檢查／補傳檢查。每次領一項，後續觸發處理其餘工作。
3. **通知傳送**：`Notification Schedule` 每 15 秒 → 領一則 outbox 通知 → 傳送 LINE → 成功刪除待送記錄；明確失敗走延後一分鐘分支。

Python 不會在 n8n 模式啟動背景排程。舊的 `/jobs/run-due` 在此模式回 409，防止舊工作流程整批執行。原本的舊格式會議保留 Wait 相容分支；新版 `/助理 新增` 不走此分支。

## 檔案與設定

- 匯入檔：`workflows/line-meeting-orchestration.json`，預設不啟用。
- `ORCHESTRATOR=n8n`
- `INTERNAL_MEETING_JOBS=false`（即使舊環境誤留 true，n8n 模式也不會啟動 Python worker）
- `N8N_INTAKE_URL=http://n8n:5678/webhook/line-meeting-intake-v3`
- n8n 與 Python 的 `CORE_API_KEY` 必須一致。JSON 只引用環境變數，不含真實金鑰。
- n8n 容器需允許 `$env.CORE_API_KEY`，參考 compose 的 `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`。
- 正式版繼續 `MEETING_SUBMISSION_CHECKS_ENABLED=false`，名單空白。這次不自行啟用 GPT。

## 切換步驟

1. 備份 n8n Workflow、Credentials、PostgreSQL volume 與 Python SQLite／登入設定檔。檢視舊流程未結束的 Wait 執行，不直接刪除資料。
2. 在 n8n **Import from File** 匯入 v3 JSON，檢查節點與核心服務網址，發布該流程。v3 使用不同 webhook 路徑，尚未切換的 Bot 仍走原入口；v3 作業 API 對舊服務可能回 404，此時尚不能算正式驗收。
3. 發布腳本先呼叫 `line-meeting-controller-ready-v3`，確認 v3 Workflow 已發布，才建置／測試／備份與更新 Bot。腳本保留舊映像和原本路由開關供失敗回復。
4. 更新後檢查 `/health`：`workflow_version=n8n-v3`、`orchestrator=n8n`、`internal_meeting_jobs=false`。readiness 回覆的 core 版本也必須為 v3。
5. 在測試群組依序驗證新增、今日查詢、改期、取消。確認每筆在 n8n Executions 出現，而且同一工作不重複領取。另建一場驗證 NAS 連結與通知。不要改寫現有 DB 來強制通過。
6. 確認舊入口與舊排程沒有使用者後，再停用舊流程；已有 Wait 執行需逐筆確認。

## 單項 API

所有核心 API 使用 `X-Core-API-Key`。

| API | 用途 |
|---|---|
| POST `/api/v1/meetings/commands` | 處理改期、取消、查詢、交付與複核；回傳 handled / reply |
| POST `/api/v1/meetings/prepare` | 新增會議、保存資料與到期時間；不自行執行排程 |
| POST `/api/v1/meetings/jobs/claim` | 原子領取一項到期工作，回傳 id、operation_key、lease_until |
| POST `/api/v1/meetings/jobs/execute/{operation_key}` | 僅執行指定的一項，驗證種類與租約 |
| POST `/api/v1/meetings/jobs/failure` | n8n 明確失敗分支呼叫，保存重試時間／最終失敗通知 |
| POST `/api/v1/meetings/notifications/claim` | 領一則通知 |
| POST `/api/v1/meetings/notifications/send` | 單次傳送，成功後刪除 outbox 記錄 |
| POST `/api/v1/meetings/notifications/defer` | 明確傳送失敗後延一分鐘再領取 |

execute/failure/send/defer body 為 `{"id": 1, "lease_until": "API領取時回傳的完整時間"}`。過期、取消或已完成的租約回 409。會議編號不是工作 id。

## 重試與資料責任

n8n 決定何時觸發、進哪個作業分支、成功或失敗後接哪個節點。Python／SQLite 保留到期時間、租約、相依順序與交易一致性，不讓兩次排程領到同一項。業務重試政策封裝在持久化 API：首次加三次，間隔 30／60／90 秒；n8n 下次排程才會重新領取。這樣重啟不會忘記已用掉的次數。

網路中斷時無法確定外部操作是否完成，錯誤輸出走 `Await Lease Recovery`，不立即再做一次。工作租約 10 分鐘，通知租約 2 分鐘。超時後可恢復，但 LINE 傳送仍是至少一次語意，極端斷線可能重複通知。

SQLite 保存會議、項目、結果、工作及 outbox；PostgreSQL 保存 n8n 流程與執行紀錄；NAS 保存檔案。資料庫不是互相替代。

## 擴充

新增作業時：增加 operation_key／單項 API、更新到期工作規則、在 `Route Meeting Operation` 增加分支與對應節點、接上成功／失敗輸出，最後加測試。新增外部入口則整理成 event_id、target_id、target_type、text、received_at、reply_token，送 `unified-meeting-intake-v3`。表單與 Google 名單仍需各自的帳號介接，並未完成。

節點行為參考：[Switch 官方文件](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.switch/)、[錯誤輸出官方說明](https://github.com/n8n-io/skills/blob/main/skills/n8n-error-handling-official/references/NODE_ERROR_OUTPUTS.md)。
