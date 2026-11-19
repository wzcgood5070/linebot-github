import re
import sqlite3
from datetime import datetime

from .meeting_commands import DATE, resolve_meeting
from .submissions import submissions_by_member


DELIVERABLES = re.compile(rf"(?:(?P<meeting>{DATE}\S*(?:\s+[^/]+)??)\s+)?(?P<names>[^/\s]+)\s*/\s*(?P<items>.+)")
REVIEW = re.compile(r"(.+)\s+(\S+)\s+(完成|未完成|無法判定)\s+(.+)")


def member_result(items: list[str], results: dict) -> str:
    if not items:
        return "尚未設定交付項目"
    missing, unknown = [], []
    for item in items:
        status, reason = results.get(item, ("無法判定", "尚未檢查"))
        if status == "未完成":
            missing.append(f"缺{item}" + (f"（{reason}）" if reason == "尚未上傳" else ""))
        elif status == "無法判定":
            unknown.append(f"{item}：{reason}")
    if missing:
        return f"未完成({'、'.join(missing + unknown)})"
    if unknown:
        return f"無法判定({'、'.join(unknown)})"
    return "完成"


def progress_report(meeting: dict, members: list[str], items: dict, ai: dict, reviews: dict) -> str:
    start = datetime.fromisoformat(meeting['starts_at'])
    lines = [f"【會議進度報告】{meeting['title']} {start:%Y/%m/%d}"]
    for member in members:
        result = member_result(items.get(member, []), ai.get(member, {}))
        if member in reviews:
            status, reason = reviews[member]
            lines.append(f"[{member}:{status}]（人工複核，{reason}）（AI判定：{result}）")
        else:
            lines.append(f"[{member}:{result}]")
    return "\n".join(lines if members else [*lines, "此場會議名單為空白。"])


def check_progress(files, items: dict[str, list[str]], judge) -> dict:
    results = {}
    for member, paths in submissions_by_member(files, items).items():
        entries = {item: ("未完成", "尚未上傳") for item in items[member]}
        for path in paths:
            remaining = [item for item, (status, _) in entries.items() if status != "完成"]
            if not remaining:
                break
            for item, outcome in judge(path, remaining).items():
                previous = entries[item]
                if outcome[0] == "完成" or previous[1] == "尚未上傳" or outcome[0] == "無法判定":
                    entries[item] = outcome
        results[member] = entries
    return results


def progress_command(store, event_id: str, target: str, text: str, now: datetime) -> str | None:
    command = re.fullmatch(r"/助理\s+(進度報告:|檢查進度\b|複核\b)\s*(.*)", text.strip())
    if not command:
        return None
    operation, argument = command.groups()
    match = DELIVERABLES.fullmatch(argument) if operation == "進度報告:" else REVIEW.fullmatch(argument)
    if operation != "檢查進度" and not match:
        return "格式：/助理 進度報告:姓名/項目、項目" if operation == "進度報告:" else "格式：/助理 複核 會議識別 姓名 結果 理由"
    selector = argument if operation == "檢查進度" else match['meeting'] if operation == "進度報告:" else match[1]
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM meeting_command_events WHERE event_id=?", (event_id,)).fetchone():
            return ""
        if selector:
            meeting, error = resolve_meeting(store, db, target, selector)
            if meeting is None:
                return error
        else:
            db.row_factory = sqlite3.Row
            meeting = db.execute(
                "SELECT * FROM meetings WHERE target_id=? AND status<>'cancelled' AND starts_at>? ORDER BY starts_at,id LIMIT 1",
                (target, now.isoformat()),
            ).fetchone()
            if meeting is None:
                return "本群組沒有尚未開始的會議。"
            meeting = dict(meeting)
        meeting_id = meeting['id']
        members = store.roster(meeting_id) or []
        ai, reviews, has_report = store.progress_snapshot(db, meeting_id)
        if operation == "檢查進度":
            store.queue_progress_check(db, meeting_id, now)
            reply = f"已排入{meeting['title']}的全場進度檢查。"
        elif operation == "複核":
            name, status, reason = match[2], match[3], match[4]
            if name not in members:
                return f"此場會議名單沒有{name}。"
            result = {name: (status, reason)}
            store.record_progress(db, meeting_id, "review", result, now)
            reviews.update(result)
            reply = "【複核已登記】\n" + progress_report(meeting, members, store.progress_items(meeting_id), ai, reviews)
        else:
            names = list(dict.fromkeys(match['names'].split('.')))
            missing = [name for name in names if name not in members]
            if missing:
                return f"此場會議名單沒有{'、'.join(missing)}。"
            items = [item.strip() for item in match['items'].split('、') if item.strip()]
            if not items:
                return "請填寫至少一項交付項目。"
            new_items = {}
            for name in names:
                for item in items:
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO meeting_deliverables(meeting_id,member,item) VALUES(?,?,?)",
                        (meeting_id, name, item),
                    )
                    if inserted.rowcount:
                        new_items.setdefault(name, []).append(item)
            if has_report and new_items:
                store.queue_progress_check(db, meeting_id, now, new_items)
            reply = f"已設定{meeting['title']}的交付項目。\n" + "\n".join(
                f"{name}：{'、'.join(values)}。" for name, values in new_items.items()
            ) if new_items else "這些交付項目已在清單中。"
        db.execute("INSERT INTO meeting_command_events(event_id,reply) VALUES(?,?)", (event_id, reply))
        return reply
