from __future__ import annotations

from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.auth import AuthenticationSettings, create_authenticator, install_authenticator
from server.cloud_parity.store import AccessDeniedError, PlatformStore
from server.inbound_control.api import create_inbound_router, issue_public_livekit_token
from server.inbound_control.metadata import InboundMetadataSigner, MetadataValidationError
from server.inbound_control.service import InboundAgentService
from server.inbound_control.store import InboundAgentStore, PublicDemoQuotaError
from server.inbound_control.worker_auth import (
    WorkerAuthenticationError,
    issue_worker_token,
    verify_worker_token,
    verify_worker_identity_token,
)


VALID_CONFIG = {
    "instructions": "你是一个耐心、清晰的企业服务助手，需要准确理解来电人的问题。",
    "welcome_message": "您好，请问有什么可以帮您？",
    "voice": "longanlingxin",
    "language": "zh-CN",
    "max_duration_seconds": 600,
    "recording_mode": "off",
    "recording_disclosure": "",
    "tools": [],
    "knowledge_sources": [],
}


@pytest.fixture()
def environment(tmp_path):
    platform = PlatformStore(tmp_path / "platform.sqlite3")
    platform.initialize()
    project = platform.create_project(name="甲公司", slug="tenant-a", owner_id="owner-a")
    other = platform.create_project(name="乙公司", slug="tenant-b", owner_id="owner-b")
    store = InboundAgentStore(platform, public_project_id=project["id"])
    store.migrate()
    signer = InboundMetadataSigner("metadata-secret-that-is-long-enough-for-tests")
    service = InboundAgentService(
        store,
        signer,
        public_hash_key="public-hash-key-for-inbound-tests",
        public_calls_per_day=2,
    )
    yield platform, store, signer, service, project, other
    platform.close()


def create_published_agent(store, project_id, actor_id="owner-a", kind="enterprise"):
    agent = store.create_agent(
        project_id=project_id,
        actor_id=actor_id,
        name="接听助手",
        description="处理客户来电",
        kind=kind,
        config=VALID_CONFIG,
    )
    version = store.publish_agent(
        project_id=project_id,
        actor_id=actor_id,
        agent_id=agent["id"],
        expected_revision=agent["draft_revision"],
    )
    return agent, version


def test_publish_creates_immutable_version_and_binding_pins_it(environment):
    _, store, _, _, project, _ = environment
    agent, version = create_published_agent(store, project["id"])
    binding = store.create_binding(
        project_id=project["id"],
        actor_id="owner-a",
        agent_id=agent["id"],
        entry_type="sip_did",
        destination="+86 138-0000-0000",
        trunk_id="trunk-a",
    )

    assert binding["destination"] == "+8613800000000"
    assert binding["agent_version_id"] == version["id"]
    assert version["config_sha256"]

    updated = store.update_agent(
        project_id=project["id"],
        actor_id="owner-a",
        agent_id=agent["id"],
        expected_revision=1,
        name="接听助手",
        description="新说明",
        config={**VALID_CONFIG, "welcome_message": "欢迎再次来电"},
    )
    assert updated["draft_revision"] == 2
    assert binding["agent_version_id"] == version["id"]
    old = store.get_runtime_version(project_id=project["id"], version_id=version["id"])
    assert old["config"]["welcome_message"] == VALID_CONFIG["welcome_message"]
    resolved_while_editing = store.resolve_binding(binding_id=binding["id"])
    assert resolved_while_editing["agent_version_id"] == version["id"]
    second_version = store.publish_agent(
        project_id=project["id"], actor_id="owner-a", agent_id=agent["id"], expected_revision=2
    )
    assert second_version["id"] != version["id"]
    assert store.resolve_binding(binding_id=binding["id"])["agent_version_id"] == version["id"]
    assert store.get_runtime_version(project_id=project["id"], version_id=version["id"])["id"] == version["id"]


