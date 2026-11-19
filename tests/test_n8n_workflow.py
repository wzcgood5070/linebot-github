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


def test_n8n_controls_branches_and_never_calls_batch_worker():
    data = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    nodes = {node['name']: node for node in data['nodes']}
    assert len(nodes) == len(data['nodes'])
    assert len({node['id'] for node in data['nodes']}) == len(nodes)
    assert 'Run Due Meeting Jobs' not in nodes
    assert not any('/jobs/run-due' in n['parameters'].get('url', '') for n in nodes.values())
    assert data['connections']['Normalize Intake']['main'][0][0]['node'] == 'Process Group Command'
    assert "$('Normalize Intake')" in nodes['Prepare Meeting']['parameters']['jsonBody']
    assert nodes['LINE Intake']['parameters']['path'] == 'line-meeting-intake-v3'
    routes = data['connections']['Route Meeting Operation']['main']
    assert len(routes) == 6
    assert len({routes[i][0]['node'] for i in range(1, 6)}) == 5
    for i in range(1, 6):
        operation = nodes[routes[i][0]['node']]
        assert '/jobs/execute/' in operation['parameters']['url']
        assert operation['onError'] == 'continueErrorOutput'
        assert not operation.get('retryOnFail')  # retry only through persisted failure branch.
    for source, connection in data['connections'].items():
        assert source in nodes
        for output in connection['main']:
            assert all(edge['node'] in nodes for edge in output)


def test_n8n_workflow_contains_no_real_secrets():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "LINE_CHANNEL_ACCESS_TOKEN" not in text
    assert "SYNOLOGY_PASSWORD" not in text
    assert "CLOUDFLARE_TUNNEL_TOKEN" not in text
