from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .api import _translate_error
from .auth import require_user_id
from .runtime import DockerRuntimeExecutor


def create_runtime_router(runtime: DockerRuntimeExecutor) -> APIRouter:
    router = APIRouter(prefix="/api/platform", tags=["cloud-parity-runtime"])

    @router.get("/projects/{project_id}/deployments/{deployment_id}/instances")
    def instances(
        project_id: str,
        deployment_id: str,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return {"items": runtime.list_instances(
                project_id=project_id, user_id=x_user_id, deployment_id=deployment_id
            )}
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/deployments/{deployment_id}/logs/collect")
    def collect(
        project_id: str,
        deployment_id: str,
        x_user_id: str = Depends(require_user_id),
        tail: int = Query(default=500, ge=1, le=5000),
    ) -> dict:
        try:
            count = runtime.collect_logs(
                project_id=project_id,
                user_id=x_user_id,
                deployment_id=deployment_id,
                tail=tail,
            )
            return {"collected": count}
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/deployments/{deployment_id}/logs")
    def logs(
        project_id: str,
        deployment_id: str,
        x_user_id: str = Depends(require_user_id),
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> dict:
        try:
            return runtime.logs_after(
                project_id=project_id,
                user_id=x_user_id,
                deployment_id=deployment_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    return router
