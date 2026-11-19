from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.nas import LocalNas, SynologyBrowserNas, local_file_versions
from app.submissions import changed_submissions, submissions_by_member


def test_files_are_assigned_using_only_names_before_the_first_underscore():
    shared = Path("王小明.李小華_API測試_20261105.pdf")
    solo = Path("王小明_陳小美_測試結果_20261105.tmp")
    wrong = Path("陳小美_測試結果.pdf")

    assert submissions_by_member([shared, solo, wrong], ["王小明", "李小華", "陳小美"]) == {
        "王小明": [shared, solo], "李小華": [shared], "陳小美": [],
    }


def test_local_nas_lists_files_from_only_the_selected_meeting():
    with TemporaryDirectory(dir=".") as root, patch.dict("os.environ", {"LOCAL_NAS_ROOT": root}):
        nas = LocalNas()
        for folder in ("20261105-1", "20261105-2"):
            (Path(root) / folder).mkdir()
        uploaded = Path(root) / "20261105-1" / "王小明_報告_20261105.pdf"
        uploaded.touch()
        assert nas.submission_files("20261105-1") == [uploaded]
        assert nas.submission_files("20261105-2") == []


def test_synology_response_only_keeps_direct_files_in_the_requested_folder():
    folder = "/meetings/20261105-1"
    valid = {"name": "test.pdf", "path": f"{folder}/test.pdf", "isdir": False}
    data = SynologyBrowserNas._api_data({"success": True, "data": {"files": [
        valid,
        {"name": "nested", "path": f"{folder}/nested", "isdir": True},
        {"name": "test.pdf", "path": "/meetings/20261105-2/test.pdf", "isdir": False},
        {"name": "../test.pdf", "path": f"{folder}/../test.pdf", "isdir": False},
    ]}})
    assert SynologyBrowserNas._direct_files(data['files'], folder) == [valid]


def test_file_overwrite_is_a_new_submission_and_deletion_is_not(tmp_path):
    unchanged = tmp_path / "unchanged.pdf"
    removed = tmp_path / "removed.pdf"
    overwritten = tmp_path / "overwritten.pdf"
    for path in (unchanged, removed, overwritten):
        path.write_bytes(b"original")
    baseline = local_file_versions([unchanged, removed, overwritten])

    overwritten.write_bytes(b"replacement document")
    removed.unlink()
    current = local_file_versions([unchanged, removed, overwritten])
    assert changed_submissions(current, baseline) == {overwritten.name}


def test_synology_file_versions_ignore_reads_but_detect_writes():
    before = {"additional": {"size": 12, "time": {"atime": 1, "mtime": 2, "ctime": 3}}}
    after_read = {"additional": {"size": 12, "time": {"atime": 40, "mtime": 2, "ctime": 3}}}
    after_write = {"additional": {"size": 12, "time": {"atime": 40, "mtime": 41, "ctime": 42}}}
    assert SynologyBrowserNas._file_version(before) == SynologyBrowserNas._file_version(after_read)
    assert SynologyBrowserNas._file_version(before) != SynologyBrowserNas._file_version(after_write)


def test_browser_file_station_response_must_match_selected_folder():
    folder = "/會議/翁 (linebot)"
    entry = {"name": "test.pdf", "path": f"{folder}/test.pdf", "isdir": False}
    elsewhere = {"name": "test.pdf", "path": "/會議/其他/test.pdf", "isdir": False}
    response = {"data": {"result": [{"data": {"files": [elsewhere]}}, {"data": {"files": [entry]}}]}}
    assert SynologyBrowserNas._browser_listing(response, folder, "") == [entry]
    assert SynologyBrowserNas._browser_listing({"data": {"files": []}}, folder, f"folder_path={folder}") == []
    assert SynologyBrowserNas._browser_listing({"data": {"files": []}}, folder, "folder_path=/其他") is None
