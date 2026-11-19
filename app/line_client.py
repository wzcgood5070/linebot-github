from __future__ import annotations

import base64
import hashlib
import hmac

import httpx


def valid_signature(body: bytes, signature: str, secret: str) -> bool:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


class LineClient:
    def __init__(self, token: str):
        self.token = token

    async def _send(self, endpoint: str, payload: dict):
        if not self.token:
            return
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"https://api.line.me/v2/bot/message/{endpoint}",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
            )
            response.raise_for_status()

    async def reply(self, reply_token: str, text: str):
        await self._send("reply", {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:5000]}]})

    async def push(self, target_id: str, text: str):
        await self._send("push", {"to": target_id, "messages": [{"type": "text", "text": text[:5000]}]})

