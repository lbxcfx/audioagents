from __future__ import annotations

from datetime import timedelta
import json
import os
from typing import Any, Callable
from urllib.parse import urlsplit
import uuid

from .store import AccessDeniedError, PlatformStore, ResourceNotFoundError, _row, _utc_now


EmbedTokenIssuer = Callable[[str, str, str, dict[str, bool], int], str]


class EmbedRateLimitError(RuntimeError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__("embed token rate limit exceeded")


def normalize_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("allowed origin must be an absolute http or https origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("allowed origin cannot contain credentials, path, query, or fragment")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def issue_embed_token(
    room_name: str,
    identity: str,
    agent_name: str,
    capabilities: dict[str, bool],
    ttl_seconds: int,
) -> str:
    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required")
    from livekit import api

    sources = ["microphone"]
    if capabilities.get("camera"):
        sources.append("camera")
    if capabilities.get("screen_share"):
        sources.extend(["screen_share", "screen_share_audio"])
    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name("Embedded agent user")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=bool(capabilities.get("text")),
                can_publish_sources=sources,
            )
        )
        .with_ttl(timedelta(seconds=ttl_seconds))
    )
    if agent_name:
        token = token.with_room_config(
            api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name=agent_name)]
            )
        )
    return token.to_jwt()


class EmbedService:
    def __init__(
        self,
        store: PlatformStore,
        token_issuer: EmbedTokenIssuer = issue_embed_token,
        token_limit_per_minute: int = 60,
    ):
        self.store = store
        self.token_issuer = token_issuer
        self.token_limit_per_minute = max(1, int(token_limit_per_minute))

    def save_config(
        self,
        *,
        project_id: str,
        actor_id: str,
        name: str,
        agent_name: str,
        room_prefix: str,
        allowed_origins: list[str],
        capabilities: dict[str, bool],
        enabled: bool = True,
        config_id: str | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "project.manage")
        if not name.strip() or not agent_name.strip():
            raise ValueError("embed name and agent_name are required")
        if not room_prefix or not all(char.isalnum() or char in "-_" for char in room_prefix):
            raise ValueError("room_prefix contains unsupported characters")
        origins = sorted(set(normalize_origin(item) for item in allowed_origins))
        if not origins:
            raise ValueError("at least one allowed origin is required")
        normalized_capabilities = {
            key: bool(capabilities.get(key, False))
            for key in ("audio", "text", "camera", "screen_share")
        }
        if not normalized_capabilities["audio"]:
            raise ValueError("audio capability is required for the voice widget")
        value_id = config_id or str(uuid.uuid4())
        now = _utc_now()
        with self.store.transaction() as conn:
            if config_id:
                cursor = conn.execute(
                    """
                    UPDATE embed_configs
                    SET name = ?, agent_name = ?, room_prefix = ?,
                        allowed_origins_json = ?, capabilities_json = ?,
                        enabled = ?, updated_at = ?
                    WHERE id = ? AND project_id = ?
                    """,
                    (
                        name.strip(), agent_name.strip(), room_prefix,
                        json.dumps(origins), json.dumps(normalized_capabilities),
                        int(enabled), now, config_id, project_id,
                    ),
                )
                if cursor.rowcount == 0:
                    raise ResourceNotFoundError("embed config not found")
            else:
                conn.execute(
                    """
                    INSERT INTO embed_configs (
                        id, project_id, name, agent_name, room_prefix,
                        allowed_origins_json, capabilities_json, enabled,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        value_id, project_id, name.strip(), agent_name.strip(), room_prefix,
                        json.dumps(origins), json.dumps(normalized_capabilities),
                        int(enabled), now, now,
                    ),
                )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="embed_config.save",
                resource_type="embed_config",
                resource_id=value_id,
                payload={"name": name.strip(), "enabled": enabled},
            )
            row = conn.execute("SELECT * FROM embed_configs WHERE id = ?", (value_id,)).fetchone()
        return self._record(row)

    def get_config(self, *, project_id: str, user_id: str, config_id: str) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "project.read")
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM embed_configs WHERE id = ? AND project_id = ?",
                (config_id, project_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("embed config not found")
        return self._record(row)

    def list_configs(self, *, project_id: str, user_id: str) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "project.read")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM embed_configs WHERE project_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (project_id,),
            ).fetchall()
        return [self._record(item) for item in rows]

    def issue_token(
        self,
        *,
        config_id: str,
        request_origin: str,
        participant_name: str = "Guest",
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        if not 30 <= ttl_seconds <= 900:
            raise ValueError("embed token TTL must be between 30 and 900 seconds")
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM embed_configs WHERE id = ?", (config_id,)).fetchone()
        if row is None:
            raise ResourceNotFoundError("embed config not found")
        config = self._record(row)
        if not config["enabled"]:
            raise AccessDeniedError("embed config is disabled")
        origin = normalize_origin(request_origin)
        if origin not in config["allowed_origins"]:
            raise AccessDeniedError("origin is not allowed")
        limit = self.store.consume_api_rate_limit(
            key=f"embed-token:{config_id}",
            limit=self.token_limit_per_minute,
        )
        if not limit["allowed"]:
            raise EmbedRateLimitError(int(limit["retry_after"]))
        room_name = f"{config['room_prefix']}-{uuid.uuid4().hex}"
        identity = f"embed:{config_id}:{uuid.uuid4().hex[:16]}"
        token = self.token_issuer(
            room_name,
            identity,
            config["agent_name"],
            config["capabilities"],
            ttl_seconds,
        )
        with self.store.transaction() as conn:
            self.store._append_audit(
                conn,
                project_id=config["project_id"],
                actor_id=identity,
                action="embed.token.issue",
                resource_type="embed_config",
                resource_id=config_id,
                payload={"origin": origin, "room_name": room_name},
            )
        return {
            "token": token,
            "url": os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880"),
            "room_name": room_name,
            "identity": identity,
            "participant_name": participant_name[:120],
            "capabilities": config["capabilities"],
            "expires_in": ttl_seconds,
        }

    def is_origin_allowed(self, *, config_id: str, request_origin: str) -> bool:
        """Validate per-widget CORS without issuing a token or consuming quota."""
        try:
            origin = normalize_origin(request_origin)
        except ValueError:
            return False
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT allowed_origins_json, enabled FROM embed_configs WHERE id = ?",
                (config_id,),
            ).fetchone()
        if row is None or not bool(row["enabled"]):
            return False
        return origin in json.loads(str(row["allowed_origins_json"] or "[]"))

    @staticmethod
    def _record(row: Any) -> dict[str, Any]:
        if row is None:
            raise ResourceNotFoundError("embed config not found")
        record = _row(row) or {}
        record["allowed_origins"] = json.loads(record.pop("allowed_origins_json"))
        record["capabilities"] = json.loads(record.pop("capabilities_json"))
        record["enabled"] = bool(record["enabled"])
        return record
