from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import tzinfo


@dataclass(frozen=True)
class MeetingRequest:
    starts_at: datetime
    title: str


DATE_TIME = re.compile(
    r"(?:(?P<year>\d{4})[年/\-])?(?P<month>\d{1,2})[月/\-](?P<day>\d{1,2})日?"
    r"(?:\s*(?P<period>上午|下午|晚上|早上))?\s*"
    r"(?P<hour>\d{1,2})(?:[:：點時](?P<minute>\d{1,2}))?分?"
)
RELATIVE = re.compile(
    r"(?P<day>今天|明天|後天)\s*(?P<period>上午|下午|晚上|早上)?\s*"
    r"(?P<hour>\d{1,2})(?:[:：點時](?P<minute>\d{1,2}))?分?"
)

LINE_MEETING_COMMAND = re.compile(
    r"會議\s+(?P<date>\d{4}/\d{1,2}/\d{1,2})\s+(?P<time>\d{1,2}:\d{2})"
)
ASSISTANT_ADD_COMMAND = re.compile(
    r"/助理\s+新增\s+(?P<date>[0-9]{4}/[0-9]{2}/[0-9]{2})\s+"
    r"(?P<time>[0-9]{2}:[0-9]{2})\s+(?P<title>\S[^\r\n]*)"
)


def is_assistant_command(text: str) -> bool:
    return re.match(r"/助理(?:\s|$)", text.strip()) is not None


def is_line_meeting_command(text: str) -> bool:
    match = LINE_MEETING_COMMAND.fullmatch(text.strip())
    if not match:
        return False
    try:
        datetime.strptime(f"{match['date']} {match['time']}", "%Y/%m/%d %H:%M")
    except ValueError:
        return False
    return True


def _hour(hour: int, period: str | None) -> int:
    if period in {"下午", "晚上"} and hour < 12:
        return hour + 12
    if period in {"上午", "早上"} and hour == 12:
        return 0
    return hour


def parse_meeting(text: str, now: datetime, tz: tzinfo) -> MeetingRequest | None:
    text = text.strip()
    if is_assistant_command(text):
        command = ASSISTANT_ADD_COMMAND.fullmatch(text)
        if not command:
            return None
        try:
            starts = datetime.strptime(f"{command['date']} {command['time']}", "%Y/%m/%d %H:%M")
        except ValueError:
            return None
        return MeetingRequest(starts.replace(tzinfo=tz), command["title"])

    now = now.astimezone(tz)
    match = DATE_TIME.search(text)
    if match:
        year = int(match.group("year") or now.year)
        month, day = int(match.group("month")), int(match.group("day"))
        hour = _hour(int(match.group("hour")), match.group("period"))
        minute = int(match.group("minute") or 0)
        try:
            starts = datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError:
            return None
        if match.group("year") is None and starts < now:
            starts = starts.replace(year=year + 1)
    else:
        match = RELATIVE.search(text)
        if not match:
            return None
        offset = {"今天": 0, "明天": 1, "後天": 2}[match.group("day")]
        date = (now + timedelta(days=offset)).date()
        starts = datetime.combine(
            date,
            datetime.min.time().replace(
                hour=_hour(int(match.group("hour")), match.group("period")),
                minute=int(match.group("minute") or 0),
            ),
            tz,
        )
    title = re.sub(r"\s+", " ", text).strip()[:80] or "會議"
    return MeetingRequest(starts, title)
