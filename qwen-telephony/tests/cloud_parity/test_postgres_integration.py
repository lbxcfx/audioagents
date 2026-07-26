from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.analytics import AnalyticsService
from server.cloud_parity.insights import InsightsService
from server.cloud_parity.store import AccessDeniedError, PlatformStore
from server.cloud_parity.telephony import CapacityExceededError, TelephonyService


def _schema_url(database_url: str, schema: str) -> str:
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


@pytest.fixture()
def postgres_store(tmp_path: Path):
    database_url = os.getenv("CLOUD_PARITY_TEST_POSTGRES_URL", "").strip()
    if not database_url:
        pytest.skip("set CLOUD_PARITY_TEST_POSTGRES_URL to run PostgreSQL integration tests")

    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = f"cloud_parity_test_{uuid.uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    store = PlatformStore(
        tmp_path / "unused.sqlite3",
        database_url=_schema_url(database_url, schema),
        min_pool_size=1,
        max_pool_size=4,
    )
    try:
        yield store
    finally:
        store.close()
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def test_postgres_migrations_are_concurrent_and_repeatable(
    postgres_store: PlatformStore,
) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        versions = list(executor.map(lambda _: postgres_store.initialize(), range(2)))

    assert versions[0] == versions[1]
    assert versions[0] >= 11
    health = postgres_store.healthcheck()
    assert health["backend"] == "postgresql"
    assert health["schema_version"] == versions[0]


def test_postgres_tenant_insights_and_analytics_flow(
    postgres_store: PlatformStore,
) -> None:
    postgres_store.initialize()
    alpha = postgres_store.create_project(
        name="Postgres Alpha", slug=f"alpha-{uuid.uuid4().hex}", owner_id="owner-a"
    )
    beta = postgres_store.create_project(
        name="Postgres Beta", slug=f"beta-{uuid.uuid4().hex}", owner_id="owner-b"
    )
    insights = InsightsService(postgres_store)
    session = insights.create_session(
        project_id=alpha["id"], actor_id="owner-a", room_name="postgres-room"
    )
    insights.append_event(
        project_id=alpha["id"],
        actor_id="owner-a",
        session_id=session["id"],
        event_type="agent.ready",
        source="agent",
    )
    insights.record_usage(
        project_id=alpha["id"],
        actor_id="owner-a",
        session_id=session["id"],
        category="llm",
        provider="qwen",
        model="qwen-plus",
        quantity=12,
        unit="tokens",
        cost_usd=0.001,
        latency_ms=25,
    )
    insights.close_session(
        project_id=alpha["id"], actor_id="owner-a", session_id=session["id"]
    )

    summary = AnalyticsService(postgres_store).summary(
        project_id=alpha["id"], user_id="owner-a"
    )
    assert summary["sessions"]["total"] == 1
    assert summary["sessions"]["completed"] == 1
    assert summary["usage"][0]["quantity"] == 12
    assert summary["events"] == {"agent.ready": 1}

    with pytest.raises(AccessDeniedError):
        insights.timeline(
            project_id=alpha["id"],
            user_id="owner-b",
            session_id=session["id"],
        )
    assert postgres_store.list_projects("owner-b") == [beta]


def test_postgres_concurrent_insights_events_have_unique_sequence(
    postgres_store: PlatformStore,
) -> None:
    postgres_store.initialize()
    project = postgres_store.create_project(
        name="Concurrent Insights",
        slug=f"insights-{uuid.uuid4().hex}",
        owner_id="owner",
    )
    insights = InsightsService(postgres_store)
    session = insights.create_session(
        project_id=project["id"],
        actor_id="owner",
        room_name="concurrent-insights-room",
    )

    def append(index: int) -> dict:
        return insights.append_event(
            project_id=project["id"],
            actor_id="owner",
            session_id=session["id"],
            event_type="concurrent.event",
            source="test",
            payload={"index": index},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        events = list(executor.map(append, range(40)))

    assert sorted(int(event["sequence"]) for event in events) == list(range(1, 41))


def test_postgres_telephony_claims_enforce_capacity_across_workers(
    postgres_store: PlatformStore,
) -> None:
    postgres_store.initialize()
    project = postgres_store.create_project(
        name="Concurrent Calls",
        slug=f"calls-{uuid.uuid4().hex}",
        owner_id="owner",
    )
    service = TelephonyService(postgres_store)
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
    service.update_limits(
        project_id=project["id"],
        user_id="owner",
        max_concurrent_calls=3,
        max_outbound_calls=3,
        max_inbound_calls=3,
        max_calls_per_minute=100,
        lease_seconds=30,
    )
    for index in range(10):
        service.enqueue_outbound(
            project_id=project["id"],
            user_id="owner",
            idempotency_key=f"postgres-call-{index}",
            destination_number=f"+8613800000{index:03d}",
            agent_name="postgres-worker-agent",
        )

    def claim(index: int) -> list[dict]:
        return service.claim_outbound(
            project_id=project["id"],
            user_id="owner",
            worker_id=f"worker-{index}",
            limit=1,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        batches = list(executor.map(claim, range(8)))

    claimed = [call for batch in batches for call in batch]
    assert len(claimed) == 3
    assert len({call["id"] for call in claimed}) == 3
    metrics = service.metrics(project_id=project["id"], user_id="owner")
    assert metrics["active_calls"] == 3
    assert metrics["queue_depth"] == 7


def test_postgres_concurrent_owner_removal_keeps_an_owner(
    postgres_store: PlatformStore,
) -> None:
    postgres_store.initialize()
    project = postgres_store.create_project(
        name="Owner Race",
        slug=f"owner-race-{uuid.uuid4().hex}",
        owner_id="owner-a",
    )
    postgres_store.add_membership(
        project_id=project["id"],
        actor_id="owner-a",
        user_id="owner-b",
        role="owner",
    )

    def remove(actor_id: str, user_id: str) -> str:
        try:
            postgres_store.remove_membership(
                project_id=project["id"], actor_id=actor_id, user_id=user_id
            )
            return "removed"
        except (AccessDeniedError, ValueError):
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda pair: remove(*pair),
                (("owner-a", "owner-b"), ("owner-b", "owner-a")),
            )
        )

    assert sorted(results) == ["rejected", "removed"]
    with postgres_store.connect() as conn:
        owners = conn.execute(
            """
            SELECT COUNT(*) AS count FROM project_memberships
            WHERE project_id = ? AND role = 'owner'
            """,
            (project["id"],),
        ).fetchone()
    assert int(owners["count"] or 0) == 1


