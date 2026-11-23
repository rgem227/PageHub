import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from fastapi import HTTPException
from pwdlib import PasswordHash

from .config import settings

passwords = PasswordHash.recommended()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def token() -> str:
    return secrets.token_urlsafe(32)


def phone_number(value: str) -> str:
    value = value.strip().replace(' ', '').replace('-', '')
    if value.startswith('+86'):
        value = value[3:]
    if not re.fullmatch(r'1[3-9]\d{9}', value):
        raise HTTPException(422, '请输入有效的中国大陆手机号')
    return value


def sign(payload: dict, seconds=300) -> str:
    data_payload = {**payload, **({'exp': int(time.time()) + seconds} if seconds is not None else {})}
    data = base64.urlsafe_b64encode(json.dumps(data_payload).encode()).decode().rstrip('=')
    signature = hmac.new(settings.secret_key.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f'{data}.{signature}'


def verify(value: str) -> dict:
    try:
        data, signature = value.split('.')
        expected = hmac.new(settings.secret_key.encode(), data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
        if 'exp' in payload and payload['exp'] <= time.time():
            raise ValueError()
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(403, '页面授权已过期，请重新打开页面')
