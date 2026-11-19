from datetime import datetime, timedelta


def initial_meeting_schedule(starts_at: datetime, registered_at: datetime) -> list[tuple[str, datetime]]:
    upload_at = starts_at.replace(hour=8, minute=0, second=0, microsecond=0)
    reminder_at = starts_at - timedelta(hours=1)
    check_at = starts_at - timedelta(minutes=15)

    if max(upload_at, reminder_at, check_at) <= registered_at < starts_at:
        return [("上傳通知", registered_at), ("進度檢查", registered_at)]

    return [
        ("上傳通知", max(upload_at, registered_at)),
        ("未繳提醒", max(reminder_at, registered_at)),
        ("進度檢查", max(check_at, registered_at)),
    ]
