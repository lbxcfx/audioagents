"""Deterministic user-facing responses for outbound task submission."""

from __future__ import annotations

import re
import threading
import time
from typing import Any


_LOCK = threading.Lock()
_RESPONSES: dict[str, tuple[float, str]] = {}
_RESPONSE_TTL_SECONDS = 10 * 60
_MAX_RESPONSES = 1000


def _session_id(value: Any) -> str:
    return str(value or "").strip()[:200]


def _failure_text(payload: dict[str, Any]) -> str:
    reason = re.sub(
        r"\s+", " ", str(payload.get("error") or payload.get("message") or "")
    ).strip()
    if not reason or reason == "outbound task was not dialed":
        return "电话未拨出。"
    if not reason.endswith(("。", "！", "？", ".", "!", "?")):
        reason += "。"
    return f"电话未拨出：{reason}"


def remember_submission_response(session_id: Any, payload: dict[str, Any]) -> None:
    """Remember one response derived solely from the submit tool result."""

    identifier = _session_id(session_id)
    if not identifier:
        return
    queued_count = int(payload.get("queued_count") or 0)
    if payload.get("ok") is True and queued_count > 0:
        response = "拨号中..."
    else:
        response = _failure_text(payload)

    remember_user_response(identifier, response)


def remember_user_response(session_id: Any, response: str) -> None:
    """Remember arbitrary deterministic text produced by an AudioAgent tool."""

    identifier = _session_id(session_id)
    if not identifier or not str(response or "").strip():
        return
    now = time.monotonic()
    with _LOCK:
        expired_before = now - _RESPONSE_TTL_SECONDS
        for key, (created_at, _text) in list(_RESPONSES.items()):
            if created_at < expired_before:
                _RESPONSES.pop(key, None)
        if len(_RESPONSES) >= _MAX_RESPONSES:
            oldest = min(_RESPONSES, key=lambda key: _RESPONSES[key][0])
            _RESPONSES.pop(oldest, None)
        _RESPONSES[identifier] = (now, str(response).strip())


def transform_submission_response(**kwargs: Any) -> str | None:
    """Replace the LLM's Weixin acknowledgement with the recorded fact."""

    if str(kwargs.get("platform") or "").strip().lower() != "weixin":
        return None
    identifier = _session_id(kwargs.get("session_id"))
    if not identifier:
        return None
    with _LOCK:
        stored = _RESPONSES.pop(identifier, None)
    if stored is None:
        return None
    created_at, response = stored
    if time.monotonic() - created_at > _RESPONSE_TTL_SECONDS:
        return None
    return response


def pending_user_response(session_id: Any) -> str | None:
    """Return a fresh deterministic response without consuming it."""

    identifier = _session_id(session_id)
    if not identifier:
        return None
    with _LOCK:
        stored = _RESPONSES.get(identifier)
    if stored is None:
        return None
    created_at, response = stored
    if time.monotonic() - created_at > _RESPONSE_TTL_SECONDS:
        with _LOCK:
            _RESPONSES.pop(identifier, None)
        return None
    return response


def clear_submission_responses() -> None:
    """Clear process-local state for tests and controlled reloads."""

    with _LOCK:
        _RESPONSES.clear()
