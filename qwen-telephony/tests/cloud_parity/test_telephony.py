from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.auth import DevelopmentAuthenticator, install_authenticator
from server.cloud_parity.deployment import SecretCipher
from server.cloud_parity.store import AccessDeniedError, PlatformStore
from server.cloud_parity.telephony import (
    CapacityExceededError,
    ComplianceBlockedError,
    IdempotencyConflictError,
    InvalidCallTransitionError,
    LeaseConflictError,
    TelephonyService,
)
from server.cloud_parity.telephony_api import create_telephony_router


@pytest.fixture()
def telephony_stack(tmp_path: Path) -> tuple[PlatformStore, TelephonyService, str]:
    store = PlatformStore(tmp_path / "telephony.sqlite3")
    assert store.initialize() >= 11
    project = store.create_project(name="Calls", slug="calls", owner_id="owner")
    service = TelephonyService(store)
    service.update_policy(
        project_id=project["id"],
        user_id="owner",
        timezone_name="UTC",
        allowed_weekdays=range(7),
        calling_window_start="00:00",
        calling_window_end="23:59",
        require_consent=False,
        consent_purpose="outbound",
        max_attempts_per_number_per_day=100,
    )
    return store, service, project["id"]


def _enqueue(
    service: TelephonyService,
    project_id: str,
    key: str,
    number: str,
    *,
    max_attempts: int = 3,
) -> dict:
    return service.enqueue_outbound(
        project_id=project_id,
        user_id="owner",
        idempotency_key=key,
        destination_number=number,
        source_number="+8610000000000",
        agent_name="commercial-agent",
        max_attempts=max_attempts,
    )


def _limits(
    service: TelephonyService,
    project_id: str,
    *,
    total: int,
    outbound: int,
    inbound: int,
    rate: int,
    lease: int = 10,
) -> None:
    service.update_limits(
        project_id=project_id,
        user_id="owner",
        max_concurrent_calls=total,
        max_outbound_calls=outbound,
        max_inbound_calls=inbound,
        max_calls_per_minute=rate,
        lease_seconds=lease,
    )


def test_trunks_limits_and_fine_grained_permissions(telephony_stack) -> None:
    store, service, project_id = telephony_stack
    store.add_membership(
        project_id=project_id, actor_id="owner", user_id="viewer", role="viewer"
    )
    trunk = service.upsert_trunk(
        project_id=project_id,
        user_id="owner",
        name="primary-pstn",
        direction="bidirectional",
        provider="telco",
        livekit_trunk_id="ST_test",
        secret_name="telco-sip-secret",
        numbers=["+8610000000000"],
    )

    assert trunk["numbers"] == ["+8610000000000"]
    assert service.list_trunks(project_id=project_id, user_id="viewer")[0]["id"] == trunk["id"]
    service.enqueue_outbound(
        project_id=project_id,
        user_id="owner",
        idempotency_key="trunk-routing",
        destination_number="+8613800000001",
        source_number="+8610000000000",
        agent_name="commercial-agent",
        trunk_id=trunk["id"],
    )
    claimed = service.claim_outbound(
        project_id=project_id,
        user_id="owner",
        worker_id="trunk-worker",
    )
    assert claimed[0]["livekit_trunk_id"] == "ST_test"
    viewer_call = service.get_call(
        project_id=project_id, user_id="viewer", call_id=claimed[0]["id"]
    )
    assert viewer_call["destination_number"].endswith("0001")
    assert viewer_call["destination_number"] != "+8613800000001"
    assert "destination_hash" not in viewer_call
    with pytest.raises(AccessDeniedError):
        service.update_limits(
            project_id=project_id,
            user_id="viewer",
            max_concurrent_calls=10,
            max_outbound_calls=8,
            max_inbound_calls=8,
            max_calls_per_minute=20,
            lease_seconds=30,
        )


def test_outbound_enqueue_is_idempotent_and_rejects_key_reuse(telephony_stack) -> None:
    _, service, project_id = telephony_stack

    first = _enqueue(service, project_id, "crm-order-42", "+8613800000001")
    repeated = _enqueue(service, project_id, "crm-order-42", "+8613800000001")

    assert repeated["id"] == first["id"]
    with pytest.raises(IdempotencyConflictError):
        _enqueue(service, project_id, "crm-order-42", "+8613800000002")
    with pytest.raises(ValueError, match="E.164"):
        _enqueue(service, project_id, "bad-phone", "13800000001")