def test_postgres_high_volume_outbound_claims_remain_unique(
    postgres_store: PlatformStore,
) -> None:
    postgres_store.initialize()
    project = postgres_store.create_project(
        name="High Volume Calls",
        slug=f"high-volume-{uuid.uuid4().hex}",
        owner_id="owner",
    )
    service = TelephonyService(postgres_store)
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
    service.update_limits(
        project_id=project["id"],
        user_id="owner",
        max_concurrent_calls=50,
        max_outbound_calls=50,
        max_inbound_calls=50,
        max_calls_per_minute=1000,
        lease_seconds=30,
    )
    for index in range(200):
        service.enqueue_outbound(
            project_id=project["id"],
            user_id="owner",
            idempotency_key=f"bulk-{index}",
            destination_number=f"+86139{index:08d}",
            agent_name="bulk-agent",
        )

    def claim(index: int) -> list[dict]:
        return service.claim_outbound(
            project_id=project["id"],
            user_id="owner",
            worker_id=f"bulk-worker-{index}",
            limit=5,
        )

    with ThreadPoolExecutor(max_workers=32) as executor:
        batches = list(executor.map(claim, range(32)))

    claimed = [call for batch in batches for call in batch]
    assert len(claimed) == 50
    assert len({call["id"] for call in claimed}) == 50
    metrics = service.metrics(project_id=project["id"], user_id="owner")
    assert metrics["active_calls"] == 50
    assert metrics["queue_depth"] == 150


def test_postgres_inbound_admission_and_reconciliation_are_concurrency_safe(
    postgres_store: PlatformStore,
) -> None:
    postgres_store.initialize()
    project = postgres_store.create_project(
        name="Inbound Concurrency",
        slug=f"inbound-{uuid.uuid4().hex}",
        owner_id="owner",
    )
    service = TelephonyService(postgres_store)
    service.update_limits(
        project_id=project["id"],
        user_id="owner",
        max_concurrent_calls=10,
        max_outbound_calls=10,
        max_inbound_calls=10,
        max_calls_per_minute=100,
        lease_seconds=10,
    )
    admitted_at = datetime.now(timezone.utc)

    def admit(index: int) -> dict | None:
        try:
            return service.admit_inbound(
                project_id=project["id"],
                user_id="owner",
                provider="carrier",
                provider_call_id=f"inbound-provider-{index}",
                worker_id=f"inbound-worker-{index}",
                source_number=f"+86138{index:08d}",
                destination_number="+8610000000000",
                agent_name="support-agent",
                room_name=f"inbound-room-{index}",
                now=admitted_at,
            )
        except CapacityExceededError:
            return None

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(admit, range(20)))
    admitted = [call for call in results if call is not None]
    assert len(admitted) == 10

    reconcile_at = admitted_at + timedelta(seconds=11)

    def reconcile(index: int) -> list[dict]:
        return service.claim_reconciliation(
            project_id=project["id"],
            user_id="owner",
            worker_id=f"reconcile-worker-{index}",
            limit=3,
            now=reconcile_at,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        batches = list(executor.map(reconcile, range(8)))
    reconciled = [call for batch in batches for call in batch]
    assert len(reconciled) == 10
    assert len({call["id"] for call in reconciled}) == 10
    assert all(call["status"] == "reconciling" for call in reconciled)


def test_postgres_distributed_api_rate_limit_is_atomic(
    postgres_store: PlatformStore,
) -> None:
    postgres_store.initialize()
    now = datetime.now(timezone.utc)

    def consume(_index: int) -> dict:
        return postgres_store.consume_api_rate_limit(
            key="shared-api-principal",
            limit=7,
            window_seconds=60,
            now=now,
        )

    with ThreadPoolExecutor(max_workers=24) as executor:
        results = list(executor.map(consume, range(24)))

    assert sum(result["allowed"] for result in results) == 7
    assert all(result["remaining"] >= 0 for result in results)