def test_cross_tenant_access_is_denied(environment):
    _, store, _, _, project, _ = environment
    agent, _ = create_published_agent(store, project["id"])
    with pytest.raises(AccessDeniedError):
        store.get_agent(project_id=project["id"], actor_id="owner-b", agent_id=agent["id"])


def test_signed_runtime_rejects_tampering_and_replay(environment):
    _, store, signer, service, project, _ = environment
    agent, version = create_published_agent(store, project["id"])
    binding = store.create_binding(
        project_id=project["id"],
        actor_id="owner-a",
        agent_id=agent["id"],
        entry_type="web",
        destination="tenant-a.example.com",
        trunk_id="",
    )
    token = signer.sign(
        {
            "kind": "enterprise",
            "binding_id": binding["id"],
            "project_id": project["id"],
            "agent_version_id": version["id"],
        }
    )

    runtime = service.resolve_runtime(token)
    assert runtime["project_id"] == project["id"]
    assert runtime["config"]["voice"] == "longanlingxin"
    with pytest.raises(Exception):
        service.resolve_runtime(token)
    with pytest.raises(MetadataValidationError):
        signer.verify(token[:-1] + ("a" if token[-1] != "a" else "b"))


def test_public_runtime_forces_tools_off_and_quota_is_atomic(environment):
    _, store, signer, service, project, _ = environment
    config = {**VALID_CONFIG, "tools": [{"name": "dangerous"}], "knowledge_sources": ["private"]}
    with pytest.raises(ValueError):
        store.create_agent(
            project_id=project["id"], actor_id="owner-a", name="不安全公开体验",
            description="不允许工具", kind="public_demo", config=config,
        )
    agent = store.create_agent(
        project_id=project["id"], actor_id="owner-a", name="公开体验",
        description="公开语音体验", kind="public_demo", config=VALID_CONFIG,
    )
    version = store.publish_agent(
        project_id=project["id"], actor_id="owner-a", agent_id=agent["id"], expected_revision=1
    )
    binding = store.create_binding(
        project_id=project["id"], actor_id="owner-a", agent_id=agent["id"],
        entry_type="web", destination="public-demo", trunk_id="",
    )
    prepared = service.prepare_public_web_session(session_id="session-one", room_name="demo-one")
    first = service.commit_public_web_session(
        source="203.0.113.10", binding=prepared["binding"], room_name="demo-one", provider_call_id="web:one"
    )
    prepared_two = service.prepare_public_web_session(session_id="session-two", room_name="demo-two")
    second = service.commit_public_web_session(
        source="203.0.113.10", binding=prepared_two["binding"], room_name="demo-two", provider_call_id="web:two"
    )
    assert first["remaining_calls"] == 1
    assert second["remaining_calls"] == 0
    with pytest.raises(PublicDemoQuotaError):
        prepared_three = service.prepare_public_web_session(session_id="session-three", room_name="demo-three")
        service.commit_public_web_session(
            source="203.0.113.10", binding=prepared_three["binding"], room_name="demo-three", provider_call_id="web:three"
        )

    runtime = service.resolve_runtime(prepared["dispatch_metadata"])
    assert runtime["config"]["tools"] == []
    assert runtime["config"]["knowledge_sources"] == []
    assert runtime["agent_version_id"] == version["id"]
    assert runtime["binding_id"] == binding["id"]


