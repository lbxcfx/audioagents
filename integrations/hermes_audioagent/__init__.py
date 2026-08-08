"""Hermes standalone plugin for AudioAgent outbound calling."""

from pathlib import Path

from . import address_book, delivery, middleware, qwen_asr, response_policy, schemas, tools


def register(ctx) -> None:
    if hasattr(ctx, "register_transcription_provider"):
        ctx.register_transcription_provider(qwen_asr.create_provider())

    for name, schema, handler in (
        (
            "audioagent_resolve_outbound_contact",
            schemas.RESOLVE_OUTBOUND_CONTACT,
            tools.resolve_outbound_contact,
        ),
        (
            "audioagent_confirm_address_book_contact",
            schemas.CONFIRM_ADDRESS_BOOK_CONTACT,
            tools.confirm_address_book_contact,
        ),
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
    ctx.register_middleware(
        "llm_request", middleware.guide_wechat_address_book_request
    )
    ctx.register_middleware("llm_request", middleware.isolate_wechat_outbound_request)
    ctx.register_middleware(
        "llm_request", middleware.guide_wechat_call_artifact_request
    )
    ctx.register_middleware(
        "llm_execution", middleware.return_tool_backed_wechat_response
    )
    ctx.register_hook(
        "transform_llm_output", response_policy.transform_submission_response
    )
    ctx.register_hook("pre_gateway_dispatch", address_book.mark_weixin_voice_input)
    delivery.start_result_forwarder()