def test_concurrent_workers_cannot_exceed_project_capacity(telephony_stack) -> None:
    _, service, project_id = telephony_stack
    _limits(service, project_id, total=1, outbound=1, inbound=1, rate=100)
    _enqueue(service, project_id, "call-a", "+8613800000001")
    _enqueue(service, project_id, "call-b", "+8613800000002")

    def claim(worker: str) -> list[dict]:
        return service.claim_outbound(
            project_id=project_id,
            user_id="owner",
            worker_id=worker,
            limit=1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("worker-a", "worker-b")))

    claimed = [call for batch in results for call in batch]
    assert len(claimed) == 1
    assert service.metrics(project_id=project_id, user_id="owner")["active_calls"] == 1
    assert service.metrics(project_id=project_id, user_id="owner")["queue_depth"] == 1


def test_rate_limit_and_lease_ownership_are_enforced(telephony_stack) -> None:
    _, service, project_id = telephony_stack
    _limits(service, project_id, total=2, outbound=2, inbound=2, rate=1)
    _enqueue(service, project_id, "rate-a", "+8613800000001")
    _enqueue(service, project_id, "rate-b", "+8613800000002")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)

    call = service.claim_outbound(
        project_id=project_id, user_id="owner", worker_id="worker-a", now=now
    )[0]
    with pytest.raises(LeaseConflictError):
        service.heartbeat(
            project_id=project_id,
            user_id="owner",
            call_id=call["id"],
            worker_id="worker-b",
            lease_token=call["lease_token"],
            now=now,
        )
    finished = service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=call["id"],
        worker_id="worker-a",
        lease_token=call["lease_token"],
        status="failed",
        failure_code="provider_rejected",
        now=now,
    )
    assert finished["status"] == "failed"
    assert service.claim_outbound(
        project_id=project_id,
        user_id="owner",
        worker_id="worker-b",
        now=now + timedelta(seconds=30),
    ) == []


def test_expired_pre_dial_lease_retries_but_dialing_call_requires_reconciliation(
    telephony_stack,
) -> None:
    _, service, project_id = telephony_stack
    _limits(service, project_id, total=2, outbound=2, inbound=2, rate=100, lease=10)
    first = _enqueue(
        service, project_id, "lease-retry", "+8613800000001", max_attempts=2
    )
    second = _enqueue(
        service, project_id, "lease-reconcile", "+8613800000002", max_attempts=2
    )
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    claimed = service.claim_outbound(
        project_id=project_id,
        user_id="owner",
        worker_id="worker-a",
        limit=2,
        now=now,
    )
    by_id = {item["id"]: item for item in claimed}
    dispatching = service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=second["id"],
        worker_id="worker-a",
        lease_token=by_id[second["id"]]["lease_token"],
        status="dispatching",
        now=now,
    )
    dialing = service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=second["id"],
        worker_id="worker-a",
        lease_token=dispatching["lease_token"],
        status="dialing",
        now=now,
    )
    assert dialing["status"] == "dialing"

    reclaimed = service.claim_outbound(
        project_id=project_id,
        user_id="owner",
        worker_id="worker-b",
        limit=2,
        now=now + timedelta(seconds=11),
    )

    assert [item["id"] for item in reclaimed] == [first["id"]]
    assert reclaimed[0]["attempt_count"] == 2
    assert reclaimed[0]["lease_token"] != by_id[first["id"]]["lease_token"]
    assert service.get_call(
        project_id=project_id, user_id="owner", call_id=second["id"]
    )["status"] == "reconciling"


def test_inbound_admission_is_idempotent_and_capacity_safe(telephony_stack) -> None:
    _, service, project_id = telephony_stack
    _limits(service, project_id, total=1, outbound=1, inbound=1, rate=100)

    inbound = service.admit_inbound(
        project_id=project_id,
        user_id="owner",
        provider="carrier",
        provider_call_id="provider-call-1",
        worker_id="inbound-worker",
        source_number="+8613800000001",
        destination_number="+8610000000000",
        agent_name="support-agent",
        room_name="inbound-room-1",
    )
    duplicate = service.admit_inbound(
        project_id=project_id,
        user_id="owner",
        provider="carrier",
        provider_call_id="provider-call-1",
        worker_id="inbound-worker",
        source_number="+8613800000001",
        destination_number="+8610000000000",
        agent_name="support-agent",
        room_name="inbound-room-1",
    )
    assert duplicate["id"] == inbound["id"]
    with pytest.raises(CapacityExceededError):
        service.admit_inbound(
            project_id=project_id,
            user_id="owner",
            provider="carrier",
            provider_call_id="provider-call-2",
            worker_id="inbound-worker",
            source_number="+8613800000002",
            destination_number="+8610000000000",
            agent_name="support-agent",
            room_name="inbound-room-2",
        )

    active = service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=inbound["id"],
        worker_id="inbound-worker",
        lease_token=inbound["lease_token"],
        status="active",
    )
    assert active["answered_at"] is not None
    completed = service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=inbound["id"],
        worker_id="inbound-worker",
        lease_token=active["lease_token"],
        status="completed",
    )
    assert completed["ended_at"] is not None
    with pytest.raises(InvalidCallTransitionError):
        service.transition_call(
            project_id=project_id,
            user_id="owner",
            call_id=inbound["id"],
            status="active",
        )


