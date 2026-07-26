from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from typing import Any, Callable
import uuid

from .insights import InsightsService, _parse_json
from .store import PlatformStore, ResourceNotFoundError, _row, _utc_now


ObserverTokenIssuer = Callable[[str, str, int], str]


def issue_livekit_observer_token(room_name: str, identity: str, ttl_seconds: int) -> str:
    """Issue a subscribe-only, hidden LiveKit room token from environment credentials."""

    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required")
    from livekit import api

    return (
        api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name("Cloud-Parity observer")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=False,
                can_subscribe=True,
                can_publish_data=False,
                hidden=True,
            )
        )
        .with_ttl(timedelta(seconds=ttl_seconds))
        .to_jwt()
    )


class ConsoleService:
    def __init__(
        self,
        store: PlatformStore,
        insights: InsightsService,
        token_issuer: ObserverTokenIssuer = issue_livekit_observer_token,
    ):
        self.store = store
        self.insights = insights
        self.token_issuer = token_issuer

    def events_after(
        self,
        *,
        project_id: str,
        user_id: str,
        session_id: str,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        self.insights.get_session(
            project_id=project_id, user_id=user_id, session_id=session_id
        )
        safe_limit = max(1, min(limit, 500))
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM session_events
                WHERE project_id = ? AND session_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (project_id, session_id, max(0, after_sequence), safe_limit),
            ).fetchall()
        events = [
            _parse_json(_row(item) or {}, "payload_json", "payload") for item in rows
        ]
        return {
            "items": events,
            "cursor": events[-1]["sequence"] if events else max(0, after_sequence),
        }

    def queue_command(
        self,
        *,
        project_id: str,
        actor_id: str,
        session_id: str,
        command_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "console.control")
        self.insights.get_session(
            project_id=project_id, user_id=actor_id, session_id=session_id
        )
        if command_type not in {"rpc", "dtmf"}:
            raise ValueError("command_type must be rpc or dtmf")
        if command_type == "dtmf":
            digits = str(payload.get("digits", ""))
            if not digits or any(char not in "0123456789*#ABCD" for char in digits):
                raise ValueError("invalid DTMF digits")
        if command_type == "rpc" and not str(payload.get("method", "")).strip():
            raise ValueError("RPC method is required")
        command_id = str(uuid.uuid4())
        created_at = _utc_now()
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO console_commands (
                    id, project_id, session_id, actor_id, command_type,
                    payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    command_id,
                    project_id,
                    session_id,
                    actor_id,
                    command_type,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                ),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action=f"console.{command_type}.queue",
                resource_type="console_command",
                resource_id=command_id,
                payload={"session_id": session_id},
            )
            row = conn.execute(
                "SELECT * FROM console_commands WHERE id = ?", (command_id,)
            ).fetchone()
        return self._command_record(row)

    def list_commands(
        self,
        *,
        project_id: str,
        user_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "session.read")
        self.insights.get_session(
            project_id=project_id, user_id=user_id, session_id=session_id
        )
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM console_commands
                WHERE project_id = ? AND session_id = ?
                ORDER BY created_at, id
                """,
                (project_id, session_id),
            ).fetchall()
        return [self._command_record(item) for item in rows]

    def claim_commands(
        self,
        *,
        project_id: str,
        actor_id: str,
        session_id: str,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, actor_id, "session.write")
        self.insights.get_session(
            project_id=project_id, user_id=actor_id, session_id=session_id
        )
        normalized_worker = worker_id.strip()
        if not normalized_worker or len(normalized_worker) > 200:
            raise ValueError("worker_id is required and must not exceed 200 characters")
        safe_limit = max(1, min(int(limit), 50))
        safe_lease = max(10, min(int(lease_seconds), 300))
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat().replace("+00:00", "Z")
        lease_expires_at = (now + timedelta(seconds=safe_lease)).isoformat().replace(
            "+00:00", "Z"
        )
        lock_suffix = " FOR UPDATE SKIP LOCKED" if self.store.backend == "postgresql" else ""
        with self.store.transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT id FROM console_commands
                WHERE project_id = ? AND session_id = ?
                  AND (status = 'queued' OR (status = 'executing' AND lease_expires_at <= ?))
                ORDER BY created_at, id LIMIT ?{lock_suffix}
                """,
                (project_id, session_id, timestamp, safe_limit),
            ).fetchall()
            identifiers = [str(row["id"]) for row in rows]
            claimed: list[dict[str, Any]] = []
            for command_id in identifiers:
                updated = conn.execute(
                    """
                    UPDATE console_commands SET status = 'executing', claimed_by = ?,
                        lease_expires_at = ?, completed_at = NULL
                    WHERE id = ? AND project_id = ? AND session_id = ?
                      AND (status = 'queued' OR (status = 'executing' AND lease_expires_at <= ?))
                    """,
                    (
                        normalized_worker,
                        lease_expires_at,
                        command_id,
                        project_id,
                        session_id,
                        timestamp,
                    ),
                )
                if int(updated.rowcount or 0) == 1:
                    claimed.append(
                        self._command_record(
                            conn.execute(
                                "SELECT * FROM console_commands WHERE id = ?", (command_id,)
                            ).fetchone()
                        )
                    )
        return claimed

    def complete_command(
        self,
        *,
        project_id: str,
        actor_id: str,
        session_id: str,
        command_id: str,
        worker_id: str,
        status: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "session.write")
        if status not in {"completed", "failed"}:
            raise ValueError("command status must be completed or failed")
        encoded_result = json.dumps(
            result, ensure_ascii=False, separators=(",", ":")
        )
        if len(encoded_result.encode("utf-8")) > 32768:
            raise ValueError("command result exceeds 32 KiB")
        now = _utc_now()
        with self.store.transaction() as conn:
            updated = conn.execute(
                """
                UPDATE console_commands SET status = ?, result_json = ?,
                    completed_at = ?, lease_expires_at = NULL
                WHERE id = ? AND project_id = ? AND session_id = ?
                  AND status = 'executing' AND claimed_by = ?
                """,
                (
                    status,
                    encoded_result,
                    now,
                    command_id,
                    project_id,
                    session_id,
                    worker_id.strip(),
                ),
            )
            if int(updated.rowcount or 0) != 1:
                raise ResourceNotFoundError("claimed console command not found")
            row = conn.execute(
                "SELECT * FROM console_commands WHERE id = ?", (command_id,)
            ).fetchone()
        self.insights.append_event(
            project_id=project_id,
            actor_id=actor_id,
            session_id=session_id,
            event_type=f"console.command.{status}",
            source="agent",
            payload={"command_id": command_id, "result": result},
        )
        return self._command_record(row)

    def observer_token(
        self,
        *,
        project_id: str,
        actor_id: str,
        session_id: str,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "console.observe")
        if not 30 <= ttl_seconds <= 900:
            raise ValueError("observer token TTL must be between 30 and 900 seconds")
        session = self.insights.get_session(
            project_id=project_id, user_id=actor_id, session_id=session_id
        )
        identity = f"observer:{actor_id}:{uuid.uuid4().hex[:12]}"
        token = self.token_issuer(session["room_name"], identity, ttl_seconds)
        with self.store.transaction() as conn:
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="console.observer_token.issue",
                resource_type="session",
                resource_id=session_id,
                payload={"identity": identity, "ttl_seconds": ttl_seconds},
            )
        return {
            "token": token,
            "room_name": session["room_name"],
            "identity": identity,
            "ttl_seconds": ttl_seconds,
            "permissions": {"subscribe": True, "publish": False, "hidden": True},
        }

    @staticmethod
    def _command_record(row: Any) -> dict[str, Any]:
        if row is None:
            raise ResourceNotFoundError("console command not found")
        record = _row(row) or {}
        record["payload"] = json.loads(record.pop("payload_json") or "{}")
        raw_result = record.pop("result_json")
        record["result"] = json.loads(raw_result) if raw_result else None
        return record
