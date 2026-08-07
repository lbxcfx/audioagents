"""Hermes standalone plugin for AudioAgent outbound calling."""

from pathlib import Path

from . import delivery, middleware, schemas, tools


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
        (
            "audioagent_get_latest_call_transcript",
            schemas.GET_LATEST_CALL_TRANSCRIPT,
            tools.get_latest_call_transcript,
        ),
        (
            "audioagent_get_latest_call_recording",
            schemas.GET_LATEST_CALL_RECORDING,
            tools.get_latest_call_recording,
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

    for skill_name in (
        "outbound-calling",
        "latest-call-transcript",
        "latest-call-recording",
    ):
        skill = Path(__file__).parent / "skills" / skill_name / "SKILL.md"
        if skill.exists():
            ctx.register_skill(skill_name, skill)
    ctx.register_middleware("llm_request", middleware.isolate_wechat_outbound_request)
    ctx.register_middleware(
        "llm_request", middleware.guide_wechat_call_artifact_request
    )
    delivery.start_result_forwarder()
