from __future__ import annotations

from server.cloud_parity.rate_limit import is_worker_control_request


def test_internal_worker_routes_use_the_worker_rate_tier() -> None:
    project = "/api/platform/projects/project-1"
    call = f"{project}/telephony/calls/call-1"
    session = f"{project}/sessions/session-1"

    worker_paths = (
        f"{project}/telephony/campaigns/materialize",
        f"{project}/telephony/dispatch/claim",
        f"{project}/telephony/reconciliation/claim",
        f"{call}/heartbeat",
        f"{call}/transition",
        f"{call}/result",
        f"{call}/recording",
        f"{session}/events",
        f"{session}/usage",
        f"{session}/console/commands/claim",
        f"{session}/console/commands/command-1/complete",
    )
    assert all(is_worker_control_request(path, "POST") for path in worker_paths)


def test_human_and_public_routes_stay_on_the_standard_rate_tier() -> None:
    project = "/api/platform/projects/project-1"

    assert not is_worker_control_request(
        f"{project}/telephony/calls/outbound", "POST"
    )
    assert not is_worker_control_request(f"{project}/telephony/metrics", "GET")
    assert not is_worker_control_request(f"{project}/sessions/session-1/close", "POST")
    assert not is_worker_control_request("/api/platform/projects", "POST")
