from __future__ import annotations

from datetime import timedelta
import base64
import binascii
import os
import json
from typing import Any, Callable, Literal
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.cloud_parity.auth import require_user_id
from server.cloud_parity.database import is_integrity_error
from server.cloud_parity.store import AccessDeniedError, ResourceNotFoundError

from .service import InboundAgentService
from .store import INBOUND_SCHEMA_VERSION, InboundAgentStore, PublicDemoQuotaError
from .knowledge import KnowledgeStore
from .tool_gateway import ToolGateway
from .content import ContentStore
from .worker_auth import WorkerAuthenticationError, verify_worker_identity_token, verify_worker_token


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: str = Field(min_length=10, max_length=20_000)
    welcome_message: str = Field(min_length=1, max_length=1_000)
    voice: Literal[
        "longanqian", "longanlingxin", "longanlufeng", "longanlingxi",
        "longanxiaoxin", "longanfengyue", "longanyuanfei", "longanhuan_v3.6",
        "longjielidou_v3.6", "longpaopao_v3.6", "longhuohuo_v3.6",
        "longchuanshu_v3.6", "loongmary", "loongeva_v3.6", "loongjohn",
    ] = "longanlingxin"
    language: str = Field(default="zh-CN", min_length=2, max_length=32)
    max_duration_seconds: int = Field(default=600, ge=30, le=7_200)
    recording_mode: Literal["off"] = "off"
    recording_disclosure: str = Field(default="", max_length=1_000)
    tools: list[str] = Field(default_factory=list, max_length=50)
    knowledge_sources: list[str] = Field(default_factory=list, max_length=100)
    content_sources: list[str] = Field(default_factory=list, max_length=100)
    avatar_enabled: bool = False
    avatar_id: str = Field(default="", max_length=120)

    @field_validator("knowledge_sources")
    @classmethod
    def validate_knowledge_sources(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 120 for value in normalized):
            raise ValueError("knowledge source identifiers must contain 1 to 120 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("knowledge source identifiers must be unique")
        return normalized


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1_000)
    kind: Literal["enterprise", "public_demo"] = "enterprise"
    config: AgentConfig


class AgentUpdate(BaseModel):
    expected_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1_000)
    config: AgentConfig


class BindingCreate(BaseModel):
    entry_type: Literal["sip_did", "web"]
    destination: str = Field(min_length=1, max_length=255)
    trunk_id: str = Field(default="", max_length=255)


class BindingVersionUpdate(BaseModel):
    agent_version_id: str = Field(min_length=1, max_length=120)


class VersionActivateRequest(BaseModel):
    agent_version_id: str = Field(min_length=1, max_length=120)


class PublicSessionRequest(BaseModel):
    participant_name: str = Field(default="访客", min_length=1, max_length=120)


class PublishRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1_000)


class KnowledgeTextDocumentCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    media_type: Literal["text/plain", "text/markdown"] = "text/plain"
    text: str = Field(min_length=1, max_length=5_000_000)


class KnowledgeDocumentCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    media_type: Literal[
        "text/plain", "text/markdown", "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]
    content_base64: str = Field(min_length=1, max_length=28_000_000)


class KnowledgeSearchRequest(BaseModel):
    knowledge_base_ids: list[str] = Field(min_length=1, max_length=20)
    document_ids: list[str] | None = Field(default=None, max_length=2_000)
    query: str = Field(min_length=1, max_length=2_000)
    limit: int = Field(default=5, ge=1, le=20)


class InternalKnowledgeSearchRequest(KnowledgeSearchRequest):
    project_id: str = Field(min_length=1, max_length=120)


class ToolConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["http_api", "mcp_streamable_http"]
    base_url: str = Field(min_length=8, max_length=2_000)
    headers: dict[str, str] = Field(default_factory=dict)


class ToolCreate(BaseModel):
    connection_id: str = Field(min_length=1, max_length=120)
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$", max_length=80)
    description: str = Field(min_length=1, max_length=1_000)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=1_000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    policy: Literal["auto", "confirm", "deny"] = "confirm"
    timeout_seconds: int = Field(default=10, ge=1, le=30)


class InternalToolCatalogRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=120)
    tool_ids: list[str] = Field(max_length=50)


class InternalToolInvokeRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=120)
    session_id: str = Field(min_length=1, max_length=120)
    tool_id: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)

class InternalToolConfirmRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=120)
    session_id: str = Field(min_length=1, max_length=120)
    confirmation_id: str = Field(min_length=1, max_length=120)

class ContentAssetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["image", "video", "pdf", "steps"]
    source_url: str = Field(min_length=8, max_length=2_000)
    description: str = Field(default="", max_length=1_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

class InternalContentRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=120)
    asset_id: str = Field(min_length=1, max_length=120)


class RuntimeRequest(BaseModel):
    metadata: str = Field(min_length=40, max_length=4096)
    room_name: str = Field(min_length=1, max_length=255)
    provider_call_id: str = Field(default="", max_length=255)


class SessionCompleteRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=120)
    duration_seconds: int = Field(ge=0, le=86_400)
    termination_reason: str = Field(default="completed", min_length=1, max_length=120)


class SipAdmissionRequest(BaseModel):
    trunk_id: str = Field(min_length=1, max_length=255)
    called_number: str = Field(min_length=3, max_length=64)
    caller_number: str = Field(default="", max_length=64)
    room_name: str = Field(min_length=1, max_length=255)
    provider_call_id: str = Field(min_length=1, max_length=255)


class DispatchSyncRequest(BaseModel):
    binding_id: str = Field(min_length=1, max_length=120)
    dispatch_rule_id: str = Field(min_length=1, max_length=255)


class MaintenanceRequest(BaseModel):
    active_grace_seconds: int = Field(default=7_500, ge=7_200, le=86_400)


TokenIssuer = Callable[[str, str, str, int], dict[str, str]]


def issue_public_livekit_token(
    room_name: str,
    identity: str,
    dispatch_metadata: str,
    ttl_seconds: int,
) -> dict[str, str]:
    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    livekit_url = os.getenv("LIVEKIT_URL", "").strip()
    if not api_key or not api_secret or not livekit_url:
        raise RuntimeError("LiveKit public session configuration is incomplete")
    from livekit import api

    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name("Public voice guest")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                # LiveKit text streams (lk.chat) use the data channel. The
                # public Agent has tools disabled, so this only enables chat.
                can_publish_data=True,
                can_publish_sources=["microphone", "camera", "screen_share"],
            )
        )
        .with_room_config(
            api.RoomConfiguration(
                agents=[
                    api.RoomAgentDispatch(
                        agent_name=os.getenv("INBOUND_PUBLIC_AGENT_NAME", "public-demo-agent"),
                        metadata=dispatch_metadata,
                    )
                ]
            )
        )
        .with_ttl(timedelta(seconds=ttl_seconds))
        .to_jwt()
    )
    return {"token": token, "url": livekit_url}

def issue_enterprise_livekit_token(room_name: str, identity: str, dispatch_metadata: str, ttl_seconds: int) -> dict[str, str]:
    api_key, api_secret = os.getenv("LIVEKIT_API_KEY", "").strip(), os.getenv("LIVEKIT_API_SECRET", "").strip()
    livekit_url = os.getenv("LIVEKIT_URL", "").strip()
    if not api_key or not api_secret or not livekit_url: raise RuntimeError("LiveKit is not configured")
    token = (api.AccessToken(api_key, api_secret).with_identity(identity).with_name("Enterprise Agent tester").with_grants(api.VideoGrants(room_join=True, room=room_name, can_publish=True, can_subscribe=True, can_publish_data=True, can_publish_sources=["microphone", "camera", "screen_share"])).with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=os.getenv("INBOUND_ENTERPRISE_AGENT_NAME", "tenant-voice-agent"), metadata=dispatch_metadata)])).with_ttl(timedelta(seconds=ttl_seconds)).to_jwt())
    return {"token": token, "url": livekit_url}


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ResourceNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PublicDemoQuotaError):
        return HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "3600"})
    if isinstance(exc, (AccessDeniedError, PermissionError)):
        return HTTPException(status_code=403, detail=str(exc))
    if is_integrity_error(exc):
        return HTTPException(status_code=409, detail="resource already exists")
    if isinstance(exc, ValueError):
        detail = str(exc)
        return HTTPException(status_code=409 if "conflict" in detail else 422, detail=detail)
    return HTTPException(status_code=500, detail="inbound agent operation failed")


