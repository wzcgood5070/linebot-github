# n8n 匯入包

新版使用同目錄的 `workflows/line-meeting-orchestration.json`。在 n8n 選擇 **Import from File** 匯入並發布，確認 Python 核心服務和 n8n 的 `CORE_API_KEY` 設定相符。`Meeting Job Schedule` 每 15 秒呼叫核心 API 處理到期作業。會議與排程都存在核心服務的 SQLite 資料庫，請保留 Compose 的 `linebot-data` volume。

切換時先確認原流程有哪些尚未結束的 Wait 執行，再停用舊版的接收入口。新版 `/助理 新增` 走資料庫排程，其他既有入口仍沿用原本的 Wait 流程。

`workflows/line-meeting-orchestration.json` 是可直接由 n8n 的 **Import from File** 匯入的 Workflow。
ZIP 只是方便搬移；n8n 介面實際選取的是解壓後的 JSON。

## 目前包含

- `LINE Intake`：由 Python 簽章驗證入口轉送的 LINE 事件。
- `Calendar Form Email Intake`：供未來行事曆、表單、Email adapter 呼叫的統一入口。
- 標準化、日期解析、去重複、登記回覆。
- Wait 節點：等待到會議前 10 分鐘。
- 呼叫 Python NAS API 建立 `MMDD` 資料夾及檔案請求。
- LINE 通知與三次重試。

## 統一入口格式

```json
{
  "event_id": "calendar:event-123",
  "target_id": "LINE群組或使用者ID",
  "target_type": "calendar",
  "text": "專案會議 2026/10/20 14:30",
  "reply_token": "",
  "received_at": "2026-10-04T10:00:00+08:00",
  "source": "calendar"
}
```

Google Calendar、表單、Email 的帳號憑證不會包含在匯入包中；未來新增 Trigger 後，轉成上述格式並接到 `Normalize Intake` 即可。

## 匯入後

1. 確認 n8n 容器具有與 Python 相同的 `CORE_API_KEY`。
2. 開啟 Workflow 並檢查所有節點。
3. 按 **Publish/Active** 啟用。
4. LINE Developers 的 Webhook 仍指向 Python：`https://你的網域/line/webhook`，由 Python 驗證 LINE 簽章後轉送 n8n。
