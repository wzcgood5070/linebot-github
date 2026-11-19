import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import datetime, timedelta

import httpx

from app import main
from app.db import MeetingStore
from app.nas import LocalNas


def test_limited_release_processes_new_line_commands_and_skips_file_checks(monkeypatch, tmp_path):
    store = MeetingStore(str(tmp_path / "release.db"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setenv("LOCAL_NAS_ROOT", str(tmp_path / "nas"))
    monkeypatch.setattr(main, "nas", LocalNas())
    monkeypatch.setattr(main, "settings", replace(
        main.settings, orchestrator="python", internal_meeting_jobs=True, submission_checks_enabled=False,
    ))
    start = datetime.now(main.settings.timezone) + timedelta(minutes=5)
    replies, pushes = [], []

    class Line:
        async def reply(self, token, message):
            replies.append(message)

        async def push(self, target, message):
            pushes.append(message)

    monkeypatch.setattr(main, "line", Line())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as api:
            for event, message in (
                ("add", f"/助理 新增 {start:%Y/%m/%d %H:%M} 部署測試"),
                ("query", f"/助理 查詢 繳交 {start:%Y/%m/%d}"),
            ):
                body = json.dumps({"events": [{
                    "type": "message", "message": {"type": "text", "text": message, "id": event},
                    "source": {"type": "group", "groupId": "group-test"}, "replyToken": event,
                }]}).encode()
                signature = hmac.digest(main.settings.line_channel_secret.encode(), body, hashlib.sha256)
                response = await api.post("/line/webhook", content=body, headers={
                    "X-Line-Signature": base64.b64encode(signature).decode(),
                })
                assert response.status_code == 200
        assert "會議編號" in replies[0]
        assert "尚未啟用" in replies[1]
        await main.process_meeting_jobs()
        assert store.get(1)['upload_url']
        assert not any("進度報告" in message or "繳交" in message for message in pushes)

    asyncio.run(scenario())


def test_meeting_from_registration_to_late_submission_and_manual_review(monkeypatch, tmp_path):
    store = MeetingStore(str(tmp_path / "meetings.db"))
    nas_root = tmp_path / "nas"
    monkeypatch.setenv("LOCAL_NAS_ROOT", str(nas_root))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "nas", LocalNas())
    starts = datetime.fromisoformat("2026-11-05T14:30:00+08:00")

    class Clock(datetime):
        current = starts - timedelta(minutes=60)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    class Line:
        replies = []
        pushes = []

        async def reply(self, token, message):
            self.replies.append(message)

        async def push(self, target, message):
            assert target == "group-test"
            self.pushes.append(message)

    line = Line()
    checked = []

    def judge(path, items, **_):
        checked.append((path.name, list(items)))
        evidence = path.read_text(encoding="utf-8")
        return {
            item: ("完成", "檔案有證據") if item in evidence else ("未完成", "檔案缺少證據")
            for item in items
        }

    async def execute_mock(function, *args):
        return function(*args)

    monkeypatch.setattr(main, "line", line)
    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(main, "check_submission", judge)
    monkeypatch.setattr(main.asyncio, "to_thread", execute_mock)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as api:
            core_headers = {"X-Core-API-Key": main.settings.core_api_key}
            registered = await api.post("/api/v1/meetings/prepare", headers=core_headers, json={
                "event_id": "register", "target_id": "group-test", "target_type": "group",
                "text": "/助理 新增 2026/11/05 14:30 系務會議", "received_at": Clock.current.isoformat(),
            })
            assert registered.status_code == 200
            meeting = registered.json()
            assert meeting["identifier"] == "2026/11/05"
            assert store.roster(meeting["meeting_id"]) == []
            with store.connect() as db:
                db.execute("UPDATE meeting_rosters SET members=? WHERE meeting_id=?", (
                    json.dumps(["王小明", "李小華"]), meeting["meeting_id"],
                ))

            async def command(event_id, text):
                body = json.dumps({"events": [{
                    "type": "message", "message": {"type": "text", "text": text, "id": event_id},
                    "source": {"type": "group", "groupId": "group-test"}, "replyToken": event_id,
                }]}).encode()
                digest = hmac.digest(main.settings.line_channel_secret.encode(), body, hashlib.sha256)
                result = await api.post("/line/webhook", content=body, headers={
                    "X-Line-Signature": base64.b64encode(digest).decode(),
                })
                assert result.status_code == 200
                return line.replies[-1]

            async def run_jobs():
                response = await api.post("/api/v1/meetings/jobs/run-due", headers=core_headers)
                assert response.status_code == 200

            assert "已設定" in await command("1", "/助理 進度報告:王小明.李小華/測試、架構圖")
            await run_jobs()
            folder = nas_root / meeting["folder_name"]
            assert folder.is_dir()
            assert any("尚未繳交：王小明、李小華" in message for message in line.pushes)
            original = folder / "王小明_報告_20261105.txt"
            original.write_text("測試", encoding="utf-8")
            status = await command("2", "/助理 查詢 繳交 2026/11/05")
            assert "王小明：已上傳" in status and "李小華：尚未上傳" in status

            Clock.current = starts - timedelta(minutes=15)
            await run_jobs()
            assert "[王小明:未完成(缺架構圖)]" in line.pushes[-1]
            assert "[李小華:未完成" in line.pushes[-1]

            extra = folder / "王小明_補傳_20261105.txt"
            extra.write_text("架構圖", encoding="utf-8")
            Clock.current += timedelta(seconds=30)
            await run_jobs()
            assert line.pushes[-1] == "【補傳檢查】\n王小明｜完成"
            assert checked[-1] == (extra.name, ["架構圖"])
            assert sum(name == original.name for name, _ in checked) == 1

            reviewed = await command("3", "/助理 複核 2026/11/05 王小明 未完成 圖面需補標註")
            assert "[王小明:未完成]（人工複核，圖面需補標註）（AI判定：完成）" in reviewed
            assert "[李小華:未完成" in reviewed

    asyncio.run(scenario())
