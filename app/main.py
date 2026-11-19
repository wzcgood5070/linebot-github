from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Annotated

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from .config import Settings
from .db import MeetingStore
from .line_client import LineClient, valid_signature
from .meeting_commands import manage_meeting, resolve_meeting
from .meeting_progress import check_progress, member_result, progress_command, progress_report
from .meeting_schedule import initial_meeting_schedule
from .nas import build_nas
from .parser import is_assistant_command, is_line_meeting_command, parse_meeting
from .progress_gpt import check_submission
from .submissions import changed_submissions, submissions_by_member

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("line-nas-bot")
settings = Settings()
store = MeetingStore(settings.db_path)
line = LineClient(settings.line_channel_access_token)
nas = build_nas(settings.nas_driver)


async def process_due_once(now: datetime | None = None):
    now = now or datetime.now(settings.timezone)
    for meeting in store.due(now):
        try:
            starts = datetime.fromisoformat(meeting["starts_at"])
            url = meeting["upload_url"] or await nas.create_upload_folder(meeting["title"], starts)
            current = store.get(meeting["id"])
            if not current or current["status"] != "pending" or current["run_at"] != meeting["run_at"]:
                continue
            await line.push(
                meeting["target_id"],
                f"會議將在 {settings.upload_lead_minutes} 分鐘後開始\n"
                f"會議：{meeting['title']}\n"
                f"時間：{starts:%Y/%m/%d %H:%M}\n"
                f"資料夾：{starts:%m%d}\n"
                f"資料上傳網址：{url}",
            )
            store.mark_done(meeting["id"], url)
            log.info("meeting_link_sent id=%s", meeting["id"])
        except Exception as exc:
            store.mark_failed(meeting["id"], repr(exc))
            log.exception("meeting_failed id=%s", meeting["id"])


async def worker():
    while True:
        if settings.orchestrator == "python":
            await process_due_once()
        await process_meeting_jobs()
        await asyncio.sleep(settings.poll_seconds)


async def execute_meeting_job(job: dict) -> dict:
    """Execute one leased operation; n8n chooses the branch and next action."""
    meeting = store.get(job["meeting_id"])
    if not meeting or not store.job_is_current(job):
        return {"ok": True, "skipped": True}
    start = datetime.fromisoformat(meeting["starts_at"])
    title = meeting["title"]
    operation = job["operation"]
    if not settings.submission_checks_enabled and operation in {"未繳提醒", "進度檢查", "補傳檢查"}:
        store.finish_job(job, datetime.now(settings.timezone))
        return {"ok": True, "skipped": True}
    upload_url = None
    progress = None
    report = None
    observed = None
    try:
        if operation == "建立上傳連結":
            upload_url = meeting["upload_url"] or await nas.create_upload_folder(
                title, start, folder_name=store.folder_name(meeting["id"]),
            )
        elif operation == "進度檢查":
            members = store.roster(meeting["id"]) or []
            all_items = store.progress_items(meeting["id"])
            selected = store.progress_job_items(job["id"])
            check_items = all_items if selected is None else selected
            results = {}
            if check_items:
                folder_name = store.folder_name(meeting["id"])
                if not folder_name:
                    raise RuntimeError("尚未取得此場會議的 NAS 資料夾")
                versions = await nas.submission_versions(folder_name) if selected is None else None
                async with nas.open_submissions(folder_name, download=True, names=set(versions) if versions is not None else None) as files:
                    judge = partial(
                        check_submission, api_key=os.getenv("OPENAI_API_KEY", ""),
                        model=os.getenv("OPENAI_PROGRESS_MODEL", ""),
                    )
                    results = await asyncio.to_thread(check_progress, files, check_items, judge)
                    if selected is None:
                        observed = {path.name: versions[path.name] for path in files}
            with store.connect() as db:
                ai, reviews, _ = store.progress_snapshot(db, meeting["id"])
            if selected is None:
                ai = results
            else:
                for member, entries in results.items():
                    ai.setdefault(member, {}).update(entries)
            progress = ("full" if selected is None else "partial", results)
            report = progress_report(meeting, members, all_items, ai, reviews)
        elif operation == "補傳檢查":
            if datetime.now(settings.timezone) < start:
                folder_name = store.folder_name(meeting['id'])
                observed = await nas.submission_versions(folder_name)
                previous = store.observed_files(meeting['id'])
                new_files = changed_submissions(observed, previous)
                if new_files:
                    report, progress = await check_late_submissions(meeting, folder_name, new_files)
        else:
            if operation == "上傳通知":
                message = (
                    f"【今日會議資料上傳】\n會議：{title}\n時間：{start:%H:%M}\n"
                    f"檔案上傳連結：{meeting['upload_url']}"
                )
            else:
                members = store.roster(meeting["id"]) or []
                heading = "【繳交狀態】" if operation == "未繳提醒" else "【會議進度報告】"
                if not members:
                    message = f"{heading}{title} {start:%Y/%m/%d}\n此場會議名單為空白。"
                elif operation == "未繳提醒":
                    folder_name = store.folder_name(meeting["id"])
                    if not folder_name:
                        raise RuntimeError("尚未取得此場會議的 NAS 資料夾")
                    async with nas.open_submissions(folder_name) as files:
                        missing = [name for name, paths in submissions_by_member(files, members).items() if not paths]
                    message = f"【尚未繳交提醒】{title} {start:%H:%M}\n" + (
                        f"尚未繳交：{'、'.join(missing)}\n請於開會前完成上傳：{meeting['upload_url']}"
                        if missing else "全員已上傳。"
                    )
            if store.job_is_current(job):
                report = message
    except Exception:
        log.exception("meeting_job_failed id=%s operation=%s", job["id"], operation)
        return {"ok": False, "job_id": job["id"], "lease_until": job["lease_until"]}
    else:
        store.finish_job(
            job, datetime.now(settings.timezone), upload_url=upload_url, report=report,
            progress=progress, observed=observed,
        )
    return {"ok": True, "job_id": job["id"]}


