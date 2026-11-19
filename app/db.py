from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .meeting_schedule import initial_meeting_schedule


class MeetingStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS meetings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    webhook_event_id TEXT UNIQUE NOT NULL,
                    target_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    run_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    upload_url TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS meeting_rosters (
                    meeting_id INTEGER PRIMARY KEY REFERENCES meetings(id),
                    members TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS meeting_folders (
                    meeting_id INTEGER PRIMARY KEY REFERENCES meetings(id),
                    folder_name TEXT NOT NULL UNIQUE
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS meeting_identifiers (
                    target_id TEXT NOT NULL,
                    identifier TEXT NOT NULL,
                    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
                    PRIMARY KEY (target_id, identifier, meeting_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS meeting_command_events (
                    event_id TEXT PRIMARY KEY,
                    reply TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS meeting_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
                    operation TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    lease_until TEXT,
                    error TEXT
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS manual_job_retries (
                    job_id INTEGER PRIMARY KEY REFERENCES meeting_jobs(id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS meeting_notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
                    message TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    lease_until TEXT
                )
            """)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meeting_deliverables (
                    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
                    member TEXT NOT NULL,
                    item TEXT NOT NULL,
                    PRIMARY KEY (meeting_id, member, item)
                );
                CREATE TABLE IF NOT EXISTS meeting_progress_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
                    kind TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meeting_progress_jobs (
                    job_id INTEGER PRIMARY KEY REFERENCES meeting_jobs(id),
                    items TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meeting_observed_files (
                    meeting_id INTEGER PRIMARY KEY REFERENCES meetings(id),
                    filenames TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    def add(self, event_id: str, target_id: str, target_type: str, title: str,
            starts_at: datetime, run_at: datetime, *, members: list | None = None,
            registered_at: datetime | None = None) -> bool:
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                day = starts_at.strftime("%Y/%m/%d")
                self.remember_identifiers(db, target_id, day)
                inserted = db.execute(
                    "INSERT INTO meetings(webhook_event_id,target_id,target_type,title,starts_at,run_at,created_at) VALUES(?,?,?,?,?,?,?)",
                    (event_id, target_id, target_type, title, starts_at.isoformat(), run_at.isoformat(), datetime.now().astimezone().isoformat()),
                )
                if members is not None:
                    db.execute(
                        "INSERT INTO meeting_rosters(meeting_id, members) VALUES(?, ?)",
                        (inserted.lastrowid, json.dumps(members, ensure_ascii=False)),
                    )
                if registered_at is not None:
                    self.ensure_folder(db, inserted.lastrowid, starts_at)
                    self.replace_jobs(db, inserted.lastrowid, starts_at, registered_at, provision=True)
                self.remember_identifiers(db, target_id, day)
            return True
        except sqlite3.IntegrityError:
            return False

    def roster(self, meeting_id: int) -> list | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT members FROM meeting_rosters WHERE meeting_id=?", (meeting_id,)
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def ensure_folder(self, db: sqlite3.Connection, meeting_id: int, start: datetime):
        db.execute(
            "INSERT OR IGNORE INTO meeting_folders(meeting_id,folder_name) VALUES(?,?)",
            (meeting_id, f"{start:%Y%m%d}-{meeting_id}"),
        )

    def folder_name(self, meeting_id: int) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT folder_name FROM meeting_folders WHERE meeting_id=?", (meeting_id,)).fetchone()
        return row[0] if row else None

    def progress_items(self, meeting_id: int) -> dict[str, list[str]]:
        items = {}
        with self.connect() as db:
            for member, item in db.execute(
                "SELECT member,item FROM meeting_deliverables WHERE meeting_id=? ORDER BY rowid", (meeting_id,),
            ):
                items.setdefault(member, []).append(item)
        return items

    def progress_job_items(self, job_id: int) -> dict[str, list[str]] | None:
        with self.connect() as db:
            row = db.execute("SELECT items FROM meeting_progress_jobs WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def observed_files(self, meeting_id: int) -> dict[str, str | None]:
        with self.connect() as db:
            row = db.execute("SELECT filenames FROM meeting_observed_files WHERE meeting_id=?", (meeting_id,)).fetchone()
        value = json.loads(row[0]) if row else {}
        return dict.fromkeys(value) if isinstance(value, list) else value

    def queue_progress_check(self, db, meeting_id: int, now: datetime, items: dict | None = None):
        job = db.execute(
            "INSERT INTO meeting_jobs(meeting_id,operation,due_at) VALUES(?, '進度檢查', ?)",
            (meeting_id, now.isoformat()),
        )
        if items is not None:
            db.execute("INSERT INTO meeting_progress_jobs(job_id,items) VALUES(?,?)", (job.lastrowid, json.dumps(items)))

    @staticmethod
    def progress_snapshot(db, meeting_id: int) -> tuple[dict, dict, bool]:
        ai, reviews = {}, {}
        has_report = False
        for kind, data in db.execute(
            "SELECT kind,result FROM meeting_progress_history WHERE meeting_id=? ORDER BY id", (meeting_id,),
        ):
            result = json.loads(data)
            if kind == 'review':
                reviews.update(result)
            elif kind == 'full':
                ai = result
                has_report = True
            else:
                for member, entries in result.items():
                    if kind == 'late':
                        entries = {item: value for item, value in entries.items() if value[0] == '完成'}
                    ai.setdefault(member, {}).update(entries)
        return ai, reviews, has_report

    @staticmethod
    def record_progress(db, meeting_id: int, kind: str, result: dict, now: datetime):
        db.execute(
            "INSERT INTO meeting_progress_history(meeting_id,kind,result,created_at) VALUES(?,?,?,?)",
            (meeting_id, kind, json.dumps(result, ensure_ascii=False), now.isoformat()),
        )

    def meetings_on(self, db: sqlite3.Connection, target_id: str, day: str) -> list[dict]:
        db.row_factory = sqlite3.Row
        meetings = [dict(row) for row in db.execute(
            """SELECT * FROM meetings WHERE target_id=? AND substr(starts_at, 1, 10)=?
               AND status<>'cancelled' ORDER BY starts_at, id""",
            (target_id, day.replace("/", "-")),
        )]
        for index, meeting in enumerate(meetings, 1):
            meeting["identifier"] = f"{day}-{index}" if len(meetings) > 1 else day
        return meetings

    def remember_identifiers(self, db: sqlite3.Connection, target_id: str, day: str):
        db.executemany(
            "INSERT OR IGNORE INTO meeting_identifiers(target_id, identifier, meeting_id) VALUES(?, ?, ?)",
            ((target_id, row["identifier"], row["id"]) for row in self.meetings_on(db, target_id, day)),
        )

    def stop_jobs(self, db: sqlite3.Connection, meeting_id: int):
        db.execute(
            "UPDATE meeting_jobs SET status='cancelled' WHERE meeting_id=? AND status IN ('pending','running','failed')",
            (meeting_id,),
        )
        db.execute("DELETE FROM meeting_notification_outbox WHERE meeting_id=?", (meeting_id,))

    def replace_jobs(self, db: sqlite3.Connection, meeting_id: int, starts_at: datetime,
                     now: datetime, *, provision: bool):
        self.stop_jobs(db, meeting_id)
        jobs = initial_meeting_schedule(starts_at, now)
        if provision:
            jobs.insert(0, ("建立上傳連結", now))
        db.executemany(
            "INSERT INTO meeting_jobs(meeting_id, operation, due_at) VALUES(?, ?, ?)",
            [(meeting_id, operation, due_at.isoformat()) for operation, due_at in jobs],
        )

    def claim_job(self, now: datetime) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.row_factory = sqlite3.Row
            job = db.execute(
                """SELECT job.* FROM meeting_jobs job JOIN meetings meeting ON meeting.id=job.meeting_id
                   WHERE meeting.status<>'cancelled' AND job.due_at<=?
                     AND (job.status='pending' OR (job.status='running' AND job.lease_until<=?))
                     AND NOT EXISTS (
                         SELECT 1 FROM meeting_jobs prior WHERE prior.meeting_id=job.meeting_id
                           AND prior.id<>job.id AND (
                             (prior.status='running' AND prior.lease_until>?) OR
                             (prior.operation='建立上傳連結' AND prior.status IN ('pending','running','failed'))
                           )
                     )
                   ORDER BY job.due_at, job.id LIMIT 1""",
                (now.isoformat(), now.isoformat(), now.isoformat()),
            ).fetchone()
            if job is None:
                return None
            lease = (now + timedelta(minutes=10)).isoformat()
            db.execute(
                "UPDATE meeting_jobs SET status='running', lease_until=? WHERE id=?", (lease, job['id']),
            )
            return {**dict(job), "lease_until": lease}

    def finish_job(self, job: dict, now: datetime, error: str | None = None,
                   *, upload_url: str | None = None, failure_message: str | None = None,
                   report: str | None = None, progress: tuple[str, dict] | None = None,
                   observed: dict[str, str] | None = None) -> bool:
        attempts = job['attempts'] + 1
        status = ("failed" if attempts >= 4 else "pending") if error else "done"
        next_due = (now + timedelta(seconds=30 * attempts)).isoformat() if error else job['due_at']
        with self.connect() as db:
            updated = db.execute(
                """UPDATE meeting_jobs SET status=?, attempts=?, due_at=?, lease_until=NULL, error=?
                   WHERE id=? AND status='running' AND lease_until=?""",
                (status, attempts, next_due, error, job['id'], job['lease_until']),
            )
            if updated.rowcount and upload_url:
                db.execute(
                    "UPDATE meetings SET status='done', upload_url=? WHERE id=?",
                    (upload_url, job['meeting_id']),
                )
            if updated.rowcount and status != "pending":
                if progress is not None and status == "done":
                    self.record_progress(db, job['meeting_id'], *progress, now)
                if observed is not None and status == "done":
                    db.execute(
                        "INSERT INTO meeting_observed_files(meeting_id,filenames) VALUES(?,?) "
                        "ON CONFLICT(meeting_id) DO UPDATE SET filenames=excluded.filenames",
                        (job['meeting_id'], json.dumps(observed, sort_keys=True)),
                    )
                    starts_at = db.execute("SELECT starts_at FROM meetings WHERE id=?", (job['meeting_id'],)).fetchone()[0]
                    next_scan = now + timedelta(seconds=30)
                    if next_scan < datetime.fromisoformat(starts_at):
                        db.execute(
                            """INSERT INTO meeting_jobs(meeting_id,operation,due_at)
                               SELECT ?, '補傳檢查', ? WHERE NOT EXISTS (
                                 SELECT 1 FROM meeting_jobs WHERE meeting_id=? AND operation='補傳檢查'
                                   AND status IN ('pending','running')
                               )""",
                            (job['meeting_id'], next_scan.isoformat(), job['meeting_id']),
                        )
                manual = db.execute("DELETE FROM manual_job_retries WHERE job_id=?", (job['id'],)).rowcount
                message = failure_message if status == "failed" else report
                if manual and status == "done":
                    status_message = f"【重試成功】已完成：{job['operation']}。"
                    message = f"{status_message}\n{message}" if message else status_message
                if message:
                    db.execute(
                        "INSERT INTO meeting_notification_outbox(meeting_id,message,due_at) VALUES(?,?,?)",
                        (job['meeting_id'], message, now.isoformat()),
                    )
            return bool(updated.rowcount) and status == "failed"

    def claim_notification(self, now: datetime) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.row_factory = sqlite3.Row
            row = db.execute(
                """SELECT outbox.*, meetings.target_id FROM meeting_notification_outbox outbox
                   JOIN meetings ON meetings.id=outbox.meeting_id WHERE meetings.status<>'cancelled'
                     AND outbox.due_at<=? AND (outbox.lease_until IS NULL OR outbox.lease_until<=?)
                   ORDER BY outbox.due_at, outbox.id LIMIT 1""",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            lease = (now + timedelta(minutes=2)).isoformat()
            db.execute("UPDATE meeting_notification_outbox SET lease_until=? WHERE id=?", (lease, row['id']))
            return {**dict(row), "lease_until": lease}

    def finish_notification(self, notification: dict, retry_at: datetime | None = None):
        key = (notification['id'], notification['lease_until'])
        with self.connect() as db:
            if retry_at is None:
                db.execute("DELETE FROM meeting_notification_outbox WHERE id=? AND lease_until=?", key)
            else:
                db.execute(
                    "UPDATE meeting_notification_outbox SET due_at=?, lease_until=NULL WHERE id=? AND lease_until=?",
                    (retry_at.isoformat(), *key),
                )

    def job_is_current(self, job: dict) -> bool:
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM meeting_jobs WHERE id=? AND status='running' AND lease_until=?",
                (job['id'], job['lease_until']),
            ).fetchone() is not None

    def is_recent_duplicate(self, target_id: str, title: str, starts_at: datetime,
                            now: datetime, window_seconds: int = 60) -> bool:
        cutoff = (now - timedelta(seconds=window_seconds)).isoformat()
        with self.connect() as db:
            row = db.execute(
                """SELECT 1 FROM meetings
                   WHERE target_id=? AND title=? AND starts_at=?
                     AND status IN ('pending','done') AND created_at>=?
                   LIMIT 1""",
                (target_id, title, starts_at.isoformat(), cutoff),
            ).fetchone()
        return row is not None

    def due(self, now: datetime) -> list[dict]:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(
                """SELECT * FROM meetings WHERE status='pending' AND run_at<=?
                   AND NOT EXISTS (SELECT 1 FROM meeting_jobs WHERE meeting_id=meetings.id) ORDER BY run_at""",
                (now.isoformat(),),
            )]

    def get(self, meeting_id: int) -> dict | None:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return dict(row) if row else None

    def mark_done(self, meeting_id: int, url: str):
        with self.connect() as db:
            db.execute(
                "UPDATE meetings SET status='done', upload_url=? WHERE id=? AND status<>'cancelled'",
                (url, meeting_id),
            )

    def mark_failed(self, meeting_id: int, error: str):
        with self.connect() as db:
            db.execute(
                "UPDATE meetings SET status='failed', error=? WHERE id=? AND status<>'cancelled'",
                (error[:1000], meeting_id),
            )
