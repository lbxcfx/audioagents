from __future__ import annotations

from pathlib import Path
import sys

import pytest
import httpx
import asyncio

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path: sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.store import PlatformStore, ResourceNotFoundError
from server.inbound_control.store import InboundAgentStore
from server.inbound_control.tool_gateway import ToolGateway, _redact, _validate_arguments


def test_tool_secrets_are_encrypted_and_agent_binding_is_project_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("INBOUND_TOOL_ALLOW_PRIVATE_NETWORKS", "true")
    platform = PlatformStore(tmp_path / "platform.sqlite3"); platform.initialize()
    first = platform.create_project(name="甲公司", slug="tools-a", owner_id="owner-a")
    second = platform.create_project(name="乙公司", slug="tools-b", owner_id="owner-b")
    gateway = ToolGateway(platform, "a-test-encryption-key-with-at-least-32-characters"); gateway.migrate()
    inbound = InboundAgentStore(platform, tool_validator=lambda project_id, ids: gateway.assert_tools(project_id=project_id, tool_ids=ids)); inbound.migrate()
    connection = gateway.create_connection(project_id=first["id"], actor_id="owner-a", name="CRM", kind="http_api", base_url="http://127.0.0.1:9999", headers={"Authorization": "Bearer top-secret"})
    with platform.connect() as conn:
        raw = str(conn.execute("SELECT encrypted_headers FROM inbound_tool_connections WHERE id = ?", (connection["id"],)).fetchone()["encrypted_headers"])
    assert "top-secret" not in raw
    tool = gateway.create_tool(project_id=first["id"], actor_id="owner-a", connection_id=connection["id"], name="query_order", description="查询订单", method="GET", path="/orders", input_schema={"type": "object"}, policy="auto", timeout_seconds=5)
    config = {"instructions": "你是企业订单客服，需要安全准确地回答客户提出的问题。", "welcome_message": "您好", "voice": "longanlingxin", "language": "zh-CN", "max_duration_seconds": 600, "recording_mode": "off", "recording_disclosure": "", "knowledge_sources": [], "tools": [tool["id"]]}
    agent = inbound.create_agent(project_id=first["id"], actor_id="owner-a", name="订单助手", description="", kind="enterprise", config=config)
    assert agent["draft_config"]["tools"] == [tool["id"]]
    with pytest.raises(ResourceNotFoundError): inbound.create_agent(project_id=second["id"], actor_id="owner-b", name="越权助手", description="", kind="enterprise", config=config)
    platform.close()

def test_tool_audit_redacts_nested_sensitive_fields():
    assert _redact({"customer": {"password": "x", "name": "张三"}, "token": "y"}) == {"customer": {"password": "***", "name": "张三"}, "token": "***"}


def test_tool_arguments_follow_declared_schema():
    schema = {
        "type": "object",
        "required": ["order_id"],
        "additionalProperties": False,
        "properties": {"order_id": {"type": "string"}, "dry_run": {"type": "boolean"}},
    }
    _validate_arguments(schema, {"order_id": "A-1", "dry_run": True})
    with pytest.raises(ValueError, match="required field"):
        _validate_arguments(schema, {})
    with pytest.raises(ValueError, match="unexpected field"):
        _validate_arguments(schema, {"order_id": "A-1", "admin": True})
    with pytest.raises(ValueError, match="must be string"):
        _validate_arguments(schema, {"order_id": 1})


def test_mcp_rpc_accepts_json_and_sse():
  async def run():
    async def json_handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "query_order"}]}}, headers={"Mcp-Session-Id": "session-a"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(json_handler)) as client:
        result = await ToolGateway._mcp_rpc(client, "https://mcp.example.test", {}, "tools/list", {}, 1)
    assert result["tools"][0]["name"] == "query_order"
    assert result["_session_id"] == "session-a"

    async def sse_handler(request):
        return httpx.Response(200, text='event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"ok"}]}}\n\n', headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(sse_handler)) as client:
        result = await ToolGateway._mcp_rpc(client, "https://mcp.example.test", {}, "tools/call", {}, 2)
    assert result["content"][0]["text"] == "ok"
  asyncio.run(run())


def test_confirm_policy_requires_persisted_one_time_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("INBOUND_TOOL_ALLOW_PRIVATE_NETWORKS", "true")
    platform=PlatformStore(tmp_path/"confirm.sqlite3");platform.initialize()
    project=platform.create_project(name="甲",slug="confirm-a",owner_id="owner-a")
    gateway=ToolGateway(platform,"another-encryption-key-with-at-least-32-characters");gateway.migrate()
    connection=gateway.create_connection(project_id=project["id"],actor_id="owner-a",name="CRM",kind="http_api",base_url="http://127.0.0.1:9999",headers={})
    tool=gateway.create_tool(project_id=project["id"],actor_id="owner-a",connection_id=connection["id"],name="create_ticket",description="创建工单",method="POST",path="/tickets",input_schema={},policy="confirm",timeout_seconds=5)
    pending=asyncio.run(gateway.invoke(project_id=project["id"],session_id="session-a",tool_id=tool["id"],arguments={"title":"退款"},idempotency_key="idempotency-a",confirmed=False))
    assert pending["status"]=="confirmation_required"
    captured={}
    async def fake_invoke(**kwargs): captured.update(kwargs);return {"status":"completed"}
    gateway.invoke=fake_invoke
    result=asyncio.run(gateway.confirm(project_id=project["id"],session_id="session-a",confirmation_id=pending["confirmation_id"]))
    assert result["status"]=="completed" and captured["arguments"]=={"title":"退款"} and captured["confirmed"] is True
    with platform.connect() as conn: assert conn.execute("SELECT status FROM inbound_tool_confirmations WHERE id = ?",(pending["confirmation_id"],)).fetchone()["status"]=="confirmed"
    platform.close()
