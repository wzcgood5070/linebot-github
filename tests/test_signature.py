import base64
import hashlib
import hmac

from app.line_client import valid_signature


def test_signature():
    body, secret = b'{"events":[]}', "secret"
    signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    assert valid_signature(body, signature, secret)
    assert not valid_signature(body + b" ", signature, secret)