def record_job_failure(job: dict):
    meeting = store.get(job["meeting_id"])
    if not meeting or not store.job_is_current(job):
        return {"skipped": True}
    start = datetime.fromisoformat(meeting["starts_at"])
    title, operation = meeting["title"], job["operation"]
    store.finish_job(
        job, datetime.now(settings.timezone), "作業執行失敗",
        failure_message=(
            f"【作業失敗】{title} {start:%Y/%m/%d}\n失敗項目：{operation}，已用盡自動重試。\n"
            f"手動重試：/助理 重試 {start:%Y/%m/%d %H:%M} {title} {operation}"
        ),
    )
    with store.connect() as db:
        state = db.execute("SELECT status,attempts,due_at FROM meeting_jobs WHERE id=?", (job['id'],)).fetchone()
    return {"recorded": True, "status": state[0], "attempts": state[1], "due_at": state[2]}


async def process_meeting_jobs() -> int:
    """Compatibility worker for explicitly selected Python mode only."""
    processed = 0
    while processed < 20 and (job := store.claim_job(datetime.now(settings.timezone))):
        processed += 1
        result = await execute_meeting_job(job)
        if not result["ok"]:
            record_job_failure(job)
    for _ in range(20):
        notification = store.claim_notification(datetime.now(settings.timezone))
        if not notification:
            break
        try:
            await line.push(notification["target_id"], notification["message"])
        except Exception:
            log.exception("meeting_notification_failed id=%s", notification["id"])
            store.finish_notification(notification, datetime.now(settings.timezone) + timedelta(minutes=1))
        else:
            store.finish_notification(notification)
    return processed