def test_consent_and_do_not_call_are_rechecked_before_dispatch(telephony_stack) -> None:
    store, service, project_id = telephony_stack
    number = "+8613800000042"
    service.update_policy(
        project_id=project_id,
        user_id="owner",
        timezone_name="UTC",
        allowed_weekdays=range(7),
        calling_window_start="00:00",
        calling_window_end="23:59",
        require_consent=True,
        consent_purpose="sales",
        max_attempts_per_number_per_day=3,
    )

    with pytest.raises(ComplianceBlockedError, match="consent"):
        _enqueue(service, project_id, "missing-consent", number)
    consent = service.record_consent(
        project_id=project_id,
        user_id="owner",
        phone_number=number,
        purpose="sales",
        status="granted",
        evidence_ref="crm://consents/42",
    )
    assert consent["phone_last4"] == "0042"
    assert "phone_hash" not in consent
    queued = _enqueue(service, project_id, "consented-call", number)

    dnc = service.upsert_do_not_call(
        project_id=project_id,
        user_id="owner",
        phone_number=number,
        reason="customer opt-out",
        source="customer-service",
    )
    assert "phone_hash" not in dnc
    assert service.claim_outbound(
        project_id=project_id, user_id="owner", worker_id="compliance-worker"
    ) == []
    blocked = service.get_call(
        project_id=project_id, user_id="owner", call_id=queued["id"]
    )
    assert blocked["status"] == "blocked"
    assert blocked["failure_code"] == "compliance_do_not_call"
    with store.connect() as conn:
        raw_audits = " ".join(
            str(row["payload_json"])
            for row in conn.execute(
                "SELECT payload_json FROM audit_logs WHERE project_id = ?", (project_id,)
            ).fetchall()
        )
    assert number not in raw_audits


def test_calling_window_schedules_and_daily_number_limit_blocks(telephony_stack) -> None:
    _, service, project_id = telephony_stack
    future = (datetime.now(timezone.utc) + timedelta(days=8)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    service.update_policy(
        project_id=project_id,
        user_id="owner",
        timezone_name="UTC",
        allowed_weekdays=[future.weekday()],
        calling_window_start="09:00",
        calling_window_end="10:00",
        require_consent=False,
        consent_purpose="outbound",
        max_attempts_per_number_per_day=1,
    )
    scheduled = service.enqueue_outbound(
        project_id=project_id,
        user_id="owner",
        idempotency_key="windowed-call",
        destination_number="+8613800000051",
        agent_name="commercial-agent",
        available_at=future,
    )
    assert scheduled["available_at"] == future.replace(hour=9).isoformat().replace(
        "+00:00", "Z"
    )

    service.update_policy(
        project_id=project_id,
        user_id="owner",
        timezone_name="UTC",
        allowed_weekdays=range(7),
        calling_window_start="00:00",
        calling_window_end="23:59",
        require_consent=False,
        consent_purpose="outbound",
        max_attempts_per_number_per_day=1,
    )
    first = _enqueue(service, project_id, "daily-limit-a", "+8613800000052")
    claimed = service.claim_outbound(
        project_id=project_id, user_id="owner", worker_id="daily-worker", limit=5
    )
    active = next(call for call in claimed if call["id"] == first["id"])
    service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=active["id"],
        worker_id="daily-worker",
        lease_token=active["lease_token"],
        status="failed",
    )
    with pytest.raises(ComplianceBlockedError, match="daily_number_attempt_limit"):
        _enqueue(service, project_id, "daily-limit-b", "+8613800000052")


