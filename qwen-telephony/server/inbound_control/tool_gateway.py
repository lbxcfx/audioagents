from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import ipaddress
import os
import socket
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlparse
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx

from server.cloud_parity.store import PlatformStore, ResourceNotFoundError
from .store import row_dict, utc_now


TOOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_tool_connections (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
 base_url TEXT NOT NULL, encrypted_headers TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
 created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE, UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS inbound_tools (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, connection_id TEXT NOT NULL, name TEXT NOT NULL,
 description TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL, input_schema_json TEXT NOT NULL,
 policy TEXT NOT NULL, timeout_seconds INTEGER NOT NULL DEFAULT 10, status TEXT NOT NULL DEFAULT 'active',
 created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
 FOREIGN KEY(connection_id) REFERENCES inbound_tool_connections(id) ON DELETE CASCADE,
 UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS inbound_tool_invocations (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT NOT NULL, tool_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, status TEXT NOT NULL, arguments_summary TEXT NOT NULL,
 result_summary TEXT NOT NULL DEFAULT '', http_status INTEGER, duration_ms INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, completed_at TEXT,
 FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
 FOREIGN KEY(tool_id) REFERENCES inbound_tools(id), UNIQUE(project_id, tool_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS inbound_tool_confirmations (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT NOT NULL, tool_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, encrypted_arguments TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL, confirmed_at TEXT,
 FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
 FOREIGN KEY(tool_id) REFERENCES inbound_tools(id), UNIQUE(project_id, tool_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_inbound_tools_project ON inbound_tools(project_id, status);
CREATE INDEX IF NOT EXISTS idx_inbound_tool_calls_project ON inbound_tool_invocations(project_id, created_at DESC);
"""

SENSITIVE_KEYS = {"password", "passwd", "secret", "token", "authorization", "api_key", "apikey", "cookie", "credit_card", "id_card"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if str(key).lower() in SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _validate_arguments(schema: dict[str, Any], value: Any, path: str = "arguments") -> None:
    """Validate the deliberately small JSON-Schema subset exposed to voice tools."""
    if not isinstance(schema, dict):
        raise ValueError("tool input schema must be an object")
    expected = schema.get("type", "object" if path == "arguments" else None)
    type_checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected is not None:
        if expected not in type_checks:
            raise ValueError(f"{path} uses unsupported schema type")
        if not type_checks[expected](value):
            raise ValueError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed value")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError(f"{path}.properties must be an object")
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ValueError(f"{path}.required must be a string array")
        missing = [item for item in required if item not in value]
        if missing:
            raise ValueError(f"{path} is missing required field: {missing[0]}")
        if schema.get("additionalProperties") is False:
            unexpected = [item for item in value if item not in properties]
            if unexpected:
                raise ValueError(f"{path} contains unexpected field: {unexpected[0]}")
        for key, item in value.items():
            if key in properties:
                _validate_arguments(properties[key], item, f"{path}.{key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_arguments(schema["items"], item, f"{path}[{index}]")


def _validate_schema(schema: dict[str, Any], path: str = "input_schema") -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"{path} must be an object")
    expected = schema.get("type", "object" if path == "input_schema" else None)
    if expected is not None and expected not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        raise ValueError(f"{path} uses unsupported schema type")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ValueError(f"{path}.required must be a string array")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError(f"{path}.properties must be an object")
    for key, child in properties.items():
        _validate_schema(child, f"{path}.properties.{key}")
    if "items" in schema:
        _validate_schema(schema["items"], f"{path}.items")


class SecretBox:
    def __init__(self, secret: str):
        if len(secret) < 32:
            raise ValueError("INBOUND_TOOL_ENCRYPTION_KEY must contain at least 32 characters")
        self.key = hashlib.sha256(secret.encode()).digest()

    def encrypt(self, value: dict[str, str]) -> str:
        nonce = os.urandom(12)
        payload = AESGCM(self.key).encrypt(nonce, json.dumps(value).encode(), b"inbound-tools-v1")
        return base64.urlsafe_b64encode(nonce + payload).decode()

    def decrypt(self, value: str) -> dict[str, str]:
        raw = base64.urlsafe_b64decode(value)
        result = json.loads(AESGCM(self.key).decrypt(raw[:12], raw[12:], b"inbound-tools-v1"))
        return {str(key): str(item) for key, item in result.items()}


class ToolGateway:
    def __init__(self, platform: PlatformStore, encryption_key: str):
        self.platform, self.secrets = platform, SecretBox(encryption_key)

    def migrate(self) -> None:
        with self.platform.transaction() as conn:
            self.platform._database.acquire_migration_lock(conn)
            conn.executescript(TOOL_SCHEMA)

    def create_connection(self, *, project_id: str, actor_id: str, name: str, kind: str, base_url: str, headers: dict[str, str]) -> dict[str, Any]:
        self.platform.require_role(project_id, actor_id, {"owner", "admin"})
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("connection URL must be an absolute HTTP(S) URL without user info")
        self._assert_safe_host(parsed.hostname)
        if kind not in {"http_api", "mcp_streamable_http"}:
            raise ValueError("unsupported connection kind")
        connection_id, now = str(uuid.uuid4()), utc_now()
        with self.platform.transaction() as conn:
            conn.execute("INSERT INTO inbound_tool_connections (id, project_id, name, kind, base_url, encrypted_headers, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (connection_id, project_id, name.strip(), kind, base_url.rstrip("/"), self.secrets.encrypt(headers), actor_id, now, now))
            self.platform._append_audit(conn, project_id=project_id, actor_id=actor_id, action="tool_connection.create", resource_type="tool_connection", resource_id=connection_id, payload={"name": name.strip(), "kind": kind, "base_url": base_url.rstrip("/")})
        return {"id": connection_id, "project_id": project_id, "name": name.strip(), "kind": kind, "base_url": base_url.rstrip("/"), "status": "active", "has_credentials": bool(headers), "created_at": now}

    @staticmethod
    def _assert_safe_host(hostname: str) -> None:
        if os.getenv("INBOUND_TOOL_ALLOW_PRIVATE_NETWORKS", "false").lower() in {"1", "true", "yes"}:
            return
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
        except socket.gaierror as exc:
            raise ValueError("connection hostname cannot be resolved") from exc
        for value in addresses:
            address = ipaddress.ip_address(value)
            if not address.is_global:
                raise ValueError("private, loopback, and link-local tool endpoints are disabled")

    def list_connections(self, *, project_id: str, actor_id: str) -> list[dict[str, Any]]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        with self.platform.connect() as conn:
            rows = conn.execute("SELECT id, project_id, name, kind, base_url, status, encrypted_headers, created_at, updated_at FROM inbound_tool_connections WHERE project_id = ? ORDER BY updated_at DESC", (project_id,)).fetchall()
        result = [row_dict(row) or {} for row in rows]
        for item in result:
            item["has_credentials"] = bool(self.secrets.decrypt(str(item.pop("encrypted_headers"))))
        return result

    def create_tool(self, *, project_id: str, actor_id: str, connection_id: str, name: str, description: str, method: str, path: str, input_schema: dict[str, Any], policy: str, timeout_seconds: int) -> dict[str, Any]:
        self.platform.require_role(project_id, actor_id, {"owner", "admin"})
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or policy not in {"auto", "confirm", "deny"} or not path.startswith("/") or path.startswith("//"):
            raise ValueError("invalid tool method, path, or policy")
        _validate_schema(input_schema)
        tool_id, now = str(uuid.uuid4()), utc_now()
        with self.platform.transaction() as conn:
            connection = conn.execute("SELECT id FROM inbound_tool_connections WHERE id = ? AND project_id = ? AND status = 'active'", (connection_id, project_id)).fetchone()
            if connection is None: raise ResourceNotFoundError("tool connection not found")
            conn.execute("INSERT INTO inbound_tools (id, project_id, connection_id, name, description, method, path, input_schema_json, policy, timeout_seconds, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (tool_id, project_id, connection_id, name.strip(), description.strip(), method, path, json.dumps(input_schema), policy, timeout_seconds, actor_id, now, now))
            self.platform._append_audit(conn, project_id=project_id, actor_id=actor_id, action="tool.create", resource_type="tool", resource_id=tool_id, payload={"name": name.strip(), "method": method, "path": path, "policy": policy})
        return self.get_tool(project_id=project_id, tool_id=tool_id)

    def get_tool(self, *, project_id: str, tool_id: str) -> dict[str, Any]:
        with self.platform.connect() as conn:
            row = conn.execute("SELECT id, project_id, connection_id, name, description, method, path, input_schema_json, policy, timeout_seconds, status, created_at, updated_at FROM inbound_tools WHERE id = ? AND project_id = ?", (tool_id, project_id)).fetchone()
        if row is None: raise ResourceNotFoundError("tool not found")
        item = row_dict(row) or {}; item["input_schema"] = json.loads(item.pop("input_schema_json")); return item

    def list_tools(self, *, project_id: str, actor_id: str) -> list[dict[str, Any]]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        with self.platform.connect() as conn:
            rows = conn.execute("SELECT id FROM inbound_tools WHERE project_id = ? ORDER BY updated_at DESC", (project_id,)).fetchall()
        return [self.get_tool(project_id=project_id, tool_id=str(row["id"])) for row in rows]

    def assert_tools(self, *, project_id: str, tool_ids: list[str]) -> None:
        for tool_id in tool_ids:
            tool = self.get_tool(project_id=project_id, tool_id=tool_id)
            if tool["status"] != "active" or tool["policy"] == "deny":
                raise ValueError("tool is not available for Agent binding")

    async def discover_mcp(self, *, project_id: str, actor_id: str, connection_id: str) -> list[dict[str, Any]]:
        self.platform.require_role(project_id, actor_id, {"owner", "admin"})
        with self.platform.connect() as conn:
            connection = conn.execute("SELECT * FROM inbound_tool_connections WHERE id = ? AND project_id = ? AND kind = 'mcp_streamable_http' AND status = 'active'", (connection_id, project_id)).fetchone()
        if connection is None: raise ResourceNotFoundError("MCP connection not found")
        self._assert_safe_host(urlparse(str(connection["base_url"])).hostname or "")
        headers = {**self.secrets.decrypt(str(connection["encrypted_headers"])), "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            initialized = await self._mcp_rpc(client, str(connection["base_url"]), headers, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "audioagents", "version": "1.0"}}, 1)
            session_header = initialized.pop("_session_id", "")
            if session_header: headers["Mcp-Session-Id"] = session_header
            listed = await self._mcp_rpc(client, str(connection["base_url"]), headers, "tools/list", {}, 2)
        tools = listed.get("tools", [])
        return [item for item in tools if isinstance(item, dict)]

    @staticmethod
    async def _mcp_rpc(client: httpx.AsyncClient, url: str, headers: dict[str, str], method: str, params: dict[str, Any], request_id: int) -> dict[str, Any]:
        response = await client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        response.raise_for_status()
        if "text/event-stream" in response.headers.get("content-type", ""):
            payloads = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
            payload = json.loads(payloads[-1]) if payloads else {}
        else: payload = response.json()
        if payload.get("error"): raise RuntimeError("MCP server returned an error")
        result = payload.get("result") or {}
        if not isinstance(result, dict): raise RuntimeError("MCP result is invalid")
        result["_session_id"] = response.headers.get("Mcp-Session-Id", "")
        return result

    async def invoke(self, *, project_id: str, session_id: str, tool_id: str, arguments: dict[str, Any], idempotency_key: str, confirmed: bool) -> dict[str, Any]:
        tool = self.get_tool(project_id=project_id, tool_id=tool_id)
        if tool["status"] != "active" or tool["policy"] == "deny": raise PermissionError("tool invocation is denied")
        _validate_arguments(tool["input_schema"], arguments)
        if tool["policy"] == "confirm" and not confirmed:
            confirmation_id, now = str(uuid.uuid4()), utc_now()
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
            with self.platform.transaction() as conn:
                existing = conn.execute("SELECT * FROM inbound_tool_confirmations WHERE project_id = ? AND tool_id = ? AND idempotency_key = ?", (project_id, tool_id, idempotency_key)).fetchone()
                if existing is None:
                    conn.execute("INSERT INTO inbound_tool_confirmations (id, project_id, session_id, tool_id, idempotency_key, encrypted_arguments, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (confirmation_id, project_id, session_id, tool_id, idempotency_key, self.secrets.encrypt({"arguments": json.dumps(arguments, ensure_ascii=False)}), expires_at, now))
                else: confirmation_id, expires_at = str(existing["id"]), str(existing["expires_at"])
            return {"status": "confirmation_required", "confirmation_id": confirmation_id, "expires_at": expires_at, "tool": {key: tool[key] for key in ("id", "name", "description", "policy")}}
        with self.platform.connect() as conn:
            connection = conn.execute("SELECT * FROM inbound_tool_connections WHERE id = ? AND project_id = ? AND status = 'active'", (tool["connection_id"], project_id)).fetchone()
        if connection is None: raise ResourceNotFoundError("tool connection not found")
        self._assert_safe_host(urlparse(str(connection["base_url"])).hostname or "")
        invocation_id, now = str(uuid.uuid4()), utc_now()
        summary = json.dumps(_redact(arguments), ensure_ascii=False)[:2000]
        with self.platform.transaction() as conn:
            existing = conn.execute("SELECT * FROM inbound_tool_invocations WHERE project_id = ? AND tool_id = ? AND idempotency_key = ?", (project_id, tool_id, idempotency_key)).fetchone()
            if existing is not None: return row_dict(existing) or {}
            conn.execute("INSERT INTO inbound_tool_invocations (id, project_id, session_id, tool_id, idempotency_key, status, arguments_summary, created_at) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)", (invocation_id, project_id, session_id, tool_id, idempotency_key, summary, now))
        started = monotonic()
        try:
            async with httpx.AsyncClient(timeout=float(tool["timeout_seconds"]), follow_redirects=False) as client:
                headers = self.secrets.decrypt(str(connection["encrypted_headers"]))
                if connection["kind"] == "mcp_streamable_http":
                    headers.update({"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
                    mcp_result = await self._mcp_rpc(client, str(connection["base_url"]), headers, "tools/call", {"name": tool["name"], "arguments": arguments}, 1)
                    result, response = json.dumps(_redact(mcp_result), ensure_ascii=False)[:100_000], None
                else:
                    kwargs = {"params": arguments} if tool["method"] == "GET" else {"json": arguments}
                    response = await client.request(tool["method"], urljoin(str(connection["base_url"]) + "/", tool["path"].lstrip("/")), headers=headers, **kwargs)
                response.raise_for_status()
                try: result = json.dumps(_redact(response.json()), ensure_ascii=False)[:100_000]
                except Exception: result = response.text[:100_000]
            status, http_status = "completed", response.status_code if response is not None else 200
        except Exception as exc:
            status, http_status, result = "failed", None, type(exc).__name__
        duration = int((monotonic() - started) * 1000)
        with self.platform.transaction() as conn:
            conn.execute("UPDATE inbound_tool_invocations SET status = ?, result_summary = ?, http_status = ?, duration_ms = ?, completed_at = ? WHERE id = ?", (status, result[:2000], http_status, duration, utc_now(), invocation_id))
        return {"id": invocation_id, "status": status, "http_status": http_status, "duration_ms": duration, "result": result}

    async def confirm(self, *, project_id: str, session_id: str, confirmation_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.platform.transaction() as conn:
            row = conn.execute("SELECT * FROM inbound_tool_confirmations WHERE id = ? AND project_id = ? AND session_id = ?", (confirmation_id, project_id, session_id)).fetchone()
            if row is None: raise ResourceNotFoundError("tool confirmation not found")
            item = row_dict(row) or {}
            if item["status"] == "confirmed":
                existing = conn.execute("SELECT * FROM inbound_tool_invocations WHERE project_id = ? AND tool_id = ? AND idempotency_key = ?", (project_id, item["tool_id"], item["idempotency_key"])).fetchone()
                return row_dict(existing) or {"status": "confirmed"}
            if item["status"] != "pending" or item["expires_at"] <= now: raise PermissionError("tool confirmation expired or is no longer pending")
            conn.execute("UPDATE inbound_tool_confirmations SET status = 'confirmed', confirmed_at = ? WHERE id = ? AND status = 'pending'", (now, confirmation_id))
        protected = self.secrets.decrypt(str(item["encrypted_arguments"]))
        arguments = json.loads(protected["arguments"])
        return await self.invoke(project_id=project_id, session_id=session_id, tool_id=str(item["tool_id"]), arguments=arguments, idempotency_key=str(item["idempotency_key"]), confirmed=True)
