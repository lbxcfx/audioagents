from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .api import _translate_error
from .auth import require_user_id
from .builder import AgentSpec, BuilderService, RevisionConflictError


class AgentSpecSave(BaseModel):
    spec: AgentSpec
    expected_revision: int | None = Field(default=None, ge=1)


class PublishRequest(BaseModel):
    expected_revision: int = Field(ge=1)


def _builder_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RevisionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return _translate_error(exc)


def create_builder_router(service: BuilderService) -> APIRouter:
    router = APIRouter(prefix="/api/platform", tags=["cloud-parity-builder"])

    @router.post("/projects/{project_id}/agent-specs", status_code=201)
    def create_spec(
        project_id: str,
        payload: AgentSpecSave,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.save(
                project_id=project_id, actor_id=x_user_id, spec=payload.spec
            )
        except Exception as exc:
            raise _builder_error(exc) from exc

    @router.get("/projects/{project_id}/agent-specs")
    def list_specs(
        project_id: str, x_user_id: str = Depends(require_user_id)
    ) -> dict:
        try:
            return {"items": service.list(project_id=project_id, user_id=x_user_id)}
        except Exception as exc:
            raise _builder_error(exc) from exc

    @router.put("/projects/{project_id}/agent-specs/{spec_id}")
    def update_spec(
        project_id: str,
        spec_id: str,
        payload: AgentSpecSave,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.save(
                project_id=project_id,
                actor_id=x_user_id,
                spec=payload.spec,
                spec_id=spec_id,
                expected_revision=payload.expected_revision,
            )
        except Exception as exc:
            raise _builder_error(exc) from exc

    @router.get("/projects/{project_id}/agent-specs/{spec_id}")
    def get_spec(
        project_id: str,
        spec_id: str,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.get(project_id=project_id, user_id=x_user_id, spec_id=spec_id)
        except Exception as exc:
            raise _builder_error(exc) from exc

    @router.post("/projects/{project_id}/agent-specs/{spec_id}/publish")
    def publish_spec(
        project_id: str,
        spec_id: str,
        payload: PublishRequest,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.publish(
                project_id=project_id,
                actor_id=x_user_id,
                spec_id=spec_id,
                expected_revision=payload.expected_revision,
            )
        except Exception as exc:
            raise _builder_error(exc) from exc

    @router.get("/projects/{project_id}/agent-specs/{spec_id}/export")
    def export_spec(
        project_id: str,
        spec_id: str,
        x_user_id: str = Depends(require_user_id),
    ) -> Response:
        try:
            data = service.export_zip(
                project_id=project_id, user_id=x_user_id, spec_id=spec_id
            )
        except Exception as exc:
            raise _builder_error(exc) from exc
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="agent-{spec_id}.zip"'},
        )

    return router
