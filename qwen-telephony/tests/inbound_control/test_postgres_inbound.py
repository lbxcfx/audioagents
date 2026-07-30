from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.store import PlatformStore
from server.inbound_control.metadata import InboundMetadataSigner
from server.inbound_control.service import InboundAgentService
from server.inbound_control.store import InboundAgentStore, PublicDemoQuotaError


def schema_url(database_url: str, schema: str) -> str:
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@pytest.fixture()
def postgres_inbound(tmp_path):
    database_url = os.getenv("CLOUD_PARITY_TEST_POSTGRES_URL", "").strip()
    if not database_url:
        pytest.skip("set CLOUD_PARITY_TEST_POSTGRES_URL to run PostgreSQL integration tests")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = f"inbound_test_{uuid.uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    platform = PlatformStore(tmp_path / "unused.sqlite3", database_url=schema_url(database_url, schema), max_pool_size=12)
    platform.initialize()
    project = platform.create_project(name="Public", slug="public", owner_id="owner")
    store = InboundAgentStore(platform, public_project_id=project["id"])
    store.migrate()
    service = InboundAgentService(
        store,
        InboundMetadataSigner("postgres-inbound-metadata-secret-long-enough"),
        public_hash_key="postgres-inbound-public-hash-key",
        public_calls_per_day=3,
    )
    config = {
        "instructions": "你是公开语音体验助手，耐心回答用户的问题。",
        "welcome_message": "您好，请问有什么可以帮您？",
        "voice": "Cherry", "language": "zh-CN", "max_duration_seconds": 180,
        "recording_mode": "off", "recording_disclosure": "", "tools": [], "knowledge_sources": [],
    }
    agent = store.create_agent(
        project_id=project["id"], actor_id="owner", name="公开体验", description="",
        kind="public_demo", config=config,
    )
    store.publish_agent(project_id=project["id"], actor_id="owner", agent_id=agent["id"], expected_revision=1)
    store.create_binding(
        project_id=project["id"], actor_id="owner", agent_id=agent["id"],
        entry_type="web", destination="postgres-public", trunk_id="",
    )
    try:
        yield platform, store, service
    finally:
        platform.close()
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_public_quota_and_completion_are_atomic(postgres_inbound):
    platform, store, service = postgres_inbound

    def reserve(index: int):
        prepared = service.prepare_public_web_session(
            session_id=f"session-{index}", room_name=f"room-{index}"
        )
        try:
            return service.commit_public_web_session(
                source="203.0.113.50", binding=prepared["binding"], room_name=f"room-{index}",
                provider_call_id=f"web:{index}",
            )
        except PublicDemoQuotaError:
            return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        sessions = [item for item in executor.map(reserve, range(20)) if item]
    assert len(sessions) == 3

    session_id = sessions[0]["session_id"]
    with ThreadPoolExecutor(max_workers=8) as executor:
        completed = list(executor.map(
            lambda _: store.complete_public_session(
                session_id=session_id, duration_seconds=77, termination_reason="completed"
            ),
            range(8),
        ))
    assert all(item["duration_seconds"] == 77 for item in completed)
    with platform.connect() as conn:
        usage = conn.execute("SELECT call_count, total_seconds FROM public_demo_usage").fetchone()
    assert usage["call_count"] == 3
    assert usage["total_seconds"] == 77
