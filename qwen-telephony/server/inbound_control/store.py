from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping
import uuid

from server.cloud_parity.store import (
    AccessDeniedError,
    PlatformStore,
    ResourceNotFoundError,
)


INBOUND_SCHEMA_VERSION = 2
INBOUND_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_agents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    draft_revision INTEGER NOT NULL DEFAULT 1,
    draft_config_json TEXT NOT NULL DEFAULT '{}',
    active_version_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS inbound_agent_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(agent_id) REFERENCES inbound_agents(id) ON DELETE CASCADE,
    UNIQUE(agent_id, revision)
);

CREATE TABLE IF NOT EXISTS inbound_agent_bindings (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_version_id TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    destination TEXT NOT NULL,
    trunk_id TEXT NOT NULL DEFAULT '',
    dispatch_rule_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(agent_id) REFERENCES inbound_agents(id) ON DELETE CASCADE,
    FOREIGN KEY(agent_version_id) REFERENCES inbound_agent_versions(id),
    UNIQUE(entry_type, destination)
);

CREATE TABLE IF NOT EXISTS inbound_agent_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_version_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    room_name TEXT NOT NULL UNIQUE,
    provider_call_id TEXT NOT NULL DEFAULT '',
    caller_hash TEXT NOT NULL DEFAULT '',
    caller_last4 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    termination_reason TEXT NOT NULL DEFAULT '',
    retention_until TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(agent_id) REFERENCES inbound_agents(id) ON DELETE CASCADE,
    FOREIGN KEY(agent_version_id) REFERENCES inbound_agent_versions(id),
    FOREIGN KEY(binding_id) REFERENCES inbound_agent_bindings(id),
    UNIQUE(provider_call_id)
);

CREATE TABLE IF NOT EXISTS public_demo_usage (
    subject_hash TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    total_seconds INTEGER NOT NULL DEFAULT 0,
    blocked_until TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(subject_hash, usage_date)
);

