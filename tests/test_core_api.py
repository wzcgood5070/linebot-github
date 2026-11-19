import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app import main
from app.db import MeetingStore


def test_prepare_meeting_returns_normalized_schedule(monkeypatch):
    test_dir = Path("pytest-cache-files")
    test_dir.mkdir(exist_ok=True)
    db_path = test_dir / f"core-api-{uuid4().hex}.db"
    monkeypatch.setattr(main, "store", MeetingStore(str(db_path)))
    payload = main.MeetingPrepareRequest(
        event_id="calendar:event-1",
        target_id="group-1",
        target_type="calendar",
        text="專案會議 2026/10/20 14:30",
        received_at=datetime.fromisoformat("2026-10-04T10:00:00+08:00"),
    )

    result = asyncio.run(main.prepare_meeting(payload, None))

    assert result["accepted"] is True
    assert result["folder_name"] == "1020"
    assert result["starts_at"] == "2026-10-20T14:30:00+08:00"
    assert result["run_at"] == "2026-10-20T14:20:00+08:00"


def test_empty_line_reply_token_is_skipped(monkeypatch):
    class LineMustNotBeCalled:
        async def reply(self, *_):
            raise AssertionError("LINE reply must not be called for non-LINE intake")

    monkeypatch.setattr(main, "line", LineMustNotBeCalled())
    result = asyncio.run(main.line_reply(main.LineReplyRequest(reply_token="", text="ok"), None))
    assert result == {"ok": True, "skipped": True}


def test_scheduled_reminder_uses_nas_listing_without_downloading(monkeypatch, tmp_path):
    store = MeetingStore(str(tmp_path / "meetings.db"))
    scheduled = datetime.fromisoformat("2026-10-20T13:30:00+08:00")
    start = scheduled.replace(hour=14)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return scheduled

    class Nas:
        async def create_upload_folder(self, title, starts_at, *, folder_name):
            assert folder_name == "20261020-1"
            return "https://example.com/sharing/test"

        @asynccontextmanager
        async def open_submissions(self, folder_name, *, download=False):
            assert folder_name == "20261020-1"
            assert download is False
            yield [Path("王小明_測試結果_20261020.pdf")]

    messages = []

    class Line:
        async def push(self, target_id, message):
            messages.append((target_id, message))

    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "nas", Nas())
    monkeypatch.setattr(main, "line", Line())
    assert store.add("event-1", "group-1", "group", "系務會議", start, scheduled,
                     members=["王小明", "李小華"], registered_at=scheduled)
    assert asyncio.run(main.process_meeting_jobs()) == 3
    assert messages[1][0] == "group-1"
    assert "尚未繳交：李小華" in messages[1][1]
