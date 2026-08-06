"""Hermes standalone plugin for AudioAgent outbound calling."""

from pathlib import Path
from uuid import uuid4

from . import schemas, tools


def _external_call_approval(tool_name: str, args: dict, **kwargs):
    # Bind Hermes' session/permanent approval choices to this exact tool call.
    # A later outbound task must always pass through a fresh human gate.
    approval_scope = str(kwargs.get("tool_call_id") or uuid4().hex)
    if tool_name == "audioagent_submit_outbound_task":
        customer_count = len(args.get("customers") or [])
        task_name = str(args.get("task_name") or "未命名任务")[:100]
        return {
            "action": "approve",
            "message": f"确认执行外部电话任务“{task_name}”，共 {customer_count} 个号码。",
            "rule_key": f"audioagent:submit:{approval_scope}",
        }
    if tool_name == "audioagent_cancel_outbound_task":
        return {
            "action": "approve",
            "message": "确认取消该外呼任务中尚未开始的电话。",
            "rule_key": f"audioagent:cancel:{approval_scope}",
        }
    return None


def register(ctx) -> None:
    for name, schema, handler in (
        (
            "audioagent_submit_outbound_task",
            schemas.SUBMIT_OUTBOUND_TASK,
            tools.submit_outbound_task,
        ),
        (
            "audioagent_get_outbound_task",
            schemas.GET_OUTBOUND_TASK,
            tools.get_outbound_task,
        ),
        (
            "audioagent_wait_outbound_task",
            schemas.WAIT_OUTBOUND_TASK,
            tools.wait_outbound_task,
        ),
        (
            "audioagent_cancel_outbound_task",
            schemas.CANCEL_OUTBOUND_TASK,
            tools.cancel_outbound_task,
        ),
    ):
        ctx.register_tool(
            name=name,
            toolset="audioagent",
            schema=schema,
            handler=handler,
            check_fn=tools.is_configured,
            emoji="☎️",
        )

    ctx.register_hook("pre_tool_call", _external_call_approval)
    skill = Path(__file__).parent / "skills" / "outbound-calling" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("outbound-calling", skill)
