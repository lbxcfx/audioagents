from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .api import _translate_error
from .auth import require_user_id
from .console import ConsoleService


class ConsoleCommandCreate(BaseModel):
    command_type: Literal["rpc", "dtmf"]
    payload: dict[str, Any] = Field(default_factory=dict)


class ObserverTokenRequest(BaseModel):
    ttl_seconds: int = Field(default=300, ge=30, le=900)


class ConsoleCommandClaim(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)
    lease_seconds: int = Field(default=30, ge=10, le=300)


class ConsoleCommandComplete(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    status: Literal["completed", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)


def create_console_router(service: ConsoleService) -> APIRouter:
    router = APIRouter(prefix="/api/platform", tags=["cloud-parity-console"])

    def require_worker_user_id(
        project_id: str,
        user_id: str = Depends(require_user_id),
    ) -> str:
        try:
            service.store.require_role(project_id, user_id, {"worker"})
        except Exception as exc:
            raise _translate_error(exc) from exc
        return user_id

    @router.get("/projects/{project_id}/sessions/{session_id}/console/events")
    def console_events(
        project_id: str,
        session_id: str,
        x_user_id: str = Depends(require_user_id),
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict:
        try:
            return service.events_after(
                project_id=project_id,
                user_id=x_user_id,
                session_id=session_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/sessions/{session_id}/console/commands", status_code=202)
    def queue_command(
        project_id: str,
        session_id: str,
        payload: ConsoleCommandCreate,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.queue_command(
                project_id=project_id,
                actor_id=x_user_id,
                session_id=session_id,
                command_type=payload.command_type,
                payload=payload.payload,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/sessions/{session_id}/console/commands")
    def list_commands(
        project_id: str,
        session_id: str,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return {
                "items": service.list_commands(
                    project_id=project_id,
                    user_id=x_user_id,
                    session_id=session_id,
                )
            }
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/sessions/{session_id}/console/commands/claim")
    def claim_commands(
        project_id: str,
        session_id: str,
        payload: ConsoleCommandClaim,
        x_user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return {
                "items": service.claim_commands(
                    project_id=project_id,
                    actor_id=x_user_id,
                    session_id=session_id,
                    **payload.model_dump(),
                )
            }
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post(
        "/projects/{project_id}/sessions/{session_id}/console/commands/{command_id}/complete"
    )
    def complete_command(
        project_id: str,
        session_id: str,
        command_id: str,
        payload: ConsoleCommandComplete,
        x_user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return service.complete_command(
                project_id=project_id,
                actor_id=x_user_id,
                session_id=session_id,
                command_id=command_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/sessions/{session_id}/console/observer-token")
    def observer_token(
        project_id: str,
        session_id: str,
        payload: ObserverTokenRequest,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.observer_token(
                project_id=project_id,
                actor_id=x_user_id,
                session_id=session_id,
                ttl_seconds=payload.ttl_seconds,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    return router
