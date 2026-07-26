from __future__ import annotations

from pathlib import Path
import sys

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.console import ConsoleService
from server.cloud_parity.insights import InsightsService
from server.cloud_parity.store import AccessDeniedError, PlatformStore


@pytest.fixture()
def console_stack(tmp_path: Path):
    issued: list[tuple[str, str, int]] = []

    def fake_issuer(room: str, identity: str, ttl: int) -> str:
        issued.append((room, identity, ttl))
        return f"signed:{room}:{identity}:{ttl}"

    store = PlatformStore(tmp_path / "console.sqlite3")
    store.initialize()
    project = store.create_project(name="Console", slug="console", owner_id="owner")
    insights = InsightsService(store)
    session = insights.create_session(
        project_id=project["id"],
        actor_id="owner",
        room_name="console-room",
        agent_name="console-agent",
    )
    console = ConsoleService(store, insights, token_issuer=fake_issuer)
    return store, insights, console, project, session, issued


def test_event_cursor_returns_only_new_events(console_stack) -> None:
    _, insights, console, project, session, _ = console_stack
    for number in range(3):
        insights.append_event(
            project_id=project["id"],
            actor_id="owner",
            session_id=session["id"],
            event_type="debug.event",
            source="test",
            payload={"number": number},
        )

    first = console.events_after(
        project_id=project["id"], user_id="owner", session_id=session["id"], limit=2
    )
    second = console.events_after(
        project_id=project["id"],
        user_id="owner",
        session_id=session["id"],
        after_sequence=first["cursor"],
    )

    assert [item["sequence"] for item in first["items"]] == [1, 2]
    assert [item["sequence"] for item in second["items"]] == [3]
    assert second["cursor"] == 3


def test_rpc_and_dtmf_are_queued_and_audited(console_stack) -> None:
    store, _, console, project, session, _ = console_stack
    rpc = console.queue_command(
        project_id=project["id"],
        actor_id="owner",
        session_id=session["id"],
        command_type="rpc",
        payload={"method": "lookup_customer", "arguments": {"id": 7}},
    )
    dtmf = console.queue_command(
        project_id=project["id"],
        actor_id="owner",
        session_id=session["id"],
        command_type="dtmf",
        payload={"digits": "12#"},
    )

    assert [item["id"] for item in console.list_commands(
        project_id=project["id"], user_id="owner", session_id=session["id"]
    )] == [rpc["id"], dtmf["id"]]
    actions = {
        item["action"]
        for item in store.list_audit_logs(project_id=project["id"], user_id="owner")
    }
    assert "console.rpc.queue" in actions
    assert "console.dtmf.queue" in actions


def test_observer_token_is_short_lived_hidden_and_owner_only(console_stack) -> None:
    store, _, console, project, session, issued = console_stack
    store.add_membership(
        project_id=project["id"], actor_id="owner", user_id="member", role="member"
    )

    result = console.observer_token(
        project_id=project["id"],
        actor_id="owner",
        session_id=session["id"],
        ttl_seconds=120,
    )

    assert issued[0][0] == "console-room"
    assert issued[0][2] == 120
    assert result["permissions"] == {"subscribe": True, "publish": False, "hidden": True}
    assert result["token"].startswith("signed:console-room:observer:owner:")
    with pytest.raises(AccessDeniedError):
        console.observer_token(
            project_id=project["id"],
            actor_id="member",
            session_id=session["id"],
        )


def test_invalid_dtmf_is_rejected(console_stack) -> None:
    _, _, console, project, session, _ = console_stack
    with pytest.raises(ValueError, match="invalid DTMF"):
        console.queue_command(
            project_id=project["id"],
            actor_id="owner",
            session_id=session["id"],
            command_type="dtmf",
            payload={"digits": "12X"},
        )


def test_agent_claims_and_completes_console_command(console_stack) -> None:
    _, _, console, project, session, _ = console_stack
    queued = console.queue_command(
        project_id=project["id"],
        actor_id="owner",
        session_id=session["id"],
        command_type="rpc",
        payload={"method": "agent.say", "arguments": {"text": "hello"}},
    )

    claimed = console.claim_commands(
        project_id=project["id"],
        actor_id="owner",
        session_id=session["id"],
        worker_id="agent-1",
    )
    assert [item["id"] for item in claimed] == [queued["id"]]
    assert console.claim_commands(
        project_id=project["id"],
        actor_id="owner",
        session_id=session["id"],
        worker_id="agent-2",
    ) == []

    completed = console.complete_command(
        project_id=project["id"],
        actor_id="owner",
        session_id=session["id"],
        command_id=queued["id"],
        worker_id="agent-1",
        status="completed",
        result={"spoken": True},
    )
    assert completed["status"] == "completed"
    assert completed["result"] == {"spoken": True}
    events = console.events_after(
        project_id=project["id"], user_id="owner", session_id=session["id"]
    )
    assert events["items"][-1]["event_type"] == "console.command.completed"
