from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .api import _translate_error
from .auth import require_user_id
from .insights import InsightsService


class SessionCreate(BaseModel):
    room_name: str = Field(min_length=1, max_length=200)
    agent_name: str = Field(default="", max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, max_length=200)


class SessionClose(BaseModel):
    status: Literal["completed", "failed", "cancelled"] = "completed"


class EventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str | None = None


class UsageCreate(BaseModel):
    category: str = Field(min_length=1, max_length=80)
    unit: str = Field(min_length=1, max_length=40)
    quantity: float = Field(ge=0)
    provider: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=160)
    cost_usd: float = Field(default=0, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)


def create_insights_router(service: InsightsService) -> APIRouter:
    router = APIRouter(prefix="/api/platform", tags=["cloud-parity-insights"])

    @router.post("/projects/{project_id}/sessions", status_code=201)
    def create_session(
        project_id: str,
        payload: SessionCreate,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.create_session(
                project_id=project_id, actor_id=x_user_id, **payload.model_dump()
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/sessions")
    def list_sessions(
        project_id: str,
        x_user_id: str = Depends(require_user_id),
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        try:
            return {
                "items": service.list_sessions(
                    project_id=project_id,
                    user_id=x_user_id,
                    status=status,
                    limit=limit,
                )
            }
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/sessions/{session_id}")
    def get_timeline(
        project_id: str,
        session_id: str,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.timeline(
                project_id=project_id, user_id=x_user_id, session_id=session_id
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/sessions/{session_id}/events", status_code=201)
    def append_event(
        project_id: str,
        session_id: str,
        payload: EventCreate,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.append_event(
                project_id=project_id,
                actor_id=x_user_id,
                session_id=session_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/sessions/{session_id}/usage", status_code=201)
    def record_usage(
        project_id: str,
        session_id: str,
        payload: UsageCreate,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.record_usage(
                project_id=project_id,
                actor_id=x_user_id,
                session_id=session_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/sessions/{session_id}/close")
    def close_session(
        project_id: str,
        session_id: str,
        payload: SessionClose,
        x_user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.close_session(
                project_id=project_id,
                actor_id=x_user_id,
                session_id=session_id,
                status=payload.status,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    return router