def test_reconciliation_and_livekit_webhook_converge_without_redial(
    telephony_stack,
) -> None:
    _, service, project_id = telephony_stack
    _limits(service, project_id, total=2, outbound=2, inbound=2, rate=100, lease=10)
    queued = _enqueue(service, project_id, "reconcile-cdr", "+8613800000061")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    leased = service.claim_outbound(
        project_id=project_id,
        user_id="owner",
        worker_id="dial-worker",
        now=now,
    )[0]
    dispatching = service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=queued["id"],
        worker_id="dial-worker",
        lease_token=leased["lease_token"],
        status="dispatching",
        room_name="reconcile-room",
        now=now,
    )
    dialing = service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=queued["id"],
        worker_id="dial-worker",
        lease_token=dispatching["lease_token"],
        status="dialing",
        now=now,
    )
    active = service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=queued["id"],
        worker_id="dial-worker",
        lease_token=dialing["lease_token"],
        status="active",
        provider_call_id="provider-61",
        now=now,
    )
    reconciliation = service.claim_reconciliation(
        project_id=project_id,
        user_id="owner",
        worker_id="reconcile-worker",
        now=now + timedelta(seconds=11),
    )
    assert len(reconciliation) == 1
    assert reconciliation[0]["id"] == active["id"]
    assert reconciliation[0]["status"] == "reconciling"
    assert reconciliation[0]["lease_token"] != active["lease_token"]

    cdr = service.observe_call(
        project_id=project_id,
        user_id="owner",
        call_id=active["id"],
        worker_id="reconcile-worker",
        lease_token=reconciliation[0]["lease_token"],
        provider_call_id="provider-61",
        sip_call_id="sip-61",
        room_name="reconcile-room",
        participant_identity="sip-call-61",
        sip_status="active",
        attributes={
            "sip.callID": "sip-61",
            "sip.callStatus": "active",
            "sip.phoneNumber": "+8613800000061",
        },
        now=now + timedelta(seconds=12),
    )
    assert cdr["attributes"]["sip.callID"] == "sip-61"
    assert "sip.phoneNumber" not in cdr["attributes"]

    webhook = service.ingest_livekit_event(
        event_id="webhook-event-61",
        event_type="participant_left",
        room_name="reconcile-room",
        participant_identity="sip-call-61",
        participant_metadata='{"call_id":"' + active["id"] + '"}',
        attributes={"sip.callID": "sip-61", "sip.callStatus": "hangup"},
        disconnect_reason="CLIENT_INITIATED",
        observed_at=now + timedelta(seconds=20),
    )
    assert webhook["outcome"] == "terminalized_completed"
    duplicate = service.ingest_livekit_event(
        event_id="webhook-event-61",
        event_type="participant_left",
        room_name="ignored",
    )
    assert duplicate["outcome"] == "terminalized_completed"
    finished = service.get_call(
        project_id=project_id, user_id="owner", call_id=active["id"]
    )
    assert finished["status"] == "completed"
    stored_cdr = service.get_cdr(
        project_id=project_id, user_id="owner", call_id=active["id"]
    )
    assert stored_cdr["ended_at"] is not None


def test_human_cold_transfer_is_allowlisted_idempotent_and_audited(
    telephony_stack,
) -> None:
    store, service, project_id = telephony_stack
    store.add_membership(
        project_id=project_id, actor_id="owner", user_id="viewer-2", role="viewer"
    )
    destination = service.upsert_transfer_destination(
        project_id=project_id,
        user_id="owner",
        name="billing-human",
        target_uri="tel:+8610000000001",
    )
    assert destination["target_uri"] == "tel:+8610000000001"
    assert service.list_transfer_destinations(
        project_id=project_id, user_id="viewer-2"
    )[0]["target_uri"] == "redacted"
    with pytest.raises(ValueError, match="tel"):
        service.upsert_transfer_destination(
            project_id=project_id,
            user_id="owner",
            name="unsafe",
            target_uri="https://attacker.example",
        )

    queued = _enqueue(service, project_id, "transfer-call", "+8613800000071")
    leased = service.claim_outbound(
        project_id=project_id, user_id="owner", worker_id="transfer-worker"
    )[0]
    dispatching = service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=queued["id"],
        worker_id="transfer-worker",
        lease_token=leased["lease_token"],
        status="dispatching",
    )
    dialing = service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=queued["id"],
        worker_id="transfer-worker",
        lease_token=dispatching["lease_token"],
        status="dialing",
    )
    active = service.transition_call(
        project_id=project_id,
        user_id="owner",
        call_id=queued["id"],
        worker_id="transfer-worker",
        lease_token=dialing["lease_token"],
        status="active",
        room_name="transfer-room",
    )
    requested = service.request_transfer(
        project_id=project_id,
        user_id="owner",
        call_id=active["id"],
        worker_id="transfer-worker",
        lease_token=active["lease_token"],
        destination_name="billing-human",
        idempotency_key="customer-request-1",
        context_summary="Customer needs a billing specialist.",
    )
    repeated = service.request_transfer(
        project_id=project_id,
        user_id="owner",
        call_id=active["id"],
        worker_id="transfer-worker",
        lease_token=active["lease_token"],
        destination_name="billing-human",
        idempotency_key="customer-request-1",
        context_summary="Customer needs a billing specialist.",
    )
    assert repeated["id"] == requested["id"]
    assert requested["target_uri"] == "tel:+8610000000001"
    transferring = service.transition_transfer(
        project_id=project_id,
        user_id="owner",
        call_id=active["id"],
        transfer_id=requested["id"],
        worker_id="transfer-worker",
        lease_token=active["lease_token"],
        status="transferring",
    )
    assert transferring["status"] == "transferring"
    completed = service.transition_transfer(
        project_id=project_id,
        user_id="owner",
        call_id=active["id"],
        transfer_id=requested["id"],
        worker_id="transfer-worker",
        lease_token=active["lease_token"],
        status="completed",
    )
    assert completed["status"] == "completed"
    assert service.get_call(
        project_id=project_id, user_id="owner", call_id=active["id"]
    )["status"] == "completed"
    assert service.list_transfers(
        project_id=project_id, user_id="owner", call_id=active["id"]
    )[0]["destination_name"] == "billing-human"


