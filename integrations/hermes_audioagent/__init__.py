"""Hermes standalone plugin for AudioAgent outbound calling."""

from pathlib import Path

from . import schemas, tools


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

    skill = Path(__file__).parent / "skills" / "outbound-calling" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("outbound-calling", skill)
