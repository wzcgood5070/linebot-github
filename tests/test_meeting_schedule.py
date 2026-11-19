from datetime import datetime, timedelta, timezone

from app.meeting_schedule import initial_meeting_schedule


TAIPEI = timezone(timedelta(hours=8))
START = datetime(2026, 11, 5, 14, 30, tzinfo=TAIPEI)


def test_initial_schedule_before_the_day_of_the_meeting():
    assert initial_meeting_schedule(START, START - timedelta(days=1)) == [
        ("上傳通知", START.replace(hour=8, minute=0)),
        ("未繳提醒", START.replace(hour=13, minute=30)),
        ("進度檢查", START.replace(hour=14, minute=15)),
    ]


def test_late_registration_skips_reminder_and_runs_upload_before_check():
    registered_at = START.replace(minute=20)
    assert initial_meeting_schedule(START, registered_at) == [
        ("上傳通知", registered_at),
        ("進度檢查", registered_at),
    ]
