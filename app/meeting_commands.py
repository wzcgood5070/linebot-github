import re
from datetime import datetime, timedelta

from .db import MeetingStore


DATE = r"[0-9]{4}/[0-9]{2}/[0-9]{2}"
TIME = r"[0-9]{2}:[0-9]{2}"
MANAGEMENT_COMMAND = re.compile(r"/助理\s+(改期|取消|查詢|重試)(?:\s+(.*))?")
SELECTOR = re.compile(rf"(?P<day>{DATE})(?:(?P<number>-[1-9][0-9]*)|\s+(?P<time>{TIME})(?:\s+(?P<title>.+))?)?")
RESCHEDULE = re.compile(rf"(?P<selector>.+?)\s+(?P<new_time>{DATE}\s+{TIME})")
RETRY_OPERATIONS = ("建立上傳連結", "上傳通知", "未繳提醒", "進度檢查", "補傳檢查")


def meeting_lines(meetings: list[dict]) -> str:
    return "\n".join(
        f"{row['identifier']} {datetime.fromisoformat(row['starts_at']):%H:%M} {row['title']}"
        for row in meetings
    )


def resolve_meeting(store, db, target_id, argument) -> tuple[dict | None, str]:
    selector = SELECTOR.fullmatch(argument)
    if not selector:
        return None, "請填寫會議日期、日期加編號，或日期加時間和會議名稱。"
    meetings = store.meetings_on(db, target_id, selector['day'])
    if selector['number']:
        matches = [row for row in meetings if row['identifier'] == argument]
        history = {row[0] for row in db.execute(
            "SELECT meeting_id FROM meeting_identifiers WHERE target_id=? AND identifier=?",
            (target_id, argument),
        )}
        if len(history) > 1 or (history and not matches):
            return None, f"會議編號已變更，請用日期和時間重新指定。\n{meeting_lines(meetings)}".rstrip()
    elif selector['time']:
        matches = [row for row in meetings if (
            datetime.fromisoformat(row['starts_at']).strftime("%H:%M") == selector['time']
            and (not selector['title'] or row['title'] == selector['title'])
        )]
    else:
        matches = meetings

    if not matches:
        return None, "找不到本群組指定的會議。"
    if len(matches) > 1:
        instruction = "日期和時間有多場會議，請加上會議名稱。" if selector['time'] else "這一天有多場會議，請帶編號重送。"
        return None, f"{instruction}\n{meeting_lines(matches)}"
    return matches[0], ""


def manage_meeting(
    store: MeetingStore, event_id: str, target_id: str, text: str, now: datetime, lead_minutes: int,
) -> str | None:
    command = MANAGEMENT_COMMAND.fullmatch(text.strip())
    if not command:
        return None
    operation, argument = command[1], command[2] or ""
    if operation == "查詢" and argument != "今天":
        return None
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM meeting_command_events WHERE event_id=?", (event_id,)).fetchone():
            return ""

        if operation == "查詢":
            meetings = store.meetings_on(db, target_id, now.strftime("%Y/%m/%d"))
            reply = f"【今日會議】\n{meeting_lines(meetings)}" if meetings else "本群組今天沒有會議。"
        else:
            new_time = None
            retry_operation = None
            if operation == "重試":
                for name in RETRY_OPERATIONS:
                    if argument.endswith(f" {name}"):
                        argument = argument.removesuffix(f" {name}")
                        retry_operation = name
                        break
            if operation == "改期":
                match = RESCHEDULE.fullmatch(argument)
                if not match:
                    return "格式：/助理 改期 原會議識別 YYYY/MM/DD HH:mm"
                try:
                    new_time = datetime.strptime(match['new_time'], "%Y/%m/%d %H:%M").replace(tzinfo=now.tzinfo)
                except ValueError:
                    return "請確認新會議日期和時間。"
                argument = match['selector']

            meeting, error = resolve_meeting(store, db, target_id, argument)
            if meeting is None:
                return error
            day = datetime.fromisoformat(meeting['starts_at']).strftime("%Y/%m/%d")
            if operation == "重試":
                failures = db.execute(
                    "SELECT id, operation FROM meeting_jobs WHERE meeting_id=? AND status='failed' ORDER BY id",
                    (meeting['id'],),
                ).fetchall()
                available = {row['operation']: row['id'] for row in failures}
                if retry_operation not in available:
                    if not available:
                        return "此場會議目前沒有可重試的失敗項目。"
                    return "請補上要重試的項目：" + "、".join(available)
                db.execute(
                    "UPDATE meeting_jobs SET status='pending', attempts=0, due_at=?, error=NULL WHERE id=?",
                    (now.isoformat(), available[retry_operation]),
                )
                db.execute("INSERT OR IGNORE INTO manual_job_retries(job_id) VALUES(?)", (available[retry_operation],))
                reply = f"已排入重試：{retry_operation}。"
                db.execute("INSERT INTO meeting_command_events(event_id, reply) VALUES(?, ?)", (event_id, reply))
                return reply

            store.remember_identifiers(db, target_id, day)
            if new_time is not None:
                new_day = new_time.strftime("%Y/%m/%d")
                store.remember_identifiers(db, target_id, new_day)
                run_at = max(now, new_time - timedelta(minutes=lead_minutes))
                db.execute(
                    "UPDATE meetings SET starts_at=?, run_at=?, status='pending', error=NULL WHERE id=?",
                    (new_time.isoformat(), run_at.isoformat(), meeting['id']),
                )
                if not meeting['upload_url']:
                    store.ensure_folder(db, meeting['id'], new_time)
                store.replace_jobs(db, meeting['id'], new_time, now, provision=not meeting['upload_url'])
                store.remember_identifiers(db, target_id, new_day)
                updated = next(row for row in store.meetings_on(db, target_id, new_day) if row['id'] == meeting['id'])
                reply = f"已將{meeting['title']}改至 {new_time:%Y/%m/%d %H:%M}。新會議編號：{updated['identifier']}。"
            else:
                db.execute("UPDATE meetings SET status='cancelled' WHERE id=?", (meeting['id'],))
                store.stop_jobs(db, meeting['id'])
                reply = f"已取消{meeting['title']}，已建立的 NAS 資料夾與上傳連結已保留。"
            store.remember_identifiers(db, target_id, day)

        db.execute("INSERT INTO meeting_command_events(event_id, reply) VALUES(?, ?)", (event_id, reply))
        return reply