async def check_late_submissions(meeting: dict, folder_name: str, new_files: set[str]):
    items = store.progress_items(meeting['id'])
    with store.connect() as db:
        ai, reviews, _ = store.progress_snapshot(db, meeting['id'])
    candidates = submissions_by_member(map(Path, new_files), items)
    pending = {
        member: [item for item in items[member] if ai.get(member, {}).get(item, (None,))[0] != "完成"]
        for member, paths in candidates.items() if paths
    }
    pending = {member: choices for member, choices in pending.items() if choices}
    if not pending:
        return None, None
    selected_files = {path.name for member in pending for path in candidates[member]}
    judge = partial(check_submission, api_key=os.getenv("OPENAI_API_KEY", ""), model=os.getenv("OPENAI_PROGRESS_MODEL", ""))
    async with nas.open_submissions(folder_name, download=True, names=selected_files) as files:
        checked = await asyncio.to_thread(check_progress, files, pending, judge)

    notices = []
    for member, results in checked.items():
        completed = {item: outcome for item, outcome in results.items() if outcome[0] == "完成"}
        merged = {**ai.get(member, {}), **completed}
        result = member_result(items[member], merged)
        if member in reviews:
            status, reason = reviews[member]
            result = f"{status}（人工複核，{reason}）（AI判定：{result}）"
        if not completed and any(outcome[0] == "無法判定" for outcome in results.values()):
            result = f"本次檔案無法判定，保留原缺項。{result}"
        notices.append(f"{member}｜{result}")
    return "【補傳檢查】\n" + "\n".join(notices), ("late", checked)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(worker()) if settings.orchestrator == "python" else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="LINE Meeting Link Bot", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "status": "ok", "orchestrator": settings.orchestrator,
        "internal_meeting_jobs": settings.orchestrator == "python",
        "workflow_version": "n8n-v3",
        "submission_checks_enabled": settings.submission_checks_enabled,
    }


class MeetingPrepareRequest(BaseModel):
    event_id: str
    target_id: str
    target_type: str = "line_group"
    text: str
    received_at: datetime | None = None


class LineReplyRequest(BaseModel):
    reply_token: str
    text: str


class LinePushRequest(BaseModel):
    target_id: str
    text: str


def require_core_api_key(x_core_api_key: Annotated[str, Header()] = ""):
    if not settings.core_api_key or x_core_api_key != settings.core_api_key:
        raise HTTPException(401, "invalid core API key")


CoreAuth = Annotated[None, Depends(require_core_api_key)]


class LeaseRequest(BaseModel):
    id: int
    lease_until: str


JOB_OPERATIONS = {
    "create-upload": "建立上傳連結",
    "upload-notice": "上傳通知",
    "missing-reminder": "未繳提醒",
    "progress-check": "進度檢查",
    "late-check": "補傳檢查",
}
job_locks: dict[int, asyncio.Lock] = {}


def leased_row(table: str, payload: LeaseRequest) -> dict:
    # Table names are internal constants, never supplied by a caller.
    import sqlite3
    with store.connect() as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            f"SELECT * FROM {table} WHERE id=? AND lease_until=?",
            (payload.id, payload.lease_until),
        ).fetchone()
    if row is None or datetime.fromisoformat(payload.lease_until) <= datetime.now(settings.timezone):
        raise HTTPException(409, "stale or expired lease")
    result = dict(row)
    if table == "meeting_jobs" and result["status"] != "running":
        raise HTTPException(409, "job is not running")
    meeting = store.get(result["meeting_id"])
    if not meeting or meeting["status"] == "cancelled":
        raise HTTPException(409, "meeting is no longer active")
    return {**result, "target_id": meeting["target_id"]}


@app.post("/api/v1/meetings/commands")
async def meeting_command(payload: MeetingPrepareRequest, _: CoreAuth):
    now = (payload.received_at or datetime.now(settings.timezone)).astimezone(settings.timezone)
    reply = await assistant_reply(payload.event_id, payload.target_id, payload.text, now)
    return {"handled": reply is not None, "reply": reply or ""}


@app.post("/api/v1/meetings/jobs/claim")
async def claim_meeting_job(_: CoreAuth):
    job = store.claim_job(datetime.now(settings.timezone))
    if job is None:
        return {"available": False}
    key = next((key for key, value in JOB_OPERATIONS.items() if value == job["operation"]), "unknown")
    return {"available": True, "operation_key": key, "id": job["id"], "lease_until": job["lease_until"]}


