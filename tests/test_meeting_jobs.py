from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db import MeetingStore
from app.meeting_commands import manage_meeting


NOW = datetime(2026, 11, 5, 14, 20, tzinfo=timezone(timedelta(hours=8)))
START = NOW.replace(minute=30)


def test_late_meeting_runs_after_provision_and_has_no_reminder():
    with TemporaryDirectory(dir=".") as directory:
        db_path = str(Path(directory) / "jobs.db")
        store = MeetingStore(db_path)
        assert store.add("a", "group", "group", "會議", START, NOW, members=[], registered_at=NOW)
        provision = store.claim_job(NOW)
        assert provision["operation"] == "建立上傳連結"
        assert MeetingStore(db_path).claim_job(NOW) is None
        store.finish_job(provision, NOW, upload_url="https://example.com/upload")
        assert store.get(1)["upload_url"] == "https://example.com/upload"
        for operation in ("上傳通知", "進度檢查"):
            job = store.claim_job(NOW)
            assert job["operation"] == operation
            store.finish_job(job, NOW)
        assert store.claim_job(NOW) is None


def test_retry_limit_and_reschedule_invalidate_claims():
    with TemporaryDirectory(dir=".") as directory:
        store = MeetingStore(str(Path(directory) / "jobs.db"))
        assert store.add("a", "group", "group", "會議", START, NOW, registered_at=NOW)
        now = NOW
        for attempt in range(4):
            job = store.claim_job(now)
            assert job["operation"] == "建立上傳連結"
            assert store.finish_job(job, now, "失敗") is (attempt == 3)
            now += timedelta(minutes=3)
        assert store.claim_job(now) is None

        manage_meeting(store, "b", "group", "/助理 改期 2026/11/05 2026/11/06 15:00", now, 10)
        claimed = store.claim_job(now)
        assert claimed["operation"] == "建立上傳連結"
        manage_meeting(store, "c", "group", "/助理 取消 2026/11/06", now, 10)
        assert not store.job_is_current(claimed)
        store.finish_job(claimed, now, upload_url="https://example.com/upload")
        assert store.get(1)["upload_url"] is None


def test_manual_retry_selects_one_job_and_retains_outbox_after_failed_delivery():
    with TemporaryDirectory(dir=".") as directory:
        path = str(Path(directory) / "jobs.db")
        store = MeetingStore(path)
        assert store.add("a", "group", "group", "會議", START, NOW, registered_at=NOW)
        provision = store.claim_job(NOW)
        store.finish_job(provision, NOW, upload_url="https://example.com/upload")
        with store.connect() as db:
            db.execute("UPDATE meeting_jobs SET status='failed' WHERE operation IN ('上傳通知','進度檢查')")

        listed = manage_meeting(store, "b", "group", "/助理 重試 2026/11/05", NOW, 10)
        assert "上傳通知、進度檢查" in listed
        command = "/助理 重試 2026/11/05 進度檢查"
        assert "已排入" in manage_meeting(store, "c", "group", command, NOW, 10)
        job = store.claim_job(NOW)
        assert job['operation'] == "進度檢查"
        store.finish_job(job, NOW)
        assert store.claim_job(NOW) is None

        notification = store.claim_notification(NOW)
        assert "重試成功" in notification['message']
        retry_at = NOW + timedelta(minutes=1)
        store.finish_notification(notification, retry_at)
        reopened = MeetingStore(path)
        assert reopened.claim_notification(NOW) is None
        assert reopened.claim_notification(retry_at)['id'] == notification['id']


def test_same_day_meetings_have_distinct_folders_and_rescheduling_preserves_the_folder():
    with TemporaryDirectory(dir=".") as directory:
        store = MeetingStore(str(Path(directory) / "jobs.db"))
        for event in ("a", "b"):
            assert store.add(event, "group", "group", event, START, NOW, registered_at=NOW)
        assert store.folder_name(1) == "20261105-1"
        assert store.folder_name(2) == "20261105-2"
        result = manage_meeting(store, "c", "group", "/助理 改期 2026/11/05 14:30 a 2026/11/06 15:00", NOW, 10)
        assert "已將a改至" in result
        assert store.folder_name(1) == "20261105-1"
