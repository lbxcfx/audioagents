"""Minimal process-local orchestration for the AudioAgent address book."""

from __future__ import annotations

import re
import threading
import time
from typing import Any


VOICE_MARKER = "[AUDIOAGENT_WEIXIN_VOICE_INPUT]"
_TTL_SECONDS = 10 * 60
_LOCK = threading.Lock()
_RESOLUTION_CONTEXT: dict[str, tuple[float, dict[str, Any]]] = {}
_PENDING: dict[str, tuple[float, dict[str, Any]]] = {}
_CONFIRMATION_CHOICE: dict[str, tuple[float, int]] = {}

_QUERY_PATTERNS = (
    re.compile(r"(?:请)?给\s*([\u3400-\u9fff·]{2,20}|[A-Za-z]{2,40})\s*打(?:个)?电话"),
    re.compile(r"(?:拨打|致电|电话联系)\s*([\u3400-\u9fff·]{2,20}|[A-Za-z]{2,40})"),
    re.compile(r"联系\s*([\u3400-\u9fff·]{2,20}|[A-Za-z]{2,40})\s*(?:，|,|。|\s|$)"),
)
_CONFIRM_PATTERN = re.compile(r"^\s*(?:确认|确定|是的|对)(?:\s*([1-9]))?\s*[。.!！]?$", re.I)


def _session_id(value: Any) -> str:
    return str(value or "").strip()[:200]


def _prune(now: float) -> None:
    threshold = now - _TTL_SECONDS
    for mapping in (_RESOLUTION_CONTEXT, _PENDING, _CONFIRMATION_CHOICE):
        for key, (created_at, _value) in list(mapping.items()):
            if created_at < threshold:
                mapping.pop(key, None)


def extract_query(text: str) -> str:
    cleaned = str(text or "").replace(VOICE_MARKER, "").strip().strip('"“”')
    for pattern in _QUERY_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return match.group(1).strip()
    return ""


def mark_resolution_context(
    session_id: Any, *, query: str, input_mode: str
) -> None:
    identifier = _session_id(session_id)
    if not identifier:
        return
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        _RESOLUTION_CONTEXT[identifier] = (
            now,
            {"query": query.strip(), "input_mode": input_mode},
        )


def resolution_context(session_id: Any) -> dict[str, Any] | None:
    identifier = _session_id(session_id)
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        stored = _RESOLUTION_CONTEXT.get(identifier)
    return dict(stored[1]) if stored else None


def store_pending(session_id: Any, payload: dict[str, Any]) -> None:
    identifier = _session_id(session_id)
    if not identifier:
        return
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        _PENDING[identifier] = (now, payload)


def pending(session_id: Any) -> dict[str, Any] | None:
    identifier = _session_id(session_id)
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        stored = _PENDING.get(identifier)
    return dict(stored[1]) if stored else None


def pop_pending(session_id: Any) -> dict[str, Any] | None:
    identifier = _session_id(session_id)
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        stored = _PENDING.pop(identifier, None)
    return dict(stored[1]) if stored else None


def note_confirmation(session_id: Any, text: str) -> bool:
    match = _CONFIRM_PATTERN.fullmatch(str(text or ""))
    if not match or pending(session_id) is None:
        return False
    choice = int(match.group(1) or "1")
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        _CONFIRMATION_CHOICE[_session_id(session_id)] = (now, choice)
    return True


def confirmation_choice(session_id: Any, fallback: int = 1) -> int:
    identifier = _session_id(session_id)
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        stored = _CONFIRMATION_CHOICE.pop(identifier, None)
    return int(stored[1]) if stored else fallback


def mark_weixin_voice_input(**kwargs: Any) -> dict[str, Any] | None:
    """Tag raw Weixin voice events before Hermes performs central STT."""

    event = kwargs.get("event")
    source = getattr(event, "source", None)
    platform_value = getattr(source, "platform", None)
    message_type_value = getattr(event, "message_type", None)
    platform = getattr(platform_value, "value", platform_value)
    message_type = getattr(message_type_value, "value", message_type_value)
    if str(platform or "").lower() != "weixin" or str(message_type or "").lower() != "voice":
        return None
    text = str(getattr(event, "text", "") or "").strip()
    if VOICE_MARKER not in text:
        text = f"{text}\n{VOICE_MARKER}".strip()
    return {"action": "rewrite", "text": text}


def clear_state() -> None:
    with _LOCK:
        _RESOLUTION_CONTEXT.clear()
        _PENDING.clear()
        _CONFIRMATION_CHOICE.clear()
