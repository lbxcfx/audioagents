from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from .analytics import AnalyticsService
from .api import _translate_error
from .auth import require_user_id


def create_analytics_router(service: AnalyticsService) -> APIRouter:
    router = APIRouter(prefix="/api/platform", tags=["cloud-parity-analytics"])

    @router.get("/projects/{project_id}/analytics/summary")
    def summary(
        project_id: str,
        x_user_id: str = Depends(require_user_id),
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        try:
            return service.summary(
                project_id=project_id, user_id=x_user_id, start=start, end=end
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/analytics/sessions")
    def sessions(
        project_id: str,
        x_user_id: str = Depends(require_user_id),
        start: str | None = None,
        end: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = None,
    ) -> dict:
        try:
            return service.list_sessions(
                project_id=project_id,
                user_id=x_user_id,
                start=start,
                end=end,
                limit=limit,
                cursor=cursor,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/projects/{project_id}/analytics/export.csv")
    def export(
        project_id: str,
        x_user_id: str = Depends(require_user_id),
        start: str | None = None,
        end: str | None = None,
    ) -> StreamingResponse:
        try:
            rows = service.export_csv(
                project_id=project_id, user_id=x_user_id, start=start, end=end
            )
        except Exception as exc:
            raise _translate_error(exc) from exc
        return StreamingResponse(
            rows,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="analytics.csv"'},
        )

    return router