@app.post("/api/v1/meetings/jobs/execute/{operation_key}")
async def execute_job_operation(operation_key: str, payload: LeaseRequest, _: CoreAuth):
    lock = job_locks.setdefault(payload.id, asyncio.Lock())
    try:
        async with lock:
            job = leased_row("meeting_jobs", payload)
            if JOB_OPERATIONS.get(operation_key) != job["operation"]:
                raise HTTPException(409, "operation does not match leased job")
            return await execute_meeting_job(job)
    finally:
        if not lock.locked() and not getattr(lock, "_waiters", None):
            job_locks.pop(payload.id, None)


@app.post("/api/v1/meetings/jobs/failure")
async def fail_job_operation(payload: LeaseRequest, _: CoreAuth):
    return record_job_failure(leased_row("meeting_jobs", payload))


@app.post("/api/v1/meetings/notifications/claim")
async def claim_meeting_notification(_: CoreAuth):
    row = store.claim_notification(datetime.now(settings.timezone))
    return {"available": False} if row is None else {
        "available": True, "id": row["id"], "lease_until": row["lease_until"],
    }


@app.post("/api/v1/meetings/notifications/send")
async def send_meeting_notification(payload: LeaseRequest, _: CoreAuth):
    row = leased_row("meeting_notification_outbox", payload)
    try:
        await line.push(row["target_id"], row["message"])
    except Exception:
        log.exception("meeting_notification_failed id=%s", row["id"])
        return {"ok": False}
    store.finish_notification(row)
    return {"ok": True}


@app.post("/api/v1/meetings/notifications/defer")
async def defer_meeting_notification(payload: LeaseRequest, _: CoreAuth):
    row = leased_row("meeting_notification_outbox", payload)
    store.finish_notification(row, datetime.now(settings.timezone) + timedelta(minutes=1))
    return {"deferred": True}


@app.post("/api/v1/meetings/jobs/run-due")
async def run_due_meeting_jobs(_: CoreAuth):
    if settings.orchestrator == "n8n":
        raise HTTPException(409, "Use the n8n v3 claim and operation APIs")
    return {"processed": await process_meeting_jobs()}


@app.post("/api/v1/meetings/prepare")
async def prepare_meeting(payload: MeetingPrepareRequest, _: CoreAuth):
    now = (payload.received_at or datetime.now(settings.timezone)).astimezone(settings.timezone)
    meeting = parse_meeting(payload.text, now, settings.timezone)
    if not meeting:
        return {"accepted": False, "reason": "not_a_meeting"}

    assistant_command = is_assistant_command(payload.text)
    run_at = max(now, meeting.starts_at - timedelta(minutes=settings.upload_lead_minutes))
    if store.is_recent_duplicate(payload.target_id, meeting.title, meeting.starts_at, now):
        return {"accepted": False, "reason": "recent_duplicate"}

    added = store.add(
        payload.event_id,
        payload.target_id,
        payload.target_type,
        meeting.title,
        meeting.starts_at,
        run_at,
        members=[] if assistant_command else None,
        registered_at=now if assistant_command else None,
    )
    if not added:
        return {"accepted": False, "reason": "duplicate_event"}

    # The inserted row is uniquely addressable by event id, and will be the
    # newest matching record. This keeps the v1 SQLite store backward compatible.
    with store.connect() as db:
        row = db.execute(
            "SELECT id FROM meetings WHERE webhook_event_id=?", (payload.event_id,)
        ).fetchone()
    result = {
        "accepted": True,
        "meeting_id": row[0],
        "title": meeting.title,
        "starts_at": meeting.starts_at.isoformat(),
        "run_at": run_at.isoformat(),
        "folder_name": meeting.starts_at.strftime("%m%d"),
        "target_id": payload.target_id,
        "lead_minutes": settings.upload_lead_minutes,
    }
    if assistant_command:
        result["folder_name"] = store.folder_name(row[0])
        with store.connect() as db:
            meetings = store.meetings_on(db, payload.target_id, meeting.starts_at.strftime("%Y/%m/%d"))
        result["identifier"] = next(entry["identifier"] for entry in meetings if entry["id"] == row[0])
        result["schedule"] = [
            {"operation": operation, "run_at": due_at.isoformat()}
            for operation, due_at in initial_meeting_schedule(meeting.starts_at, now)
        ]
    return result


