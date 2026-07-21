from __future__ import annotations

import hashlib
import hmac
from contextvars import ContextVar, Token

from app.config import settings


_request_id: ContextVar[str] = ContextVar("request_id", default="unknown")


def set_request_id(value: str) -> Token[str]:
    return _request_id.set(value)


def reset_request_id(token: Token[str]) -> None:
    _request_id.reset(token)


def get_request_id() -> str:
    return _request_id.get()


def safe_chat_id(value: str | None) -> str:
    if not value:
        return "none"
    secret = settings.search_guard_hmac_secret or settings.suvvy_webhook_token
    if not secret:
        # Never emit a reversible or cheaply enumerable identifier when the
        # keyed pseudonymisation secret has not been configured.
        return "present"
    return hmac.new(
        secret.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12]
