from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from time import monotonic
from typing import Any
import uuid

import httpx
import jwt
from livekit import api, rtc
from livekit.agents import Agent, AgentServer, AgentSession, AutoSubscribe, JobContext, cli
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


class InboundVoiceAgent(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions.strip())

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

        async def handle_text(_session: AgentSession, event: TextInputEvent) -> None:
            text = event.text.strip()
            if not text or len(text) > 4_000:
                return
            async with text_lock:
                text_messages.append({"role": "user", "content": text})
                completion = await text_client.chat.completions.create(
                    model=os.getenv("QWEN_TEXT_MODEL", "qwen-plus"),
                    messages=text_messages[-21:],
                    temperature=0.3,
                    timeout=20,
                )
                reply = (completion.choices[0].message.content or "").strip()
                if not reply:
                    reply = "抱歉，我暂时没有生成有效回复，请稍后再试。"
                text_messages.append({"role": "assistant", "content": reply})
                await ctx.room.local_participant.send_text(reply, topic="lk.transcription")

        await session.start(
            agent=InboundVoiceAgent(instructions),
            room=ctx.room,
            room_options=RoomOptions(text_input=TextInputOptions(text_input_cb=handle_text)),
        )

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