@app.post("/api/v1/meetings/{meeting_id}/create-file-request")
async def create_file_request(meeting_id: int, _: CoreAuth, expected_start: str | None = None):
    meeting = store.get(meeting_id)
    if not meeting:
        raise HTTPException(404, "meeting not found")
    if meeting["status"] == "cancelled" or (expected_start and meeting["starts_at"] != expected_start):
        return {"meeting_id": meeting_id, "skipped": True}
    try:
        starts = datetime.fromisoformat(meeting["starts_at"])
        url = meeting["upload_url"] or await nas.create_upload_folder(meeting["title"], starts)
        current = store.get(meeting_id)
        if not current or current["status"] == "cancelled" or current["starts_at"] != meeting["starts_at"]:
            return {"meeting_id": meeting_id, "skipped": True}
        store.mark_done(meeting_id, url)
        return {
            "meeting_id": meeting_id,
            "title": meeting["title"],
            "starts_at": meeting["starts_at"],
            "folder_name": starts.strftime("%m%d"),
            "target_id": meeting["target_id"],
            "upload_url": url,
            "already_done": meeting["status"] == "done" and bool(meeting["upload_url"]),
        }
    except Exception as exc:
        store.mark_failed(meeting_id, repr(exc))
        raise


@app.post("/api/v1/line/reply")
async def line_reply(payload: LineReplyRequest, _: CoreAuth):
    if not payload.reply_token:
        return {"ok": True, "skipped": True}
    await line.reply(payload.reply_token, payload.text)
    return {"ok": True, "skipped": False}


@app.post("/api/v1/line/push")
async def line_push(payload: LinePushRequest, _: CoreAuth):
    await line.push(payload.target_id, payload.text)
    return {"ok": True}


async def query_submissions(target_id: str, selector: str) -> str:
    with store.connect() as db:
        meeting, error = resolve_meeting(store, db, target_id, selector)
    if meeting is None:
        return error
    starts = datetime.fromisoformat(meeting['starts_at'])
    heading = f"【繳交狀態】{meeting['title']} {starts:%Y/%m/%d}"
    members = store.roster(meeting['id']) or []
    if not members:
        return f"{heading}\n此場會議名單為空白。"
    folder_name = store.folder_name(meeting['id'])
    if not meeting['upload_url'] or not folder_name:
        return f"{heading}\n此場會議的上傳資料夾還沒準備好。"
    try:
        async with nas.open_submissions(folder_name) as files:
            assigned = submissions_by_member(files, members)
    except Exception:
        log.exception("submission_query_failed meeting_id=%s", meeting['id'])
        return f"{heading}\n暫時無法讀取 NAS 檔案清單。"
    return "\n".join([heading, *(f"{member}：{'已上傳' if paths else '尚未上傳'}" for member, paths in assigned.items())])