def test_api_rbac_conflict_and_public_token_contract(environment):
    platform, store, _, service, project, _ = environment
    app = FastAPI()
    install_authenticator(app, create_authenticator(AuthenticationSettings(mode="development")))
    issued = []

    def token_issuer(room, identity, metadata, ttl):
        issued.append((room, identity, metadata, ttl))
        return {"token": "test-token", "url": "ws://livekit.test"}

    app.include_router(
        create_inbound_router(store, service, worker_secret="worker-secret", token_issuer=token_issuer)
    )
    client = TestClient(app)
    headers = {"X-User-ID": "owner-a"}
    response = client.post(
        f"/inbound-api/projects/{project['id']}/agents",
        headers=headers,
        json={"name": "公开体验", "description": "测试", "kind": "public_demo", "config": VALID_CONFIG},
    )
    assert response.status_code == 201
    agent = response.json()
    published = client.post(
        f"/inbound-api/projects/{project['id']}/agents/{agent['id']}/publish",
        headers=headers,
        json={"expected_revision": 1},
    )
    assert published.status_code == 200
    bound = client.post(
        f"/inbound-api/projects/{project['id']}/agents/{agent['id']}/bindings",
        headers=headers,
        json={"entry_type": "web", "destination": "public-api-demo", "trunk_id": ""},
    )
    assert bound.status_code == 201

    public = client.post(
        "/inbound-api/public/demo/web-sessions",
        headers={"X-Forwarded-For": "198.51.100.7"},
        json={"participant_name": "访客"},
    )
    assert public.status_code == 201
    assert public.json()["token"] == "test-token"
    assert public.json()["room_name"].startswith("demo-")
    assert issued[0][3] <= 300

    second_public = client.post(
        "/inbound-api/public/demo/web-sessions",
        headers={"X-Forwarded-For": "192.0.2.1"},
        json={"participant_name": "访客"},
    )
    assert second_public.status_code == 201
    spoofed_third = client.post(
        "/inbound-api/public/demo/web-sessions",
        headers={"X-Forwarded-For": "192.0.2.2"},
        json={"participant_name": "访客"},
    )
    assert spoofed_third.status_code == 429

    denied = client.get(
        f"/inbound-api/projects/{project['id']}/agents", headers={"X-User-ID": "owner-b"}
    )
    assert denied.status_code == 403


def test_public_livekit_token_allows_chat_data_but_not_arbitrary_sources(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a-livekit-secret-with-more-than-32-characters")
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit.test")
    issued = issue_public_livekit_token("text-room", "text-user", "signed-metadata", 120)
    claims = __import__("jwt").decode(issued["token"], options={"verify_signature": False})
    grant = claims["video"]
    assert grant["canPublishData"] is True
    assert grant["canPublishSources"] == ["microphone"]
    assert claims["roomConfig"]["agents"][0]["agentName"] == "public-demo-agent"


def test_public_completion_is_idempotent_and_accounts_duration(environment):
    _, store, _, service, project, _ = environment
    agent, _ = create_published_agent(store, project["id"], kind="public_demo")
    store.create_binding(
        project_id=project["id"], actor_id="owner-a", agent_id=agent["id"],
        entry_type="web", destination="duration-demo", trunk_id="",
    )
    prepared = service.prepare_public_web_session(session_id="duration-session", room_name="duration-room")
    session = service.commit_public_web_session(
        source="198.51.100.44", binding=prepared["binding"],
        room_name="duration-room", provider_call_id="web:duration",
    )
    first = store.complete_public_session(
        session_id=session["session_id"], duration_seconds=123, termination_reason="time_limit"
    )
    second = store.complete_public_session(
        session_id=session["session_id"], duration_seconds=999, termination_reason="duplicate"
    )
    assert first["duration_seconds"] == 123
    assert second["duration_seconds"] == 123
    with store.platform.connect() as conn:
        usage = conn.execute(
            "SELECT total_seconds FROM public_demo_usage WHERE subject_hash = ?",
            (service.subject_hash("198.51.100.44"),),
        ).fetchone()
    assert usage["total_seconds"] == 123


def test_expired_public_reservation_is_reaped_and_cannot_activate(environment):
    _, store, _, service, project, _ = environment
    agent, _ = create_published_agent(store, project["id"], kind="public_demo")
    binding = store.create_binding(
        project_id=project["id"], actor_id="owner-a", agent_id=agent["id"],
        entry_type="web", destination="expiry-demo", trunk_id="",
    )
    session = store.create_public_session(
        subject_hash=service.subject_hash("203.0.113.99"), max_calls_per_day=3,
        max_total_seconds=600, binding=binding, room_name="expiry-room",
        provider_call_id="web:expiry", reservation_ttl_seconds=30,
    )
    with store.platform.transaction() as conn:
        conn.execute(
            "UPDATE inbound_agent_sessions SET reservation_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", session["session_id"]),
        )
    result = store.reap_stale_sessions()
    assert result["expired_reservations"] == 1
    with pytest.raises(AccessDeniedError):
        store.activate_session(
            session_id=session["session_id"], binding_id=binding["id"],
            room_name="expiry-room", provider_call_id="web:expiry",
        )


