from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import MeetingStore
from app.meeting_progress import check_progress, progress_command, progress_report


NOW = datetime(2026, 11, 5, 12, tzinfo=timezone(timedelta(hours=8)))


def test_append_after_report_checks_only_new_items_and_keeps_review_history(tmp_path):
    store = MeetingStore(str(tmp_path / "progress.db"))
    members = ["王小明", "李小華", "陳小美"]
    store.add("meeting", "group", "group", "會議", NOW + timedelta(hours=2), NOW, members=members)
    def command(event, text):
        return progress_command(store, event, "group", text, NOW)

    assert "已設定" in command("1", "/助理 進度報告:2026/11/05 王小明.李小華/API 串接")
    with store.connect() as db:
        store.record_progress(db, 1, "full", {"王小明": {"API 串接": ["完成", "測試"]}}, NOW)
    assert "已設定" in command("2", "/助理 進度報告:王小明/API 串接、架構圖")
    assert command("2", "/助理 進度報告:王小明/API 串接、架構圖") == ""
    job = store.claim_job(NOW)
    assert store.progress_job_items(job['id']) == {"王小明": ["架構圖"]}
    assert "第一次" in command("3", "/助理 複核 2026/11/05 王小明 完成 第一次")
    assert "第二次" in command("4", "/助理 複核 2026/11/05 王小明 未完成 第二次")
    store.finish_job(job, NOW, progress=("partial", {"王小明": {"架構圖": ["完成", "有圖"]}}))
    with store.connect() as db:
        ai, reviews, _ = store.progress_snapshot(db, 1)
        assert db.execute("SELECT COUNT(*) FROM meeting_progress_history WHERE kind='review'").fetchone()[0] == 2
    message = progress_report(store.get(1), members, store.progress_items(1), ai, reviews)
    assert "[王小明:未完成]（人工複核，第二次）（AI判定：完成）" in message
    assert "[陳小美:尚未設定交付項目]" in message


def test_progress_matches_shared_files_and_keeps_completed_evidence():
    first = Path("王小明.李小華_報告_20261105.pdf")
    second = Path("王小明_架構_20261105.pdf")
    calls = []

    def judge(path, items):
        calls.append((path, items))
        return {item: ("完成", "有證據") if item == "測試" or path == second else ("無法判定", "無架構圖") for item in items}

    results = check_progress([first, second], {"王小明": ["測試", "架構"], "李小華": ["測試"]}, judge)
    assert calls == [(first, ["測試", "架構"]), (second, ["架構"]), (first, ["測試"])]
    assert all(status == "完成" for items in results.values() for status, _ in items.values())


def test_incremental_results_preserve_missing_items_and_stop_polling_at_meeting_start(tmp_path):
    store = MeetingStore(str(tmp_path / "progress.db"))
    store.add("meeting", "group", "group", "會議", NOW + timedelta(seconds=60), NOW, members=["王小明"])
    with store.connect() as db:
        store.queue_progress_check(db, 1, NOW)
    original = {"王小明": {"架構": ["未完成", "尚未上傳"], "測試": ["未完成", "尚未上傳"]}}
    store.finish_job(store.claim_job(NOW), NOW, progress=("full", original), observed={"initial.pdf": "1"})
    next_scan = NOW + timedelta(seconds=30)
    job = store.claim_job(next_scan)
    assert job['operation'] == "補傳檢查"
    late = {"王小明": {"架構": ["完成", "有圖"], "測試": ["無法判定", "掃描不清"]}}
    store.finish_job(job, next_scan, progress=("late", late), observed={"initial.pdf": "1", "new.pdf": "2"})
    assert store.claim_job(NOW + timedelta(minutes=1)) is None
    assert store.observed_files(1) == {"initial.pdf": "1", "new.pdf": "2"}
    with store.connect() as db:
        ai, _, _ = store.progress_snapshot(db, 1)
    assert ai == {"王小明": {"架構": ["完成", "有圖"], "測試": ["未完成", "尚未上傳"]}}
