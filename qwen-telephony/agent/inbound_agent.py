from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import hashlib
import logging
import os
from time import monotonic
from typing import Any
import uuid

import httpx
import jwt
from livekit import api, rtc
from livekit.agents import Agent, AgentServer, AgentSession, AutoSubscribe, JobContext, cli, function_tool
from livekit.agents.voice.room_io import RoomOptions, TextInputEvent, TextInputOptions
from openai import AsyncOpenAI

from qwen_audio_realtime import QwenAudioRealtimeModel


logger = logging.getLogger("inbound-agent")


class InboundControlError(RuntimeError):
    pass


class InboundControlClient:
    def __init__(self) -> None:
        base_url = os.getenv("INBOUND_CONTROL_URL", "http://127.0.0.1:8092").rstrip("/")
        secret = os.getenv("INBOUND_WORKER_SECRET", "").strip()
        if not secret:
            raise ValueError("INBOUND_WORKER_SECRET is required")
        self._secret = secret
        self._subject = os.getenv("INBOUND_WORKER_ID", f"inbound-worker-{os.getpid()}")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(10.0, connect=3.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    def _headers(self, *scopes: str) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "iss": "audioagents-inbound-worker",
                "aud": "audioagents-inbound-control",
                "sub": self._subject,
                "scope": list(scopes),
                "iat": now,
                "exp": now + timedelta(seconds=60),
                "jti": str(uuid.uuid4()),
            },
            self._secret,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    async def resolve(self, *, metadata: str, room_name: str) -> dict[str, Any]:
        response = await self._client.post(
            "/inbound-api/internal/runtime",
            json={"metadata": metadata, "room_name": room_name, "provider_call_id": ""},
            headers=self._headers("runtime:read"),
        )
        if response.status_code != 200:
            raise InboundControlError(f"runtime admission failed with status {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("config"):
            raise InboundControlError("runtime admission returned an invalid payload")
        return payload

    async def complete(
        self,
        *,
        session_id: str,
        duration_seconds: int,
        reason: str,
    ) -> None:
        if not session_id:
            return
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                response = await self._client.post(
                    "/inbound-api/internal/sessions/complete",
                    json={
                        "session_id": session_id,
                        "duration_seconds": max(0, duration_seconds),
                        "termination_reason": reason[:120] or "completed",
                    },
                    headers=self._headers("session:complete"),
                )
                if response.status_code == 200:
                    return
                if response.status_code < 500 and response.status_code != 429:
                    raise InboundControlError(
                        f"session completion failed permanently with status {response.status_code}"
                    )
                last_error = InboundControlError(
                    f"session completion failed with status {response.status_code}"
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
            if attempt < 4:
                await asyncio.sleep(0.4 * (2**attempt))
        raise InboundControlError("session completion retries exhausted") from last_error

    async def search_knowledge(
        self, *, project_id: str, knowledge_base_ids: list[str], query: str, limit: int = 5,
        document_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        response = await self._client.post(
            "/inbound-api/internal/knowledge/search",
            json={"project_id": project_id, "knowledge_base_ids": knowledge_base_ids, "document_ids": document_ids, "query": query, "limit": limit},
            headers=self._headers("knowledge:read"),
        )
        if response.status_code != 200:
            raise InboundControlError(f"knowledge search failed with status {response.status_code}")
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise InboundControlError("knowledge search returned an invalid payload")
        return [item for item in items if isinstance(item, dict)]

    async def tool_catalog(self, *, project_id: str, tool_ids: list[str]) -> list[dict[str, Any]]:
        response = await self._client.post("/inbound-api/internal/tools/catalog", json={"project_id": project_id, "tool_ids": tool_ids}, headers=self._headers("tool:invoke"))
        if response.status_code != 200: raise InboundControlError(f"tool catalog failed with status {response.status_code}")
        items = response.json().get("items", [])
        return [item for item in items if isinstance(item, dict)]

    async def invoke_tool(self, *, project_id: str, session_id: str, tool_id: str, arguments: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        response = await self._client.post("/inbound-api/internal/tools/invoke", json={"project_id": project_id, "session_id": session_id, "tool_id": tool_id, "arguments": arguments, "idempotency_key": idempotency_key}, headers=self._headers("tool:invoke"))
        if response.status_code != 200: raise InboundControlError(f"tool invocation failed with status {response.status_code}")
        return response.json()

    async def confirm_tool(self, *, project_id: str, session_id: str, confirmation_id: str) -> dict[str, Any]:
        response = await self._client.post("/inbound-api/internal/tools/confirm", json={"project_id": project_id, "session_id": session_id, "confirmation_id": confirmation_id}, headers=self._headers("tool:invoke"))
        if response.status_code != 200: raise InboundControlError(f"tool confirmation failed with status {response.status_code}")
        return response.json()

    async def get_content(self, *, project_id: str, asset_id: str) -> dict[str, Any]:
        response = await self._client.post("/inbound-api/internal/content/get", json={"project_id": project_id, "asset_id": asset_id}, headers=self._headers("runtime:read"))
        if response.status_code != 200: raise InboundControlError(f"content lookup failed with status {response.status_code}")
        return response.json()

    async def admit_sip(
        self,
        *,
        trunk_id: str,
        called_number: str,
        caller_number: str,
        room_name: str,
        provider_call_id: str,
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/inbound-api/internal/sip/admit",
            json={
                "trunk_id": trunk_id,
                "called_number": called_number,
                "caller_number": caller_number,
                "room_name": room_name,
                "provider_call_id": provider_call_id,
            },
            headers=self._headers("sip:admit"),
        )
        if response.status_code != 200:
            raise InboundControlError(f"SIP admission failed with status {response.status_code}")
        return response.json()

    async def close(self) -> None:
        await self._client.aclose()


async def start_external_avatar(ctx: JobContext, config: dict[str, Any]) -> None:
    if not config.get("avatar_enabled"): return
    endpoint = os.getenv("INBOUND_AVATAR_PROVIDER_URL", "").rstrip("/")
    provider_secret = os.getenv("INBOUND_AVATAR_PROVIDER_SECRET", "").strip()
    avatar_id = str(config.get("avatar_id") or "").strip()
    if not endpoint or not provider_secret or not avatar_id:
        logger.warning("Avatar requested but provider configuration is incomplete; continuing audio-only")
        return
    livekit_token = (api.AccessToken(os.getenv("LIVEKIT_API_KEY", ""), os.getenv("LIVEKIT_API_SECRET", "")).with_identity(f"avatar:{uuid.uuid4().hex}").with_name("AI digital avatar").with_grants(api.VideoGrants(room_join=True, room=ctx.room.name, can_publish=True, can_subscribe=True)).with_ttl(timedelta(hours=2)).to_jwt())
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{endpoint}/sessions", headers={"Authorization": f"Bearer {provider_secret}"}, json={"avatar_id": avatar_id, "room_name": ctx.room.name, "livekit_url": os.getenv("LIVEKIT_URL", ""), "livekit_token": livekit_token, "audio_participant_identity": ctx.room.local_participant.identity})
            response.raise_for_status()
        logger.info("External avatar session started for room=%s", ctx.room.name)
    except Exception:
        logger.exception("Avatar provider failed; continuing audio-only")


class InboundVoiceAgent(Agent):
    def __init__(self, instructions: str, *, tools: list[Any] | None = None) -> None:
        super().__init__(instructions=instructions.strip(), tools=tools or [])

    async def on_enter(self) -> None:
        logger.info("Inbound voice agent entered the session")


server = AgentServer(
    port=int(os.getenv("INBOUND_AGENT_PORT", "18082")),
    http_proxy=None,
    load_threshold=float(os.getenv("INBOUND_AGENT_LOAD_THRESHOLD", "0.85")),
)


@server.rtc_session(agent_name=os.getenv("INBOUND_AGENT_NAME", "tenant-voice-agent"))
async def entrypoint(ctx: JobContext) -> None:
    started = monotonic()
    control = InboundControlClient()
    session_id = ""
    final_reason = "participant_disconnected"
    limit_task: asyncio.Task[None] | None = None
    finalized = False
    text_client: AsyncOpenAI | None = None

    async def finalize_once(reason: str = "") -> None:
        nonlocal finalized, final_reason, text_client
        if finalized:
            return
        if reason and final_reason == "participant_disconnected":
            final_reason = reason[:120]
        if limit_task and limit_task is not asyncio.current_task() and not limit_task.done():
            limit_task.cancel()
            try:
                await limit_task
            except asyncio.CancelledError:
                pass
        try:
            await control.complete(
                session_id=session_id,
                duration_seconds=int(monotonic() - started),
                reason=final_reason,
            )
            finalized = True
        except Exception:
            logger.exception("Unable to finalize inbound session")
        if text_client is not None:
            await text_client.close()
            text_client = None
        await control.close()

    ctx.add_shutdown_callback(finalize_once)
    try:
        metadata = str(ctx.job.metadata or "")
        if not metadata or len(metadata) > 4096:
            raise InboundControlError("signed inbound metadata is required")
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        try:
            dispatch = json.loads(metadata)
        except (TypeError, json.JSONDecodeError):
            dispatch = None
        if isinstance(dispatch, dict) and dispatch.get("kind") == "sip_inbound":
            participant = await asyncio.wait_for(
                ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP),
                timeout=20,
            )
            attributes = dict(participant.attributes or {})
            provider_call_id = str(
                attributes.get("sip.callID")
                or attributes.get("sip.callIDFull")
                or f"{ctx.room.name}:{participant.identity}"
            )
            runtime = await control.admit_sip(
                trunk_id=str(attributes.get("sip.trunkID") or ""),
                called_number=str(attributes.get("sip.trunkPhoneNumber") or ""),
                caller_number=str(attributes.get("sip.phoneNumber") or ""),
                room_name=ctx.room.name,
                provider_call_id=provider_call_id,
            )
        else:
            runtime = await control.resolve(metadata=metadata, room_name=ctx.room.name)
        session_id = str(runtime.get("session_id") or "")
        config = runtime["config"]
        project_id = str(runtime.get("project_id") or "")
        knowledge_base_ids = [str(value) for value in config.get("knowledge_sources") or [] if str(value)]
        knowledge_document_ids = [str(value) for value in config.get("knowledge_document_ids") or [] if str(value)]
        configured_tool_ids = [str(value) for value in config.get("tools") or [] if str(value)]
        content_source_ids = [str(value) for value in config.get("content_sources") or [] if str(value)]
        instructions = str(config.get("instructions") or "").strip()
        welcome = str(config.get("welcome_message") or "").strip()
        voice = str(config.get("voice") or "").strip() or None
        max_duration = max(30, min(int(runtime.get("max_duration_seconds") or 180), 7200))
        if len(instructions) < 10:
            raise InboundControlError("published inbound instructions are invalid")

        session = AgentSession(
            llm=QwenAudioRealtimeModel(
                api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
                voice=voice,
            )
        )
        text_client = AsyncOpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
            base_url=os.getenv(
                "QWEN_OPENAI_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
        )
        text_messages: list[dict[str, str]] = [{"role": "system", "content": instructions}]
        text_lock = asyncio.Lock()

        async def handle_vision_stream(reader: Any, _identity: str) -> None:
            if os.getenv("INBOUND_VISION_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
                await ctx.room.local_participant.send_text(json.dumps({"status": "disabled", "message": "视觉能力未启用，已保持纯语音。"}, ensure_ascii=False), topic="inbound.vision.result")
                return
            try:
                payload = json.loads(await reader.read_all())
                image, question = str(payload.get("image") or ""), str(payload.get("question") or "请描述画面中的相关事实。")[:1000]
                if not image.startswith("data:image/jpeg;base64,") or len(image) > 7_000_000: raise ValueError("invalid vision frame")
                completion = await text_client.chat.completions.create(model=os.getenv("INBOUND_VISION_MODEL", "qwen3.5-omni-flash"), messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": image}}, {"type": "text", "text": question}]}], timeout=float(os.getenv("INBOUND_VISION_TIMEOUT_SECONDS", "12")), max_tokens=500)
                observation = (completion.choices[0].message.content or "").strip()
                await ctx.room.local_participant.send_text(json.dumps({"status": "completed", "observation": observation}, ensure_ascii=False), topic="inbound.vision.result")
                session.generate_reply(instructions=f"视觉工具观察到：{observation}\n请结合当前对话简洁回答；不确定时说明并建议人工确认。")
            except Exception:
                logger.exception("Vision frame analysis failed")
                await ctx.room.local_participant.send_text(json.dumps({"status": "failed", "message": "画面识别失败，已继续纯语音服务。"}, ensure_ascii=False), topic="inbound.vision.result")

        ctx.room.register_text_stream_handler("inbound.vision", lambda reader, identity: asyncio.create_task(handle_vision_stream(reader, identity)))

        async def retrieve(query: str) -> list[dict[str, Any]]:
            if not knowledge_base_ids:
                return []
            return await control.search_knowledge(project_id=project_id, knowledge_base_ids=knowledge_base_ids, document_ids=knowledge_document_ids, query=query)

        realtime_tools: list[Any] = []
        if knowledge_base_ids:
            @function_tool(name="search_knowledge")
            async def search_knowledge(query: str) -> str:
                """Search bound enterprise knowledge before answering policy or product questions."""
                items = await retrieve(query)
                return json.dumps({
                    "found": bool(items),
                    "results": [{
                        "content": item.get("content", ""),
                        "citation": {key: item.get(key, "") for key in ("chunk_id", "document_id", "filename", "heading")},
                    } for item in items],
                }, ensure_ascii=False)
            realtime_tools.append(search_knowledge)
        tool_catalog = await control.tool_catalog(project_id=project_id, tool_ids=configured_tool_ids) if configured_tool_ids else []
        if tool_catalog and session_id:
            tool_by_name = {str(item["name"]): item for item in tool_catalog}
            instructions += "\n\n可用业务工具：\n" + "\n".join(f"- {item['name']}: {item['description']}（策略 {item['policy']}）" for item in tool_catalog)

            @function_tool(name="call_business_tool")
            async def call_business_tool(tool_name: str, arguments_json: str) -> str:
                """Call one whitelisted business tool. arguments_json must be a JSON object."""
                tool = tool_by_name.get(tool_name)
                if tool is None: return json.dumps({"status": "denied", "reason": "tool is not in the Agent whitelist"}, ensure_ascii=False)
                try: arguments = json.loads(arguments_json)
                except json.JSONDecodeError: return json.dumps({"status": "invalid_arguments"})
                if not isinstance(arguments, dict): return json.dumps({"status": "invalid_arguments"})
                result = await control.invoke_tool(project_id=project_id, session_id=session_id, tool_id=str(tool["id"]), arguments=arguments, idempotency_key=f"{session_id}:{tool['id']}:{hashlib.sha256(arguments_json.encode()).hexdigest()}")
                if result.get("status") == "confirmation_required":
                    await ctx.room.local_participant.send_text(json.dumps(result, ensure_ascii=False), topic="inbound.tool.confirmation")
                return json.dumps(result, ensure_ascii=False)
            realtime_tools.append(call_business_tool)

            async def handle_tool_confirmation(reader: Any, _identity: str) -> None:
                try:
                    payload = json.loads(await reader.read_all())
                    confirmation_id = str(payload.get("confirmation_id") or "")
                    if not confirmation_id: raise ValueError("confirmation id is required")
                    result = await control.confirm_tool(project_id=project_id, session_id=session_id, confirmation_id=confirmation_id)
                    await ctx.room.local_participant.send_text(json.dumps(result, ensure_ascii=False), topic="inbound.tool.result")
                    session.generate_reply(instructions="客户已经通过会话界面确认业务操作。请根据工具结果简洁告知执行结果。")
                except Exception:
                    logger.exception("Tool confirmation failed")
                    await ctx.room.local_participant.send_text(json.dumps({"status": "failed", "message": "确认未生效，业务操作没有执行。"}, ensure_ascii=False), topic="inbound.tool.result")
            ctx.room.register_text_stream_handler("inbound.tool.confirm", lambda reader, identity: asyncio.create_task(handle_tool_confirmation(reader, identity)))
        if content_source_ids:
            @function_tool(name="show_content")
            async def show_content(asset_id: str) -> str:
                """Show one approved enterprise image, video, PDF, or step card to the customer."""
                if asset_id not in content_source_ids: return json.dumps({"status": "denied"})
                asset = await control.get_content(project_id=project_id, asset_id=asset_id)
                await ctx.room.local_participant.send_text(json.dumps(asset, ensure_ascii=False), topic="inbound.content")
                return json.dumps({"status": "shown", "asset_id": asset_id}, ensure_ascii=False)
            realtime_tools.append(show_content)

        async def handle_text(_session: AgentSession, event: TextInputEvent) -> None:
            text = event.text.strip()
            if not text or len(text) > 4_000:
                return
            async with text_lock:
                text_messages.append({"role": "user", "content": text})
                citations = await retrieve(text)
                request_messages = text_messages[-21:]
                if citations:
                    context = "\n\n".join(
                        f"[来源: {item.get('filename', '')} / {item.get('heading', '') or '正文'}]\n{item.get('content', '')}"
                        for item in citations
                    )
                    request_messages = [*request_messages, {"role": "system", "content": "以下是本轮检索到的企业资料。仅在资料支持时作答，并在答案末尾列出来源文件名：\n" + context}]
                completion = await text_client.chat.completions.create(
                    model=os.getenv("QWEN_TEXT_MODEL", "qwen-plus"),
                    messages=request_messages,
                    temperature=0.3,
                    timeout=20,
                )
                reply = (completion.choices[0].message.content or "").strip()
                if not reply:
                    reply = "抱歉，我暂时没有生成有效回复，请稍后再试。"
                text_messages.append({"role": "assistant", "content": reply})
                await ctx.room.local_participant.send_text(reply, topic="lk.transcription")

        await session.start(
            agent=InboundVoiceAgent(instructions, tools=realtime_tools),
            room=ctx.room,
            room_options=RoomOptions(text_input=TextInputOptions(text_input_cb=handle_text)),
        )
        await start_external_avatar(ctx, config)

        if welcome:
            session.generate_reply(
                instructions=f"请自然、完整并且只说下面这句欢迎语：{welcome}"
            )

        async def enforce_duration_limit() -> None:
            nonlocal final_reason
            await asyncio.sleep(max_duration)
            final_reason = "time_limit"
            try:
                session.generate_reply(
                    instructions="请简短告知体验时间已到，礼貌道别，不要继续提问。"
                )
                await asyncio.sleep(3)
            finally:
                for attempt in range(3):
                    try:
                        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
                        break
                    except Exception:
                        if attempt == 2:
                            logger.exception("Unable to delete time-limited room after retries")
                        else:
                            await asyncio.sleep(0.25 * (attempt + 1))
                await finalize_once("time_limit")
                ctx.shutdown(reason="time_limit")

        limit_task = asyncio.create_task(enforce_duration_limit())

    except Exception:
        final_reason = "admission_failed"
        logger.exception("Inbound session failed closed: room=%s", ctx.room.name)
        try:
            await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
        except Exception:
            logger.exception("Unable to delete rejected inbound room")
        await finalize_once(final_reason)
        ctx.shutdown(reason=final_reason)


if __name__ == "__main__":
    if os.getenv("INBOUND_AGENT_SYSTEM_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        async def disabled_health_server() -> None:
            async def health_response(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                try:
                    await reader.read(4096)
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: 39\r\nConnection: close\r\n\r\n"
                        b'{"status":"disabled","accepting":false}'
                    )
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            listener = await asyncio.start_server(
                health_response, "0.0.0.0", int(os.getenv("INBOUND_AGENT_PORT", "18082"))
            )
            logger.warning("Inbound Agent is disabled; health-only process is running")
            async with listener:
                await listener.serve_forever()

        asyncio.run(disabled_health_server())
    else:
        cli.run_app(server)
