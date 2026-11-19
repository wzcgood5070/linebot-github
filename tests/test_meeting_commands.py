from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db import MeetingStore
from app.meeting_commands import manage_meeting


NOW = datetime(2026, 11, 5, 8, tzinfo=timezone.utc)


def test_group_meetings_follow_time_order_and_stale_number_cannot_cancel_wrong_meeting():
    with TemporaryDirectory(dir=".") as directory:
        store = MeetingStore(str(Path(directory) / "meetings.db"))
        for event, hour, title in [("a", 15, "甲"), ("b", 14, "乙"), ("c", 13, "丙")]:
            start = NOW.replace(hour=hour)
            assert store.add(event, "group", "group", title, start, start)

        lookup = manage_meeting(store, "d", "group", "/助理 查詢 今天", NOW, 10)
        assert lookup.splitlines()[1:] == [
            "2026/11/05-1 13:00 丙", "2026/11/05-2 14:00 乙", "2026/11/05-3 15:00 甲",
        ]
        reply = manage_meeting(store, "e", "group", "/助理 取消 2026/11/05-1", NOW, 10)
        assert "日期和時間重新指定" in reply
        assert store.get(3)["status"] == "pending"


def test_reschedule_and_cancel_keep_url_and_deduplicate_commands():
    with TemporaryDirectory(dir=".") as directory:
        store = MeetingStore(str(Path(directory) / "meetings.db"))
        assert store.add("a", "group", "group", "甲", NOW.replace(hour=14), NOW)
        store.mark_done(1, "https://example.com/upload")
        command = "/助理 改期 2026/11/05 14:00 甲 2026/11/06 15:00"
        assert "新會議編號：2026/11/06" in manage_meeting(store, "b", "group", command, NOW, 10)
        assert manage_meeting(store, "b", "group", command, NOW, 10) == ""
        assert store.get(1)["run_at"] == "2026-11-06T14:50:00+00:00"
        assert "找不到" in manage_meeting(store, "c", "other", "/助理 取消 2026/11/06", NOW, 10)
        assert "已取消" in manage_meeting(store, "d", "group", "/助理 取消 2026/11/06", NOW, 10)
        assert store.get(1)["status"] == "cancelled"
        assert store.get(1)["upload_url"] == "https://example.com/upload"
