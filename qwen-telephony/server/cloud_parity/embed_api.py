from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .api import _translate_error
from .auth import require_user_id
from .embed import EmbedRateLimitError, EmbedService


class EmbedConfigSave(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    agent_name: str = Field(min_length=1, max_length=120)
    room_prefix: str = Field(default="embed", min_length=1, max_length=80)
    allowed_origins: list[str] = Field(min_length=1, max_length=100)
    capabilities: dict[str, bool] = Field(default_factory=lambda: {"audio": True, "text": True})
    enabled: bool = True


class EmbedTokenRequest(BaseModel):
    participant_name: str = Field(default="Guest", max_length=120)
    ttl_seconds: int = Field(default=300, ge=30, le=900)


def create_embed_router(service: EmbedService) -> APIRouter:
    router = APIRouter(tags=["cloud-parity-embed"])

    @router.post("/api/platform/projects/{project_id}/embed-configs", status_code=201)
    def create_config(
        project_id: str,
        payload: EmbedConfigSave,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.save_config(
                project_id=project_id, actor_id=x_user_id, **payload.model_dump()
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/api/platform/projects/{project_id}/embed-configs")
    def list_configs(
        project_id: str, x_user_id: str = Depends(require_user_id)
    ) -> dict:
        try:
            return {"items": service.list_configs(project_id=project_id, user_id=x_user_id)}
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.put("/api/platform/projects/{project_id}/embed-configs/{config_id}")
    def update_config(
        project_id: str,
        config_id: str,
        payload: EmbedConfigSave,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.save_config(
                project_id=project_id,
                actor_id=x_user_id,
                config_id=config_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/api/embed/{config_id}/token")
    def public_token(
        config_id: str,
        payload: EmbedTokenRequest,
        origin: str = Header(alias="Origin"),
    ) -> dict:
        try:
            return service.issue_token(
                config_id=config_id,
                request_origin=origin,
                **payload.model_dump(),
            )
        except EmbedRateLimitError as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        except Exception as exc:
            raise _translate_error(exc) from exc

    return router