def test_public_global_capacity_is_enforced(environment):
    _, store, _, service, project, _ = environment
    agent, _ = create_published_agent(store, project["id"], kind="public_demo")
    binding = store.create_binding(
        project_id=project["id"], actor_id="owner-a", agent_id=agent["id"],
        entry_type="web", destination="capacity-demo", trunk_id="",
    )
    store.create_public_session(
        subject_hash=service.subject_hash("203.0.113.1"), max_calls_per_day=3,
        max_total_seconds=600, binding=binding, room_name="capacity-one",
        provider_call_id="web:capacity-one", max_concurrent_sessions=1,
    )
    with pytest.raises(PublicDemoQuotaError, match="capacity"):
        store.create_public_session(
            subject_hash=service.subject_hash("203.0.113.2"), max_calls_per_day=3,
            max_total_seconds=600, binding=binding, room_name="capacity-two",
            provider_call_id="web:capacity-two", max_concurrent_sessions=1,
        )


def test_public_agent_cannot_be_created_outside_reserved_project(environment):
    _, store, _, _, _, other = environment
    with pytest.raises(AccessDeniedError):
        store.create_agent(
            project_id=other["id"], actor_id="owner-b", name="伪公开Agent",
            description="不允许", kind="public_demo", config=VALID_CONFIG,
        )


def test_enterprise_sip_admission_resolves_did_and_is_idempotent(environment):
    _, store, _, service, project, _ = environment
    agent, version = create_published_agent(store, project["id"])
    binding = store.create_binding(
        project_id=project["id"], actor_id="owner-a", agent_id=agent["id"],
        entry_type="sip_did", destination="+8613800001234", trunk_id="ST_enterprise",
    )
    first = service.admit_sip(
        trunk_id="ST_enterprise", called_number="+8613800001234",
        caller_number="+8613900005678", room_name="sip-room-1", provider_call_id="call-sip-1",
    )
    second = service.admit_sip(
        trunk_id="ST_enterprise", called_number="+8613800001234",
        caller_number="+8613900005678", room_name="sip-room-1", provider_call_id="call-sip-1",
    )
    assert first["session_id"] == second["session_id"]
    assert first["agent_version_id"] == version["id"]
    assert first["binding_id"] == binding["id"]
    completed = store.complete_session(
        session_id=first["session_id"], duration_seconds=88, termination_reason="caller_left"
    )
    assert completed["duration_seconds"] == 88
    assert completed["status"] == "completed"

    with pytest.raises(Exception):
        service.admit_sip(
            trunk_id="ST_enterprise", called_number="+8613800099999",
            caller_number="+8613900005678", room_name="sip-room-2", provider_call_id="call-sip-2",
        )


def test_public_sip_admission_is_idempotent_without_double_quota(environment):
    _, store, _, service, project, _ = environment
    agent, _ = create_published_agent(store, project["id"], kind="public_demo")
    store.create_binding(
        project_id=project["id"], actor_id="owner-a", agent_id=agent["id"],
        entry_type="sip_did", destination="+8613800008888", trunk_id="ST_public",
    )
    arguments = dict(
        trunk_id="ST_public", called_number="+8613800008888",
        caller_number="+8613900009999", room_name="public-sip-room",
        provider_call_id="public-call-one",
    )
    first = service.admit_sip(**arguments)
    second = service.admit_sip(**arguments)
    assert first["session_id"] == second["session_id"]
    with store.platform.connect() as conn:
        usage = conn.execute("SELECT call_count FROM public_demo_usage").fetchone()
    assert usage["call_count"] == 1