def create_inbound_router(
    store: InboundAgentStore,
    service: InboundAgentService,
    *,
    knowledge_store: KnowledgeStore | None = None,
    tool_gateway: ToolGateway | None = None,
    content_store: ContentStore | None = None,
    worker_secret: str,
    token_issuer: TokenIssuer = issue_public_livekit_token,
    enabled: bool = True,
) -> APIRouter:
    router = APIRouter(prefix="/inbound-api", tags=["inbound-agents"])
    worker_secrets = tuple(
        value for value in (worker_secret, os.getenv("INBOUND_WORKER_SECRET_PREVIOUS", "").strip()) if value
    )
    identity_config = os.getenv("INBOUND_WORKER_IDENTITIES_JSON", "").strip()
    worker_identities = json.loads(identity_config) if identity_config else {}

    def require_worker(authorization: str, scope: str) -> dict[str, Any]:
        supplied = authorization.removeprefix("Bearer ").strip()
        try:
            if worker_identities:
                return verify_worker_identity_token(
                    supplied, identities=worker_identities, required_scope=scope
                )
            return verify_worker_token(supplied, secrets=worker_secrets, required_scope=scope)
        except WorkerAuthenticationError as exc:
            raise HTTPException(status_code=401, detail="worker authentication failed") from exc

    @router.get("/health/live", include_in_schema=False)
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/ready", include_in_schema=False)
    def ready() -> dict[str, Any]:
        health = store.healthcheck()
        if health["schema_version"] != INBOUND_SCHEMA_VERSION:
            raise HTTPException(status_code=503, detail="inbound schema is not ready")
        if knowledge_store is not None:
            knowledge_health = knowledge_store.healthcheck()
            if knowledge_health["schema_version"] != 4:
                raise HTTPException(status_code=503, detail="knowledge schema is not ready")
            health["knowledge_schema_version"] = knowledge_health["schema_version"]
        return health

    @router.get("/public/demo")
    def public_demo() -> dict[str, Any]:
        if not enabled:
            return {"available": False, "reason_code": "feature_disabled"}
        try:
            return service.public_demo_info()
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/public/demo/web-sessions", status_code=201)
    def create_public_session(payload: PublicSessionRequest, request: Request) -> dict[str, Any]:
        if not enabled:
            raise HTTPException(status_code=503, detail="inbound agent system is disabled")
        # Uvicorn/our ingress is responsible for resolving trusted proxy headers.
        # Never accept a caller-controlled forwarding header at the application layer.
        source = request.client.host if request.client else "unknown"
        try:
            room_name = f"demo-{uuid.uuid4().hex}"
            identity = f"demo:{uuid.uuid4().hex}"
            session_id = str(uuid.uuid4())
            prepared = service.prepare_public_web_session(
                session_id=session_id,
                room_name=room_name,
            )
            credentials = token_issuer(
                room_name,
                identity,
                prepared["dispatch_metadata"],
                min(300, int(prepared["max_duration_seconds"]) + 60),
            )
            session = service.commit_public_web_session(
                source=source,
                binding=prepared["binding"],
                room_name=room_name,
                provider_call_id=f"web:{identity}",
            )
            return {
                **session,
                **credentials,
                "room_name": room_name,
                "identity": identity,
                "participant_name": payload.participant_name,
                "max_duration_seconds": prepared["max_duration_seconds"],
            }
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/public/demo/sessions/{session_id}")
    def public_session_status(session_id: str) -> dict[str, Any]:
        try:
            return store.get_public_session_status(session_id=session_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/agents")
    def list_agents(project_id: str, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        try:
            return {"items": store.list_agents(project_id=project_id, actor_id=user_id)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/knowledge-bases")
    def list_knowledge_bases(project_id: str, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if knowledge_store is None:
            raise HTTPException(status_code=503, detail="knowledge service is disabled")
        try:
            return {"items": knowledge_store.list_bases(project_id=project_id, actor_id=user_id)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/knowledge-bases", status_code=201)
    def create_knowledge_base(project_id: str, payload: KnowledgeBaseCreate, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if knowledge_store is None:
            raise HTTPException(status_code=503, detail="knowledge service is disabled")
        try:
            return knowledge_store.create_base(project_id=project_id, actor_id=user_id, name=payload.name, description=payload.description)
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/knowledge-bases/{base_id}/documents/text", status_code=201)
    def add_knowledge_text(project_id: str, base_id: str, payload: KnowledgeTextDocumentCreate, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if knowledge_store is None:
            raise HTTPException(status_code=503, detail="knowledge service is disabled")
        try:
            return knowledge_store.add_text_document(project_id=project_id, actor_id=user_id, base_id=base_id, filename=payload.filename, media_type=payload.media_type, text=payload.text)
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/knowledge-bases/{base_id}/documents")
    def list_knowledge_documents(project_id: str, base_id: str, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if knowledge_store is None:
            raise HTTPException(status_code=503, detail="knowledge service is disabled")
        try:
            return {"items": knowledge_store.list_documents(project_id=project_id, actor_id=user_id, base_id=base_id)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/knowledge-bases/{base_id}/documents", status_code=202)
    def add_knowledge_document(project_id: str, base_id: str, payload: KnowledgeDocumentCreate, background_tasks: BackgroundTasks, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if knowledge_store is None:
            raise HTTPException(status_code=503, detail="knowledge service is disabled")
        try:
            data = base64.b64decode(payload.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail="document content is not valid base64") from exc
        try:
            job = knowledge_store.queue_document(project_id=project_id, actor_id=user_id, base_id=base_id, filename=payload.filename, media_type=payload.media_type, data=data)
            if os.getenv("INBOUND_KNOWLEDGE_INLINE_WORKER", "false").lower() in {"1", "true", "yes", "on"}:
                background_tasks.add_task(knowledge_store.process_job, job["id"])
            return job
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/knowledge-jobs/{job_id}")
    def get_knowledge_job(project_id: str, job_id: str, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if knowledge_store is None: raise HTTPException(status_code=503, detail="knowledge service is disabled")
        try: return knowledge_store.get_job(project_id=project_id, actor_id=user_id, job_id=job_id)
        except Exception as exc: raise _http_error(exc) from exc

    @router.delete("/projects/{project_id}/knowledge-bases/{base_id}/documents/{document_id}")
    def delete_knowledge_document(project_id: str, base_id: str, document_id: str, user_id: str = Depends(require_user_id)) -> dict[str, str]:
        if knowledge_store is None:
            raise HTTPException(status_code=503, detail="knowledge service is disabled")
        try:
            return knowledge_store.delete_document(project_id=project_id, actor_id=user_id, base_id=base_id, document_id=document_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/knowledge/search")
    def search_knowledge(project_id: str, payload: KnowledgeSearchRequest, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if knowledge_store is None:
            raise HTTPException(status_code=503, detail="knowledge service is disabled")
        try:
            store.platform.require_permission(project_id, user_id, "agent.read")
            return {"items": knowledge_store.search(project_id=project_id, base_ids=payload.knowledge_base_ids, document_ids=payload.document_ids, query=payload.query, limit=payload.limit)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/tool-connections")
    def list_tool_connections(project_id: str, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if tool_gateway is None: raise HTTPException(status_code=503, detail="tool gateway is disabled")
        try: return {"items": tool_gateway.list_connections(project_id=project_id, actor_id=user_id)}
        except Exception as exc: raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/tool-connections", status_code=201)
    def create_tool_connection(project_id: str, payload: ToolConnectionCreate, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if tool_gateway is None: raise HTTPException(status_code=503, detail="tool gateway is disabled")
        try: return tool_gateway.create_connection(project_id=project_id, actor_id=user_id, **payload.model_dump())
        except Exception as exc: raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/tool-connections/{connection_id}/discover")
    async def discover_mcp_tools(project_id: str, connection_id: str, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if tool_gateway is None: raise HTTPException(status_code=503, detail="tool gateway is disabled")
        try: return {"items": await tool_gateway.discover_mcp(project_id=project_id, actor_id=user_id, connection_id=connection_id)}
        except Exception as exc: raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/tools")
    def list_tools(project_id: str, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if tool_gateway is None: raise HTTPException(status_code=503, detail="tool gateway is disabled")
        try: return {"items": tool_gateway.list_tools(project_id=project_id, actor_id=user_id)}
        except Exception as exc: raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/tools", status_code=201)
    def create_tool(project_id: str, payload: ToolCreate, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if tool_gateway is None: raise HTTPException(status_code=503, detail="tool gateway is disabled")
        try: return tool_gateway.create_tool(project_id=project_id, actor_id=user_id, **payload.model_dump())
        except Exception as exc: raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/content-assets")
    def list_content_assets(project_id: str, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if content_store is None: raise HTTPException(status_code=503, detail="content service is disabled")
        try: return {"items": content_store.list(project_id=project_id, actor_id=user_id)}
        except Exception as exc: raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/content-assets", status_code=201)
    def create_content_asset(project_id: str, payload: ContentAssetCreate, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if content_store is None: raise HTTPException(status_code=503, detail="content service is disabled")
        try: return content_store.create(project_id=project_id, actor_id=user_id, **payload.model_dump())
        except Exception as exc: raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/video-service/presets")
    def get_video_service_presets(project_id: str, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if content_store is None: raise HTTPException(status_code=503, detail="content service is disabled")
        try: return content_store.video_service_presets(project_id=project_id, actor_id=user_id)
        except Exception as exc: raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/content-assets/{asset_id}/publish")
    def publish_content_asset(project_id: str, asset_id: str, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        if content_store is None: raise HTTPException(status_code=503, detail="content service is disabled")
        try: return content_store.publish(project_id=project_id, actor_id=user_id, asset_id=asset_id)
        except Exception as exc: raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/agents", status_code=201)
    def create_agent(
        project_id: str,
        payload: AgentCreate,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            return store.create_agent(
                project_id=project_id,
                actor_id=user_id,
                name=payload.name,
                description=payload.description,
                kind=payload.kind,
                config=payload.config.model_dump(),
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/agents/{agent_id}")
    def get_agent(
        project_id: str,
        agent_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            item = store.get_agent(project_id=project_id, actor_id=user_id, agent_id=agent_id)
            item["bindings"] = store.list_bindings(
                project_id=project_id, actor_id=user_id, agent_id=agent_id
            )
            return item
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.put("/projects/{project_id}/agents/{agent_id}")
    def update_agent(
        project_id: str,
        agent_id: str,
        payload: AgentUpdate,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            return store.update_agent(
                project_id=project_id,
                actor_id=user_id,
                agent_id=agent_id,
                expected_revision=payload.expected_revision,
                name=payload.name,
                description=payload.description,
                config=payload.config.model_dump(),
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/agents/{agent_id}/publish")
    def publish_agent(
        project_id: str,
        agent_id: str,
        payload: PublishRequest,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            return store.publish_agent(
                project_id=project_id,
                actor_id=user_id,
                agent_id=agent_id,
                expected_revision=payload.expected_revision,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/agents/{agent_id}/test-sessions", status_code=201)
    def create_agent_test_session(project_id: str, agent_id: str, user_id: str = Depends(require_user_id)) -> dict[str, Any]:
        try:
            store.platform.require_permission(project_id, user_id, "agent.read")
            bindings = store.list_bindings(project_id=project_id, actor_id=user_id, agent_id=agent_id)
            binding = next((item for item in bindings if item["entry_type"] == "web" and item["status"] == "active"), None)
            if binding is None: raise ValueError("publish the Agent and create an active web binding before testing")
            room_name, identity = f"agent-test-{uuid.uuid4().hex}", f"tester:{uuid.uuid4().hex}"
            session = store.create_enterprise_session(binding=binding, room_name=room_name, provider_call_id=f"test:{identity}", caller_hash="", caller_last4="", max_concurrent_sessions=20)
            metadata = service.signer.sign({"kind": "enterprise", "binding_id": binding["id"], "project_id": project_id, "agent_version_id": binding["agent_version_id"], "session_id": session["id"], "room_name": room_name})
            credentials = issue_enterprise_livekit_token(room_name, identity, metadata, 900)
            return {**credentials, "session_id": session["id"], "room_name": room_name, "identity": identity, "max_duration_seconds": 840}
        except Exception as exc: raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/agents/{agent_id}/versions")
    def list_versions(
        project_id: str,
        agent_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            return {"items": store.list_versions(project_id=project_id, actor_id=user_id, agent_id=agent_id)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/agents/{agent_id}/activate-version")
    def activate_version(
        project_id: str,
        agent_id: str,
        payload: VersionActivateRequest,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            return store.activate_version(
                project_id=project_id, actor_id=user_id, agent_id=agent_id,
                version_id=payload.agent_version_id,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/projects/{project_id}/agents/{agent_id}/bindings", status_code=201)
    def create_binding(
        project_id: str,
        agent_id: str,
        payload: BindingCreate,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            return store.create_binding(
                project_id=project_id,
                actor_id=user_id,
                agent_id=agent_id,
                entry_type=payload.entry_type,
                destination=payload.destination,
                trunk_id=payload.trunk_id,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.put("/projects/{project_id}/agents/{agent_id}/bindings/{binding_id}/version")
    def update_binding_version(
        project_id: str,
        agent_id: str,
        binding_id: str,
        payload: BindingVersionUpdate,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            binding = store.get_binding(project_id=project_id, actor_id=user_id, binding_id=binding_id)
            if binding["agent_id"] != agent_id:
                raise ResourceNotFoundError("inbound binding not found")
            return store.update_binding_version(
                project_id=project_id, actor_id=user_id, binding_id=binding_id,
                version_id=payload.agent_version_id,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.delete("/projects/{project_id}/agents/{agent_id}/bindings/{binding_id}")
    def disable_binding(
        project_id: str,
        agent_id: str,
        binding_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            binding = store.get_binding(project_id=project_id, actor_id=user_id, binding_id=binding_id)
            if binding["agent_id"] != agent_id:
                raise ResourceNotFoundError("inbound binding not found")
            return store.disable_binding(project_id=project_id, actor_id=user_id, binding_id=binding_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/agents/{agent_id}/sessions")
    def list_sessions(
        project_id: str,
        agent_id: str,
        limit: int = 100,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            return {"items": store.list_sessions(
                project_id=project_id, actor_id=user_id, agent_id=agent_id, limit=limit
            )}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/agents/{agent_id}/sessions/{session_id}")
    def get_session(
        project_id: str,
        agent_id: str,
        session_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            item = store.get_session(project_id=project_id, actor_id=user_id, session_id=session_id)
            if item["agent_id"] != agent_id:
                raise ResourceNotFoundError("inbound session not found")
            return item
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/projects/{project_id}/agents/{agent_id}/analytics")
    def analytics(
        project_id: str,
        agent_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict[str, Any]:
        try:
            return store.session_analytics(project_id=project_id, actor_id=user_id, agent_id=agent_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/internal/runtime")
    def runtime(
        payload: RuntimeRequest,
        authorization: str = Header(default="", alias="Authorization"),
    ) -> dict[str, Any]:
        require_worker(authorization, "runtime:read")
        try:
            return service.resolve_runtime(
                payload.metadata,
                observed_room_name=payload.room_name,
                provider_call_id=payload.provider_call_id,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/internal/knowledge/search")
    def internal_search_knowledge(
        payload: InternalKnowledgeSearchRequest,
        authorization: str = Header(default="", alias="Authorization"),
    ) -> dict[str, Any]:
        require_worker(authorization, "knowledge:read")
        if knowledge_store is None:
            raise HTTPException(status_code=503, detail="knowledge service is disabled")
        try:
            return {"items": knowledge_store.search(project_id=payload.project_id, base_ids=payload.knowledge_base_ids, document_ids=payload.document_ids, query=payload.query, limit=payload.limit)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/internal/tools/catalog")
    def internal_tool_catalog(payload: InternalToolCatalogRequest, authorization: str = Header(default="", alias="Authorization")) -> dict[str, Any]:
        require_worker(authorization, "tool:invoke")
        if tool_gateway is None: raise HTTPException(status_code=503, detail="tool gateway is disabled")
        try:
            tool_gateway.assert_tools(project_id=payload.project_id, tool_ids=payload.tool_ids)
            return {"items": [tool_gateway.get_tool(project_id=payload.project_id, tool_id=tool_id) for tool_id in payload.tool_ids]}
        except Exception as exc: raise _http_error(exc) from exc

    @router.post("/internal/content/get")
    def internal_content_get(payload: InternalContentRequest, authorization: str = Header(default="", alias="Authorization")) -> dict[str, Any]:
        require_worker(authorization, "runtime:read")
        if content_store is None: raise HTTPException(status_code=503, detail="content service is disabled")
        try: return content_store.get(project_id=payload.project_id, asset_id=payload.asset_id, published_only=True)
        except Exception as exc: raise _http_error(exc) from exc

    @router.post("/internal/tools/invoke")
    async def internal_tool_invoke(payload: InternalToolInvokeRequest, authorization: str = Header(default="", alias="Authorization")) -> dict[str, Any]:
        require_worker(authorization, "tool:invoke")
        if tool_gateway is None: raise HTTPException(status_code=503, detail="tool gateway is disabled")
        try:
            # The published Agent version is the authoritative whitelist, not the caller payload.
            with store.platform.connect() as conn:
                row = conn.execute("SELECT v.config_json FROM inbound_agent_sessions s JOIN inbound_agent_versions v ON v.id = s.agent_version_id AND v.project_id = s.project_id WHERE s.id = ? AND s.project_id = ?", (payload.session_id, payload.project_id)).fetchone()
            if row is None or payload.tool_id not in json.loads(row["config_json"]).get("tools", []):
                raise AccessDeniedError("tool is not allowed by the session Agent version")
            return await tool_gateway.invoke(**payload.model_dump(), confirmed=False)
        except Exception as exc: raise _http_error(exc) from exc

    @router.post("/internal/tools/confirm")
    async def internal_tool_confirm(payload: InternalToolConfirmRequest, authorization: str = Header(default="", alias="Authorization")) -> dict[str, Any]:
        require_worker(authorization, "tool:invoke")
        if tool_gateway is None: raise HTTPException(status_code=503, detail="tool gateway is disabled")
        try: return await tool_gateway.confirm(**payload.model_dump())
        except Exception as exc: raise _http_error(exc) from exc

    @router.post("/internal/sessions/complete")
    def complete_session(
        payload: SessionCompleteRequest,
        authorization: str = Header(default="", alias="Authorization"),
    ) -> dict[str, Any]:
        require_worker(authorization, "session:complete")
        try:
            return store.complete_session(
                session_id=payload.session_id,
                duration_seconds=payload.duration_seconds,
                termination_reason=payload.termination_reason,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/internal/sessions/reap")
    def reap_sessions(
        payload: MaintenanceRequest,
        authorization: str = Header(default="", alias="Authorization"),
    ) -> dict[str, int]:
        require_worker(authorization, "maintenance:run")
        return store.reap_stale_sessions(active_grace_seconds=payload.active_grace_seconds)

    @router.post("/internal/sip/admit")
    def admit_sip(
        payload: SipAdmissionRequest,
        authorization: str = Header(default="", alias="Authorization"),
    ) -> dict[str, Any]:
        require_worker(authorization, "sip:admit")
        try:
            return service.admit_sip(**payload.model_dump())
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/internal/sip/bindings")
    def active_sip_bindings(
        authorization: str = Header(default="", alias="Authorization"),
    ) -> dict[str, Any]:
        require_worker(authorization, "dispatch:sync")
        return {"items": store.list_active_sip_bindings()}

    @router.post("/internal/sip/dispatch-synced")
    def dispatch_synced(
        payload: DispatchSyncRequest,
        authorization: str = Header(default="", alias="Authorization"),
    ) -> dict[str, Any]:
        require_worker(authorization, "dispatch:sync")
        try:
            return store.mark_binding_dispatched(
                binding_id=payload.binding_id,
                dispatch_rule_id=payload.dispatch_rule_id,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    return router
