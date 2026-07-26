from __future__ import annotations

from pathlib import Path
import sys

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.analytics import AnalyticsService
from server.cloud_parity.insights import InsightsService
from server.cloud_parity.store import AccessDeniedError, PlatformStore


@pytest.fixture()
def analytics_stack(tmp_path: Path):
    store = PlatformStore(tmp_path / "analytics.sqlite3")
    store.initialize()
    project = store.create_project(name="Analytics", slug="analytics", owner_id="owner")
    other = store.create_project(name="Other", slug="analytics-other", owner_id="other")
    insights = InsightsService(store)
    sessions = []
    for index in range(3):
        session = insights.create_session(
            project_id=project["id"], actor_id="owner", room_name=f"room-{index}"
        )
        insights.append_event(
            project_id=project["id"], actor_id="owner", session_id=session["id"],
            event_type="user.transcript", source="stt", payload={"index": index},
        )
        insights.record_usage(
            project_id=project["id"], actor_id="owner", session_id=session["id"],
            category="llm", provider="qwen", model="qwen-plus", quantity=10,
            unit="tokens", cost_usd=0.01, latency_ms=100 + index,
        )
        if index < 2:
            insights.close_session(
                project_id=project["id"], actor_id="owner", session_id=session["id"]
            )
        sessions.append(session)
    return store, AnalyticsService(store), project, other, sessions


def test_summary_matches_raw_session_and_usage_records(analytics_stack) -> None:
    _, analytics, project, _, _ = analytics_stack
    result = analytics.summary(project_id=project["id"], user_id="owner")

    assert result["sessions"]["total"] == 3
    assert result["sessions"]["completed"] == 2
    assert result["sessions"]["active"] == 1
    assert result["usage"][0]["quantity"] == 30
    assert result["usage"][0]["cost_usd"] == 0.03
    assert result["usage"][0]["request_count"] == 3
    assert result["events"] == {"user.transcript": 3}


def test_session_listing_uses_keyset_pagination_without_duplicates(analytics_stack) -> None:
    _, analytics, project, _, _ = analytics_stack
    first = analytics.list_sessions(
        project_id=project["id"], user_id="owner", limit=2
    )
    second = analytics.list_sessions(
        project_id=project["id"], user_id="owner", limit=2, cursor=first["next_cursor"]
    )

    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert not ({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
    assert second["next_cursor"] is None


def test_analytics_is_project_isolated(analytics_stack) -> None:
    _, analytics, project, other, _ = analytics_stack
    with pytest.raises(AccessDeniedError):
        analytics.summary(project_id=project["id"], user_id="other")
    assert analytics.summary(project_id=other["id"], user_id="other")["sessions"]["total"] == 0


def test_csv_export_is_streamed_and_requires_elevated_permission(analytics_stack) -> None:
    store, analytics, project, _, _ = analytics_stack
    chunks = list(analytics.export_csv(project_id=project["id"], user_id="owner"))
    content = "".join(chunks)
    assert content.startswith("id,room_name,agent_name,status")
    assert content.count("\n") == 4

    store.add_membership(
        project_id=project["id"], actor_id="owner", user_id="viewer", role="viewer"
    )
    with pytest.raises(AccessDeniedError):
        list(analytics.export_csv(project_id=project["id"], user_id="viewer"))
