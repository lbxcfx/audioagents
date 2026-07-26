from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .api import _translate_error
from .auth import require_user_id
from .deployment import DeploymentDriverUnavailableError, DeploymentService


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class BuildCreate(BaseModel):
    source_ref: str = Field(min_length=1, max_length=1000)
    image_ref: str = Field(min_length=1, max_length=500)
    spec: dict[str, Any] = Field(default_factory=dict)


class DeploymentCreate(BaseModel):
    agent_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    desired_replicas: int = Field(default=1, ge=0, le=100)


class RolloutCreate(BaseModel):
    version_id: str = Field(min_length=1)


class SecretPut(BaseModel):
    value: str = Field(min_length=1, max_length=16_384)


def create_deployment_router(service: DeploymentService) -> APIRouter:
    router = APIRouter(prefix="/api/platform", tags=["cloud-parity-deployment"])

    @router.get("/deployment-capabilities")
    def deployment_capabilities(
        _x_user_id: str = Depends(require_user_id),
    ) -> dict:
        return service.capabilities()

    @router.post("/projects/{project_id}/agents", status_code=201)
    def create_agent(
        project_id: str,
        payload: AgentCreate,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.create_agent(
                project_id=project_id, actor_id=x_user_id, **payload.model_dump()
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/agents")
    def list_agents(
        project_id: str, x_user_id: str = Depends(require_user_id)
    ) -> dict:
        try:
            return {"items": service.list_agents(project_id=project_id, user_id=x_user_id)}
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/agents/{agent_id}/versions")
    def list_versions(
        project_id: str,
        agent_id: str,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return {"items": service.list_versions(
                project_id=project_id, user_id=x_user_id, agent_id=agent_id
            )}
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/agents/{agent_id}/builds", status_code=201)
    def build_agent(
        project_id: str,
        agent_id: str,
        payload: BuildCreate,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.build_version(
                project_id=project_id,
                actor_id=x_user_id,
                agent_id=agent_id,
                **payload.model_dump(),
            )
        except DeploymentDriverUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/deployments", status_code=201)
    def create_deployment(
        project_id: str,
        payload: DeploymentCreate,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.create_deployment(
                project_id=project_id, actor_id=x_user_id, **payload.model_dump()
            )
        except DeploymentDriverUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/deployments")
    def list_deployments(
        project_id: str, x_user_id: str = Depends(require_user_id)
    ) -> dict:
        try:
            return {"items": service.list_deployments(
                project_id=project_id, user_id=x_user_id
            )}
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/deployments/{deployment_id}")
    def get_deployment(
        project_id: str,
        deployment_id: str,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.get_deployment(project_id, x_user_id, deployment_id)
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/deployments/{deployment_id}/rollout")
    def rollout(
        project_id: str,
        deployment_id: str,
        payload: RolloutCreate,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.rollout(
                project_id=project_id,
                actor_id=x_user_id,
                deployment_id=deployment_id,
                version_id=payload.version_id,
            )
        except DeploymentDriverUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/deployments/{deployment_id}/rollback")
    def rollback(
        project_id: str,
        deployment_id: str,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.rollback(
                project_id=project_id,
                actor_id=x_user_id,
                deployment_id=deployment_id,
            )
        except DeploymentDriverUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.put("/projects/{project_id}/secrets/{name}")
    def put_secret(
        project_id: str,
        name: str,
        payload: SecretPut,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.put_secret(
                project_id=project_id, actor_id=x_user_id, name=name, value=payload.value
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/secrets")
    def list_secrets(
        project_id: str, x_user_id: str = Depends(require_user_id)
    ) -> dict:
        try:
            return {
                "items": service.list_secrets(project_id=project_id, user_id=x_user_id)
            }
        except Exception as exc:
            raise _translate_error(exc) from exc

    return router
