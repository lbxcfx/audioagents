from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any
import uuid

from .store import PlatformStore, ResourceNotFoundError, _row, _utc_now


def _parse_json(record: dict[str, Any], field: str, target: str) -> dict[str, Any]:
    record[target] = json.loads(record.pop(field) or "{}")
    return record


class InsightsService:
    """Tenant-safe session timeline and model usage service."""

    def __init__(self, store: PlatformStore):
        self.store = store

    def create_session(
        self,
        *,
        project_id: str,
        actor_id: str,
        room_name: str,
        agent_name: str = "",
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "session.write")
        if not room_name.strip():
            raise ValueError("room_name is required")
        project = self.store.get_project(project_id, actor_id)
        value_id = session_id or str(uuid.uuid4())
        now = _utc_now()
        retention = datetime.now(timezone.utc) + timedelta(days=int(project["retention_days"]))
        retention_until = retention.isoformat().replace("+00:00", "Z")
        with self.store.transaction() as conn:
            inserted = conn.execute(
                """
                INSERT INTO agent_sessions (
                    id, project_id, room_name, agent_name, status, metadata_json,
                    started_at, retention_until, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    value_id,
                    project_id,
                    room_name.strip(),
                    agent_name.strip(),
                    json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
                    now,
                    retention_until,
                    now,
                    now,
                ),
            )
            if int(inserted.rowcount or 0) == 1:
                self.store._append_audit(
                    conn,
                    project_id=project_id,
                    actor_id=actor_id,
                    action="session.create",
                    resource_type="session",
                    resource_id=value_id,
                    payload={"room_name": room_name.strip(), "agent_name": agent_name.strip()},
                )
            else:
                existing = conn.execute(
                    "SELECT project_id, room_name FROM agent_sessions WHERE id = ?",
                    (value_id,),
                ).fetchone()
                if (
                    existing is None
                    or str(existing["project_id"]) != project_id
                    or str(existing["room_name"]) != room_name.strip()
                ):
                    raise ValueError("session_id is already assigned to another session")
        return self.get_session(project_id=project_id, user_id=actor_id, session_id=value_id)

    def get_session(self, *, project_id: str, user_id: str, session_id: str) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "session.read")
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE id = ? AND project_id = ?",
                (session_id, project_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("session not found")
        return _parse_json(_row(row) or {}, "metadata_json", "metadata")

    def list_sessions(
        self,
        *,
        project_id: str,
        user_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "session.read")
        safe_limit = max(1, min(limit, 500))
        query = "SELECT * FROM agent_sessions WHERE project_id = ?"
        params: list[Any] = [project_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY started_at DESC, id DESC LIMIT ?"
        params.append(safe_limit)
        with self.store.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_parse_json(_row(item) or {}, "metadata_json", "metadata") for item in rows]

    def close_session(
        self,
        *,
        project_id: str,
        actor_id: str,
        session_id: str,
        status: str = "completed",
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "session.write")
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("unsupported terminal session status")
        now = _utc_now()
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_sessions
                SET status = ?, ended_at = COALESCE(ended_at, ?), updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (status, now, now, session_id, project_id),
            )
            if cursor.rowcount == 0:
                raise ResourceNotFoundError("session not found")
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="session.close",
                resource_type="session",
                resource_id=session_id,
                payload={"status": status},
            )
        return self.get_session(project_id=project_id, user_id=actor_id, session_id=session_id)

    def append_event(
        self,
        *,
        project_id: str,
        actor_id: str,
        session_id: str,
        event_type: str,
        source: str,
        payload: dict[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "session.write")
        if not event_type.strip() or not source.strip():
            raise ValueError("event_type and source are required")
        event_id = str(uuid.uuid4())
        event_time = occurred_at or _utc_now()
        lock_suffix = " FOR UPDATE" if self.store.backend == "postgresql" else ""
        with self.store.transaction() as conn:
            exists = conn.execute(
                f"SELECT 1 FROM agent_sessions WHERE id = ? AND project_id = ?{lock_suffix}",
                (session_id, project_id),
            ).fetchone()
            if exists is None:
                raise ResourceNotFoundError("session not found")
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM session_events WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["value"]
            )
            conn.execute(
                """
                INSERT INTO session_events (
                    id, project_id, session_id, sequence, event_type, source,
                    payload_json, occurred_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    project_id,
                    session_id,
                    sequence,
                    event_type.strip(),
                    source.strip(),
                    json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                    event_time,
                    _utc_now(),
                ),
            )
            row = conn.execute("SELECT * FROM session_events WHERE id = ?", (event_id,)).fetchone()
        return _parse_json(_row(row) or {}, "payload_json", "payload")

    def record_usage(
        self,
        *,
        project_id: str,
        actor_id: str,
        session_id: str,
        category: str,
        unit: str,
        quantity: float,
        provider: str = "",
        model: str = "",
        cost_usd: float = 0,
        latency_ms: float | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "session.write")
        if quantity < 0 or cost_usd < 0:
            raise ValueError("usage quantity and cost cannot be negative")
        usage_id = str(uuid.uuid4())
        created_at = _utc_now()
        with self.store.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM agent_sessions WHERE id = ? AND project_id = ?",
                (session_id, project_id),
            ).fetchone()
            if exists is None:
                raise ResourceNotFoundError("session not found")
            conn.execute(
                """
                INSERT INTO usage_records (
                    id, project_id, session_id, category, provider, model,
                    quantity, unit, cost_usd, latency_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage_id,
                    project_id,
                    session_id,
                    category,
                    provider,
                    model,
                    quantity,
                    unit,
                    cost_usd,
                    latency_ms,
                    created_at,
                ),
            )
            row = conn.execute("SELECT * FROM usage_records WHERE id = ?", (usage_id,)).fetchone()
        return _row(row) or {}

    def timeline(self, *, project_id: str, user_id: str, session_id: str) -> dict[str, Any]:
        session = self.get_session(project_id=project_id, user_id=user_id, session_id=session_id)
        with self.store.connect() as conn:
            event_rows = conn.execute(
                """
                SELECT * FROM session_events
                WHERE project_id = ? AND session_id = ?
                ORDER BY sequence
                """,
                (project_id, session_id),
            ).fetchall()
            usage_rows = conn.execute(
                """
                SELECT * FROM usage_records
                WHERE project_id = ? AND session_id = ?
                ORDER BY created_at, id
                """,
                (project_id, session_id),
            ).fetchall()
        events = [
            _parse_json(_row(item) or {}, "payload_json", "payload") for item in event_rows
        ]
        usage = [_row(item) or {} for item in usage_rows]
        return {
            "session": session,
            "events": events,
            "usage": usage,
            "summary": {
                "event_count": len(events),
                "usage_count": len(usage),
                "cost_usd": round(sum(float(item["cost_usd"]) for item in usage), 8),
            },
        }