@app.post("/line/webhook")
async def webhook(request: Request, x_line_signature: str = Header(default="")):
    body = await request.body()
    if not valid_signature(body, x_line_signature, settings.line_channel_secret):
        raise HTTPException(401, "invalid LINE signature")
    payload = await request.json()
    events = payload.get("events", [])
    log.info("line_webhook_received events=%s", len(events))
    for event in events:
        source = event.get("source", {})
        source_type = source.get("type")
        if source_type not in {"group", "room", "user"}:
            source_type = "unknown"
        if event.get("type") != "message" or event.get("message", {}).get("type") != "text":
            log.info("line_event_ignored reason=not_text_message source=%s", source_type)
            continue
        target_id = source.get("groupId") or source.get("roomId") or source.get("userId")
        if not target_id:
            log.info("line_event_ignored reason=no_target source=%s", source_type)
            continue
        message = event["message"]["text"]
        assistant_command = is_assistant_command(message)
        log.info(
            "line_text_received source=%s group=%s assistant_command=%s reply_token=%s",
            source_type, bool(source.get("groupId")), assistant_command, bool(event.get("replyToken")),
        )
        if assistant_command and not source.get("groupId"):
            log.info("line_event_ignored reason=assistant_command_outside_group source=%s", source_type)
            continue
        if not assistant_command and not is_line_meeting_command(message):
            continue
        if settings.orchestrator == "n8n":
            intake = {
                "event_id": f"message:{event['message']['id']}",
                "target_id": target_id,
                "target_type": source.get("type", "user"),
                "text": message,
                "reply_token": event.get("replyToken", ""),
                "received_at": datetime.now(settings.timezone).isoformat(),
                "source": "line",
            }
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(settings.n8n_intake_url, json=intake)
                response.raise_for_status()
            log.info("line_forwarded_to_n8n event_id=%s", intake["event_id"])
            continue
        if assistant_command:
            reply = await assistant_reply(
                f"message:{event['message']['id']}", target_id, message, datetime.now(settings.timezone),
            )
            if reply is not None:
                log.info("line_assistant_request_handled reply_generated=%s", bool(reply))
                if reply and event.get("replyToken"):
                    await line.reply(event["replyToken"], reply)
                    log.info("line_assistant_reply_sent")
                continue
        if not assistant_command and not is_line_meeting_command(message):
            log.info("line_message_ignored reason=command_format")
            continue
        meeting = parse_meeting(message, datetime.now(settings.timezone), settings.timezone)
        if not meeting:
            log.info("non_meeting_message_ignored target=%s", target_id)
            continue
        now = datetime.now(settings.timezone)
        run_at = max(
            now,
            meeting.starts_at - timedelta(minutes=settings.upload_lead_minutes),
        )
        if store.is_recent_duplicate(target_id, meeting.title, meeting.starts_at, now):
            log.info("recent_duplicate_ignored target=%s", target_id)
            continue
        # Text message IDs identify the user message itself.  Deduplicating on
        # this value also covers rare cases where the same message arrives in
        # more than one webhook event with different webhookEventId values.
        dedupe_id = f"message:{event['message']['id']}"
        added = store.add(
            dedupe_id, target_id, source.get("type", "user"), meeting.title, meeting.starts_at, run_at,
            members=[] if assistant_command else None,
            registered_at=now if assistant_command else None,
        )
        if added:
            if assistant_command:
                with store.connect() as db:
                    meetings = store.meetings_on(db, target_id, meeting.starts_at.strftime("%Y/%m/%d"))
                identifier = next(row["identifier"] for row in meetings if row["webhook_event_id"] == dedupe_id)
                if event.get("replyToken"):
                    await line.reply(event["replyToken"], f"已登記{meeting.title}：{meeting.starts_at:%Y/%m/%d %H:%M}。會議編號：{identifier}。")
                continue
            await line.reply(
                event["replyToken"],
                f"已登記：{meeting.starts_at:%Y/%m/%d %H:%M}\n"
                f"我會在 {settings.upload_lead_minutes} 分鐘前建立 {meeting.starts_at:%m%d} 資料夾，"
                "並把檔案請求網址傳到這個對話。",
            )
    return {"ok": True}


async def assistant_reply(event_id: str, target_id: str, message: str, now: datetime) -> str | None:
    parts = message.split(maxsplit=3)
    submission_command = (
        parts[:3] == ["/助理", "查詢", "繳交"] or
        (len(parts) > 1 and (parts[1] in {"複核", "檢查進度"} or parts[1].startswith("進度報告:")))
    )
    if not settings.submission_checks_enabled and submission_command:
        return "繳交查詢和進度檢查目前尚未啟用。"
    reply = manage_meeting(
        store, event_id, target_id, message,
        now, settings.upload_lead_minutes,
    )
    if reply is None:
        reply = progress_command(store, event_id, target_id, message, now)
    if reply is None and parts[:3] == ["/助理", "查詢", "繳交"]:
        reply = await query_submissions(target_id, parts[3] if len(parts) == 4 else "")
    return reply
