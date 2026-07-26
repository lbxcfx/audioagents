from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .database import is_integrity_error
from .auth import optional_user_id, require_user_id
from .store import AccessDeniedError, MIGRATIONS, PlatformStore, ResourceNotFoundError


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=80)
    owner_id: str | None = Field(default=None, min_length=1, max_length=120)
    retention_days: int = Field(default=30, ge=1, le=3650)


class MembershipUpsert(BaseModel):
    user_id: str = Field(min_length=1, max_length=120)
    role: Literal["owner", "admin", "member", "viewer", "worker"]


class TokenRevokeRequest(BaseModel):
    reason: str = Field(default="user_logout", min_length=1, max_length=500)


def _jwt_expiry(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        expires = int(claims.get("exp") or 0)
        if expires <= 0:
            return None
        return datetime.fromtimestamp(expires, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (ValueError, IndexError, KeyError, json.JSONDecodeError):
        return None


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ResourceNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AccessDeniedError):
        return HTTPException(status_code=403, detail=str(exc))
    if is_integrity_error(exc):
        return HTTPException(status_code=409, detail="resource already exists")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="platform operation failed")


def create_platform_router(store: PlatformStore) -> APIRouter:
    router = APIRouter(prefix="/api/platform", tags=["cloud-parity-platform"])

    @router.get("/health")
    def platform_health() -> dict:
        try:
            database = store.healthcheck()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"status": "unavailable", "component": "database"},
            ) from exc
        return {"status": "ok", "module": "platform-foundation", **database}

    @router.get("/health/live", include_in_schema=False)
    def platform_liveness() -> dict:
        return {"status": "ok"}

    @router.get("/health/ready", include_in_schema=False)
    def platform_readiness() -> dict:
        try:
            database = store.healthcheck()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"status": "unavailable", "component": "database"},
            ) from exc
        expected_schema = MIGRATIONS[-1][0]
        if int(database["schema_version"]) != expected_schema:
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "not_ready",
                    "component": "schema",
                    "expected": expected_schema,
                    "actual": database["schema_version"],
                },
            )
        return {**database, "status": "ready"}

    @router.post("/auth/revoke")
    def revoke_current_access_token(
        payload: TokenRevokeRequest,
        authorization: str = Header(default="", alias="Authorization"),
        user_id: str = Depends(require_user_id),
    ) -> dict:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=422, detail="Bearer access token is required")
        token = authorization[len("Bearer ") :].strip()
        if not token:
            raise HTTPException(status_code=422, detail="Bearer access token is required")
        return store.revoke_access_token(
            token=token,
            subject=user_id,
            revoked_by=user_id,
            reason=payload.reason,
            expires_at=_jwt_expiry(token),
        )

    @router.post("/projects", status_code=201)
    def create_project(
        payload: ProjectCreate,
        authenticated_user_id: str | None = Depends(optional_user_id),
    ) -> dict:
        try:
            owner_id = authenticated_user_id or payload.owner_id
            if not owner_id:
                raise ValueError("owner_id is required in development mode")
            return store.create_project(
                name=payload.name,
                slug=payload.slug,
                owner_id=owner_id,
                retention_days=payload.retention_days,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects")
    def list_projects(user_id: str = Depends(require_user_id)) -> dict:
        return {"items": store.list_projects(user_id)}

    @router.get("/projects/{project_id}")
    def get_project(project_id: str, user_id: str = Depends(require_user_id)) -> dict:
        try:
            return store.get_project(project_id, user_id)
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.put("/projects/{project_id}/members/{user_id}")
    def put_membership(
        project_id: str,
        user_id: str,
        payload: MembershipUpsert,
        actor_id: str = Depends(require_user_id),
    ) -> dict:
        if payload.user_id != user_id:
            raise HTTPException(status_code=422, detail="path and payload user_id must match")
        try:
            return store.add_membership(
                project_id=project_id,
                actor_id=actor_id,
                user_id=user_id,
                role=payload.role,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/members")
    def list_memberships(
        project_id: str,
        actor_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return {
                "items": store.list_memberships(
                    project_id=project_id,
                    actor_id=actor_id,
                )
            }
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.delete("/projects/{project_id}/members/{user_id}")
    def delete_membership(
        project_id: str,
        user_id: str,
        actor_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return store.remove_membership(
                project_id=project_id,
                actor_id=actor_id,
                user_id=user_id,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/audit-logs")
    def list_audit_logs(
        project_id: str,
        user_id: str = Depends(require_user_id),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        try:
            return {
                "items": store.list_audit_logs(
                    project_id=project_id,
                    user_id=user_id,
                    limit=limit,
                )
            }
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/projects/{project_id}/maintenance/retention/purge")
    def purge_project_retention(
        project_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return store.purge_expired_project_data(
                project_id=project_id,
                actor_id=user_id,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    return router
