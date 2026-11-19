import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import datetime, timedelta

import httpx

from app import main
from app.db import MeetingStore


def test_signed_commands_are_forwarded_to_n8n_before_business_logic(monkeypatch, tmp_path):
    store = MeetingStore(str(tmp_path / 'routing.db'))
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(main, 'settings', replace(main.settings, orchestrator='n8n', internal_meeting_jobs=True))
    forwarded = []
    client_type = httpx.AsyncClient

    async def intake(request):
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={'received': True})

    def client(*args, **kwargs):
        if 'transport' not in kwargs:
            kwargs['transport'] = httpx.MockTransport(intake)
        return client_type(*args, **kwargs)

    monkeypatch.setattr(main.httpx, 'AsyncClient', client)

    async def scenario():
        async with client_type(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as api:
            for i, text in enumerate(['/助理 新增 2026/12/01 14:30 會議', '/助理 查詢 今天', '/助理 取消 2026/12/01']):
                body = json.dumps({'events': [{
                    'type': 'message', 'message': {'type': 'text', 'id': str(i), 'text': text},
                    'source': {'type': 'group', 'groupId': 'group'}, 'replyToken': str(i),
                }]}).encode()
                signature = base64.b64encode(hmac.digest(main.settings.line_channel_secret.encode(), body, hashlib.sha256)).decode()
                response = await api.post('/line/webhook', content=body, headers={'X-Line-Signature': signature})
                assert response.status_code == 200
        assert len(forwarded) == 3
        assert store.get(1) is None  # n8n has not called the command/prepare API yet.
        assert main.health()['internal_meeting_jobs'] is False
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
            assert store.get(1) is None

    asyncio.run(scenario())


def test_n8n_claim_branch_failure_retry_and_notification_pipeline(monkeypatch, tmp_path):
    store = MeetingStore(str(tmp_path / 'controller.db'))
    clock = datetime.fromisoformat('2026-11-19T13:10:00+08:00')

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    class Nas:
        calls = 0

        async def create_upload_folder(self, title, starts, *, folder_name):
            self.calls += 1
            assert folder_name == '20261119-1'
            if self.calls == 1:
                raise RuntimeError('temporary NAS outage')
            return 'https://example.test/sharing/abc'

    class Line:
        calls = 0
        sent = []

        async def push(self, target, message):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('temporary LINE outage')
            self.sent.append((target, message))

    line, nas = Line(), Nas()
    monkeypatch.setattr(main, 'datetime', Clock)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(main, 'nas', nas)
    monkeypatch.setattr(main, 'line', line)
    monkeypatch.setattr(main, 'settings', replace(main.settings, orchestrator='n8n', submission_checks_enabled=False))

    async def scenario():
        nonlocal clock
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as api:
            headers = {'X-Core-API-Key': main.settings.core_api_key}

            async def post(path, body=None):
                return await api.post('/api/v1/' + path, json=body or {}, headers=headers)

            assert (await api.post('/api/v1/meetings/jobs/claim')).status_code == 401
            registered = await post('meetings/prepare', {
                'event_id': 'add', 'target_id': 'group', 'target_type': 'group',
                'text': '/助理 新增 2026/11/19 14:00 測試', 'received_at': clock.isoformat(),
            })
            assert registered.json()['accepted']
            claim = (await post('meetings/jobs/claim')).json()
            lease = {k: claim[k] for k in ('id', 'lease_until')}
            assert claim['operation_key'] == 'create-upload'
            assert not (await post('meetings/jobs/claim')).json()['available']
            assert (await post('meetings/jobs/execute/progress-check', lease)).status_code == 409
            assert not (await post('meetings/jobs/execute/create-upload', lease)).json()['ok']
            # API does not retry autonomously. n8n explicitly takes the failure branch.
            assert nas.calls == 1
            assert (await post('meetings/jobs/failure', lease)).json()['recorded']
            assert not (await post('meetings/jobs/claim')).json()['available']
            clock += timedelta(seconds=31)
            claim = (await post('meetings/jobs/claim')).json()
            lease = {k: claim[k] for k in ('id', 'lease_until')}
            assert (await post('meetings/jobs/execute/create-upload', lease)).json()['ok']
            assert (await post('meetings/jobs/execute/create-upload', lease)).status_code == 409
            assert nas.calls == 2
            claim = (await post('meetings/jobs/claim')).json()
            assert claim['operation_key'] == 'upload-notice'
            lease = {k: claim[k] for k in ('id', 'lease_until')}
            assert (await post('meetings/jobs/execute/upload-notice', lease)).json()['ok']
            assert line.calls == 0  # delivery is a separate visible n8n flow.
            notice = (await post('meetings/notifications/claim')).json()
            lease = {k: notice[k] for k in ('id', 'lease_until')}
            assert not (await post('meetings/notifications/send', lease)).json()['ok']
            assert (await post('meetings/notifications/defer', lease)).json()['deferred']
            assert not (await post('meetings/notifications/claim')).json()['available']
            clock += timedelta(minutes=1)
            notice = (await post('meetings/notifications/claim')).json()
            lease = {k: notice[k] for k in ('id', 'lease_until')}
            assert (await post('meetings/notifications/send', lease)).json()['ok']
            assert not (await post('meetings/notifications/claim')).json()['available']
            assert 'https://example.test/sharing/abc' in line.sent[0][1]
            assert (await post('meetings/jobs/run-due')).status_code == 409
            reply = (await post('meetings/commands', {
                'event_id': 'cancel', 'target_id': 'group', 'text': '/助理 取消 2026/11/19',
                'received_at': clock.isoformat(),
            })).json()
            assert reply['handled'] and '已取消' in reply['reply']
            assert not (await post('meetings/jobs/claim')).json()['available']

    asyncio.run(scenario())
