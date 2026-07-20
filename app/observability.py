from __future__ import annotations

import hashlib
from contextvars import ContextVar, Token


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
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