CREATE TABLE IF NOT EXISTS inbound_metadata_nonces (
    nonce TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    consumed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inbound_agents_project_updated
    ON inbound_agents(project_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_bindings_project_agent
    ON inbound_agent_bindings(project_id, agent_id, status);
CREATE INDEX IF NOT EXISTS idx_inbound_sessions_project_started
    ON inbound_agent_sessions(project_id, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_sessions_active
    ON inbound_agent_sessions(project_id, status, started_at);
"""

INBOUND_SCHEMA_V2 = """
ALTER TABLE inbound_agent_sessions ADD COLUMN reservation_expires_at TEXT;
CREATE INDEX IF NOT EXISTS idx_inbound_sessions_expiry
    ON inbound_agent_sessions(status, reservation_expires_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def row_dict(row: Mapping[str, Any] | Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class PublicDemoQuotaError(RuntimeError):
    pass


class InboundAgentStore:
    def __init__(self, platform: PlatformStore, *, public_project_id: str = ""):
        self.platform = platform
        self.public_project_id = public_project_id.strip()

    def _require_public_admin(self, project_id: str, actor_id: str, kind: str) -> None:
        if kind != "public_demo":
            return
        if not self.public_project_id or project_id != self.public_project_id:
            raise AccessDeniedError("public demo agents belong to the configured platform project")
        self.platform.require_role(project_id, actor_id, {"owner", "admin"})

    def migrate(self) -> None:
        with self.platform.transaction() as conn:
            self.platform._database.acquire_migration_lock(conn)
            conn.executescript(INBOUND_SCHEMA)
            conn.execute(
                "INSERT INTO inbound_schema_migrations (version, applied_at) VALUES (?, ?) "
                "ON CONFLICT(version) DO NOTHING",
                (1, utc_now()),
            )
            applied = conn.execute(
                "SELECT 1 FROM inbound_schema_migrations WHERE version = 2"
            ).fetchone()
            if applied is None:
                conn.executescript(INBOUND_SCHEMA_V2)
                conn.execute(
                    "INSERT INTO inbound_schema_migrations (version, applied_at) VALUES (2, ?)",
                    (utc_now(),),
                )

    def healthcheck(self) -> dict[str, Any]:
        with self.platform.connect() as conn:
            row = conn.execute("SELECT MAX(version) AS version FROM inbound_schema_migrations").fetchone()
        return {"status": "ok", "schema_version": int(row["version"] or 0)}

    def _agent(self, conn: Any, project_id: str, agent_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM inbound_agents WHERE id = ? AND project_id = ?",
            (agent_id, project_id),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("inbound agent not found")
        item = row_dict(row) or {}
        item["draft_config"] = json.loads(item.pop("draft_config_json"))
        return item

    def create_agent(
        self,
        *,
        project_id: str,
        actor_id: str,
        name: str,
        description: str,
        kind: str,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "agent.write")
        if kind not in {"enterprise", "public_demo"}:
            raise ValueError("invalid inbound agent kind")
        self._require_public_admin(project_id, actor_id, kind)
        if config.get("tools") or config.get("knowledge_sources"):
            raise ValueError("tools and knowledge are disabled until the isolated runtime is enabled")
        if config.get("recording_mode", "off") != "off":
            raise ValueError("inbound recording is disabled until disclosure and retention are configured")
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("agent name is required")
        now = utc_now()
        agent_id = str(uuid.uuid4())
        with self.platform.transaction() as conn:
            conn.execute(
                """
                INSERT INTO inbound_agents (
                    id, project_id, kind, name, description, draft_config_json,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (agent_id, project_id, kind, clean_name, description.strip(), canonical_json(config), actor_id, now, now),
            )
            self.platform._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="inbound_agent.create",
                resource_type="inbound_agent",
                resource_id=agent_id,
                payload={"kind": kind, "name": clean_name},
            )
        return self.get_agent(project_id=project_id, actor_id=actor_id, agent_id=agent_id)

    def list_agents(self, *, project_id: str, actor_id: str) -> list[dict[str, Any]]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        with self.platform.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.project_id, a.kind, a.name, a.description, a.status,
                       a.draft_revision, a.active_version_id, a.created_at, a.updated_at,
                       (SELECT COUNT(*) FROM inbound_agent_bindings b
                        WHERE b.agent_id = a.id AND b.status = 'active') AS binding_count,
                       (SELECT COUNT(*) FROM inbound_agent_sessions s
                        WHERE s.agent_id = a.id) AS session_count
                FROM inbound_agents a
                WHERE a.project_id = ? ORDER BY a.updated_at DESC, a.id DESC
                """,
                (project_id,),
            ).fetchall()
        return [row_dict(row) or {} for row in rows]

    def get_agent(self, *, project_id: str, actor_id: str, agent_id: str) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        with self.platform.connect() as conn:
            return self._agent(conn, project_id, agent_id)

    def update_agent(
        self,
        *,
        project_id: str,
        actor_id: str,
        agent_id: str,
        expected_revision: int,
        name: str,
        description: str,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "agent.write")
        now = utc_now()
        with self.platform.transaction() as conn:
            current = self._agent(conn, project_id, agent_id)
            self._require_public_admin(project_id, actor_id, str(current["kind"]))
            if config.get("tools") or config.get("knowledge_sources"):
                raise ValueError("tools and knowledge are disabled until the isolated runtime is enabled")
            if config.get("recording_mode", "off") != "off":
                raise ValueError("inbound recording is disabled until disclosure and retention are configured")
            if int(current["draft_revision"]) != expected_revision:
                raise ValueError("agent draft revision conflict")
            result = conn.execute(
                """
                UPDATE inbound_agents
                SET name = ?, description = ?, draft_config_json = ?,
                    draft_revision = draft_revision + 1, updated_at = ?
                WHERE id = ? AND project_id = ? AND draft_revision = ?
                """,
                (name.strip(), description.strip(), canonical_json(config), now, agent_id, project_id, expected_revision),
            )
            if getattr(result, "rowcount", 1) == 0:
                raise ValueError("agent draft revision conflict")
            self.platform._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="inbound_agent.update",
                resource_type="inbound_agent",
                resource_id=agent_id,
                payload={"from_revision": expected_revision, "to_revision": expected_revision + 1},
            )
        return self.get_agent(project_id=project_id, actor_id=actor_id, agent_id=agent_id)

    def publish_agent(
        self,
        *,
        project_id: str,
        actor_id: str,
        agent_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self.platform.require_role(project_id, actor_id, {"owner", "admin"})
        now = utc_now()
        version_id = str(uuid.uuid4())
        with self.platform.transaction() as conn:
            agent = self._agent(conn, project_id, agent_id)
            self._require_public_admin(project_id, actor_id, str(agent["kind"]))
            if int(agent["draft_revision"]) != expected_revision:
                raise ValueError("agent draft revision conflict")
            config_text = canonical_json(agent["draft_config"])
            digest = hashlib.sha256(config_text.encode("utf-8")).hexdigest()
            existing = conn.execute(
                "SELECT * FROM inbound_agent_versions WHERE agent_id = ? AND revision = ?",
                (agent_id, agent["draft_revision"]),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO inbound_agent_versions (
                        id, project_id, agent_id, revision, config_json, config_sha256,
                        published_by, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (version_id, project_id, agent_id, agent["draft_revision"], config_text, digest, actor_id, now),
                )
            else:
                existing_item = row_dict(existing) or {}
                if existing_item["config_sha256"] != digest:
                    raise ValueError("published agent revision is immutable")
                version_id = str(existing_item["id"])
            conn.execute(
                "UPDATE inbound_agents SET active_version_id = ?, status = 'published', updated_at = ? WHERE id = ?",
                (version_id, now, agent_id),
            )
            self.platform._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="inbound_agent.publish",
                resource_type="inbound_agent_version",
                resource_id=version_id,
                payload={"agent_id": agent_id, "revision": expected_revision},
            )
        return self.get_version_for_actor(
            project_id=project_id, actor_id=actor_id, version_id=version_id
        )

    def get_version_for_actor(self, *, project_id: str, actor_id: str, version_id: str) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        return self.get_runtime_version(project_id=project_id, version_id=version_id)

    def list_versions(self, *, project_id: str, actor_id: str, agent_id: str) -> list[dict[str, Any]]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        with self.platform.connect() as conn:
            self._agent(conn, project_id, agent_id)
            rows = conn.execute(
                """
                SELECT id, project_id, agent_id, revision, config_sha256, published_by, published_at
                FROM inbound_agent_versions
                WHERE project_id = ? AND agent_id = ?
                ORDER BY revision DESC
                """,
                (project_id, agent_id),
            ).fetchall()
        return [row_dict(row) or {} for row in rows]

    def activate_version(
        self, *, project_id: str, actor_id: str, agent_id: str, version_id: str
    ) -> dict[str, Any]:
        self.platform.require_role(project_id, actor_id, {"owner", "admin"})
        now = utc_now()
        with self.platform.transaction() as conn:
            agent = self._agent(conn, project_id, agent_id)
            self._require_public_admin(project_id, actor_id, str(agent["kind"]))
            version = conn.execute(
                "SELECT * FROM inbound_agent_versions WHERE id = ? AND project_id = ? AND agent_id = ?",
                (version_id, project_id, agent_id),
            ).fetchone()
            if version is None:
                raise ResourceNotFoundError("inbound agent version not found")
            conn.execute(
                "UPDATE inbound_agents SET active_version_id = ?, status = 'published', updated_at = ? WHERE id = ?",
                (version_id, now, agent_id),
            )
            self.platform._append_audit(
                conn, project_id=project_id, actor_id=actor_id,
                action="inbound_agent.version.activate", resource_type="inbound_agent_version",
                resource_id=version_id, payload={"agent_id": agent_id},
            )
        return self.get_version_for_actor(project_id=project_id, actor_id=actor_id, version_id=version_id)

    def get_runtime_version(self, *, project_id: str, version_id: str) -> dict[str, Any]:
        with self.platform.connect() as conn:
            row = conn.execute(
                """
                SELECT v.*, a.kind, a.name, a.status AS agent_status
                FROM inbound_agent_versions v
                JOIN inbound_agents a ON a.id = v.agent_id AND a.project_id = v.project_id
                WHERE v.id = ? AND v.project_id = ?
                """,
                (version_id, project_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("active inbound agent version not found")
        item = row_dict(row) or {}
        item["config"] = json.loads(item.pop("config_json"))
        return item

    def create_binding(
        self,
        *,
        project_id: str,
        actor_id: str,
        agent_id: str,
        entry_type: str,
        destination: str,
        trunk_id: str,
    ) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "telephony.manage")
        if entry_type not in {"sip_did", "web"}:
            raise ValueError("invalid inbound entry type")
        normalized = destination.strip().lower() if entry_type == "web" else "+" + "".join(filter(str.isdigit, destination))
        if normalized in {"", "+"}:
            raise ValueError("binding destination is required")
        now = utc_now()
        binding_id = str(uuid.uuid4())
        with self.platform.transaction() as conn:
            agent = self._agent(conn, project_id, agent_id)
            self._require_public_admin(project_id, actor_id, str(agent["kind"]))
            version_id = str(agent.get("active_version_id") or "")
            if agent["status"] != "published" or not version_id:
                raise ValueError("agent must be published before binding")
            conn.execute(
                """
                INSERT INTO inbound_agent_bindings (
                    id, project_id, agent_id, agent_version_id, entry_type,
                    destination, trunk_id, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (binding_id, project_id, agent_id, version_id, entry_type, normalized, trunk_id.strip(), actor_id, now, now),
            )
            self.platform._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="inbound_agent.binding.create",
                resource_type="inbound_agent_binding",
                resource_id=binding_id,
                payload={"agent_id": agent_id, "entry_type": entry_type, "destination": normalized},
            )
        return self.get_binding(project_id=project_id, actor_id=actor_id, binding_id=binding_id)

    def get_binding(self, *, project_id: str, actor_id: str, binding_id: str) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "telephony.read")
        with self.platform.connect() as conn:
            row = conn.execute(
                "SELECT * FROM inbound_agent_bindings WHERE id = ? AND project_id = ?",
                (binding_id, project_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("inbound binding not found")
        return row_dict(row) or {}

    def list_bindings(self, *, project_id: str, actor_id: str, agent_id: str) -> list[dict[str, Any]]:
        self.platform.require_permission(project_id, actor_id, "telephony.read")
        with self.platform.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM inbound_agent_bindings WHERE project_id = ? AND agent_id = ? ORDER BY created_at",
                (project_id, agent_id),
            ).fetchall()
        return [row_dict(row) or {} for row in rows]

    def update_binding_version(
        self,
        *,
        project_id: str,
        actor_id: str,
        binding_id: str,
        version_id: str,
    ) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "telephony.manage")
        now = utc_now()
        with self.platform.transaction() as conn:
            binding = conn.execute(
                "SELECT * FROM inbound_agent_bindings WHERE id = ? AND project_id = ?",
                (binding_id, project_id),
            ).fetchone()
            if binding is None:
                raise ResourceNotFoundError("inbound binding not found")
            version = conn.execute(
                "SELECT 1 FROM inbound_agent_versions WHERE id = ? AND project_id = ? AND agent_id = ?",
                (version_id, project_id, binding["agent_id"]),
            ).fetchone()
            if version is None:
                raise ResourceNotFoundError("inbound agent version not found")
            conn.execute(
                "UPDATE inbound_agent_bindings SET agent_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, now, binding_id),
            )
            self.platform._append_audit(
                conn, project_id=project_id, actor_id=actor_id,
                action="inbound_agent.binding.version", resource_type="inbound_agent_binding",
                resource_id=binding_id, payload={"agent_version_id": version_id},
            )
        return self.get_binding(project_id=project_id, actor_id=actor_id, binding_id=binding_id)

    def disable_binding(self, *, project_id: str, actor_id: str, binding_id: str) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "telephony.manage")
        now = utc_now()
        with self.platform.transaction() as conn:
            cursor = conn.execute(
                "UPDATE inbound_agent_bindings SET status = 'disabled', updated_at = ? "
                "WHERE id = ? AND project_id = ?",
                (now, binding_id, project_id),
            )
            if getattr(cursor, "rowcount", 0) == 0:
                raise ResourceNotFoundError("inbound binding not found")
            self.platform._append_audit(
                conn, project_id=project_id, actor_id=actor_id,
                action="inbound_agent.binding.disable", resource_type="inbound_agent_binding",
                resource_id=binding_id, payload={},
            )
        return {"id": binding_id, "status": "disabled"}

    def resolve_binding(self, *, binding_id: str) -> dict[str, Any]:
        with self.platform.connect() as conn:
            row = conn.execute(
                """
                SELECT b.*, a.kind, a.status AS agent_status
                FROM inbound_agent_bindings b
                JOIN inbound_agents a ON a.id = b.agent_id AND a.project_id = b.project_id
                JOIN inbound_agent_versions v
                    ON v.id = b.agent_version_id
                    AND v.agent_id = b.agent_id
                    AND v.project_id = b.project_id
                WHERE b.id = ? AND b.status = 'active'
                """,
                (binding_id,),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("active inbound binding not found")
        return row_dict(row) or {}

    def resolve_sip_binding(self, *, trunk_id: str, called_number: str) -> dict[str, Any]:
        normalized = "+" + "".join(filter(str.isdigit, called_number))
        with self.platform.connect() as conn:
            row = conn.execute(
                """
                SELECT b.*, a.kind, a.status AS agent_status
                FROM inbound_agent_bindings b
                JOIN inbound_agents a ON a.id = b.agent_id AND a.project_id = b.project_id
                JOIN inbound_agent_versions v
                    ON v.id = b.agent_version_id
                    AND v.agent_id = b.agent_id
                    AND v.project_id = b.project_id
                WHERE b.entry_type = 'sip_did' AND b.destination = ?
                    AND b.trunk_id = ? AND b.status = 'active'
                """,
                (normalized, trunk_id.strip()),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("active SIP binding not found")
        return row_dict(row) or {}

    def list_active_sip_bindings(self) -> list[dict[str, Any]]:
        with self.platform.connect() as conn:
            rows = conn.execute(
                """
                SELECT b.*, a.kind
                FROM inbound_agent_bindings b
                JOIN inbound_agents a ON a.id = b.agent_id AND a.project_id = b.project_id
                WHERE b.entry_type = 'sip_did' AND b.status = 'active'
                ORDER BY b.created_at, b.id
                """
            ).fetchall()
        return [row_dict(row) or {} for row in rows]

    def mark_binding_dispatched(self, *, binding_id: str, dispatch_rule_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.platform.transaction() as conn:
            cursor = conn.execute(
                "UPDATE inbound_agent_bindings SET dispatch_rule_id = ?, updated_at = ? "
                "WHERE id = ? AND status = 'active'",
                (dispatch_rule_id.strip(), now, binding_id),
            )
            if getattr(cursor, "rowcount", 0) == 0:
                raise ResourceNotFoundError("active inbound binding not found")
            row = conn.execute(
                "SELECT * FROM inbound_agent_bindings WHERE id = ?", (binding_id,)
            ).fetchone()
        return row_dict(row) or {}

    def consume_nonce(self, *, nonce: str, expires_at: str) -> None:
        with self.platform.transaction() as conn:
            conn.execute("DELETE FROM inbound_metadata_nonces WHERE expires_at < ?", (utc_now(),))
            conn.execute(
                "INSERT INTO inbound_metadata_nonces (nonce, expires_at, consumed_at) VALUES (?, ?, ?)",
                (nonce, expires_at, utc_now()),
            )

    def create_public_session(
        self,
        *,
        subject_hash: str,
        max_calls_per_day: int,
        max_total_seconds: int,
        binding: Mapping[str, Any],
        room_name: str,
        provider_call_id: str,
        retention_days: int = 1,
        reservation_ttl_seconds: int = 300,
        max_concurrent_sessions: int = 20,
    ) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        now = utc_now()
        session_id = str(binding.get("session_id") or uuid.uuid4())
        retention_until = (
            datetime.now(timezone.utc) + timedelta(days=retention_days)
        ).isoformat().replace("+00:00", "Z")
        reservation_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(30, reservation_ttl_seconds))
        ).isoformat().replace("+00:00", "Z")
        with self.platform.transaction() as conn:
            # Serialize public admission in PostgreSQL so the global capacity
            # limit remains effective when several control-plane replicas run.
            if self.platform.backend == "postgresql":
                conn.execute("SELECT pg_advisory_xact_lock(?)", (741_903_112,))
            existing = conn.execute(
                "SELECT * FROM inbound_agent_sessions WHERE provider_call_id = ?",
                (provider_call_id,),
            ).fetchone()
            if existing is not None:
                existing_item = row_dict(existing) or {}
                if existing_item["binding_id"] != binding["id"] or existing_item["room_name"] != room_name:
                    raise AccessDeniedError("provider call id is already bound to another session")
                return {
                    "session_id": existing_item["id"],
                    "allowed": True,
                    "remaining_calls": 0,
                    "remaining_seconds": 0,
                    "replayed": True,
                }
            conn.execute(
                "UPDATE inbound_agent_sessions SET status = 'expired', ended_at = ?, "
                "termination_reason = 'reservation_expired', updated_at = ? "
                "WHERE status = 'reserved' AND reservation_expires_at IS NOT NULL "
                "AND reservation_expires_at <= ?",
                (now, now, now),
            )
            active = conn.execute(
                "SELECT COUNT(*) AS count FROM inbound_agent_sessions s "
                "JOIN inbound_agents a ON a.id = s.agent_id AND a.project_id = s.project_id "
                "WHERE a.kind = 'public_demo' AND s.status IN ('reserved', 'active')"
            ).fetchone()
            if int(active["count"] or 0) >= max(1, max_concurrent_sessions):
                raise PublicDemoQuotaError("public demo is at capacity; please try again later")
            conn.execute(
                """
                INSERT INTO public_demo_usage (
                    subject_hash, usage_date, call_count, total_seconds, updated_at
                ) VALUES (?, ?, 0, 0, ?)
                ON CONFLICT(subject_hash, usage_date) DO NOTHING
                """,
                (subject_hash, today, now),
            )
            lock_suffix = " FOR UPDATE" if self.platform.backend == "postgresql" else ""
            row = conn.execute(
                "SELECT * FROM public_demo_usage WHERE subject_hash = ? AND usage_date = ?" + lock_suffix,
                (subject_hash, today),
            ).fetchone()
            item = row_dict(row) or {"call_count": 0, "total_seconds": 0, "blocked_until": None}
            blocked_until = item.get("blocked_until")
            if blocked_until and str(blocked_until) > now:
                raise PublicDemoQuotaError("public demo access is temporarily blocked")
            if int(item["call_count"]) >= max_calls_per_day:
                raise PublicDemoQuotaError("public demo daily call limit reached")
            if int(item["total_seconds"]) >= max_total_seconds:
                raise PublicDemoQuotaError("public demo daily duration limit reached")
            conn.execute(
                "UPDATE public_demo_usage SET call_count = call_count + 1, updated_at = ? "
                "WHERE subject_hash = ? AND usage_date = ?",
                (now, subject_hash, today),
            )
            conn.execute(
                """
                INSERT INTO inbound_agent_sessions (
                    id, project_id, agent_id, agent_version_id, binding_id,
                    entry_type, room_name, provider_call_id, caller_hash,
                    status, started_at, reservation_expires_at, retention_until,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?)
                """,
                (
                    session_id, binding["project_id"], binding["agent_id"],
                    binding["agent_version_id"], binding["id"], binding["entry_type"],
                    room_name, provider_call_id, subject_hash, now,
                    reservation_expires_at, retention_until, now, now,
                ),
            )
        return {
            "session_id": session_id,
            "allowed": True,
            "remaining_calls": max(0, max_calls_per_day - int(item["call_count"]) - 1),
            "remaining_seconds": max(0, max_total_seconds - int(item["total_seconds"])),
        }

    def activate_session(
        self,
        *,
        session_id: str,
        binding_id: str,
        room_name: str,
        provider_call_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.platform.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM inbound_agent_sessions WHERE id = ? AND binding_id = ?",
                (session_id, binding_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("reserved inbound session not found")
            item = row_dict(row) or {}
            if item["room_name"] != room_name:
                raise AccessDeniedError("session room does not match metadata")
            if item["status"] not in {"reserved", "active"}:
                raise AccessDeniedError("reserved inbound session is no longer active")
            if item["status"] == "reserved":
                if item.get("reservation_expires_at") and item["reservation_expires_at"] <= now:
                    conn.execute(
                        "UPDATE inbound_agent_sessions SET status = 'expired', ended_at = ?, "
                        "termination_reason = 'reservation_expired', updated_at = ? WHERE id = ?",
                        (now, now, session_id),
                    )
                    raise AccessDeniedError("reserved inbound session has expired")
                conn.execute(
                    "UPDATE inbound_agent_sessions SET status = 'active', provider_call_id = ?, updated_at = ? WHERE id = ?",
                    (provider_call_id or item["provider_call_id"], now, session_id),
                )
            updated = conn.execute(
                "SELECT * FROM inbound_agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return row_dict(updated) or {}

    def reap_stale_sessions(self, *, active_grace_seconds: int = 7_500) -> dict[str, int]:
        """Close abandoned reservations and implausibly long active sessions."""
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat().replace("+00:00", "Z")
        active_before = (now_dt - timedelta(seconds=max(7_200, active_grace_seconds))).isoformat().replace(
            "+00:00", "Z"
        )
        with self.platform.transaction() as conn:
            reserved = conn.execute(
                "UPDATE inbound_agent_sessions SET status = 'expired', ended_at = ?, "
                "termination_reason = 'reservation_expired', updated_at = ? "
                "WHERE status = 'reserved' AND reservation_expires_at IS NOT NULL "
                "AND reservation_expires_at <= ?",
                (now, now, now),
            )
            active = conn.execute(
                "UPDATE inbound_agent_sessions SET status = 'failed', ended_at = ?, "
                "termination_reason = 'worker_completion_timeout', updated_at = ? "
                "WHERE status = 'active' AND started_at <= ?",
                (now, now, active_before),
            )
        return {
            "expired_reservations": max(0, int(getattr(reserved, "rowcount", 0))),
            "failed_active_sessions": max(0, int(getattr(active, "rowcount", 0))),
        }

    def create_enterprise_session(
        self,
        *,
        binding: Mapping[str, Any],
        room_name: str,
        provider_call_id: str,
        caller_hash: str,
        caller_last4: str,
        retention_days: int = 30,
        max_concurrent_sessions: int = 100,
    ) -> dict[str, Any]:
        if not provider_call_id.strip():
            raise ValueError("provider call id is required for SIP admission")
        now = utc_now()
        retention_until = (
            datetime.now(timezone.utc) + timedelta(days=retention_days)
        ).isoformat().replace("+00:00", "Z")
        session_id = str(uuid.uuid4())
        with self.platform.transaction() as conn:
            if self.platform.backend == "postgresql":
                lock_key = int(hashlib.sha256(binding["project_id"].encode("utf-8")).hexdigest()[:8], 16)
                conn.execute("SELECT pg_advisory_xact_lock(?)", (lock_key,))
            existing = conn.execute(
                "SELECT * FROM inbound_agent_sessions WHERE provider_call_id = ?",
                (provider_call_id,),
            ).fetchone()
            if existing is not None:
                item = row_dict(existing) or {}
                if item["binding_id"] != binding["id"] or item["room_name"] != room_name:
                    raise AccessDeniedError("provider call id is already bound to another session")
                return item
            active = conn.execute(
                "SELECT COUNT(*) AS count FROM inbound_agent_sessions "
                "WHERE project_id = ? AND status IN ('reserved', 'active')",
                (binding["project_id"],),
            ).fetchone()
            if int(active["count"] or 0) >= max(1, max_concurrent_sessions):
                raise PublicDemoQuotaError("enterprise inbound concurrency limit reached")
            conn.execute(
                """
                INSERT INTO inbound_agent_sessions (
                    id, project_id, agent_id, agent_version_id, binding_id,
                    entry_type, room_name, provider_call_id, caller_hash, caller_last4,
                    status, started_at, retention_until, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'sip_did', ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    session_id, binding["project_id"], binding["agent_id"],
                    binding["agent_version_id"], binding["id"], room_name,
                    provider_call_id, caller_hash, caller_last4, now, retention_until, now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM inbound_agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return row_dict(row) or {}

    def complete_session(
        self, *, session_id: str, duration_seconds: int, termination_reason: str
    ) -> dict[str, Any]:
        with self.platform.connect() as conn:
            row = conn.execute(
                """
                SELECT a.kind FROM inbound_agent_sessions s
                JOIN inbound_agents a ON a.id = s.agent_id AND a.project_id = s.project_id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("inbound session not found")
        if row["kind"] == "public_demo":
            return self.complete_public_session(
                session_id=session_id,
                duration_seconds=duration_seconds,
                termination_reason=termination_reason,
            )
        duration = max(0, min(int(duration_seconds), 7_200))
        now = utc_now()
        with self.platform.transaction() as conn:
            lock_suffix = " FOR UPDATE" if self.platform.backend == "postgresql" else ""
            current = conn.execute(
                "SELECT * FROM inbound_agent_sessions WHERE id = ?" + lock_suffix,
                (session_id,),
            ).fetchone()
            item = row_dict(current) or {}
            if item.get("status") == "completed":
                return item
            conn.execute(
                """
                UPDATE inbound_agent_sessions SET status = 'completed', ended_at = ?,
                    duration_seconds = ?, termination_reason = ?, updated_at = ? WHERE id = ?
                """,
                (now, duration, termination_reason[:120], now, session_id),
            )
            updated = conn.execute(
                "SELECT * FROM inbound_agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return row_dict(updated) or {}

    def get_session(self, *, project_id: str, actor_id: str, session_id: str) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "session.read")
        with self.platform.connect() as conn:
            row = conn.execute(
                "SELECT * FROM inbound_agent_sessions WHERE id = ? AND project_id = ?",
                (session_id, project_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("inbound session not found")
        item = row_dict(row) or {}
        item.pop("caller_hash", None)
        return item

    def get_public_session_status(self, *, session_id: str) -> dict[str, Any]:
        with self.platform.connect() as conn:
            row = conn.execute(
                """
                SELECT s.id, s.status, s.started_at, s.ended_at, s.duration_seconds,
                       s.termination_reason
                FROM inbound_agent_sessions s
                JOIN inbound_agents a ON a.id = s.agent_id AND a.project_id = s.project_id
                WHERE s.id = ? AND a.kind = 'public_demo'
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("public demo session not found")
        return row_dict(row) or {}

    def list_sessions(
        self, *, project_id: str, actor_id: str, agent_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.platform.require_permission(project_id, actor_id, "session.read")
        with self.platform.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, project_id, agent_id, agent_version_id, entry_type, room_name,
                       caller_last4, status, started_at, ended_at, duration_seconds,
                       termination_reason
                FROM inbound_agent_sessions
                WHERE project_id = ? AND agent_id = ?
                ORDER BY started_at DESC, id DESC LIMIT ?
                """,
                (project_id, agent_id, max(1, min(limit, 500))),
            ).fetchall()
        return [row_dict(row) or {} for row in rows]

    def session_analytics(self, *, project_id: str, actor_id: str, agent_id: str) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "analytics.read")
        with self.platform.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total_sessions,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_sessions,
                       COALESCE(SUM(duration_seconds), 0) AS total_seconds,
                       COALESCE(AVG(CASE WHEN duration_seconds > 0 THEN duration_seconds END), 0) AS average_seconds
                FROM inbound_agent_sessions WHERE project_id = ? AND agent_id = ?
                """,
                (project_id, agent_id),
            ).fetchone()
        return row_dict(row) or {}

    def complete_public_session(
        self,
        *,
        session_id: str,
        duration_seconds: int,
        termination_reason: str,
    ) -> dict[str, Any]:
        duration = max(0, min(int(duration_seconds), 86_400))
        now = utc_now()
        with self.platform.transaction() as conn:
            lock_suffix = " FOR UPDATE" if self.platform.backend == "postgresql" else ""
            row = conn.execute(
                """
                SELECT s.*, v.config_json, a.kind
                FROM inbound_agent_sessions s
                JOIN inbound_agent_versions v ON v.id = s.agent_version_id
                JOIN inbound_agents a ON a.id = s.agent_id AND a.project_id = s.project_id
                WHERE s.id = ?
                """ + lock_suffix,
                (session_id,),
            ).fetchone()
            item = row_dict(row)
            if item is None:
                raise ResourceNotFoundError("inbound session not found")
            if item["kind"] != "public_demo":
                raise AccessDeniedError("session is not a public demo session")
            if item["status"] == "completed":
                return item
            config = json.loads(str(item.get("config_json") or "{}"))
            duration = min(duration, int(config.get("max_duration_seconds") or duration or 0))
            cursor = conn.execute(
                """
                UPDATE inbound_agent_sessions
                SET status = 'completed', ended_at = ?, duration_seconds = ?,
                    termination_reason = ?, updated_at = ?
                WHERE id = ? AND status <> 'completed'
                """,
                (now, duration, termination_reason[:120], now, session_id),
            )
            if getattr(cursor, "rowcount", 0) == 0:
                current = conn.execute(
                    "SELECT * FROM inbound_agent_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                return row_dict(current) or {}
            usage_date = str(item["started_at"])[:10]
            conn.execute(
                """
                UPDATE public_demo_usage SET total_seconds = total_seconds + ?, updated_at = ?
                WHERE subject_hash = ? AND usage_date = ?
                """,
                (duration, now, item["caller_hash"], usage_date),
            )
            updated = conn.execute(
                "SELECT * FROM inbound_agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return row_dict(updated) or {}

    def find_public_binding(self) -> dict[str, Any]:
        if not self.public_project_id:
            raise ResourceNotFoundError("public demo project is not configured")
        with self.platform.connect() as conn:
            rows = conn.execute(
                """
                SELECT b.*, a.name, a.description
                FROM inbound_agent_bindings b
                JOIN inbound_agents a ON a.id = b.agent_id AND a.project_id = b.project_id
                WHERE a.kind = 'public_demo' AND a.project_id = ? AND a.status = 'published'
                    AND b.entry_type = 'web' AND b.status = 'active'
                ORDER BY b.created_at
                """,
                (self.public_project_id,),
            ).fetchall()
        if not rows:
            raise ResourceNotFoundError("public demo is not configured")
        if len(rows) != 1:
            raise ValueError("public demo binding configuration is ambiguous")
        return row_dict(rows[0]) or {}

    def find_public_phone_binding(self) -> dict[str, Any] | None:
        if not self.public_project_id:
            return None
        with self.platform.connect() as conn:
            rows = conn.execute(
                """
                SELECT b.*, a.name, a.description
                FROM inbound_agent_bindings b
                JOIN inbound_agents a ON a.id = b.agent_id AND a.project_id = b.project_id
                WHERE a.kind = 'public_demo' AND a.project_id = ? AND a.status = 'published'
                    AND b.entry_type = 'sip_did' AND b.status = 'active'
                ORDER BY b.created_at
                """,
                (self.public_project_id,),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError("public demo phone configuration is ambiguous")
        return row_dict(rows[0]) if rows else None