def test_inbound_capacity_can_overflow_to_an_allowlisted_human_destination(
    telephony_stack,
) -> None:
    _, service, project_id = telephony_stack
    _limits(service, project_id, total=1, outbound=1, inbound=1, rate=100)
    service.upsert_transfer_destination(
        project_id=project_id,
        user_id="owner",
        name="overflow-human",
        target_uri="tel:+8610000000002",
    )
    service.update_policy(
        project_id=project_id,
        user_id="owner",
        timezone_name="UTC",
        allowed_weekdays=range(7),
        calling_window_start="00:00",
        calling_window_end="23:59",
        require_consent=False,
        consent_purpose="outbound",
        max_attempts_per_number_per_day=100,
        inbound_overflow_mode="transfer",
        inbound_overflow_destination_name="overflow-human",
    )
    service.admit_inbound(
        project_id=project_id,
        user_id="owner",
        provider="carrier",
        provider_call_id="capacity-call-1",
        worker_id="inbound-worker-1",
        source_number="+8613800000091",
        destination_number="+8610000000000",
        agent_name="support-agent",
        room_name="capacity-room-1",
    )
    overflow = service.admit_inbound(
        project_id=project_id,
        user_id="owner",
        provider="carrier",
        provider_call_id="capacity-call-2",
        worker_id="inbound-worker-2",
        source_number="+8613800000092",
        destination_number="+8610000000000",
        agent_name="support-agent",
        room_name="capacity-room-2",
    )
    assert overflow["overflow"] == {
        "mode": "transfer",
        "destination_name": "overflow-human",
    }
    duplicate = service.admit_inbound(
        project_id=project_id,
        user_id="owner",
        provider="carrier",
        provider_call_id="capacity-call-2",
        worker_id="inbound-worker-2",
        source_number="+8613800000092",
        destination_number="+8610000000000",
        agent_name="support-agent",
        room_name="capacity-room-2",
    )
    assert duplicate["id"] == overflow["id"]
    assert duplicate["overflow"]["destination_name"] == "overflow-human"


def test_telephony_api_hides_lease_tokens_from_read_endpoints(telephony_stack) -> None:
    store, service, project_id = telephony_stack
    app = FastAPI()
    install_authenticator(app, DevelopmentAuthenticator())
    app.include_router(create_telephony_router(service))
    client = TestClient(app)

    created = client.post(
        f"/api/platform/projects/{project_id}/telephony/calls/outbound",
        headers={"X-User-ID": "owner"},
        json={
            "idempotency_key": "api-call",
            "destination_number": "+8613800000001",
            "agent_name": "api-agent",
        },
    )
    assert created.status_code == 202
    claim = client.post(
        f"/api/platform/projects/{project_id}/telephony/dispatch/claim",
        headers={"X-User-ID": "owner"},
        json={"worker_id": "api-worker", "limit": 1},
    )
    assert claim.status_code == 200
    assert claim.json()["items"][0]["lease_token"]

    listed = client.get(
        f"/api/platform/projects/{project_id}/telephony/calls",
        headers={"X-User-ID": "owner"},
    )
    assert listed.status_code == 200
    assert "lease_token" not in listed.json()["items"][0]


def test_production_campaign_pause_resume_and_trunk_cps(telephony_stack) -> None:
    _, service, project_id = telephony_stack
    trunk = service.upsert_trunk(
        project_id=project_id,
        user_id="owner",
        name="paced-trunk",
        direction="outbound",
        provider="carrier",
        livekit_trunk_id="ST_paced",
        numbers=["+8610000000000"],
        max_concurrent_calls=10,
        max_calls_per_second=1,
    )
    contacts = [
        service.upsert_contact(
            project_id=project_id,
            user_id="owner",
            external_id=f"crm-{index}",
            name=f"Contact {index}",
            phone_number=f"+86138000001{index:02d}",
        )
        for index in range(2)
    ]
    campaign = service.create_campaign(
        project_id=project_id,
        user_id="owner",
        name="Commercial campaign",
        agent_name="commercial-agent",
        trunk_id=trunk["id"],
        source_number="+8610000000000",
        max_concurrent_calls=2,
    )
    added = service.add_campaign_contacts(
        project_id=project_id,
        user_id="owner",
        campaign_id=campaign["id"],
        contact_ids=[item["id"] for item in contacts],
    )
    assert added["added"] == 2
    running = service.set_campaign_status(
        project_id=project_id,
        user_id="owner",
        campaign_id=campaign["id"],
        status="running",
    )
    assert running["enqueue_result"] == {"queued": 2, "blocked": 0}

    first_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    first = service.claim_outbound(
        project_id=project_id,
        user_id="owner",
        worker_id="paced-worker-1",
        limit=10,
        now=first_at,
    )
    assert len(first) == 1
    assert service.claim_outbound(
        project_id=project_id,
        user_id="owner",
        worker_id="paced-worker-2",
        limit=10,
        now=first_at,
    ) == []

    service.set_campaign_status(
        project_id=project_id,
        user_id="owner",
        campaign_id=campaign["id"],
        status="paused",
    )
    assert service.claim_outbound(
        project_id=project_id,
        user_id="owner",
        worker_id="paced-worker-3",
        limit=10,
        now=first_at + timedelta(seconds=2),
    ) == []
    service.set_campaign_status(
        project_id=project_id,
        user_id="owner",
        campaign_id=campaign["id"],
        status="running",
    )
    second = service.claim_outbound(
        project_id=project_id,
        user_id="owner",
        worker_id="paced-worker-4",
        limit=10,
        now=first_at + timedelta(seconds=2),
    )
    assert len(second) == 1