def test_enterprise_project_capacity_does_not_block_other_tenant(environment):
    _, store, _, _, project, other = environment
    agent_a, _ = create_published_agent(store, project["id"])
    agent_b, _ = create_published_agent(store, other["id"], actor_id="owner-b")
    binding_a = store.create_binding(
        project_id=project["id"], actor_id="owner-a", agent_id=agent_a["id"],
        entry_type="sip_did", destination="+8613800010001", trunk_id="ST_a",
    )
    binding_b = store.create_binding(
        project_id=other["id"], actor_id="owner-b", agent_id=agent_b["id"],
        entry_type="sip_did", destination="+8613800010002", trunk_id="ST_b",
    )
    store.create_enterprise_session(
        binding=binding_a, room_name="tenant-a-one", provider_call_id="tenant-a-one",
        caller_hash="hash-a", caller_last4="0001", max_concurrent_sessions=1,
    )
    with pytest.raises(PublicDemoQuotaError, match="concurrency"):
        store.create_enterprise_session(
            binding=binding_a, room_name="tenant-a-two", provider_call_id="tenant-a-two",
            caller_hash="hash-a", caller_last4="0002", max_concurrent_sessions=1,
        )
    allowed = store.create_enterprise_session(
        binding=binding_b, room_name="tenant-b-one", provider_call_id="tenant-b-one",
        caller_hash="hash-b", caller_last4="0003", max_concurrent_sessions=1,
    )
    assert allowed["project_id"] == other["id"]


def test_worker_tokens_are_short_lived_scoped_and_rotation_aware():
    current = "current-worker-secret-with-more-than-32-characters"
    previous = "previous-worker-secret-with-more-than-32-characters"
    token = issue_worker_token(current, subject="worker-1", scopes=["runtime:read"])
    claims = verify_worker_token(
        token, secrets=(previous, current), required_scope="runtime:read"
    )
    assert claims["sub"] == "worker-1"
    assert claims["jti"]
    with pytest.raises(WorkerAuthenticationError):
        verify_worker_token(token, secrets=(current,), required_scope="session:complete")
    identities = {"public-worker": {"secret": current, "scopes": ["runtime:read", "session:complete"]}}
    forged = issue_worker_token(current, subject="public-worker", scopes=["sip:admit"])
    with pytest.raises(WorkerAuthenticationError):
        verify_worker_identity_token(forged, identities=identities, required_scope="sip:admit")


def test_feature_flag_fails_closed_for_public_enterprise_and_internal(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUD_PARITY_ENV", "test")
    monkeypatch.setenv("CLOUD_PARITY_AUTH_MODE", "development")
    monkeypatch.setenv("CLOUD_PARITY_DATABASE_PATH", str(tmp_path / "disabled.sqlite3"))
    monkeypatch.setenv("INBOUND_AGENT_SYSTEM_ENABLED", "false")
    monkeypatch.setenv("INBOUND_METADATA_SECRET", "metadata-secret-with-more-than-32-characters")
    monkeypatch.setenv("INBOUND_WORKER_SECRET", "worker-secret-with-more-than-32-characters")
    from server.inbound_control.main import create_app

    client = TestClient(create_app())
    assert client.get("/inbound-api/health/live").status_code == 200
    assert client.get("/inbound-api/public/demo").status_code == 503
    assert client.get(
        "/inbound-api/projects/project/agents", headers={"X-User-ID": "owner"}
    ).status_code == 503
    assert client.post(
        "/inbound-api/internal/runtime",
        json={"metadata": "x" * 50, "room_name": "room"},
    ).status_code == 503
