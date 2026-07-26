from __future__ import annotations

import re


_PROJECT = r"/api/platform/projects/[^/]+"
_CALL = rf"{_PROJECT}/telephony/calls/[^/]+"
_SESSION = rf"{_PROJECT}/sessions/[^/]+"

_WORKER_POST_PATHS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"{_PROJECT}/telephony/campaigns/materialize",
        rf"{_PROJECT}/telephony/calls/inbound",
        rf"{_PROJECT}/telephony/dispatch/claim",
        rf"{_PROJECT}/telephony/reconciliation/claim",
        rf"{_CALL}/(?:observe|heartbeat|transition|result|recording)",
        rf"{_CALL}/transfers",
        rf"{_CALL}/transfers/[^/]+/transition",
        rf"{_PROJECT}/sessions",
        rf"{_SESSION}/(?:events|usage)",
        rf"{_SESSION}/console/commands/claim",
        rf"{_SESSION}/console/commands/[^/]+/complete",
    )
)


def is_worker_control_request(path: str, method: str) -> bool:
    """Return whether a request is an internal Dispatcher/Agent operation.

    Authentication and the exact ``worker`` project role are still enforced by
    the endpoint. This classification only selects a capacity-appropriate rate
    limit before endpoint dependencies run.
    """

    if method.upper() != "POST":
        return False
    normalized = path.rstrip("/") or "/"
    return any(pattern.fullmatch(normalized) for pattern in _WORKER_POST_PATHS)
