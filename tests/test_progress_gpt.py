import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.progress_gpt import check_submission


def test_gpt_receives_pdf_and_returns_named_item_results():
    result = {"測試結果": {"status": "完成", "reason": "第 1 頁列出測試結果"}}
    response = {"status": "completed", "output": [{
        "type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}],
    }]}
    with TemporaryDirectory(dir=".") as directory:
        path = Path(directory) / "test.pdf"
        path.write_bytes(b"%PDF-1.7")
        with patch("app.progress_gpt.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as send:
            assert check_submission(path, ["測試結果"], api_key="test", model="test") == {
                "測試結果": ("完成", "第 1 頁列出測試結果"),
            }
        request = json.loads(send.call_args.args[0].data)
        assert request["input"][0]["content"][1]["type"] == "input_file"
        assert request["store"] is False


def test_office_conversion_failure_is_reported_without_calling_gpt():
    with patch("app.progress_gpt.subprocess.run", side_effect=FileNotFoundError), patch(
        "app.progress_gpt.urlopen"
    ) as send:
        result = check_submission(Path("test.pptx"), ["架構圖"], api_key="test", model="test")
    assert result == {"架構圖": ("無法判定", "Word 或 PowerPoint 無法轉成 PDF")}
    send.assert_not_called()