def test_campaign_start_is_bounded_and_dispatcher_materializes_remainder(
    telephony_stack,
) -> None:
    _, service, project_id = telephony_stack
    contacts = [
        service.upsert_contact(
            project_id=project_id,
            user_id="owner",
            external_id=f"batch-{index}",
            phone_number=f"+86139{index:08d}",
        )
        for index in range(101)
    ]
    campaign = service.create_campaign(
        project_id=project_id,
        user_id="owner",
        name="Bounded campaign",
        agent_name="commercial-agent",
        trunk_id=None,
    )
    service.add_campaign_contacts(
        project_id=project_id,
        user_id="owner",
        campaign_id=campaign["id"],
        contact_ids=[item["id"] for item in contacts],
    )

    started = service.set_campaign_status(
        project_id=project_id,
        user_id="owner",
        campaign_id=campaign["id"],
        status="running",
    )
    assert started["enqueue_result"] == {"queued": 100, "blocked": 0}
    result = service.materialize_campaigns(
        project_id=project_id,
        user_id="owner",
        campaign_id=campaign["id"],
        limit=10,
    )
    assert result == {"scanned": 1, "queued": 1, "blocked": 0, "pending": 0}

    listed = service.list_campaigns(project_id=project_id, user_id="owner")
    assert listed[0]["queued_count"] == 101


def test_outbound_emergency_pause_and_source_allowlist(telephony_stack) -> None:
    _, service, project_id = telephony_stack
    trunk = service.upsert_trunk(
        project_id=project_id,
        user_id="owner",
        name="restricted-trunk",
        direction="outbound",
        provider="carrier",
        livekit_trunk_id="ST_restricted",
        numbers=["+8610000000000"],
    )
    with pytest.raises(ValueError, match="not allowlisted"):
        service.enqueue_outbound(
            project_id=project_id,
            user_id="owner",
            idempotency_key="spoofed-caller-id",
            destination_number="+8613800000200",
            source_number="+8610000000999",
            agent_name="commercial-agent",
            trunk_id=trunk["id"],
        )

    service.update_policy(
        project_id=project_id,
        user_id="owner",
        timezone_name="UTC",
        allowed_weekdays=range(7),
        calling_window_start="00:00",
        calling_window_end="23:59",
        require_consent=False,
        consent_purpose="outbound",
        max_attempts_per_number_per_day=100,
        outbound_enabled=False,
    )
    with pytest.raises(ComplianceBlockedError, match="outbound_paused"):
        service.enqueue_outbound(
            project_id=project_id,
            user_id="owner",
            idempotency_key="emergency-stop",
            destination_number="+8613800000201",
            source_number="+8610000000000",
            agent_name="commercial-agent",
            trunk_id=trunk["id"],
        )


