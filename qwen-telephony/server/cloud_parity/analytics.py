from __future__ import annotations

import base64
import csv
from datetime import datetime, timezone
from io import StringIO
import json
from typing import Any, Iterator

from .store import PlatformStore, _row


def _utc_bound(value: str | None, *, end: bool = False) -> str:
    if not value:
        return "9999-12-31T23:59:59Z" if end else "0001-01-01T00:00:00Z"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _encode_cursor(started_at: str, row_id: str) -> str:
    raw = json.dumps([started_at, row_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[str, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        started_at, row_id = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return str(started_at), str(row_id)
    except Exception as exc:
        raise ValueError("invalid analytics cursor") from exc


class AnalyticsService:
    def __init__(self, store: PlatformStore):
        self.store = store

    def summary(
        self,
        *,
        project_id: str,
        user_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "analytics.read")
        start_at, end_at = _utc_bound(start), _utc_bound(end, end=True)
        duration_expression = (
            "EXTRACT(EPOCH FROM (ended_at::timestamptz - started_at::timestamptz))"
            if self.store.backend == "postgresql"
            else "(julianday(ended_at) - julianday(started_at)) * 86400"
        )
        with self.store.connect() as conn:
            session = conn.execute(
                f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                       AVG(CASE WHEN ended_at IS NOT NULL
                           THEN {duration_expression} END
                       ) AS avg_duration_seconds
                FROM agent_sessions
                WHERE project_id = ? AND started_at >= ? AND started_at < ?
                """,
                (project_id, start_at, end_at),
            ).fetchone()
            usage_rows = conn.execute(
                """
                SELECT u.category, u.provider, u.model, u.unit,
                       SUM(u.quantity) AS quantity, SUM(u.cost_usd) AS cost_usd,
                       AVG(u.latency_ms) AS avg_latency_ms, COUNT(*) AS request_count
                FROM usage_records u
                JOIN agent_sessions s ON s.id = u.session_id AND s.project_id = u.project_id
                WHERE u.project_id = ? AND s.started_at >= ? AND s.started_at < ?
                GROUP BY u.category, u.provider, u.model, u.unit
                ORDER BY u.category, u.provider, u.model, u.unit
                """,
                (project_id, start_at, end_at),
            ).fetchall()
            attempts = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                       AVG(latency_ms) AS avg_latency_ms
                FROM inference_attempts
                WHERE project_id = ? AND created_at >= ? AND created_at < ?
                """,
                (project_id, start_at, end_at),
            ).fetchone()
            event_rows = conn.execute(
                """
                SELECT e.event_type, COUNT(*) AS count
                FROM session_events e
                JOIN agent_sessions s ON s.id = e.session_id AND s.project_id = e.project_id
                WHERE e.project_id = ? AND s.started_at >= ? AND s.started_at < ?
                GROUP BY e.event_type ORDER BY e.event_type
                """,
                (project_id, start_at, end_at),
            ).fetchall()
        total_attempts = int(attempts["total"] or 0)
        succeeded = int(attempts["succeeded"] or 0)
        return {
            "range": {"start": start_at, "end": end_at},
            "sessions": {
                "total": int(session["total"] or 0),
                "active": int(session["active"] or 0),
                "completed": int(session["completed"] or 0),
                "failed": int(session["failed"] or 0),
                "avg_duration_seconds": round(float(session["avg_duration_seconds"] or 0), 3),
            },
            "usage": [
                {
                    **(_row(item) or {}),
                    "quantity": float(item["quantity"] or 0),
                    "cost_usd": round(float(item["cost_usd"] or 0), 8),
                    "avg_latency_ms": round(float(item["avg_latency_ms"] or 0), 3),
                }
                for item in usage_rows
            ],
            "inference": {
                "attempts": total_attempts,
                "succeeded": succeeded,
                "success_rate": round(succeeded / total_attempts, 6) if total_attempts else 0,
                "avg_latency_ms": round(float(attempts["avg_latency_ms"] or 0), 3),
            },
            "events": {item["event_type"]: int(item["count"]) for item in event_rows},
        }

    def list_sessions(
        self,
        *,
        project_id: str,
        user_id: str,
        start: str | None = None,
        end: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "analytics.read")
        start_at, end_at = _utc_bound(start), _utc_bound(end, end=True)
        safe_limit = max(1, min(limit, 200))
        params: list[Any] = [project_id, start_at, end_at]
        cursor_clause = ""
        if cursor:
            cursor_started, cursor_id = _decode_cursor(cursor)
            cursor_clause = " AND (s.started_at < ? OR (s.started_at = ? AND s.id < ?))"
            params.extend([cursor_started, cursor_started, cursor_id])
        params.append(safe_limit + 1)
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT s.*,
                       COALESCE((SELECT SUM(cost_usd) FROM usage_records u
                                 WHERE u.session_id = s.id), 0) AS cost_usd,
                       COALESCE((SELECT COUNT(*) FROM session_events e
                                 WHERE e.session_id = s.id), 0) AS event_count
                FROM agent_sessions s
                WHERE s.project_id = ? AND s.started_at >= ? AND s.started_at < ?
                {cursor_clause}
                ORDER BY s.started_at DESC, s.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        has_more = len(rows) > safe_limit
        selected = rows[:safe_limit]
        items = []
        for row in selected:
            record = _row(row) or {}
            record["metadata"] = json.loads(record.pop("metadata_json") or "{}")
            record["cost_usd"] = round(float(record["cost_usd"] or 0), 8)
            items.append(record)
        next_cursor = None
        if has_more and items:
            next_cursor = _encode_cursor(items[-1]["started_at"], items[-1]["id"])
        return {"items": items, "next_cursor": next_cursor}

    def export_csv(
        self,
        *,
        project_id: str,
        user_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> Iterator[str]:
        self.store.require_permission(project_id, user_id, "analytics.export")
        start_at, end_at = _utc_bound(start), _utc_bound(end, end=True)
        with self.store.connect() as conn:
            cursor = conn.execute(
                """
                SELECT s.id, s.room_name, s.agent_name, s.status, s.started_at, s.ended_at,
                       COALESCE(SUM(u.cost_usd), 0) AS cost_usd,
                       COUNT(u.id) AS usage_records
                FROM agent_sessions s
                LEFT JOIN usage_records u ON u.session_id = s.id AND u.project_id = s.project_id
                WHERE s.project_id = ? AND s.started_at >= ? AND s.started_at < ?
                GROUP BY s.id
                ORDER BY s.started_at DESC, s.id DESC
                """,
                (project_id, start_at, end_at),
            )
            headers = [
                "id", "room_name", "agent_name", "status", "started_at", "ended_at",
                "cost_usd", "usage_records",
            ]
            buffer = StringIO()
            writer = csv.writer(buffer)
            writer.writerow(headers)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for row in rows:
                    writer.writerow([row[key] for key in headers])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
