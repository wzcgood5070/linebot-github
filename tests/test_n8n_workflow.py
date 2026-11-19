import json
from pathlib import Path


WORKFLOW = Path("n8n/workflows/line-meeting-orchestration.json")


def test_n8n_workflow_is_importable_json_with_expected_flow():
    data = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    names = {node["name"] for node in data["nodes"]}
    assert data["active"] is False
    assert {
        "LINE Intake",
        "Calendar Form Email Intake",
        "Normalize Intake",
        "Prepare Meeting",
        "Wait Until Lead Time",
        "Create NAS File Request",
        "Notify Target",
    } <= names
    assert "LINE Intake" in data["connections"]
    assert "Create NAS File Request" in data["connections"]


def test_n8n_workflow_contains_no_real_secrets():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "LINE_CHANNEL_ACCESS_TOKEN" not in text
    assert "SYNOLOGY_PASSWORD" not in text
    assert "CLOUDFLARE_TUNNEL_TOKEN" not in text