def test_phone_numbers_are_encrypted_at_rest_and_decrypted_for_authorized_workers(
    tmp_path: Path,
) -> None:
    store = PlatformStore(tmp_path / "encrypted-phones.sqlite3")
    store.initialize()
    project = store.create_project(name="Encrypted", slug="encrypted", owner_id="owner")
    service = TelephonyService(store, phone_cipher=SecretCipher.generate())
    service.update_policy(
        project_id=project["id"], user_id="owner", timezone_name="UTC",
        allowed_weekdays=range(7), calling_window_start="00:00",
        calling_window_end="23:59", require_consent=False,
        consent_purpose="outbound", max_attempts_per_number_per_day=100,
    )
    contact = service.upsert_contact(
        project_id=project["id"], user_id="owner", external_id="pii-1",
        phone_number="+8613800000300", metadata={"account": "sensitive"},
    )
    call = service.enqueue_outbound(
        project_id=project["id"], user_id="owner", idempotency_key="encrypted-call",
        destination_number="+8613800000300", source_number="+8610000000000",
        agent_name="commercial-agent", metadata={"customer": "sensitive"},
    )
    with store.connect() as conn:
        raw_contact = conn.execute(
            "SELECT phone_number, metadata_json FROM telephony_contacts WHERE id = ?", (contact["id"],)
        ).fetchone()
        raw_call = conn.execute(
            "SELECT source_number, destination_number, metadata_json FROM call_jobs WHERE id = ?", (call["id"],)
        ).fetchone()
    assert str(raw_contact["phone_number"]).startswith("enc:v1:")
    assert str(raw_call["source_number"]).startswith("enc:v1:")
    assert str(raw_call["destination_number"]).startswith("enc:v1:")
    assert str(raw_contact["metadata_json"]).startswith("encjson:v1:")
    assert str(raw_call["metadata_json"]).startswith("encjson:v1:")
    assert call["destination_number"] == "+8613800000300"

    store.add_membership(
        project_id=project["id"], actor_id="owner", user_id="viewer", role="viewer"
    )
    viewer_call = service.get_call(
        project_id=project["id"], user_id="viewer", call_id=call["id"]
    )
    assert viewer_call["destination_number"] != "+8613800000300"
    assert viewer_call["metadata"] == {}


def test_project_retention_purges_terminal_calls_but_keeps_legal_contact_data(
    telephony_stack,
) -> None:
    store, service, project_id = telephony_stack
    contact = service.upsert_contact(
        project_id=project_id, user_id="owner", external_id="retained-contact",
        phone_number="+8613800000400",
    )
    call = service.enqueue_outbound(
        project_id=project_id, user_id="owner", idempotency_key="expired-call",
        destination_number="+8613800000400", agent_name="commercial-agent",
    )
    expired = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat().replace(
        "+00:00", "Z"
    )
    with store.transaction() as conn:
        conn.execute(
            "UPDATE call_jobs SET status = 'completed', ended_at = ?, updated_at = ? WHERE id = ?",
            (expired, expired, call["id"]),
        )
    result = store.purge_expired_project_data(
        project_id=project_id,
        actor_id="owner",
        now=datetime.now(timezone.utc),
    )
    assert result["calls"] == 1
    with pytest.raises(Exception, match="call not found"):
        service.get_call(project_id=project_id, user_id="owner", call_id=call["id"])
    assert service.list_contacts(project_id=project_id, user_id="owner")[0]["id"] == contact["id"]


def test_answering_machine_result_is_lease_owned_and_persisted(telephony_stack) -> None:
    _, service, project_id = telephony_stack
    call = _enqueue(service, project_id, "amd-call", "+8613800000500")
    leased = service.claim_outbound(
        project_id=project_id, user_id="owner", worker_id="amd-worker"
    )[0]
    service.transition_call(
        project_id=project_id, user_id="owner", call_id=call["id"],
        status="dispatching", worker_id="amd-worker", lease_token=leased["lease_token"],
    )
    service.transition_call(
        project_id=project_id, user_id="owner", call_id=call["id"], status="dialing",
        worker_id="amd-worker", lease_token=leased["lease_token"],
    )
    service.transition_call(
        project_id=project_id, user_id="owner", call_id=call["id"], status="active",
        worker_id="amd-worker", lease_token=leased["lease_token"],
    )
    with pytest.raises(LeaseConflictError):
        service.record_call_result(
            project_id=project_id, user_id="owner", call_id=call["id"],
            worker_id="other-worker", lease_token=leased["lease_token"],
            answering_machine_category="machine-vm", disposition="voicemail_detected",
        )
    updated = service.record_call_result(
        project_id=project_id, user_id="owner", call_id=call["id"],
        worker_id="amd-worker", lease_token=leased["lease_token"],
        answering_machine_category="machine-vm", disposition="voicemail_detected",
    )
    assert updated["answering_machine_category"] == "machine-vm"
    assert updated["disposition"] == "voicemail_detected"


