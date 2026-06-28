from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable
from uuid import uuid4

import httpx
from livekit.agents import APIConnectOptions, DEFAULT_API_CONNECT_OPTIONS
from livekit.agents import llm as lk_llm
from livekit.agents.types import NOT_GIVEN, NotGivenOr


logger = logging.getLogger("qwen-phone-agent.dialogue")


def _env_enabled(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def latest_user_text(chat_ctx: lk_llm.ChatContext) -> str:
    for message in reversed(chat_ctx.messages()):
        if message.role == "user":
            return message.text_content or ""
    return ""


class ScriptFirstLLM(lk_llm.LLM):
    """A LiveKit LLM adapter that tries dialogue-service before the real LLM."""

    def __init__(
        self,
        *,
        upstream: lk_llm.LLM,
        session_id: str,
        scene_id: int | None = None,
        dialogue_url: str | None = None,
        enabled: bool | None = None,
        timeout: float = 0.8,
        on_dialogue_result: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> None:
        super().__init__()
        self._upstream = upstream
        self._session_id = session_id
        self._scene_id = scene_id
        self._dialogue_url = (
            dialogue_url
            or os.getenv("QWEN_DIALOGUE_URL")
            or "http://127.0.0.1:8090/api/dialogue/turn"
        )
        self._enabled = _env_enabled(os.getenv("QWEN_NLU_ENABLED"), True) if enabled is None else enabled
        self._timeout = timeout
        self._on_dialogue_result = on_dialogue_result

    @property
    def model(self) -> str:
        return f"script-first/{self._upstream.model}"

    @property
    def provider(self) -> str:
        return "dialogue-service"

    async def aclose(self) -> None:
        await self._upstream.aclose()

    def chat(
        self,
        *,
        chat_ctx: lk_llm.ChatContext,
        tools: list[lk_llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[lk_llm.ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> lk_llm.LLMStream:
        return ScriptFirstLLMStream(
            self,
            upstream=self._upstream,
            enabled=self._enabled,
            dialogue_url=self._dialogue_url,
            session_id=self._session_id,
            scene_id=self._scene_id,
            timeout=self._timeout,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            on_dialogue_result=self._on_dialogue_result,
        )


class ScriptFirstLLMStream(lk_llm.LLMStream):
    def __init__(
        self,
        llm: ScriptFirstLLM,
        *,
        upstream: lk_llm.LLM,
        enabled: bool,
        dialogue_url: str,
        session_id: str,
        scene_id: int | None,
        timeout: float,
        chat_ctx: lk_llm.ChatContext,
        tools: list[lk_llm.Tool],
        conn_options: APIConnectOptions,
        parallel_tool_calls: NotGivenOr[bool],
        tool_choice: NotGivenOr[lk_llm.ToolChoice],
        extra_kwargs: NotGivenOr[dict[str, Any]],
        on_dialogue_result: Callable[[dict[str, Any]], Awaitable[None] | None] | None,
    ) -> None:
        super().__init__(llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._upstream = upstream
        self._enabled = enabled
        self._dialogue_url = dialogue_url
        self._session_id = session_id
        self._scene_id = scene_id
        self._timeout = timeout
        self._parallel_tool_calls = parallel_tool_calls
        self._tool_choice = tool_choice
        self._extra_kwargs = extra_kwargs
        self._on_dialogue_result = on_dialogue_result

    async def _run(self) -> None:
        text = latest_user_text(self._chat_ctx).strip()
        if self._enabled and text:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        self._dialogue_url,
                        json={
                            "session_id": self._session_id,
                            "scene_id": self._scene_id,
                            "text": text,
                            "channel": "livekit_voice",
                        },
                    )
                    response.raise_for_status()
                    result = response.json()
                if result.get("handled") and result.get("text"):
                    await self._emit_fixed_reply(result["text"], result)
                    return
                logger.info(
                    "dialogue-service fallback: route=%s reason=%s",
                    result.get("route_type"),
                    result.get("reason"),
                )
            except Exception:
                logger.exception("dialogue-service unavailable, falling back to upstream LLM")

        upstream_stream = self._upstream.chat(
            chat_ctx=self._chat_ctx,
            tools=self._tools,
            conn_options=self._conn_options,
            parallel_tool_calls=self._parallel_tool_calls,
            tool_choice=self._tool_choice,
            extra_kwargs=self._extra_kwargs,
        )
        async with upstream_stream:
            async for chunk in upstream_stream:
                await self._event_ch.send(chunk)

    async def _emit_fixed_reply(self, text: str, result: dict[str, Any]) -> None:
        request_id = f"dialogue_{uuid4().hex[:12]}"
        prompt_tokens = max(1, len(latest_user_text(self._chat_ctx)) // 2)
        completion_tokens = max(1, len(text) // 2)
        await self._event_ch.send(
            lk_llm.ChatChunk(
                id=request_id,
                delta=lk_llm.ChoiceDelta(
                    role="assistant",
                    content=text,
                    extra={
                        "route_type": result.get("route_type"),
                        "scene_id": result.get("scene_id"),
                        "next_node_id": result.get("next_node_id"),
                        "label": result.get("label"),
                        "should_hangup": result.get("should_hangup"),
                        "hangup_delay_ms": result.get("hangup_delay_ms"),
                    },
                ),
            )
        )
        await self._event_ch.send(
            lk_llm.ChatChunk(
                id=request_id,
                usage=lk_llm.CompletionUsage(
                    completion_tokens=completion_tokens,
                    prompt_tokens=prompt_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ),
            )
        )
        if self._on_dialogue_result:
            awaitable = self._on_dialogue_result(result)
            if awaitable is not None:
                await awaitable
