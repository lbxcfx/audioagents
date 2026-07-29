from __future__ import annotations

from pathlib import Path
import sys

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.insights import InsightsService
from server.cloud_parity.store import AccessDeniedError, PlatformStore, ResourceNotFoundError


@pytest.fixture()
def services(tmp_path: Path) -> tuple[PlatformStore, InsightsService, dict, dict]:
    store = PlatformStore(tmp_path / "insights.sqlite3")
    store.initialize()
    first = store.create_project(name="First", slug="first", owner_id="owner-1")
    second = store.create_project(name="Second", slug="second", owner_id="owner-2")
    return store, InsightsService(store), first, second


def test_timeline_orders_events_and_aggregates_usage(services) -> None:
    _, insights, project, _ = services
    session = insights.create_session(
        project_id=project["id"],
        actor_id="owner-1",
        room_name="room-1",
        agent_name="sales-agent",
        metadata={"contact_id": 42},
    )
    first = insights.append_event(
        project_id=project["id"],
        actor_id="owner-1",
        session_id=session["id"],
        event_type="user.transcript",
        source="stt",
        payload={"text": "你好"},
        occurred_at="2026-07-25T10:00:02Z",
    )
    second = insights.append_event(
        project_id=project["id"],
        actor_id="owner-1",
        session_id=session["id"],
        event_type="agent.response",
        source="llm",
        payload={"text": "您好"},
        occurred_at="2026-07-25T10:00:01Z",
    )
    insights.record_usage(
        project_id=project["id"],
        actor_id="owner-1",
        session_id=session["id"],
        category="llm",
        provider="qwen",
        model="qwen-plus",
        quantity=100,
        unit="tokens",
        cost_usd=0.002,
        latency_ms=180,
    )

    timeline = insights.timeline(
        project_id=project["id"], user_id="owner-1", session_id=session["id"]
    )

    assert [item["sequence"] for item in timeline["events"]] == [1, 2]
    assert [item["id"] for item in timeline["events"]] == [first["id"], second["id"]]
    assert timeline["session"]["metadata"] == {"contact_id": 42}
    assert timeline["summary"] == {
        "event_count": 2,
        "usage_count": 1,
        "cost_usd": 0.002,
    }


def test_sessions_are_isolated_by_project(services) -> None:
    _, insights, first, second = services
    session = insights.create_session(
        project_id=first["id"], actor_id="owner-1", room_name="private-room"
    )

    with pytest.raises(AccessDeniedError):
        insights.timeline(
            project_id=first["id"], user_id="owner-2", session_id=session["id"]
        )
    with pytest.raises(ResourceNotFoundError):
        insights.timeline(
            project_id=second["id"], user_id="owner-2", session_id=session["id"]
        )


def test_viewer_can_read_but_cannot_write_session_events(services) -> None:
    store, insights, project, _ = services
    session = insights.create_session(
        project_id=project["id"], actor_id="owner-1", room_name="viewer-room"
    )
    store.add_membership(
        project_id=project["id"], actor_id="owner-1", user_id="viewer", role="viewer"
    )

    assert insights.timeline(
        project_id=project["id"], user_id="viewer", session_id=session["id"]
    )["session"]["id"] == session["id"]
    with pytest.raises(AccessDeniedError):
        insights.append_event(
            project_id=project["id"],
            actor_id="viewer",
            session_id=session["id"],
            event_type="forbidden",
            source="viewer",
        )


def test_close_session_is_idempotent_for_end_time(services) -> None:
    _, insights, project, _ = services
    session = insights.create_session(
        project_id=project["id"], actor_id="owner-1", room_name="close-room"
    )
    first = insights.close_session(
        project_id=project["id"], actor_id="owner-1", session_id=session["id"]
    )
    second = insights.close_session(
        project_id=project["id"], actor_id="owner-1", session_id=session["id"]
    )

    assert first["status"] == second["status"] == "completed"
    assert first["ended_at"] == second["ended_at"]


def test_worker_can_write_session_lifecycle_without_read_permission(services) -> None:
    store, insights, project, _ = services
    store.add_membership(
        project_id=project["id"], actor_id="owner-1", user_id="worker", role="worker"
    )

    session = insights.create_session(
        project_id=project["id"], actor_id="worker", room_name="worker-room"
    )
    event = insights.append_event(
        project_id=project["id"],
        actor_id="worker",
        session_id=session["id"],
        event_type="agent.started",
        source="worker",
    )
    closed = insights.close_session(
        project_id=project["id"], actor_id="worker", session_id=session["id"]
    )

    assert event["session_id"] == session["id"]
    assert closed["status"] == "completed"
    with pytest.raises(AccessDeniedError):
        insights.get_session(
            project_id=project["id"], user_id="worker", session_id=session["id"]
        )
