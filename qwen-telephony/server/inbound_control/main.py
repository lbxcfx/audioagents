from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.cloud_parity.auth import create_authenticator, install_authenticator
from server.cloud_parity.config import PlatformSettings
from server.cloud_parity.store import PlatformStore

from .api import create_inbound_router
from .metadata import InboundMetadataSigner
from .service import InboundAgentService
from .store import InboundAgentStore


ROOT = Path(__file__).resolve().parents[3]


def create_app() -> FastAPI:
    settings = PlatformSettings.from_env(ROOT)
    metadata_secret = os.getenv("INBOUND_METADATA_SECRET", "").strip()
    worker_secret = os.getenv("INBOUND_WORKER_SECRET", "").strip()
    if settings.environment in {"staging", "production"}:
        if len(metadata_secret) < 32 or not os.getenv("INBOUND_WORKER_IDENTITIES_JSON", "").strip():
            raise ValueError("inbound metadata and worker secrets must contain at least 32 characters")
    metadata_secret = metadata_secret or "development-inbound-metadata-secret-change-me"
    worker_secret = worker_secret or "development-inbound-worker-secret-change-me"

    platform = PlatformStore(
        settings.database_path,
        default_retention_days=settings.default_retention_days,
        database_url=settings.database_url,
        min_pool_size=settings.database_pool_min_size,
        max_pool_size=settings.database_pool_max_size,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    platform.initialize()
    inbound = InboundAgentStore(
        platform,
        public_project_id=os.getenv("INBOUND_PUBLIC_PROJECT_ID", ""),
    )
    inbound.migrate()
    service = InboundAgentService(
        inbound,
        InboundMetadataSigner(metadata_secret),
        public_hash_key=settings.phone_hash_key,
        public_calls_per_day=int(os.getenv("INBOUND_PUBLIC_CALLS_PER_DAY", "3")),
        public_seconds_per_day=int(os.getenv("INBOUND_PUBLIC_SECONDS_PER_DAY", "600")),
        public_session_seconds=int(os.getenv("INBOUND_PUBLIC_SESSION_SECONDS", "180")),
        public_max_concurrent_sessions=int(os.getenv("INBOUND_PUBLIC_MAX_CONCURRENT", "20")),
        enterprise_max_concurrent_sessions=int(os.getenv("INBOUND_ENTERPRISE_MAX_CONCURRENT", "100")),
    )

    app = FastAPI(title="Audio Agents Inbound Control API", version="1.0.0")
    app.state.platform_store = platform
    app.state.inbound_store = inbound
    install_authenticator(
        app,
        create_authenticator(
            settings.authentication,
            revocation_checker=platform.is_access_token_revoked,
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-User-ID"],
    )
    inbound_enabled = os.getenv("INBOUND_AGENT_SYSTEM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

    @app.middleware("http")
    async def inbound_feature_gate(request, call_next):
        if (
            request.url.path.startswith("/inbound-api/")
            and request.url.path not in {"/inbound-api/health/live", "/inbound-api/health/ready"}
            and not inbound_enabled
        ):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=503, content={"detail": "inbound agent system is disabled", "code": "feature_disabled"})
        return await call_next(request)

    app.include_router(
        create_inbound_router(
            inbound,
            service,
            worker_secret=worker_secret,
            enabled=inbound_enabled,
        )
    )

    @app.on_event("shutdown")
    def close_database() -> None:
        platform.close()

    return app


app = create_app()
