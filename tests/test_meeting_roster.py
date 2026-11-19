from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db import MeetingStore


def test_empty_roster_is_saved_with_meeting_and_survives_reopening_database():
    with TemporaryDirectory(dir=".") as directory:
        path = str(Path(directory) / "meetings.db")
        store = MeetingStore(path)
        now = datetime.now(timezone.utc)
        assert store.add("event-1", "group-1", "group", "系務會議", now, now, members=[])
        assert not store.add("event-1", "group-1", "group", "系務會議", now, now, members=[])
        assert store.add("event-2", "group-1", "group", "既有入口", now, now)

        reopened = MeetingStore(path)
        assert reopened.roster(1) == []
        assert reopened.roster(2) is None
