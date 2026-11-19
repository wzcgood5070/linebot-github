from datetime import datetime, timedelta, timezone

from app.parser import is_line_meeting_command, parse_meeting


TZ = timezone(timedelta(hours=8), name="Asia/Taipei")
NOW = datetime(2026, 8, 21, 10, 0, tzinfo=TZ)


def test_absolute_chinese_datetime():
    result = parse_meeting("下週會議 2026年8月25日下午2點30分", NOW, TZ)
    assert result.starts_at == datetime(2026, 8, 25, 14, 30, tzinfo=TZ)


def test_relative_datetime():
    result = parse_meeting("明天上午9:15 開會", NOW, TZ)
    assert result.starts_at == datetime(2026, 8, 22, 9, 15, tzinfo=TZ)


def test_missing_datetime():
    assert parse_meeting("找時間開會", NOW, TZ) is None


def test_assistant_add_command_uses_the_supplied_title_and_time():
    result = parse_meeting("/助理 新增 2026/11/05 14:30 系務會議 第一場", NOW, TZ)
    assert result.title == "系務會議 第一場"
    assert result.starts_at == datetime(2026, 11, 5, 14, 30, tzinfo=TZ)


def test_assistant_command_does_not_fall_back_to_free_text_parsing():
    for text in (
        "/助理 新增 2026/11/05 14:30",
        "/助理 新增 2026/02/30 14:30 系務會議",
        "/助理 新增 2026/11/05 24:00 系務會議",
        "/助理 改期 2026/11/05 2026/11/06 14:30",
    ):
        assert parse_meeting(text, NOW, TZ) is None


def test_line_command_accepts_only_explicit_valid_meetings():
    assert is_line_meeting_command("會議 2026/10/25 14:30")
    assert is_line_meeting_command(" 會議 2026/10/25 14:30\n")
    for text in (
        "你好", "2026/10/25 14:30", "專案會議 2026/10/25 14:30",
        "會議 明天 14:30", "會議 2026/02/30 14:30",
        "會議 2026/10/25 25:00", "會議 2026/10/25 14:30 其他文字",
    ):
        assert not is_line_meeting_command(text), text
