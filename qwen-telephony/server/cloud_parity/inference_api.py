from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .api import _translate_error
from .auth import require_user_id
from .inference import InferenceGateway


class RoutePut(BaseModel):
    descriptor: str = Field(min_length=1, max_length=200)
    modality: Literal["stt", "llm", "tts"]
    provider: str = Field(min_length=1, max_length=80)
    provider_model: str = Field(min_length=1, max_length=200)
    priority: int = Field(default=100, ge=0, le=10_000)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class InferenceRequest(BaseModel):
    descriptor: str = Field(min_length=1, max_length=200)
    modality: Literal["stt", "llm", "tts"]
    input: dict[str, Any]
    parameters: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None


def create_inference_router(gateway: InferenceGateway) -> APIRouter:
    router = APIRouter(prefix="/api/platform", tags=["cloud-parity-inference"])

    @router.put("/projects/{project_id}/inference/routes")
    def put_route(
        project_id: str,
        payload: RoutePut,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return gateway.put_route(
                project_id=project_id, actor_id=x_user_id, **payload.model_dump()
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/inference/routes")
    def list_routes(
        project_id: str, x_user_id: str = Depends(require_user_id)
    ) -> dict:
        try:
            return {"items": gateway.list_routes(project_id=project_id, user_id=x_user_id)}
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/inference")
    async def invoke(
        project_id: str,
        payload: InferenceRequest,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return await gateway.invoke(
                project_id=project_id,
                actor_id=x_user_id,
                descriptor=payload.descriptor,
                modality=payload.modality,
                input_data=payload.input,
                parameters=payload.parameters,
                session_id=payload.session_id,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    return router