def test_recording_policy_is_snapshotted_and_recording_reference_is_protected(
    telephony_stack, monkeypatch,
) -> None:
    store, service, project_id = telephony_stack
    service.update_policy(
        project_id=project_id, user_id="owner", timezone_name="UTC",
        allowed_weekdays=range(7), calling_window_start="00:00",
        calling_window_end="23:59", require_consent=False,
        consent_purpose="outbound", max_attempts_per_number_per_day=100,
        recording_mode="always",
        recording_disclosure_text="本次通话将被录音。",
    )
    call = _enqueue(service, project_id, "recorded-call", "+8613800000600")
    assert call["recording_mode"] == "always"
    assert call["recording_disclosure_text"] == "本次通话将被录音。"
    leased = service.claim_outbound(
        project_id=project_id, user_id="owner", worker_id="recording-worker"
    )[0]
    for status in ("dispatching", "dialing", "active"):
        service.transition_call(
            project_id=project_id, user_id="owner", call_id=call["id"],
            status=status, worker_id="recording-worker", lease_token=leased["lease_token"],
        )
    recorded = service.record_call_recording(
        project_id=project_id, user_id="owner", call_id=call["id"],
        worker_id="recording-worker", lease_token=leased["lease_token"],
        egress_id="EG_recording_1", status="active",
        storage_uri=f"s3://recordings/{call['id']}.ogg",
    )
    assert recorded["recording_status"] == "active"
    stopping = service.record_call_recording(
        project_id=project_id, user_id="owner", call_id=call["id"],
        worker_id="recording-worker", lease_token=leased["lease_token"],
        egress_id="EG_recording_1", status="stopping",
        storage_uri=f"s3://recordings/{call['id']}.ogg",
    )
    assert stopping["recording_status"] == "stopping"
    egress_event = service.ingest_livekit_event(
        event_id="egress-event-recording-1",
        event_type="egress_ended",
        room_name=str(stopping["room_name"]),
        egress_id="EG_recording_1",
        egress_status="EGRESS_COMPLETE",
        egress_storage_uri=f"s3://recordings/{call['id']}.ogg",
    )
    assert egress_event["outcome"] == "recording_completed"
    completed = service.get_call(
        project_id=project_id, user_id="owner", call_id=call["id"]
    )
    assert completed["recording_status"] == "completed"
    stale_agent_update = service.record_call_recording(
        project_id=project_id, user_id="owner", call_id=call["id"],
        worker_id="recording-worker", lease_token=leased["lease_token"],
        egress_id="EG_recording_1", status="active",
        storage_uri=f"s3://recordings/{call['id']}.ogg",
    )
    assert stale_agent_update["recording_status"] == "completed"
    store.add_membership(
        project_id=project_id, actor_id="owner", user_id="recording-viewer", role="viewer"
    )
    viewer = service.get_call(
        project_id=project_id, user_id="recording-viewer", call_id=call["id"]
    )
    assert viewer["recording_storage_uri"] == ""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("CLOUD_PARITY_RECORDING_S3_ENDPOINT", "https://objects.example.com")
    access = service.get_recording_access(
        project_id=project_id, user_id="owner", call_id=call["id"], ttl_seconds=60
    )
    assert access["url"].startswith("https://objects.example.com/recordings/")
    assert access["ttl_seconds"] == 60
    with pytest.raises(AccessDeniedError):
        service.get_recording_access(
            project_id=project_id,
            user_id="recording-viewer",
            call_id=call["id"],
        )


def test_contact_erasure_removes_terminal_campaign_calls(telephony_stack) -> None:
    _, service, project_id = telephony_stack
    contact = service.upsert_contact(
        project_id=project_id, user_id="owner", external_id="erase-me",
        phone_number="+8613800000700",
    )
    campaign = service.create_campaign(
        project_id=project_id, user_id="owner", name="erase-campaign",
        agent_name="commercial-agent", trunk_id=None,
    )
    service.add_campaign_contacts(
        project_id=project_id, user_id="owner", campaign_id=campaign["id"],
        contact_ids=[contact["id"]],
    )
    service.set_campaign_status(
        project_id=project_id, user_id="owner", campaign_id=campaign["id"],
        status="running",
    )
    with pytest.raises(ValueError, match="queued or active"):
        service.delete_contact(
            project_id=project_id, user_id="owner", contact_id=contact["id"]
        )
    service.set_campaign_status(
        project_id=project_id, user_id="owner", campaign_id=campaign["id"],
        status="canceled",
    )
    erased = service.delete_contact(
        project_id=project_id, user_id="owner", contact_id=contact["id"]
    )
    assert erased["deleted"] is True
    assert erased["erased_calls"] == 1
    assert service.list_contacts(project_id=project_id, user_id="owner") == []


def test_contact_listing_uses_stable_cursor_and_server_search(telephony_stack) -> None:
    _, service, project_id = telephony_stack
    for index, name in enumerate(("Alice", "Bob", "Carol"), start=1):
        service.upsert_contact(
            project_id=project_id,
            user_id="owner",
            external_id=f"crm-{index}",
            phone_number=f"+86138000000{index:02d}",
            name=name,
        )

    first = service.list_contacts_page(
        project_id=project_id, user_id="owner", limit=2
    )
    second = service.list_contacts_page(
        project_id=project_id,
        user_id="owner",
        limit=2,
        cursor=first["next_cursor"],
    )
    found = service.list_contacts_page(
        project_id=project_id,
        user_id="owner",
        search="bob",
    )

    assert len(first["items"]) == 2
    assert first["next_cursor"]
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert not ({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
    assert [item["external_id"] for item in found["items"]] == ["crm-2"]
