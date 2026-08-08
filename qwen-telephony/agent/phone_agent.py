from __future__ import annotations

import asyncio
import audioop
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import threading
from time import perf_counter, time_ns
from typing import Any
import uuid
import wave

from dotenv import load_dotenv
from google.protobuf.duration_pb2 import Duration
import httpx
from livekit import api, rtc
from livekit.agents import (
    AMD,
    Agent,
    AgentServer,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RunContext,
    TurnHandlingOptions,
    cli,
    function_tool,
    metrics,
    room_io,
    stt,
    utils,
)
from livekit.agents import llm
from livekit.agents.inference_runner import _InferenceRunner
from livekit.plugins import openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from openai import AsyncOpenAI

from dialogue_llm import ScriptFirstLLM
from qwen_audio_realtime import (
    CLASSIC_PIPELINE,
    DEFAULT_REALTIME_OPENINGS,
    QwenAudioRealtimeModel,
    REALTIME_PIPELINE,
    load_realtime_instructions,
    voice_pipeline,
)
from qwen_providers import QwenASR, QwenRealtimeASR, QwenTTS, register_recorded_audio
from sip_registration import SIPRegistrationError, register_from_env


# Importing livekit.plugins.turn_detector registers both its English and
# multilingual inference runners. This service only constructs
# MultilingualModel, so avoid loading a second, unused English ONNX model in
# every worker process. The multilingual runner remains registered.
_InferenceRunner.registered_runners.pop("lk_end_of_utterance_en", None)


def _provider_env_prefix(provider: str) -> str:
    normalized = provider.strip().upper().replace("-", "_").replace(".", "_")
    if normalized and not re.fullmatch(r"[A-Z0-9_]+", normalized):
        raise ValueError("SIP trunk provider contains unsupported characters")
    return f"QWEN_SIP_{normalized}" if normalized else "QWEN_SIP"


def _provider_dial_target(phone_number: str, provider: str = "") -> str:
    """Translate an E.164 contact number to the carrier's SIP dial string."""
    number = phone_number.strip()
    provider_prefix = _provider_env_prefix(provider)
    prefix = os.getenv(
        f"{provider_prefix}_DIAL_PREFIX", os.getenv("QWEN_SIP_DIAL_PREFIX", "")
    ).strip()
    strip_country_code = os.getenv(
        f"{provider_prefix}_STRIP_COUNTRY_CODE",
        os.getenv("QWEN_SIP_STRIP_COUNTRY_CODE", ""),
    ).strip()
    if not prefix and not strip_country_code:
        return number
    if prefix and not prefix.isdigit():
        raise ValueError("QWEN_SIP_DIAL_PREFIX must contain digits only")
    normalized = number.removeprefix("+")
    if not normalized.isdigit():
        raise ValueError("outbound phone number must be E.164 digits")
    if strip_country_code:
        if not strip_country_code.isdigit():
            raise ValueError("QWEN_SIP_STRIP_COUNTRY_CODE must contain digits only")
        if normalized.startswith(strip_country_code):
            normalized = normalized[len(strip_country_code) :]
    return f"{prefix}{normalized}"


def _provider_registration_env_prefix(provider: str) -> str:
    provider_prefix = _provider_env_prefix(provider) + "_REGISTER"
    if os.getenv(f"{provider_prefix}_ENABLED", "").strip():
        return provider_prefix
    return "QWEN_SIP_REGISTER"


def _registration_refresh_seconds(env_prefix: str, expires: int) -> int:
    """Renew before expiry while honoring an explicit carrier refresh period."""

    configured = os.getenv(f"{env_prefix}_REFRESH_SECONDS", "").strip()
    if configured:
        refresh = int(configured)
        if not 30 <= refresh <= expires:
            raise ValueError(
                f"{env_prefix}_REFRESH_SECONDS must be between 30 and {expires}"
            )
        return refresh
    return max(30, expires - min(60, max(1, expires // 10)))


async def _sip_registration_renewal_loop(env_prefix: str) -> None:
    while True:
        try:
            registration = await asyncio.to_thread(register_from_env, env_prefix)
            if registration is None:
                logger.info("SIP registration keeper disabled: profile=%s", env_prefix)
                return
            refresh_seconds = _registration_refresh_seconds(
                env_prefix, registration.expires
            )
            logger.info(
                "SIP registration renewed: profile=%s status=%s realm=%s "
                "expires=%ss next_refresh=%ss",
                env_prefix,
                registration.status_code,
                registration.realm,
                registration.expires,
                refresh_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SIP registration renewal failed: profile=%s", env_prefix)
            refresh_seconds = min(
                60,
                max(
                    5,
                    int(os.getenv(f"{env_prefix}_RETRY_SECONDS", "15")),
                ),
            )
        await asyncio.sleep(refresh_seconds)


def _outbound_sip_media_config() -> api.SIPMediaConfig:
    """End a call promptly once the carrier RTP stream disappears."""

    return api.SIPMediaConfig(
        codecs=[api.SIPCodec(name="PCMU", rate=8_000)],
        only_listed_codecs=True,
        media_timeout=_bounded_duration(
            "QWEN_SIP_MEDIA_TIMEOUT_SECONDS",
            5,
            minimum=3,
            maximum=600,
        )
    )


def _customer_hangup_only(job: dict[str, Any] | None) -> bool:
    """Whether a normal outbound conversation must wait for the callee to hang up."""

    return bool(job and str(job.get("direction") or "") == "outbound") and os.getenv(
        "QWEN_OUTBOUND_CUSTOMER_HANGUP_ONLY", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}


ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "qwen-telephony" / "config" / "local.env", override=False)

logger = logging.getLogger("qwen-phone-agent")
_telephony_control_client: httpx.AsyncClient | None = None

GREETING_TEXT = os.getenv(
    "QWEN_GREETING_TEXT",
    "您好，我是智能语音助手，很高兴为您服务。请问有什么可以帮助您？",
).strip()
GREETING_AUDIO_PATH = ROOT / "qwen-telephony" / "cache" / "greeting.wav"
GREETING_ROOM_AUDIO_PATH = ROOT / "qwen-telephony" / "cache" / "greeting_24k.wav"
GREETING_AUDIO_LOCK_PATH = ROOT / "qwen-telephony" / "cache" / "greeting.wav.lock"
ROOM_AUDIO_SAMPLE_RATE = int(os.getenv("QWEN_ROOM_AUDIO_SAMPLE_RATE", str(QwenTTS.sample_rate_hz)))
FINAL_GOODBYE_TEXT = "感谢您的时间，再见。"
WECHAT_ADDED_NOTICE_TEXT = "好的，已经加您了，请您通过一下。"
_REALTIME_FIXED_AUDIO: dict[str, bytes] = {}
_REALTIME_SCENE_CACHE: dict[int, dict[str, Any]] = {}
_REALTIME_SCENE_CACHE_UPDATED_AT: dict[int, float] = {}


def _normalize_wav_bytes(audio_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(audio_bytes), "rb") as reader:
        num_channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        pcm = reader.readframes(reader.getnframes())

    if not pcm:
        raise ValueError("greeting wav contains no audio frames")

    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(num_channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    return output.getvalue()


def _wav_cache_is_valid(
    path: Path,
    *,
    sample_rate: int,
    min_duration: float = 0.2,
    max_duration: float = 15.0,
) -> bool:
    if not path.exists() or path.stat().st_size <= 44:
        return False

    try:
        with wave.open(str(path), "rb") as reader:
            duration = reader.getnframes() / reader.getframerate()
            return (
                reader.getnchannels() == QwenTTS.num_channels_count
                and reader.getsampwidth() == 2
                and reader.getframerate() == sample_rate
                and min_duration <= duration <= max_duration
            )
    except (EOFError, wave.Error, OSError, ZeroDivisionError):
        return False


def _is_valid_greeting_audio_cache() -> bool:
    return _wav_cache_is_valid(GREETING_AUDIO_PATH, sample_rate=QwenTTS.sample_rate_hz)


def _is_valid_room_greeting_audio_cache() -> bool:
    return _wav_cache_is_valid(
        GREETING_ROOM_AUDIO_PATH,
        sample_rate=ROOM_AUDIO_SAMPLE_RATE,
    )


def _convert_wav_to_sample_rate(audio_bytes: bytes, sample_rate: int) -> bytes:
    with wave.open(io.BytesIO(audio_bytes), "rb") as reader:
        num_channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        input_rate = reader.getframerate()
        pcm = reader.readframes(reader.getnframes())

    if not pcm:
        raise ValueError("greeting wav contains no audio frames")

    if input_rate != sample_rate:
        pcm, _ = audioop.ratecv(pcm, sample_width, num_channels, input_rate, sample_rate, None)

    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(num_channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    return output.getvalue()


def _prepare_wav_for_room_playback(audio_bytes: bytes) -> bytes:
    normalized = _normalize_wav_bytes(audio_bytes)
    return _convert_wav_to_sample_rate(normalized, ROOM_AUDIO_SAMPLE_RATE)


def _select_realtime_opening(scene: dict[str, Any] | None) -> str:
    configured = os.getenv("QWEN_REALTIME_OPENING_TEXT", "").strip()
    if configured:
        return configured
    if not scene:
        return secrets.choice(DEFAULT_REALTIME_OPENINGS)

    flow = scene.get("flow") if isinstance(scene.get("flow"), dict) else {}
    nodes = flow.get("nodes") if isinstance(flow.get("nodes"), list) else []
    entry_id = str(flow.get("entry_node") or "").strip()
    if entry_id == "rider_opening":
        return secrets.choice(DEFAULT_REALTIME_OPENINGS)
    entry = next(
        (
            node
            for node in nodes
            if isinstance(node, dict) and str(node.get("id") or "") == entry_id
        ),
        None,
    )
    opening = str((entry or {}).get("text") or "").strip()
    return opening or DEFAULT_REALTIME_OPENINGS[0]


def _outbound_identity_opening(customer_name: str) -> str:
    """Build the deterministic first sentence for a managed outbound task."""

    normalized = re.sub(r"\s+", "", customer_name).strip("，。！？? ")
    if not normalized or "{{" in normalized or "}}" in normalized:
        return "您好，我是李宝祥的智能助理，请问怎么称呼您？"
    return f"您好，我是李宝祥的智能助理，请问您是{normalized}吗？"


def _set_session_audio_io_enabled(session: AgentSession, enabled: bool) -> None:
    """Gate Realtime media while deterministic audio owns the opening."""

    if session.input.audio:
        session.input.set_audio_enabled(enabled)
    if session.output.audio:
        session.output.set_audio_enabled(enabled)


def _initial_realtime_chat_context(
    *,
    selected_pipeline: str,
    realtime_opening: str,
    task_prompt_override: str,
) -> llm.ChatContext | None:
    if selected_pipeline != REALTIME_PIPELINE:
        return None
    if realtime_opening:
        # Seed the exact opening before Realtime starts accepting caller audio.
        # The fixed audio is media transport only; Qwen needs the corresponding
        # assistant turn, not a copy of the WAV bytes.
        chat_ctx = llm.ChatContext.empty()
        chat_ctx.add_message(role="assistant", content=realtime_opening)
        return chat_ctx
    # Managed task calls use a deterministic, pre-synthesized identity
    # question. Do not inject a fake user turn: Qwen can reject or time out
    # while synchronizing that context before any real customer audio exists.
    return None


def _append_local_transcript(
    *, room_name: str, role: str, text: str, item_id: str = "", source: str = "realtime"
) -> None:
    directory = os.getenv("QWEN_REALTIME_TRANSCRIPT_DIR", "").strip()
    if not directory or role not in {"user", "assistant"} or not text.strip():
        return
    safe_room = re.sub(r"[^A-Za-z0-9_.-]+", "_", room_name).strip("._") or "unknown-room"
    target_dir = Path(directory).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp_ns": time_ns(),
        "room": room_name,
        "role": role,
        "text": text.strip(),
        "item_id": item_id,
        "source": source,
    }
    with (target_dir / f"{safe_room}.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False) + "\n")


def _is_wechat_added_notice(text: str) -> bool:
    compact = re.sub(r"[\s，。！？、,.!?]", "", text)
    return "已经加您了" in compact and "请您通过" in compact


def _is_short_wechat_acknowledgement(text: str) -> bool:
    compact = re.sub(r"[\s，。！？、,.!?]", "", text)
    return compact in {
        "好",
        "好的",
        "嗯好",
        "嗯好的",
        "行",
        "可以",
        "知道了",
        "没问题",
        "会通过",
        "我会通过",
    }


def _is_customer_goodbye(text: str) -> bool:
    """Match explicit customer endings without treating incidental prose as hangup."""

    compact = re.sub(r"[\s，。！？、,.!?]", "", text)
    return compact in {
        "再见",
        "好再见",
        "好的再见",
        "嗯再见",
        "那再见",
        "行再见",
        "拜拜",
        "好拜拜",
        "好的拜拜",
        "先这样再见",
        "挂了",
        "先挂了",
        "我先挂了",
    }


def _normalized_spoken_text(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?]", "", str(text or "")).lower()


def _function_call_requests_wechat_notice(function_call: Any) -> bool:
    return str(getattr(function_call, "name", "")) == "complete_wechat_followup"


def _programmatic_audio_tool_names(event: Any) -> set[str]:
    return {
        str(getattr(call, "name", ""))
        for call in getattr(event, "function_calls", [])
        if str(getattr(call, "name", ""))
        in {"complete_wechat_followup", "end_call"}
    }


def _cancel_tool_reply_for_programmatic_audio(event: Any) -> set[str]:
    names = _programmatic_audio_tool_names(event)
    if not names:
        return set()
    event.cancel_tool_reply()
    return names


def _programmatic_audio_action(tool_names: set[str]) -> str | None:
    """Choose one deterministic transition when a model batches tool calls.

    A confirmed WeChat follow-up must always be announced before a terminal
    goodbye. Qwen can return multiple function calls in a single response, so
    the transition order cannot depend on the order of those calls.
    """

    if "complete_wechat_followup" in tool_names:
        return "wechat_notice"
    if "end_call" in tool_names:
        return "final_goodbye"
    return None


def _start_single_flight_task(
    task: asyncio.Task[Any] | None,
    coroutine_factory: Any,
    *,
    name: str,
) -> asyncio.Task[Any]:
    """Start a call-lifetime action once, even after its task has completed."""

    if task is None:
        return asyncio.create_task(coroutine_factory(), name=name)
    return task


def _finish_wechat_notice_playout(
    *,
    awaiting_acknowledgement: bool,
    start_close_timer: Any,
) -> None:
    """Start the silence clock immediately after remote-bound media drains."""

    if awaiting_acknowledgement:
        start_close_timer()


def _sip_call_status(participant: Any) -> str:
    return str((getattr(participant, "attributes", {}) or {}).get("sip.callStatus", "")).lower()


def _active_sip_participant(room: rtc.Room) -> rtc.RemoteParticipant | None:
    for participant in room.remote_participants.values():
        if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            continue
        if _sip_call_status(participant) in {"active", "answered", "established"}:
            return participant
    return None


async def _wait_for_active_sip_participant(
    room: rtc.Room,
    *,
    timeout: float,
) -> rtc.RemoteParticipant:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if participant := _active_sip_participant(room):
            return participant
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("SIP participant did not become active before opening")
        await asyncio.sleep(0.02)


async def ensure_realtime_fixed_audio_cache() -> None:
    """Pre-generate deterministic Realtime phrases before any call is assigned."""

    configured_opening = os.getenv("QWEN_REALTIME_OPENING_TEXT", "").strip()
    required_texts = (
        *DEFAULT_REALTIME_OPENINGS,
        *((configured_opening,) if configured_opening else ()),
        WECHAT_ADDED_NOTICE_TEXT,
        FINAL_GOODBYE_TEXT,
    )
    missing = [text for text in required_texts if text not in _REALTIME_FIXED_AUDIO]
    if not missing:
        return

    qwen_tts = QwenTTS()
    try:
        for text in missing:
            audio_bytes, _, _ = await qwen_tts.synthesize_audio_bytes(text)
            _REALTIME_FIXED_AUDIO[text] = _prepare_wav_for_room_playback(audio_bytes)
        logger.info("Realtime fixed audio cache ready: phrases=%d", len(_REALTIME_FIXED_AUDIO))
    finally:
        await qwen_tts.aclose()


async def _audio_frames_from_wav_bytes(audio_bytes: bytes, *, label: str):
    started = perf_counter()
    frame_count = 0
    audio_duration = 0.0
    with wave.open(io.BytesIO(audio_bytes), "rb") as reader:
        sample_rate = reader.getframerate()
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        if (
            sample_rate != ROOM_AUDIO_SAMPLE_RATE
            or channels != QwenTTS.num_channels_count
            or sample_width != 2
        ):
            raise ValueError(f"{label} wav has unexpected audio parameters")

        samples_per_frame = sample_rate // 50
        while True:
            pcm = reader.readframes(samples_per_frame)
            if not pcm:
                break
            samples = len(pcm) // (sample_width * channels)
            if frame_count == 0:
                logger.info(
                    "fixed_audio_first_frame_ready label=%s elapsed=%.3fs",
                    label,
                    perf_counter() - started,
                )
            frame_count += 1
            audio_duration += samples / sample_rate
            yield rtc.AudioFrame(pcm, sample_rate, channels, samples)

    logger.info(
        "fixed_audio_source_exhausted label=%s frames=%d "
        "audio_duration=%.3fs elapsed=%.3fs",
        label,
        frame_count,
        audio_duration,
        perf_counter() - started,
    )


async def _play_wav_bytes_direct(
    room: rtc.Room,
    audio_bytes: bytes,
    *,
    label: str,
    track_name: str,
    tail_silence_ms: int = 0,
    on_first_frame_queued: Any = None,
) -> float:
    """Play a fixed phrase outside AgentSession so Realtime VAD cannot interrupt it."""

    started = perf_counter()
    source = rtc.AudioSource(
        ROOM_AUDIO_SAMPLE_RATE,
        QwenTTS.num_channels_count,
        queue_size_ms=5000,
    )
    track = rtc.LocalAudioTrack.create_audio_track(track_name, source)
    publication = await room.local_participant.publish_track(track)
    frame_count = 0
    audio_duration = 0.0
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as reader:
            sample_rate = reader.getframerate()
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            if (
                sample_rate != ROOM_AUDIO_SAMPLE_RATE
                or channels != QwenTTS.num_channels_count
                or sample_width != 2
            ):
                raise ValueError(f"{label} wav has unexpected audio parameters")

            samples_per_frame = sample_rate // 50
            while True:
                pcm = reader.readframes(samples_per_frame)
                if not pcm:
                    break
                samples = len(pcm) // (sample_width * channels)
                is_first_frame = frame_count == 0
                await source.capture_frame(
                    rtc.AudioFrame(pcm, sample_rate, channels, samples)
                )
                if is_first_frame:
                    logger.info(
                        "%s direct playback first frame queued in %.3fs",
                        label,
                        perf_counter() - started,
                    )
                    if on_first_frame_queued is not None:
                        try:
                            on_first_frame_queued()
                        except Exception:
                            logger.exception(
                                "%s first-frame callback failed", label
                            )
                frame_count += 1
                audio_duration += samples / sample_rate

        # Keep the RTP track alive with actual media after the spoken phrase.
        # Waiting after unpublishing the track does not protect carrier-side
        # jitter buffers from losing the last spoken packet.
        tail_samples = max(0, tail_silence_ms) * ROOM_AUDIO_SAMPLE_RATE // 1000
        remaining_tail_samples = tail_samples
        bytes_per_sample = 2 * QwenTTS.num_channels_count
        while remaining_tail_samples > 0:
            samples = min(ROOM_AUDIO_SAMPLE_RATE // 50, remaining_tail_samples)
            await source.capture_frame(
                rtc.AudioFrame(
                    b"\x00" * samples * bytes_per_sample,
                    ROOM_AUDIO_SAMPLE_RATE,
                    QwenTTS.num_channels_count,
                    samples,
                )
            )
            remaining_tail_samples -= samples

        await source.wait_for_playout()
        logger.info(
            "%s direct playback completed: frames=%d audio_duration=%.3fs "
            "tail_silence_ms=%d elapsed=%.3fs",
            label,
            frame_count,
            audio_duration,
            max(0, tail_silence_ms),
            perf_counter() - started,
        )
        return audio_duration
    finally:
        sid = getattr(publication, "sid", "")
        if sid:
            await room.local_participant.unpublish_track(sid)
        await source.aclose()


def _wav_pcm_sha256(audio_bytes: bytes) -> str:
    with wave.open(io.BytesIO(audio_bytes), "rb") as reader:
        return hashlib.sha256(reader.readframes(reader.getnframes())).hexdigest()


def _write_audio_ab_mapping(
    *,
    room_name: str,
    source: Path,
    prepared_audio: bytes,
    pcm_sha256: str,
    mapping: dict[str, str],
) -> Path:
    configured_dir = os.getenv("QWEN_REALTIME_AB_RESULT_DIR", "").strip()
    target_dir = (
        Path(configured_dir).expanduser()
        if configured_dir
        else ROOT / "qwen-telephony" / "data" / "ab-tests"
    )
    safe_room = re.sub(r"[^A-Za-z0-9_.-]+", "_", room_name).strip("._") or "unknown-room"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{safe_room}.mapping.json"
    with wave.open(io.BytesIO(prepared_audio), "rb") as reader:
        payload = {
            "room": room_name,
            "source_file": str(source.resolve()),
            "source_file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "prepared_wav_sha256": hashlib.sha256(prepared_audio).hexdigest(),
            "pcm_sha256": pcm_sha256,
            "sample_rate": reader.getframerate(),
            "channels": reader.getnchannels(),
            "sample_width_bytes": reader.getsampwidth(),
            "pcm_frames": reader.getnframes(),
            "mapping": mapping,
            "blinded": True,
            "created_at_ns": time_ns(),
        }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("Blind A/B mapping saved: room=%s path=%s", room_name, target)
    return target


async def _play_wav_bytes_via_agent_output(
    audio_output: Any,
    audio_bytes: bytes,
    *,
    label: str,
) -> float:
    """Feed PCM through the exact AgentSession output used by Qwen Realtime."""

    started = perf_counter()
    audio_duration = 0.0
    frame_count = 0
    async for frame in _audio_frames_from_wav_bytes(audio_bytes, label=label):
        await audio_output.capture_frame(frame)
        audio_duration += frame.duration
        frame_count += 1
    audio_output.flush()
    playback = await audio_output.wait_for_playout()
    if getattr(playback, "interrupted", False):
        raise RuntimeError(f"{label} playback was interrupted")
    logger.info(
        "%s AgentSession playback completed: frames=%d audio_duration=%.3fs "
        "elapsed=%.3fs",
        label,
        frame_count,
        audio_duration,
        perf_counter() - started,
    )
    return audio_duration


def _ensure_room_greeting_audio_cache() -> bool:
    if _is_valid_room_greeting_audio_cache():
        return True
    if not _is_valid_greeting_audio_cache() and not _repair_greeting_audio_cache():
        return False

    try:
        GREETING_ROOM_AUDIO_PATH.write_bytes(_prepare_wav_for_room_playback(GREETING_AUDIO_PATH.read_bytes()))
        logger.info(
            "Greeting room audio cache generated: %s",
            GREETING_ROOM_AUDIO_PATH,
        )
        return _is_valid_room_greeting_audio_cache()
    except Exception:
        logger.exception("Greeting room audio cache generation failed")
        GREETING_ROOM_AUDIO_PATH.unlink(missing_ok=True)
        return False


def _repair_greeting_audio_cache() -> bool:
    if not GREETING_AUDIO_PATH.exists() or GREETING_AUDIO_PATH.stat().st_size <= 44:
        return False

    try:
        normalized = _normalize_wav_bytes(GREETING_AUDIO_PATH.read_bytes())
        GREETING_AUDIO_PATH.write_bytes(normalized)
        logger.info("Greeting audio cache WAV header repaired: %s", GREETING_AUDIO_PATH)
        return _is_valid_greeting_audio_cache()
    except Exception:
        logger.exception("Greeting audio cache repair failed, regenerating")
        GREETING_AUDIO_PATH.unlink(missing_ok=True)
        return False


async def ensure_greeting_audio_cache() -> None:
    if (_is_valid_greeting_audio_cache() or _repair_greeting_audio_cache()) and _ensure_room_greeting_audio_cache():
        return

    GREETING_AUDIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd: int | None = None
    for _ in range(100):
        try:
            lock_fd = os.open(
                GREETING_AUDIO_LOCK_PATH,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            break
        except FileExistsError:
            if (_is_valid_greeting_audio_cache() or _repair_greeting_audio_cache()) and _ensure_room_greeting_audio_cache():
                return
            await asyncio.sleep(0.1)

    if lock_fd is None:
        logger.warning("Greeting audio cache lock timeout, skipping prewarm")
        return

    started = perf_counter()
    qwen_tts = QwenTTS()
    try:
        if (_is_valid_greeting_audio_cache() or _repair_greeting_audio_cache()) and _ensure_room_greeting_audio_cache():
            return
        audio_bytes, _, _ = await qwen_tts.synthesize_audio_bytes(GREETING_TEXT)
        normalized = _normalize_wav_bytes(audio_bytes)
        GREETING_AUDIO_PATH.write_bytes(normalized)
        GREETING_ROOM_AUDIO_PATH.write_bytes(_prepare_wav_for_room_playback(normalized))
        logger.info(
            "Greeting audio cache generated in %.2fs: %s",
            perf_counter() - started,
            GREETING_AUDIO_PATH,
        )
    finally:
        await qwen_tts.aclose()
        os.close(lock_fd)
        GREETING_AUDIO_LOCK_PATH.unlink(missing_ok=True)


def prewarm_process(proc: JobProcess) -> None:
    # Each LiveKit job process owns one preloaded VAD model. Streams created by
    # AgentSession and StreamAdapter keep their own state, while the ONNX model
    # is loaded only once per process.
    proc.userdata["vad"] = _build_session_vad()
    async def prewarm_audio() -> None:
        await ensure_greeting_audio_cache()
        await ensure_realtime_fixed_audio_cache()

    try:
        asyncio.run(prewarm_audio())
    except Exception:
        logger.exception("Fixed audio cache prewarm failed")


async def _logged_audio_frames_from_file(file_path: str):
    started = perf_counter()
    playout_seconds = 0.0
    frame_count = 0
    async for frame in utils.audio.audio_frames_from_file(
        file_path,
        sample_rate=ROOM_AUDIO_SAMPLE_RATE,
        num_channels=QwenTTS.num_channels_count,
    ):
        if frame_count == 0:
            logger.info(
                "Greeting audio first frame decoded in %.3fs",
                perf_counter() - started,
            )
        frame_count += 1
        yield frame
        playout_seconds += frame.samples_per_channel / frame.sample_rate
        sleep_for = started + playout_seconds - perf_counter()
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

    logger.info(
        "Greeting audio file decoded: frames=%d audio_duration=%.3fs elapsed=%.3fs",
        frame_count,
        playout_seconds,
        perf_counter() - started,
    )


async def play_greeting_audio_direct(room: rtc.Room) -> None:
    if not _ensure_room_greeting_audio_cache():
        logger.warning("Greeting direct playback skipped: room audio cache missing")
        return

    started = perf_counter()
    source = rtc.AudioSource(
        ROOM_AUDIO_SAMPLE_RATE,
        QwenTTS.num_channels_count,
        queue_size_ms=5000,
    )
    track = rtc.LocalAudioTrack.create_audio_track("greeting-audio", source)
    publication = await room.local_participant.publish_track(track)

    frame_count = 0
    audio_duration = 0.0
    try:
        with wave.open(str(GREETING_ROOM_AUDIO_PATH), "rb") as reader:
            sample_rate = reader.getframerate()
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            if sample_rate != ROOM_AUDIO_SAMPLE_RATE or channels != QwenTTS.num_channels_count or sample_width != 2:
                raise ValueError("room greeting wav has unexpected audio parameters")

            samples_per_frame = sample_rate // 50
            while True:
                pcm = reader.readframes(samples_per_frame)
                if not pcm:
                    break
                samples = len(pcm) // (sample_width * channels)
                frame = rtc.AudioFrame(
                    pcm,
                    sample_rate,
                    channels,
                    samples,
                )
                if frame_count == 0:
                    logger.info(
                        "Greeting direct playback first frame queued in %.3fs",
                        perf_counter() - started,
                    )
                frame_count += 1
                audio_duration += samples / sample_rate
                await source.capture_frame(frame)

        await source.wait_for_playout()
        logger.info(
            "Greeting direct playback completed: frames=%d audio_duration=%.3fs elapsed=%.3fs",
            frame_count,
            audio_duration,
            perf_counter() - started,
        )
    finally:
        sid = getattr(publication, "sid", "")
        if sid:
            await room.local_participant.unpublish_track(sid)
        await source.aclose()


async def fetch_dialogue_opening(session_id: str) -> dict | None:
    if os.getenv("QWEN_NLU_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return None

    scene_id = int(os.getenv("QWEN_DIALOGUE_SCENE_ID", "0")) or None
    turn_url = os.getenv("QWEN_DIALOGUE_URL", "http://127.0.0.1:8090/api/dialogue/turn")
    start_url = turn_url.rsplit("/", 1)[0] + "/start"
    payload: dict[str, object] = {"session_id": session_id}
    if scene_id:
        payload["scene_id"] = scene_id

    try:
        async with httpx.AsyncClient(timeout=float(os.getenv("QWEN_DIALOGUE_TIMEOUT", "0.8"))) as client:
            response = await client.post(
                start_url,
                headers=_telephony_control_headers(),
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
    except Exception:
        logger.exception("Dialogue opening fetch failed, using static greeting")
        return None

    if body.get("handled") and body.get("text"):
        logger.info(
            "Dialogue opening selected: scene=%s node=%s",
            body.get("scene_id"),
            body.get("next_node_id"),
        )
        audio = body.get("audio") if isinstance(body.get("audio"), dict) else {}
        audio_url = str(body.get("audio_url") or audio.get("url") or "").strip()
        if audio_url:
            base_url = turn_url.split("/api/", 1)[0] + "/"
            register_recorded_audio(str(body["text"]), audio_url, base_url)
        return body
    logger.info("Dialogue opening unavailable, route=%s reason=%s", body.get("route_type"), body.get("reason"))
    return None


async def fetch_realtime_scene(scene_id: int | None) -> dict[str, Any] | None:
    """Load the published front-end dialogue definition for Realtime prompting."""
    if not scene_id:
        return None
    turn_url = os.getenv("QWEN_DIALOGUE_URL", "http://127.0.0.1:8090/api/dialogue/turn")
    base_url = turn_url.split("/api/", 1)[0]
    try:
        async with httpx.AsyncClient(timeout=float(os.getenv("QWEN_DIALOGUE_TIMEOUT", "0.8"))) as client:
            response = await client.get(
                f"{base_url}/api/dialogue/scenes/{scene_id}",
                headers=_telephony_control_headers(),
            )
            response.raise_for_status()
            body = response.json()
            if isinstance(body, dict):
                _REALTIME_SCENE_CACHE[scene_id] = body
                _REALTIME_SCENE_CACHE_UPDATED_AT[scene_id] = perf_counter()
                return body
            return None
    except Exception:
        logger.exception("Realtime scene fetch failed; using default prompt: scene=%s", scene_id)
        return None


def _start_realtime_scene_fetch(
    scene_id: int | None,
) -> tuple[dict[str, Any] | None, asyncio.Task[dict[str, Any] | None] | None]:
    if not scene_id:
        return None, None
    cached = _REALTIME_SCENE_CACHE.get(scene_id)
    cached_at = _REALTIME_SCENE_CACHE_UPDATED_AT.get(scene_id)
    cache_ttl = _configured_float(
        "QWEN_REALTIME_SCENE_CACHE_TTL_SECONDS",
        30.0,
        minimum=1.0,
        maximum=3600.0,
    )
    if (
        cached is not None
        and cached_at is not None
        and perf_counter() - cached_at <= cache_ttl
    ):
        return cached, None
    if cached is not None:
        logger.info("Realtime scene cache expired; refreshing scene=%s", scene_id)
    return cached, asyncio.create_task(
        fetch_realtime_scene(scene_id), name=f"realtime-scene-{scene_id}"
    )


async def play_text_audio_direct(room: rtc.Room, text: str) -> None:
    started = perf_counter()
    qwen_tts = QwenTTS()
    try:
        audio_bytes, _, _ = await qwen_tts.synthesize_audio_bytes(text)
        room_audio = _prepare_wav_for_room_playback(audio_bytes)
    finally:
        await qwen_tts.aclose()

    source = rtc.AudioSource(
        ROOM_AUDIO_SAMPLE_RATE,
        QwenTTS.num_channels_count,
        queue_size_ms=5000,
    )
    track = rtc.LocalAudioTrack.create_audio_track("dialogue-opening-audio", source)
    publication = await room.local_participant.publish_track(track)

    frame_count = 0
    audio_duration = 0.0
    try:
        with wave.open(io.BytesIO(room_audio), "rb") as reader:
            sample_rate = reader.getframerate()
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            samples_per_frame = sample_rate // 50
            while True:
                pcm = reader.readframes(samples_per_frame)
                if not pcm:
                    break
                samples = len(pcm) // (sample_width * channels)
                frame = rtc.AudioFrame(pcm, sample_rate, channels, samples)
                frame_count += 1
                audio_duration += samples / sample_rate
                await source.capture_frame(frame)

        await source.wait_for_playout()
        logger.info(
            "Dialogue opening playback completed: frames=%d audio_duration=%.3fs elapsed=%.3fs",
            frame_count,
            audio_duration,
            perf_counter() - started,
        )
    finally:
        sid = getattr(publication, "sid", "")
        if sid:
            await room.local_participant.unpublish_track(sid)
        await source.aclose()


async def warm_up_qwen_llm_after_greeting() -> None:
    await warm_up_qwen_llm()


def start_llm_warmup_background_thread() -> None:
    if os.getenv("QWEN_LLM_WARMUP", "true").lower() not in {"1", "true", "yes", "on"}:
        return

    thread = threading.Thread(
        target=lambda: asyncio.run(warm_up_qwen_llm_after_greeting()),
        name="qwen-llm-warmup",
        daemon=True,
    )
    thread.start()


def _livekit_http_url() -> str:
    url = os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880")
    if url.startswith("wss://"):
        return "https://" + url[len("wss://") :]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://") :]
    return url


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _configured_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw_value = os.getenv(name)
    try:
        value = default if raw_value is None or not raw_value.strip() else float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _configured_int(name: str, default: int, *, allowed: set[int]) -> int:
    raw_value = os.getenv(name)
    try:
        value = default if raw_value is None or not raw_value.strip() else int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value not in allowed:
        choices = ", ".join(str(item) for item in sorted(allowed))
        raise ValueError(f"{name} must be one of: {choices}")
    return value


def _build_session_vad(*, loader: Any | None = None) -> Any:
    sample_rate = _configured_int(
        "QWEN_VAD_SAMPLE_RATE", 8000, allowed={8000, 16000}
    )

    vad_loader = loader or silero.VAD.load
    return vad_loader(
        min_speech_duration=_configured_float(
            "QWEN_VAD_MIN_SPEECH_SECONDS", 0.05, minimum=0.01, maximum=5.0
        ),
        min_silence_duration=_configured_float(
            "QWEN_VAD_MIN_SILENCE_SECONDS", 0.55, minimum=0.05, maximum=5.0
        ),
        prefix_padding_duration=_configured_float(
            "QWEN_VAD_PREFIX_PADDING_SECONDS", 0.2, minimum=0.0, maximum=5.0
        ),
        activation_threshold=_configured_float(
            "QWEN_VAD_ACTIVATION_THRESHOLD", 0.45, minimum=0.0, maximum=1.0
        ),
        sample_rate=sample_rate,
    )


def _turn_detection_mode() -> str:
    mode = os.getenv("QWEN_TURN_DETECTION_MODE", "multilingual").strip().lower()
    if mode == "text":
        mode = "multilingual"
    if mode not in {"multilingual", "vad"}:
        raise ValueError(
            "QWEN_TURN_DETECTION_MODE must be 'multilingual' (or 'text') or 'vad'"
        )
    return mode


def _build_turn_detector(*, model_factory: Any | None = None) -> Any:
    if _turn_detection_mode() == "vad":
        return "vad"

    # The official plugin switches to an HTTP service when this variable is
    # present. This project intentionally guarantees local inference so call
    # transcripts cannot be sent to an unexpected endpoint.
    if os.getenv("LIVEKIT_REMOTE_EOT_URL", "").strip():
        raise ValueError(
            "LIVEKIT_REMOTE_EOT_URL must be unset when using the local multilingual "
            "turn detector"
        )

    raw_threshold = os.getenv("QWEN_TURN_DETECTOR_THRESHOLD", "").strip()
    threshold = (
        _configured_float(
            "QWEN_TURN_DETECTOR_THRESHOLD", 0.5, minimum=0.0, maximum=1.0
        )
        if raw_threshold
        else None
    )
    if model_factory is None:
        model_factory = MultilingualModel
    return model_factory(unlikely_threshold=threshold)


def _turn_endpointing_options() -> dict[str, str | float]:
    mode = os.getenv("QWEN_ENDPOINTING_MODE", "dynamic").strip().lower()
    if mode not in {"fixed", "dynamic"}:
        raise ValueError("QWEN_ENDPOINTING_MODE must be 'fixed' or 'dynamic'")
    minimum_delay = _configured_float(
        "QWEN_ENDPOINTING_MIN_DELAY", 0.5, minimum=0.05, maximum=10.0
    )
    maximum_delay = _configured_float(
        "QWEN_ENDPOINTING_MAX_DELAY", 3.0, minimum=0.05, maximum=15.0
    )
    if minimum_delay > maximum_delay:
        raise ValueError(
            "QWEN_ENDPOINTING_MIN_DELAY must not exceed QWEN_ENDPOINTING_MAX_DELAY"
        )

    options: dict[str, str | float] = {
        "mode": mode,
        "min_delay": minimum_delay,
        "max_delay": maximum_delay,
    }
    if mode == "dynamic":
        options["alpha"] = _configured_float(
            "QWEN_ENDPOINTING_ALPHA", 0.9, minimum=0.0, maximum=1.0
        )
    return options


def _bounded_duration(name: str, default: int, *, minimum: int, maximum: int) -> Duration:
    seconds = max(minimum, min(_env_int(name, default), maximum))
    return Duration(seconds=seconds)


def _dialogue_audio_path(audio_url: str) -> Path | None:
    if not audio_url.startswith("/static/dialogue-audio/"):
        return None
    filename = Path(audio_url).name
    return ROOT / "qwen-telephony" / "server" / "static" / "dialogue-audio" / filename


def _wav_duration_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as reader:
            rate = reader.getframerate()
            if rate <= 0:
                return None
            return reader.getnframes() / rate
    except (wave.Error, OSError, EOFError):
        return None


def estimate_audio_duration_seconds(text: str, audio_url: str = "") -> float:
    path = _dialogue_audio_path(audio_url)
    if path and path.exists():
        duration = _wav_duration_seconds(path)
        if duration:
            return duration

    chars_per_second = max(1.0, _env_float("QWEN_INTERRUPT_ESTIMATE_CHARS_PER_SECOND", 4.5))
    return max(1.0, len(text.strip()) / chars_per_second)


def interrupt_gate_percent(result: dict[str, Any]) -> float:
    interrupt = result.get("interrupt") if isinstance(result.get("interrupt"), dict) else {}
    raw_percent = interrupt.get("allow_after_percent") or interrupt.get("allowAfterPercent")
    if raw_percent is None:
        raw_percent = os.getenv("QWEN_RECORDED_AUDIO_INTERRUPT_AFTER_PERCENT", "50")
    try:
        percent = float(raw_percent)
    except (TypeError, ValueError):
        percent = 50.0
    return min(100.0, max(0.0, percent))


def schedule_interrupt_gate(speech_handle: Any, *, text: str, audio_url: str, result: dict[str, Any]) -> None:
    percent = interrupt_gate_percent(result)
    if percent <= 0:
        speech_handle.allow_interruptions = True
        return
    if percent >= 100:
        speech_handle.allow_interruptions = False
        return

    duration = estimate_audio_duration_seconds(text, audio_url)
    delay_seconds = max(0.0, duration * percent / 100.0)
    speech_handle.allow_interruptions = False
    logger.info(
        "Recorded audio interruption gated: speech=%s percent=%.1f delay=%.2fs duration=%.2fs",
        getattr(speech_handle, "id", ""),
        percent,
        delay_seconds,
        duration,
    )

    async def _enable_later() -> None:
        await asyncio.sleep(delay_seconds)
        if speech_handle.done() or speech_handle.interrupted:
            return
        speech_handle.allow_interruptions = True
        logger.info("Recorded audio interruption enabled: speech=%s", getattr(speech_handle, "id", ""))

    asyncio.create_task(_enable_later())


async def hangup_room_after_dialogue_end(room_name: str, delay_ms: int) -> None:
    delay_seconds = max(0, delay_ms) / 1000
    logger.info("Dialogue end reached; scheduling LiveKit hangup in %.2fs", delay_seconds)
    await asyncio.sleep(delay_seconds)

    lkapi = api.LiveKitAPI(
        url=_livekit_http_url(),
        api_key=os.getenv("LIVEKIT_API_KEY", "devkey"),
        api_secret=os.getenv("LIVEKIT_API_SECRET", "secret"),
    )
    removed: list[str] = []
    errors: list[str] = []
    try:
        try:
            participants = await lkapi.room.list_participants(api.ListParticipantsRequest(room=room_name))
            identities = [item.identity for item in participants.participants if item.identity]
        except Exception as exc:
            identities = []
            errors.append(f"list_participants failed: {exc}")

        for identity in dict.fromkeys(identities):
            try:
                await lkapi.room.remove_participant(api.RoomParticipantIdentity(room=room_name, identity=identity))
                removed.append(identity)
            except Exception as exc:
                errors.append(f"remove_participant {identity} failed: {exc}")

        try:
            await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception as exc:
            errors.append(f"delete_room failed: {exc}")
    finally:
        await lkapi.aclose()

    if errors:
        logger.warning("Dialogue end hangup completed with errors: removed=%s errors=%s", removed, errors)
    else:
        logger.info("Dialogue end hangup completed: removed=%s room=%s", removed, room_name)


async def warm_up_qwen_llm() -> None:
    if os.getenv("QWEN_LLM_WARMUP", "true").lower() not in {"1", "true", "yes", "on"}:
        return

    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    if not dashscope_key:
        logger.warning("skip LLM warm-up: DASHSCOPE_API_KEY is missing")
        return

    client = AsyncOpenAI(
        api_key=dashscope_key,
        base_url=os.getenv(
            "QWEN_LLM_BASE_URL",
            "https://llm-vfnjvqxp5829jfc6.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        ),
    )
    started = perf_counter()
    try:
        await client.chat.completions.create(
            model=os.getenv("QWEN_LLM_MODEL", "qwen3.7-flash"),
            messages=[{"role": "user", "content": "你好"}],
            temperature=0,
            max_tokens=1,
            timeout=10,
            extra_body={"enable_thinking": False},
        )
        logger.info("LLM warm-up completed in %.2fs", perf_counter() - started)
    except Exception:
        logger.exception("LLM warm-up failed")
    finally:
        await client.close()


def _outbound_job(raw_metadata: str) -> dict[str, Any] | None:
    if not raw_metadata.strip():
        return None
    if raw_metadata.startswith("enc:v1:"):
        key = os.getenv("CLOUD_PARITY_DISPATCH_METADATA_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "encrypted outbound metadata requires CLOUD_PARITY_DISPATCH_METADATA_KEY"
            )
        try:
            from cryptography.fernet import Fernet

            raw_metadata = Fernet(key.encode("ascii")).decrypt(
                raw_metadata.removeprefix("enc:v1:").encode("ascii")
            ).decode("utf-8")
        except Exception as exc:
            raise RuntimeError(
                "outbound telephony job metadata authentication failed"
            ) from exc
    try:
        payload = json.loads(raw_metadata)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid agent job metadata")
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "telephony.outbound":
        return None
    required = (
        "project_id", "call_id", "worker_id", "lease_token",
        "phone_number", "livekit_trunk_id",
    )
    if any(not str(payload.get(key) or "").strip() for key in required):
        raise RuntimeError("outbound telephony job metadata is incomplete")
    return payload


def _inbound_job(raw_metadata: str) -> dict[str, Any] | None:
    if not raw_metadata.strip():
        return None
    try:
        payload = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "telephony.inbound":
        return None
    if not str(payload.get("project_id") or "").strip():
        raise RuntimeError("inbound telephony job metadata is incomplete")
    return payload


def _metadata_scene_id(raw_metadata: str, managed_job: dict[str, Any] | None) -> int | None:
    candidate = (managed_job or {}).get("scene_id")
    if candidate in {None, ""} and raw_metadata.strip() and not raw_metadata.startswith("enc:v1:"):
        try:
            payload = json.loads(raw_metadata)
            if isinstance(payload, dict):
                candidate = payload.get("scene_id")
        except json.JSONDecodeError:
            pass
    try:
        return int(candidate) if candidate not in {None, ""} else None
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid dialogue scene id in job metadata: %r", candidate)
        return None


def _telephony_control_headers() -> dict[str, str]:
    token = os.getenv("CLOUD_PARITY_SERVICE_BEARER_TOKEN", "").strip()
    token_file = os.getenv("CLOUD_PARITY_SERVICE_BEARER_TOKEN_FILE", "").strip()
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("service bearer token file cannot be read") from exc
        if not token:
            raise RuntimeError("service bearer token file is empty")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {
        "X-User-ID": os.getenv("CLOUD_PARITY_SERVICE_USER_ID", "telephony-worker").strip()
    }


def _new_telephony_control_client() -> httpx.AsyncClient:
    timeout = float(os.getenv("CLOUD_PARITY_CONTROL_TIMEOUT_SECONDS", "5"))
    max_connections = max(
        4, int(os.getenv("CLOUD_PARITY_CONTROL_MAX_CONNECTIONS", "20"))
    )
    max_keepalive = min(
        max_connections,
        max(2, int(os.getenv("CLOUD_PARITY_CONTROL_MAX_KEEPALIVE", "10"))),
    )
    return httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive,
            keepalive_expiry=30,
        ),
    )


async def _close_telephony_control_client() -> None:
    global _telephony_control_client
    client = _telephony_control_client
    _telephony_control_client = None
    if client is not None and not client.is_closed:
        await client.aclose()


async def _telephony_control_post(
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    base_url = os.getenv("CLOUD_PARITY_CONTROL_URL", "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("CLOUD_PARITY_CONTROL_URL is required for managed telephony jobs")
    url = f"{base_url}{path}"
    client = _telephony_control_client
    owns_client = client is None or client.is_closed
    if owns_client:
        client = _new_telephony_control_client()
    try:
        response = await client.post(url, headers=_telephony_control_headers(), json=payload)
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code >= 400:
        raise RuntimeError(
            f"telephony control request failed: path={path.rsplit('/', 1)[-1]} "
            f"status={response.status_code}"
        )
    return response.json()


async def _insights_create_session(
    job: dict[str, Any], *, room_name: str, agent_name: str
) -> str:
    session_id = str(job["call_id"])
    await _telephony_control_post(
        f"/api/platform/projects/{job['project_id']}/sessions",
        {
            "session_id": session_id,
            "room_name": room_name,
            "agent_name": agent_name,
            "metadata": {
                "call_id": job["call_id"],
                "direction": str(job.get("direction") or ""),
            },
        },
    )
    return session_id


async def _insights_event(
    job: dict[str, Any],
    session_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    await _telephony_control_post(
        f"/api/platform/projects/{job['project_id']}/sessions/{session_id}/events",
        {
            "event_type": event_type,
            "source": "agent",
            "payload": payload or {},
        },
    )


async def _insights_record_session_usage(
    job: dict[str, Any], session_id: str, usage_summary: str
) -> None:
    await _telephony_control_post(
        f"/api/platform/projects/{job['project_id']}/sessions/{session_id}/usage",
        {
            "category": "agent_runtime",
            "provider": "livekit-agents",
            "model": os.getenv("QWEN_LLM_MODEL", "qwen3.7-flash"),
            "quantity": 1,
            "unit": "session",
            "cost_usd": 0,
        },
    )
    await _insights_event(
        job,
        session_id,
        "agent.usage",
        {"summary": usage_summary[:4000]},
    )


async def _insights_close_session(
    job: dict[str, Any], session_id: str, status: str
) -> None:
    await _telephony_control_post(
        f"/api/platform/projects/{job['project_id']}/sessions/{session_id}/close",
        {"status": status},
    )


_DTMF_CODES = {
    **{str(number): number for number in range(10)},
    "*": 10,
    "#": 11,
    "A": 12,
    "B": 13,
    "C": 14,
    "D": 15,
}


async def _execute_console_command(
    ctx: JobContext,
    session: AgentSession,
    command: dict[str, Any],
    managed_job: dict[str, Any] | None = None,
    sip_identity: str = "",
) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    command_type = str(command.get("command_type") or "")
    if command_type == "dtmf":
        digits = str(payload.get("digits") or "")
        for digit in digits:
            await ctx.room.local_participant.publish_dtmf(
                code=_DTMF_CODES[digit], digit=digit
            )
        return {"digits_sent": len(digits)}
    if command_type != "rpc":
        raise ValueError("unsupported console command")

    method = str(payload.get("method") or "").strip()
    arguments = payload.get("arguments")
    safe_arguments = arguments if isinstance(arguments, dict) else {}
    if method == "agent.say":
        text = str(safe_arguments.get("text") or "").strip()
        if not text or len(text) > 2000:
            raise ValueError("agent.say text is required and must not exceed 2000 characters")
        speech = session.say(
            text,
            allow_interruptions=bool(safe_arguments.get("allow_interruptions", True)),
        )
        await speech.wait_for_playout()
        return {"spoken": True}
    if method == "call.hangup":
        ctx.shutdown(reason="console requested hangup")
        return {"hangup_requested": True}
    if method == "call.transfer":
        if not managed_job or not sip_identity:
            raise ValueError("call.transfer requires an active managed SIP call")
        destination_name = str(safe_arguments.get("destination_name") or "").strip()
        reason = str(safe_arguments.get("reason") or "operator requested transfer").strip()
        if not destination_name:
            raise ValueError("call.transfer destination_name is required")
        if len(reason) > 2000:
            raise ValueError("call.transfer reason must not exceed 2000 characters")
        await _execute_managed_transfer(
            ctx,
            managed_job,
            sip_identity,
            destination_name,
            reason,
        )
        return {"transfer_completed": True, "destination_name": destination_name}

    destination = str(payload.get("destination_identity") or "").strip()
    if not destination:
        raise ValueError("destination_identity is required for remote RPC")
    response = await ctx.room.local_participant.perform_rpc(
        destination_identity=destination,
        method=method,
        payload=json.dumps(safe_arguments, ensure_ascii=False, separators=(",", ":")),
        response_timeout=min(30.0, max(1.0, float(payload.get("timeout_seconds") or 10))),
    )
    return {"response": str(response)[:16000]}


async def _console_command_loop(
    ctx: JobContext,
    session: AgentSession,
    job: dict[str, Any],
    session_id: str,
    sip_identity: str,
) -> None:
    worker_id = f"agent:{os.getpid()}:{ctx.room.name}"[:200]
    poll_seconds = _console_poll_interval(session_id)
    claim_path = (
        f"/api/platform/projects/{job['project_id']}/sessions/{session_id}"
        "/console/commands/claim"
    )
    while True:
        commands: list[dict[str, Any]] = []
        try:
            response = await _telephony_control_post(
                claim_path,
                {"worker_id": worker_id, "limit": 10, "lease_seconds": 30},
            )
            commands = list(response.get("items") or [])
            for command in commands:
                status = "completed"
                try:
                    result = await _execute_console_command(
                        ctx,
                        session,
                        command,
                        managed_job=job,
                        sip_identity=sip_identity,
                    )
                except Exception as exc:
                    status = "failed"
                    result = {"error": type(exc).__name__, "detail": str(exc)[:1000]}
                    logger.exception(
                        "Console command failed: session_id=%s command_id=%s",
                        session_id,
                        command.get("id"),
                    )
                await _telephony_control_post(
                    f"/api/platform/projects/{job['project_id']}/sessions/{session_id}"
                    f"/console/commands/{command['id']}/complete",
                    {"worker_id": worker_id, "status": status, "result": result},
                )
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                try:
                    retry_after = float(exc.response.headers.get("Retry-After", "1"))
                except ValueError:
                    retry_after = 1.0
                logger.warning(
                    "Console command polling rate-limited: session_id=%s retry_after=%s",
                    session_id,
                    retry_after,
                )
                await asyncio.sleep(max(poll_seconds, min(retry_after, 30.0)))
                continue
            logger.exception("Console command polling failed: session_id=%s", session_id)
        except Exception:
            logger.exception("Console command polling failed: session_id=%s", session_id)
        await asyncio.sleep(0 if commands else poll_seconds)


def _console_poll_interval(session_id: str) -> float:
    base = min(
        5.0,
        max(0.25, float(os.getenv("CLOUD_PARITY_CONSOLE_POLL_SECONDS", "2"))),
    )
    # Deterministic per-session jitter avoids synchronized polling after a pod
    # rollout without making tests or command latency unpredictable.
    bucket = hashlib.sha256(session_id.encode("utf-8")).digest()[0] / 255.0
    return base * (0.85 + bucket * 0.30)


async def _telephony_control_request(
    job: dict[str, Any],
    endpoint: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return await _telephony_control_post(
        f"/api/platform/projects/{job['project_id']}"
        f"/telephony/calls/{job['call_id']}/{endpoint}",
        payload,
    )


async def _telephony_transition(
    job: dict[str, Any],
    status: str,
    **fields: Any,
) -> dict[str, Any]:
    return await _telephony_control_request(
        job,
        "transition",
        {
            "status": status,
            "worker_id": job.get("worker_id", ""),
            "lease_token": job.get("lease_token", ""),
            **fields,
        },
    )


async def _telephony_record_result(
    job: dict[str, Any],
    *,
    answering_machine_category: str = "",
    disposition: str = "",
) -> dict[str, Any]:
    return await _telephony_control_request(
        job,
        "result",
        {
            "worker_id": job.get("worker_id", ""),
            "lease_token": job.get("lease_token", ""),
            "answering_machine_category": answering_machine_category,
            "disposition": disposition,
        },
    )


async def _telephony_record_recording(
    job: dict[str, Any],
    *,
    egress_id: str,
    status: str,
    storage_uri: str,
) -> dict[str, Any]:
    return await _telephony_control_request(
        job,
        "recording",
        {
            "worker_id": job.get("worker_id", ""),
            "lease_token": job.get("lease_token", ""),
            "egress_id": egress_id,
            "status": status,
            "storage_uri": storage_uri,
        },
    )


async def _start_managed_recording(
    ctx: JobContext,
    job: dict[str, Any],
    *,
    on_created: Any = None,
) -> tuple[list[str], str]:
    if str(job.get("recording_mode") or "off") != "always":
        return [], ""
    bucket = os.getenv("QWEN_RECORDING_S3_BUCKET", "").strip()
    region = os.getenv("QWEN_RECORDING_S3_REGION", "").strip()
    if not bucket or not region:
        raise RuntimeError("recording requires QWEN_RECORDING_S3_BUCKET and region")
    prefix = os.getenv("QWEN_RECORDING_S3_PREFIX", "telephony-recordings").strip(" /")
    if not prefix or ".." in prefix:
        raise RuntimeError("invalid QWEN_RECORDING_S3_PREFIX")

    filepath = f"{prefix}/{job['project_id']}/{job['call_id']}.mp3"

    def upload() -> api.S3Upload:
        return api.S3Upload(
            access_key=os.getenv("QWEN_RECORDING_S3_ACCESS_KEY", "").strip(),
            secret=os.getenv("QWEN_RECORDING_S3_SECRET", "").strip(),
            session_token=os.getenv("QWEN_RECORDING_S3_SESSION_TOKEN", "").strip(),
            region=region,
            endpoint=os.getenv("QWEN_RECORDING_S3_ENDPOINT", "").strip(),
            bucket=bucket,
            force_path_style=os.getenv(
                "QWEN_RECORDING_S3_FORCE_PATH_STYLE", "false"
            ).strip().lower() in {"1", "true", "yes", "on"},
        )

    info = await ctx.api.egress.start_room_composite_egress(
        api.RoomCompositeEgressRequest(
            room_name=ctx.room.name,
            audio_only=True,
            audio_mixing=api.AudioMixing.DUAL_CHANNEL_AGENT,
            # Egress otherwise defaults MP3 to 44.1 kHz/128 kbps. The PSTN
            # side is 8 kHz PCMU, so 16 kHz/64 kbps stereo preserves all
            # telephone-band content without wasteful upsampling.
            advanced=api.EncodingOptions(
                audio_codec=api.AudioCodec.AC_MP3,
                audio_frequency=_configured_int(
                    "QWEN_RECORDING_AUDIO_FREQUENCY_HZ",
                    16_000,
                    allowed={8_000, 16_000, 24_000, 32_000, 44_100, 48_000},
                ),
                audio_bitrate=_configured_int(
                    "QWEN_RECORDING_AUDIO_BITRATE_KBPS",
                    64,
                    allowed={32, 48, 64, 96, 128, 160, 192, 256, 320},
                ),
            ),
            file_outputs=[
                api.EncodedFileOutput(
                    file_type=api.EncodedFileType.MP3,
                    filepath=filepath,
                    s3=upload(),
                )
            ],
        ),
    )
    egress_ids = [str(info.egress_id or "")]
    if not egress_ids[0]:
        raise RuntimeError("LiveKit RoomCompositeEgress did not return an egress id")
    storage_uri = f"s3://{bucket}/{filepath}"
    if on_created is not None:
        on_created(egress_ids, storage_uri)
    await _wait_for_egress_active(ctx, egress_ids[0])
    await _telephony_record_recording(
        job,
        # The durable schema has one provider egress id; use the caller track
        # as the primary id while the job retains and stops both ids.
        egress_id=egress_ids[0],
        status="active",
        storage_uri=storage_uri,
    )
    return egress_ids, storage_uri


async def _wait_for_egress_active(
    ctx: JobContext,
    egress_id: str,
    *,
    timeout_seconds: float = 10.0,
    poll_seconds: float = 0.05,
) -> None:
    """Wait for the media pipeline, rather than only the start RPC, to be active."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    terminal = {
        api.EgressStatus.EGRESS_COMPLETE,
        api.EgressStatus.EGRESS_FAILED,
        api.EgressStatus.EGRESS_ABORTED,
        api.EgressStatus.EGRESS_LIMIT_REACHED,
    }
    while True:
        response = await ctx.api.egress.list_egress(
            api.ListEgressRequest(egress_id=egress_id)
        )
        info = next(
            (item for item in response.items if str(item.egress_id) == egress_id),
            None,
        )
        if info is not None:
            if info.status == api.EgressStatus.EGRESS_ACTIVE:
                return
            if info.status in terminal:
                status = api.EgressStatus.Name(int(info.status))
                raise RuntimeError(f"recording entered terminal state before active: {status}")
        if loop.time() >= deadline:
            raise TimeoutError(f"recording did not become active within {timeout_seconds}s")
        await asyncio.sleep(poll_seconds)


async def _wait_for_egress_complete(
    ctx: JobContext,
    egress_id: str,
    *,
    timeout_seconds: float = 15.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        response = await ctx.api.egress.list_egress(
            api.ListEgressRequest(egress_id=egress_id)
        )
        info = next(
            (item for item in response.items if str(item.egress_id) == egress_id),
            None,
        )
        if info is not None:
            if info.status == api.EgressStatus.EGRESS_COMPLETE:
                return
            if info.status in {
                api.EgressStatus.EGRESS_FAILED,
                api.EgressStatus.EGRESS_ABORTED,
                api.EgressStatus.EGRESS_LIMIT_REACHED,
            }:
                status = api.EgressStatus.Name(int(info.status))
                raise RuntimeError(f"track recording ended unsuccessfully: {status}")
        if loop.time() >= deadline:
            raise TimeoutError(f"track recording did not complete within {timeout_seconds}s")
        await asyncio.sleep(0.05)


async def _play_recording_disclosure(session: AgentSession, job: dict[str, Any]) -> None:
    if str(job.get("recording_mode") or "off") != "always":
        return
    disclosure = str(job.get("recording_disclosure_text") or "").strip()
    if not disclosure:
        logger.info("Recording disclosure is disabled for room policy")
        return
    notice = session.say(disclosure, allow_interruptions=False)
    await notice.wait_for_playout()


def _telephony_heartbeat_interval(job: dict[str, Any]) -> float:
    requested = max(1.0, min(float(job.get("heartbeat_seconds") or 10), 60.0))
    lease_seconds = max(3.0, float(job.get("lease_seconds") or requested * 3))
    return max(1.0, min(requested, lease_seconds / 3.0))


def _outbound_shutdown_transition(
    *, answered: bool, reason: str, normal_disconnect: bool = False
) -> tuple[str, str]:
    normalized = reason.strip().lower()
    if not answered:
        return "reconciling", "agent_shutdown_before_answer"
    # Deleting the LiveKit room after a remote SIP BYE or an intentional local
    # hangup may surface to the finalizer as "parent process shutdown".
    # Preserve the earlier explicit completion observation so a successfully
    # answered call is not rewritten as an agent runtime failure.
    if normal_disconnect:
        return "completed", ""
    failure_markers = (
        "failed",
        "error",
        "exception",
        "crash",
        "rejected",
        "mandatory recording",
        "worker shutdown",
        "process shutdown",
    )
    if any(marker in normalized for marker in failure_markers):
        return "failed", "agent_runtime_terminated"
    return "completed", ""


async def _telephony_heartbeat(job: dict[str, Any]) -> None:
    interval = _telephony_heartbeat_interval(job)
    failures = 0
    while True:
        await asyncio.sleep(interval)
        try:
            await _telephony_control_request(
                job,
                "heartbeat",
                {"worker_id": job["worker_id"], "lease_token": job["lease_token"]},
            )
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            logger.exception(
                "Telephony lease heartbeat failed: call_id=%s failures=%s",
                job.get("call_id"),
                failures,
            )


async def _cancel_task(task: asyncio.Task[Any] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _sip_failure(exc: Exception) -> tuple[str, str, bool]:
    try:
        code = int(getattr(exc, "sip_status_code", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    if code in {486, 600, 603}:
        return "busy", f"sip_{code}", False
    if code in {408, 480}:
        return "no_answer", f"sip_{code}", False
    return "failed", f"sip_{code}" if code else "sip_error", code >= 500 or code == 0


def _livekit_ringing_timeout_failure(
    exc: Exception,
) -> tuple[str, str, bool] | None:
    """Classify LiveKit's ring timeout when no SIP response metadata is present.

    LiveKit SIP returns a generic Twirp ServerError for its local
    ``ringing_timeout`` instead of SipCallError.  This is a definitive
    no-answer result, not an uncertain provider side effect.
    """

    if not isinstance(exc, api.TwirpError) or isinstance(exc, api.SipCallError):
        return None
    try:
        status = int(getattr(exc, "status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    message = str(getattr(exc, "message", "") or exc).strip().lower()
    if status == 408 and "sip request timed out" in message:
        return "no_answer", "sip_408", True
    return None


async def _admit_inbound_call(
    config: dict[str, Any],
    participant: rtc.RemoteParticipant,
    room_name: str,
) -> dict[str, Any]:
    attributes = dict(participant.attributes or {})
    provider_call_id = (
        attributes.get("sip.callID")
        or attributes.get("sip.callIDFull")
        or f"{room_name}:{participant.identity}"
    )
    project_id = str(config["project_id"])
    worker_id = str(
        config.get("worker_id")
        or os.getenv("CLOUD_PARITY_TELEPHONY_WORKER_ID")
        or f"agent-{os.getpid()}"
    )
    return await _telephony_control_post(
        f"/api/platform/projects/{project_id}/telephony/calls/inbound",
        {
            "provider": str(config.get("provider") or "livekit-sip"),
            "provider_call_id": str(provider_call_id),
            "worker_id": worker_id,
            "source_number": str(attributes.get("sip.phoneNumber") or ""),
            "destination_number": str(attributes.get("sip.trunkPhoneNumber") or ""),
            "agent_name": str(
                config.get("agent_name")
                or os.getenv("QWEN_AGENT_EXPLICIT_NAME")
                or os.getenv("LIVEKIT_AGENT_NAME")
                or "qwen-phone-agent"
            ),
            "room_name": room_name,
            "trunk_id": config.get("trunk_id"),
            "metadata": {
                "livekit_participant_identity": participant.identity,
                "livekit_sip_trunk_id": attributes.get("sip.trunkID", ""),
                "livekit_dispatch_rule_id": attributes.get("sip.ruleID", ""),
            },
        },
    )


async def _execute_managed_transfer(
    ctx: JobContext,
    job: dict[str, Any],
    sip_identity: str,
    destination_name: str,
    reason: str,
) -> None:
    transfer = await _telephony_control_post(
        f"/api/platform/projects/{job['project_id']}"
        f"/telephony/calls/{job['call_id']}/transfers",
        {
            "worker_id": job["worker_id"],
            "lease_token": job["lease_token"],
            "destination_name": destination_name,
            "idempotency_key": f"agent-{uuid.uuid4().hex}",
            "context_summary": reason[:2000],
        },
    )
    transition_path = (
        f"/api/platform/projects/{job['project_id']}"
        f"/telephony/calls/{job['call_id']}/transfers/{transfer['id']}/transition"
    )
    await _telephony_control_post(
        transition_path,
        {
            "worker_id": job["worker_id"],
            "lease_token": job["lease_token"],
            "status": "transferring",
        },
    )
    try:
        await ctx.api.sip.transfer_sip_participant(
            api.TransferSIPParticipantRequest(
                room_name=ctx.room.name,
                participant_identity=sip_identity,
                transfer_to=str(transfer["target_uri"]),
                play_dialtone=True,
            )
        )
    except Exception as exc:
        try:
            await _telephony_control_post(
                transition_path,
                {
                    "worker_id": job["worker_id"],
                    "lease_token": job["lease_token"],
                    "status": "failed",
                    "failure_code": "livekit_transfer_failed",
                    "failure_detail": type(exc).__name__,
                },
            )
        except Exception:
            logger.exception("Unable to persist human transfer failure")
        raise
    await _telephony_control_post(
        transition_path,
        {
            "worker_id": job["worker_id"],
            "lease_token": job["lease_token"],
            "status": "completed",
        },
    )


_POSITIVE_CUSTOMER_REPLY = re.compile(
    r"(?:可以|好呀|好啊|好的|没问题|行(?:的|啊)?|同意|答应|有时间|能去|能参加)"
)
_NEGATIVE_CUSTOMER_REPLY = re.compile(
    r"(?:不可以|不行|没时间|不方便|不用|不了|拒绝|不同意)"
)
_RUNTIME_HANGUP_MECHANICS = re.compile(
    r"(?:连续\s*\d+(?:\.\d+)?\s*秒.*未回应|系统主动挂机|沉默计时|响应超时)"
)
_ACTIONABLE_REMINDER = re.compile(
    r"(?:地点|地址|时间|稍后|联系|确认|回复|提醒|记得|需要|请|改到|改为|安排)"
)
_IDENTITY_REJECTION = re.compile(
    r"(?:不是|打错|找错|不认识|没有这个人|换人了|空号)"
)


def _is_identity_turn(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return any(
        marker in compact
        for marker in ("智能助理", "请问您是", "请问是", "怎么称呼您")
    )


def _clean_summary_clause(text: str) -> str:
    cleaned = text.strip()
    # Spoken business turns often begin with a recipient salutation. It is
    # useful in the call but never belongs in the persisted business summary.
    cleaned = re.sub(
        r"^[^，,。。！？!?;；:：]{0,16}(?:您好|你好)(?:呀|啊|哈)?[！!,，、\s]*",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"^(?:嗯|好的呀|好的|好呀|明白了|没问题|是这样(?:的)?)[！!,，、\s]*",
        "",
        cleaned,
    )
    return cleaned.rstrip("。！？!?；; ")


def _fallback_business_summary(
    *, customer_name: str, reason: str, turns: list[tuple[str, str]]
) -> str:
    """Summarize an answered call without exposing runtime hangup mechanics."""

    customer_label = customer_name.strip() or "客户"
    user_indexes = [index for index, (role, _text) in enumerate(turns) if role == "user"]
    if not user_indexes:
        return reason.strip() or "电话已结束，但未收到客户回应。"

    last_business_reply = ""
    for user_index in reversed(user_indexes):
        reply = turns[user_index][1].strip()
        business = ""
        for role, text in reversed(turns[:user_index]):
            if role == "assistant" and not _is_identity_turn(text):
                business = _clean_summary_clause(text)
                if business:
                    break
        if not business:
            continue
        if not last_business_reply:
            last_business_reply = reply

        reminder = ""
        for role, text in turns[user_index + 1 :]:
            if role == "assistant" and not _is_identity_turn(text):
                reminder = _clean_summary_clause(text)
                if reminder:
                    break

        # Friendly acknowledgements such as "太好啦，我转告他" are dialogue
        # closings, not useful follow-up reminders for the operator.
        if reminder and not _ACTIONABLE_REMINDER.search(reminder):
            reminder = ""

        if _NEGATIVE_CUSTOMER_REPLY.search(reply):
            summary = f"{customer_label}未同意：{business}"
        elif _POSITIVE_CUSTOMER_REPLY.search(reply):
            summary = f"{customer_label}已同意：{business}"
        else:
            continue
        if reminder and reminder != business:
            summary += f"；提示：{reminder}"
        return summary + "。"

    if last_business_reply:
        reply = last_business_reply.rstrip("。！？!? ")
        return f"已完成与{customer_label}的电话沟通；对方最后回复：“{reply}”。"
    return reason.strip() or "电话已结束，但没有可验证的业务对话。"


def _evidence_based_call_result(
    *, customer_name: str, turns: list[tuple[str, str]]
) -> str | None:
    """Derive a result only from a customer reply to a spoken call turn."""

    for user_index in range(len(turns) - 1, -1, -1):
        role, raw_reply = turns[user_index]
        if role != "user" or not raw_reply.strip():
            continue
        reply = raw_reply.strip()
        preceding_assistant = next(
            (
                text.strip()
                for prior_role, text in reversed(turns[:user_index])
                if prior_role == "assistant" and text.strip()
            ),
            "",
        )
        if not preceding_assistant:
            continue
        if _is_identity_turn(preceding_assistant):
            if _IDENTITY_REJECTION.search(reply):
                customer_label = customer_name.strip() or "客户"
                return f"{customer_label}身份未确认；对方回复：“{reply}”。"
            # “是” only confirms identity. It is never evidence that the
            # customer accepted an invitation or any other business request.
            continue
        return _fallback_business_summary(
            customer_name=customer_name,
            reason="",
            turns=turns[: user_index + 1],
        )
    return None


def _sanitize_business_summary(
    proposed: str, *, customer_name: str, turns: list[tuple[str, str]]
) -> str:
    normalized = proposed.strip()
    if not _RUNTIME_HANGUP_MECHANICS.search(normalized):
        return normalized
    return _fallback_business_summary(
        customer_name=customer_name,
        reason="通话已结束，但未收到客户回应。",
        turns=turns,
    )


class PhoneAgent(Agent):
    def __init__(
        self,
        *,
        ctx: JobContext | None = None,
        managed_job: dict[str, Any] | None = None,
        sip_identity: str = "",
        instructions: str | None = None,
        chat_ctx: llm.ChatContext | None = None,
    ) -> None:
        self._job_ctx = ctx
        self._managed_job = managed_job
        self._sip_identity = sip_identity
        self._configured_audio_sample_played = False
        self._configured_audio_ab_test_played = False
        self._agent_session: AgentSession | None = None
        self._pending_business_event_tasks: set[asyncio.Task[None]] = set()
        self._business_result_saved = False
        self._business_result_lock = asyncio.Lock()
        self._last_user_messages: list[str] = []
        self._conversation_turns: list[tuple[str, str]] = []
        super().__init__(
            instructions=(
                instructions
                if instructions is not None
                else os.getenv(
                    "QWEN_AGENT_INSTRUCTIONS",
                    (
                    "你是一个中文语音电话助手，负责直接回答用户问题。"
                    "回答要准确、简洁、自然，适合电话语音播报。"
                    "优先给出结论，再补充必要说明。"
                    "如果没有听清用户问题，只回答：我没有听清，请再说一遍。"
                    "通常不超过三句话，除非用户明确要求详细解释。"
                    ),
                )
            ).strip(),
            chat_ctx=chat_ctx,
        )

    async def on_enter(self) -> None:
        logger.info("PhoneAgent.on_enter: ready")

    async def wait_for_pending_business_events(self) -> None:
        pending = [task for task in self._pending_business_event_tasks if not task.done()]
        if not pending:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out waiting for %d pending business event(s)",
                len(pending),
            )

    def note_user_transcript(self, text: str) -> None:
        normalized = text.strip()
        if normalized:
            self._last_user_messages = [*self._last_user_messages[-4:], normalized[:500]]
            self._conversation_turns = [
                *self._conversation_turns[-15:],
                ("user", normalized[:500]),
            ]

    def note_assistant_transcript(self, text: str) -> None:
        normalized = text.strip()
        if normalized:
            self._conversation_turns = [
                *self._conversation_turns[-15:],
                ("assistant", normalized[:500]),
            ]

    async def persist_fallback_call_result(self, reason: str) -> None:
        if not self._managed_job or self._business_result_saved:
            return
        async with self._business_result_lock:
            if self._business_result_saved:
                return
            normalized_reason = reason.strip() or "通话结束"
            summary = _fallback_business_summary(
                customer_name=str(self._managed_job.get("customer_name") or ""),
                reason=normalized_reason,
                turns=self._conversation_turns,
            )
            saved = await self._record_realtime_business_event(
                "call.result",
                {"summary": summary[:4000], "intent_label": ""},
            )
            if saved:
                self._business_result_saved = True

    @function_tool(
        description=(
            "Play the single operator-configured WAV sample to the caller in full. "
            "Use only when the active prompt explicitly asks for an audio rating, and "
            "only after confirming the caller's identity. The tool has no path argument."
        )
    )
    async def play_configured_audio_sample(self) -> str:
        if self._configured_audio_sample_played:
            return "音频已经播放过，不要重复播放；现在继续询问评分。"
        if not self._job_ctx:
            return "当前通话无法播放配置的音频。"
        configured = os.getenv("QWEN_REALTIME_SAMPLE_AUDIO_FILE", "").strip()
        if not configured:
            return "没有配置待播放音频。"
        source = Path(configured).expanduser()
        if source.suffix.lower() != ".wav" or not source.is_file():
            return "配置的 WAV 音频不存在或格式不受支持。"
        if source.stat().st_size > 50 * 1024 * 1024:
            return "配置的 WAV 音频过大，无法播放。"
        audio = _prepare_wav_for_room_playback(source.read_bytes())
        await _play_wav_bytes_direct(
            self._job_ctx.room,
            audio,
            label="Configured rating audio sample",
            track_name="realtime-rating-audio-sample",
        )
        self._configured_audio_sample_played = True
        _append_local_transcript(
            room_name=self._job_ctx.room.name,
            role="assistant",
            text=f"[已完整播放音频：{source.name}]",
            source="programmatic_audio_sample",
        )
        return "音频已完整播放。现在立即询问客户：如果满分为10分，请问您给几分？"

    @function_tool(
        description=(
            "Run the operator-configured blind A/B playback exactly once. The same "
            "WAV/PCM content is randomized between a direct LiveKit audio track and "
            "the active AgentSession RoomIO output. Use only when the active prompt "
            "explicitly asks for a strict audio-path A/B test and after confirming "
            "the caller's identity. Never reveal the hidden mapping to the caller."
        )
    )
    async def play_configured_audio_ab_test(self, ctx: RunContext) -> str:
        if self._configured_audio_ab_test_played:
            return "样本A和样本B已经播放过，不要重复播放；现在继续收集两个评分。"
        if not self._job_ctx or not self._agent_session:
            return "当前通话无法运行A/B测试。"
        configured = os.getenv("QWEN_REALTIME_AB_AUDIO_FILE", "").strip()
        if not configured:
            return "没有配置A/B测试音频。"
        source = Path(configured).expanduser()
        if source.suffix.lower() != ".wav" or not source.is_file():
            return "配置的A/B测试 WAV 音频不存在或格式不受支持。"
        if source.stat().st_size > 50 * 1024 * 1024:
            return "配置的A/B测试 WAV 音频过大，无法播放。"

        # A spoken lead-in can precede a tool call in the same model response.
        # Waiting here prevents that lead-in from overlapping the first sample.
        await ctx.wait_for_playout()
        audio = _prepare_wav_for_room_playback(source.read_bytes())
        pcm_sha256 = _wav_pcm_sha256(audio)
        paths = ["direct_local_audio_track", "agent_session_roomio"]
        secrets.SystemRandom().shuffle(paths)
        mapping = {"A": paths[0], "B": paths[1]}
        _write_audio_ab_mapping(
            room_name=self._job_ctx.room.name,
            source=source,
            prepared_audio=audio,
            pcm_sha256=pcm_sha256,
            mapping=mapping,
        )

        async def play(label: str, path: str) -> None:
            if path == "direct_local_audio_track":
                await _play_wav_bytes_direct(
                    self._job_ctx.room,
                    audio,
                    label=f"Blind A/B sample {label} direct",
                    track_name=f"realtime-ab-{label.lower()}-direct",
                )
            else:
                audio_output = self._agent_session.output.audio
                if audio_output is None:
                    raise RuntimeError("AgentSession RoomIO audio output is unavailable")
                await _play_wav_bytes_via_agent_output(
                    audio_output,
                    audio,
                    label=f"Blind A/B sample {label} RoomIO",
                )
            _append_local_transcript(
                room_name=self._job_ctx.room.name,
                role="assistant",
                text=f"[样本{label}已完整播放]",
                source="programmatic_audio_ab_test",
            )

        await play("A", mapping["A"])
        await asyncio.sleep(1.5)
        await play("B", mapping["B"])
        self._configured_audio_ab_test_played = True
        logger.info(
            "Blind A/B playback completed: room=%s pcm_sha256=%s",
            self._job_ctx.room.name,
            pcm_sha256,
        )
        return (
            "样本A和样本B均已完整播放。不要透露后台映射。"
            "现在先询问样本A的0到10分并确认，再询问样本B的0到10分并确认。"
        )

    @function_tool(
        description=(
            "Transfer the current caller to a configured human service destination. "
            "Use only after the caller explicitly asks for a human or the issue requires escalation."
        )
    )
    async def transfer_to_human(self, destination_name: str, reason: str) -> str:
        if not self._job_ctx or not self._managed_job or not self._sip_identity:
            return "当前通话未接入受管客服转接，请继续协助用户。"
        job = self._managed_job
        try:
            await _execute_managed_transfer(
                self._job_ctx,
                job,
                self._sip_identity,
                destination_name,
                reason,
            )
            return "已为您转接人工客服，请稍候。"
        except Exception as exc:
            logger.exception(
                "Human transfer failed: call_id=%s destination=%s",
                job.get("call_id"),
                destination_name,
            )
            return "人工客服暂时无法接通，我会继续为您处理。"

    async def _record_realtime_business_event(
        self, event_type: str, payload: dict[str, Any]
    ) -> bool:
        if not self._managed_job:
            logger.info("Realtime business event without managed call: %s %s", event_type, payload)
            return False
        try:
            await _insights_event(
                self._managed_job,
                str(self._managed_job["call_id"]),
                event_type,
                payload,
            )
            return True
        except Exception:
            logger.exception(
                "Unable to persist Realtime business event: call_id=%s type=%s",
                self._managed_job.get("call_id"),
                event_type,
            )
            return False

    @function_tool(
        description="Save the current customer intent label after applying the prompt rules."
    )
    async def save_intent_label(self, label: str, evidence: str) -> str:
        normalized = label.strip().upper()
        if normalized not in {"A类", "B类", "C类"}:
            return "意向标签无效，未保存。"
        saved = await self._record_realtime_business_event(
            "call.intent_label",
            {"label": normalized, "evidence": evidence.strip()[:1000]},
        )
        return "意向标签已保存。" if saved else "意向标签暂时无法保存。"

    @function_tool(
        description=(
            "Save a result only after the customer answered a spoken business request. "
            "The runtime derives the stored result from the transcript and ignores unsupported claims."
        )
    )
    async def save_call_result(
        self,
        summary: str,
        intent_label: str = "",
    ) -> str:
        if not summary.strip():
            return "通话摘要为空，未保存。"
        summary = _evidence_based_call_result(
            customer_name=str((self._managed_job or {}).get("customer_name") or ""),
            turns=self._conversation_turns,
        )
        if not summary:
            logger.warning(
                "Rejected unsupported call result: call_id=%s",
                (self._managed_job or {}).get("call_id"),
            )
            return "尚无可验证的业务答复，未保存；请先说明具体事项并取得客户答复。"
        async with self._business_result_lock:
            saved = await self._record_realtime_business_event(
                "call.result",
                {
                    "summary": summary.strip()[:4000],
                    # A model-supplied label is not evidence. Dedicated intent
                    # labeling has its own explicit evidence-bearing tool.
                    "intent_label": "",
                },
            )
            if saved:
                self._business_result_saved = True
        return "通话结果已保存。" if saved else "通话结果暂时无法保存。"

    @function_tool(
        description=(
            "Complete a confirmed WeChat follow-up. Use only after the customer has "
            "confirmed the WeChat ID. The program immediately plays the fixed notice."
        )
    )
    async def complete_wechat_followup(
        self,
        summary: str,
        intent_label: str = "",
    ) -> str:
        if not summary.strip():
            return "通话摘要为空，未提交。"

        async def persist() -> None:
            async with self._business_result_lock:
                saved = await self._record_realtime_business_event(
                    "call.result",
                    {
                        "summary": summary.strip()[:4000],
                        "intent_label": intent_label.strip().upper()[:10],
                    },
                )
                if not saved:
                    logger.warning("Confirmed WeChat follow-up result was not persisted")
                else:
                    self._business_result_saved = True

        task = asyncio.create_task(persist(), name="persist-wechat-call-result")
        self._pending_business_event_tasks.add(task)
        task.add_done_callback(self._pending_business_event_tasks.discard)
        return "微信跟进结果已提交。"

    @function_tool(
        description="Record a customer question that isn't covered by the supplied facts or knowledge."
    )
    async def record_unresolved_question(self, question: str) -> str:
        if not question.strip():
            return "问题为空，未记录。"
        saved = await self._record_realtime_business_event(
            "call.unresolved_question", {"question": question.strip()[:2000]}
        )
        return "未解决问题已记录。" if saved else "未解决问题暂时无法记录。"

    @function_tool(
        description=(
            "Request deterministic call completion. Do not speak a goodbye before or "
            "after calling this tool; the program plays the fixed goodbye in full."
        )
    )
    async def end_call(self, ctx: RunContext, reason: str) -> str:
        if (
            self._managed_job
            and str(self._managed_job.get("direction") or "") == "outbound"
            and str(self._managed_job.get("realtime_prompt") or "").strip()
            and not self._business_result_saved
        ):
            return "尚无已验证的客户业务答复，不能结束通话；请继续说明事项并等待客户答复。"
        if voice_pipeline() == REALTIME_PIPELINE:
            return "结束请求已接收，程序将播放统一结束语。"
        if not self._job_ctx:
            return "当前通话不支持自动挂机。"
        if _customer_hangup_only(self._managed_job):
            logger.info(
                "Ignoring AI end_call for customer-hangup-only outbound call: reason=%s",
                reason[:200],
            )
            return "请说完结束语并等待客户先挂机；不要再次调用 end_call。"
        session = ctx.session
        current_speech = ctx.speech_handle
        next_speech: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def on_speech_created(event: Any) -> None:
            handle = getattr(event, "speech_handle", None)
            if handle is not None and handle is not current_speech and not next_speech.done():
                next_speech.set_result(handle)

        session.on("speech_created", on_speech_created)

        async def shutdown_after_playout() -> None:
            try:
                # The tool belongs to current_speech. Waiting in this detached task is
                # safe after the tool returns and ensures its whole turn is drained.
                await asyncio.wait_for(current_speech.wait_for_playout(), timeout=30.0)

                # Some Realtime providers generate the spoken tool reply as a second
                # SpeechHandle. If one was created, drain that handle as well.
                if next_speech.done():
                    followup = next_speech.result()
                    await asyncio.wait_for(followup.wait_for_playout(), timeout=30.0)
                else:
                    try:
                        followup = await asyncio.wait_for(
                            asyncio.shield(next_speech), timeout=1.0
                        )
                        await asyncio.wait_for(followup.wait_for_playout(), timeout=30.0)
                    except asyncio.TimeoutError:
                        pass
            except asyncio.TimeoutError:
                logger.warning("Realtime final response playout timed out; forcing hangup")
            except Exception:
                logger.exception("Unable to wait for Realtime final response playout")
            finally:
                session.off("speech_created", on_speech_created)
                await asyncio.sleep(
                    max(0, _env_int("QWEN_AUDIO_REALTIME_PLAYOUT_TAIL_MS", 400)) / 1000
                )
                if self._job_ctx:
                    self._job_ctx.shutdown(reason=f"realtime end_call: {reason[:200]}")

        asyncio.create_task(shutdown_after_playout())
        return "请完整播报当前结束节点的话术；系统会在音频实际播放完成后挂机。"


server = AgentServer(
    port=int(os.getenv("QWEN_AGENT_PORT", "18081")),
    http_proxy=None,
    setup_fnc=prewarm_process,
    load_threshold=float(os.getenv("QWEN_AGENT_LOAD_THRESHOLD", "0.95")),
)

_sip_registration_tasks: set[asyncio.Task[None]] = set()


@server.once("worker_started")
def _start_sip_registration_keeper() -> None:
    profiles = [
        item.strip()
        for item in os.getenv("QWEN_SIP_REGISTRATION_KEEPALIVE_PROFILES", "").split(",")
        if item.strip()
    ]
    for env_prefix in dict.fromkeys(profiles):
        task = asyncio.create_task(
            _sip_registration_renewal_loop(env_prefix),
            name=f"sip-registration-{env_prefix.lower()}",
        )
        _sip_registration_tasks.add(task)
        task.add_done_callback(_sip_registration_tasks.discard)


@server.rtc_session(agent_name=os.getenv("QWEN_AGENT_EXPLICIT_NAME", os.getenv("LIVEKIT_AGENT_NAME", "")))
async def entrypoint(ctx: JobContext) -> None:
    global _telephony_control_client
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    ctx.log_context_fields = {"room": ctx.room.name}

    raw_job_metadata = ctx.job.metadata or ""
    outbound_job = _outbound_job(raw_job_metadata)
    inbound_config = _inbound_job(raw_job_metadata)
    managed_job = outbound_job
    managed_sip_identity = ""
    heartbeat_task: asyncio.Task[None] | None = None
    console_task: asyncio.Task[None] | None = None
    insights_session_id = ""
    telephony_terminal = False
    outbound_call_answered = False
    outbound_call_ended_normally = False
    shutdown_finalizers: list[Any] = []
    if outbound_job or inbound_config:
        if _telephony_control_client is None or _telephony_control_client.is_closed:
            _telephony_control_client = _new_telephony_control_client()
    if outbound_job:
        outbound_job["direction"] = "outbound"
        managed_sip_identity = f"sip-{outbound_job['call_id']}"

    if inbound_config:
        try:
            sip_participant = await ctx.wait_for_participant(
                kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            )
            admitted = await _admit_inbound_call(
                inbound_config,
                sip_participant,
                ctx.room.name,
            )
            managed_job = {
                "project_id": inbound_config["project_id"],
                "call_id": admitted["id"],
                "direction": "inbound",
                "worker_id": admitted["lease_owner"],
                "lease_token": admitted["lease_token"],
                "heartbeat_seconds": inbound_config.get("heartbeat_seconds", 10),
                "recording_mode": admitted.get("recording_mode", "off"),
                "recording_disclosure_text": admitted.get(
                    "recording_disclosure_text", ""
                ),
            }
            managed_sip_identity = sip_participant.identity
            heartbeat_task = asyncio.create_task(_telephony_heartbeat(managed_job))
            await _telephony_transition(managed_job, "active", room_name=ctx.room.name)
            overflow = admitted.get("overflow") if isinstance(admitted, dict) else None
            if isinstance(overflow, dict) and overflow.get("mode") == "transfer":
                await _execute_managed_transfer(
                    ctx,
                    managed_job,
                    managed_sip_identity,
                    str(overflow["destination_name"]),
                    "Inbound AI concurrency capacity exhausted; overflow routing.",
                )
                telephony_terminal = True
                await _cancel_task(heartbeat_task)
                ctx.shutdown(reason="inbound call transferred to overflow destination")
                return
        except Exception:
            logger.exception("Inbound call admission failed; rejecting room=%s", ctx.room.name)
            try:
                await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
            except Exception:
                logger.exception("Unable to delete rejected inbound room=%s", ctx.room.name)
            await _cancel_task(heartbeat_task)
            ctx.shutdown(reason="inbound admission rejected")
            return

    if managed_job:
        try:
            insights_session_id = await _insights_create_session(
                managed_job,
                room_name=ctx.room.name,
                agent_name=str(
                    os.getenv("QWEN_AGENT_EXPLICIT_NAME")
                    or os.getenv("LIVEKIT_AGENT_NAME")
                    or "qwen-phone-agent"
                ),
            )
            await _insights_event(
                managed_job,
                insights_session_id,
                "agent.started",
                {"room_name": ctx.room.name, "direction": managed_job.get("direction", "")},
            )
        except Exception:
            insights_session_id = ""
            logger.exception(
                "Unable to initialize managed Insights session: call_id=%s",
                managed_job.get("call_id"),
            )

    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    qwen_base_url = os.getenv(
        "QWEN_LLM_BASE_URL",
        "https://llm-vfnjvqxp5829jfc6.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )
    selected_pipeline = voice_pipeline()
    metadata_scene_id = _metadata_scene_id(raw_job_metadata, managed_job)
    scene_id = int(metadata_scene_id or os.getenv("QWEN_DIALOGUE_SCENE_ID", "0")) or None

    session_vad = None
    asr_provider = None
    turn_detector = None
    if selected_pipeline == CLASSIC_PIPELINE:
        use_realtime_asr = os.getenv("QWEN_USE_REALTIME_ASR", "true").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        session_vad = ctx.proc.userdata.get("vad")
        if session_vad is None:
            logger.warning("Silero VAD was not prewarmed; loading it in the job process")
            session_vad = _build_session_vad()
        asr_provider = (
            QwenRealtimeASR()
            if use_realtime_asr
            else stt.StreamAdapter(
                stt=QwenASR(),
                vad=session_vad,
            )
        )
        turn_detector = _build_turn_detector()
        logger.info(
            "Voice pipeline selected: mode=classic asr=%s vad=silero turn_detection=%s",
            "qwen-realtime-websocket" if use_realtime_asr else "qwen-http-vad-adapter",
            _turn_detection_mode(),
        )
    else:
        logger.info(
            "Voice pipeline selected: mode=realtime model=%s",
            os.getenv("QWEN_AUDIO_REALTIME_MODEL", "qwen-audio-3.0-realtime-plus"),
        )

    hangup_task: asyncio.Task[None] | None = None
    current_speech_handle: Any = None
    recording_egress_ids: list[str] = []
    recording_storage_uri = ""
    recording_stop_lock = asyncio.Lock()
    recording_start_task: asyncio.Task[bool] | None = None
    sip_disconnect_task: asyncio.Task[None] | None = None

    async def stop_managed_recording(*, request_stop: bool = True) -> None:
        nonlocal recording_egress_ids
        async with recording_stop_lock:
            if not recording_egress_ids:
                return
            egress_ids = recording_egress_ids
            # Clear first so a simultaneous JobContext shutdown cannot issue a
            # second StopEgress request while this one is in flight.
            recording_egress_ids = []
            recording_status = "stopping"
            try:
                if request_stop:
                    await asyncio.gather(
                        *(
                            ctx.api.egress.stop_egress(
                                api.StopEgressRequest(egress_id=egress_id)
                            )
                            for egress_id in egress_ids
                        )
                    )
                await asyncio.gather(
                    *(
                        _wait_for_egress_complete(ctx, egress_id)
                        for egress_id in egress_ids
                    )
                )
                recording_status = "completed"
            except Exception:
                logger.exception(
                    "Unable to finalize dual-track recording: egress_ids=%s",
                    egress_ids,
                )
                recording_status = "failed"
            if managed_job:
                try:
                    await _telephony_record_recording(
                        managed_job,
                        egress_id=egress_ids[0],
                        status=recording_status,
                        storage_uri=recording_storage_uri,
                    )
                except Exception:
                    logger.exception(
                        "Unable to persist final recording status: call_id=%s",
                        managed_job["call_id"],
                    )

    def on_dialogue_result(result: dict) -> None:
        nonlocal hangup_task
        audio = result.get("audio") if isinstance(result.get("audio"), dict) else {}
        audio_url = str(result.get("audio_url") or audio.get("url") or "").strip()
        if audio_url and current_speech_handle is not None:
            schedule_interrupt_gate(
                current_speech_handle,
                text=str(result.get("text") or ""),
                audio_url=audio_url,
                result=result,
            )

        if not result.get("should_hangup"):
            return
        if _customer_hangup_only(managed_job):
            logger.info(
                "Ignoring dialogue should_hangup for customer-hangup-only outbound room=%s",
                ctx.room.name,
            )
            return
        if hangup_task and not hangup_task.done():
            logger.info("Dialogue end hangup already scheduled for room=%s", ctx.room.name)
            return
        try:
            delay_ms = int(result.get("hangup_delay_ms") or _env_int("QWEN_DIALOGUE_HANGUP_DELAY_MS", 3500))
        except (TypeError, ValueError):
            delay_ms = 3500
        delay_ms = max(delay_ms, _env_int("QWEN_DIALOGUE_HANGUP_MIN_DELAY_MS", 3500))
        hangup_task = asyncio.create_task(hangup_room_after_dialogue_end(ctx.room.name, delay_ms))

    realtime_instructions = ""
    realtime_scene: dict[str, Any] | None = None
    realtime_scene_task: asyncio.Task[dict[str, Any] | None] | None = None
    realtime_opening = ""
    task_prompt_override = str((managed_job or {}).get("realtime_prompt") or "").strip()
    if selected_pipeline == CLASSIC_PIPELINE:
        session = AgentSession(
            stt=asr_provider,
            vad=session_vad,
            llm=ScriptFirstLLM(
                upstream=openai.LLM(
                    model=os.getenv("QWEN_LLM_MODEL", "qwen3.7-flash"),
                    api_key=dashscope_key,
                    base_url=qwen_base_url,
                    extra_body={"enable_thinking": False},
                ),
                session_id=ctx.room.name,
                scene_id=scene_id,
                dialogue_url=os.getenv("QWEN_DIALOGUE_URL", "http://127.0.0.1:8090/api/dialogue/turn"),
                timeout=float(os.getenv("QWEN_DIALOGUE_TIMEOUT", "0.8")),
                on_dialogue_result=on_dialogue_result,
            ),
            tts=QwenTTS(),
            turn_handling=TurnHandlingOptions(
                turn_detection=turn_detector,
                endpointing=_turn_endpointing_options(),
                preemptive_generation={
                    "enabled": True,
                    "preemptive_tts": True,
                    "max_speech_duration": 8.0,
                    "max_retries": 3,
                },
                interruption={
                    "resume_false_interruption": True,
                    "false_interruption_timeout": 0.4,
                },
            ),
            aec_warmup_duration=1.0,
        )
    else:
        # Resolve a cold dynamic scene before exposing the Realtime session to
        # caller audio. The fetch is bounded by QWEN_DIALOGUE_TIMEOUT and avoids
        # ever playing the default recruitment opening for another scene.
        realtime_scene, realtime_scene_task = _start_realtime_scene_fetch(scene_id)
        if realtime_scene is None and realtime_scene_task is not None:
            realtime_scene = await realtime_scene_task
            realtime_scene_task = None
            if realtime_scene is None:
                raise RuntimeError(
                    f"Realtime scene {scene_id} could not be loaded; refusing wrong fallback"
                )
        # A task-owned prompt must supply its own business opening. Reusing the
        # global recruitment greeting leaks an unrelated scene into the call.
        realtime_opening = "" if task_prompt_override else _select_realtime_opening(realtime_scene)
        realtime_instructions = load_realtime_instructions(
            root=ROOT,
            session_id=ctx.room.name,
            scene_id=scene_id,
            prompt_override=task_prompt_override,
            customer_name=str((managed_job or {}).get("customer_name") or ""),
            customer_company=str((managed_job or {}).get("customer_company") or ""),
            customer_phone=str(
                (managed_job or {}).get("phone_number")
                or (managed_job or {}).get("source_number")
                or ""
            ),
            customer_profile=str((managed_job or {}).get("customer_profile") or ""),
            scene=realtime_scene,
        )
        session = AgentSession(
            llm=QwenAudioRealtimeModel(api_key=str(dashscope_key or "")),
            # Realtime owns normal conversational speech. Keep the existing TTS
            # only as an operational fallback for exact compliance notices,
            # voicemail text and supervisor console announcements (`say`).
            tts=QwenTTS(),
            aec_warmup_duration=1.0,
        )

    metrics_event_count = 0
    wechat_close_task: asyncio.Task[None] | None = None
    wechat_notice_task: asyncio.Task[None] | None = None
    final_goodbye_task: asyncio.Task[None] | None = None
    customer_goodbye_task: asyncio.Task[None] | None = None
    customer_silence_task: asyncio.Task[None] | None = None
    programmatic_turn_tasks: set[asyncio.Task[None]] = set()
    programmatic_turn_lock = asyncio.Lock()
    awaiting_wechat_acknowledgement = False
    assistant_business_turns = 0
    last_assistant_utterance = ""
    max_conversation_turns = max(
        1, min(_env_int("QWEN_MAX_CONVERSATION_TURNS", 8), 8)
    )

    async def fixed_realtime_audio(text: str, *, label: str) -> bytes:
        audio = _REALTIME_FIXED_AUDIO.get(text)
        if audio:
            return audio
        logger.warning("%s cache miss; synthesizing before playout", label)
        qwen_tts = QwenTTS()
        try:
            generated, _, _ = await qwen_tts.synthesize_audio_bytes(text)
            audio = _prepare_wav_for_room_playback(generated)
            _REALTIME_FIXED_AUDIO[text] = audio
            return audio
        finally:
            await qwen_tts.aclose()

    async def sync_programmatic_assistant_turn(
        text: str, *, source: str, persist_insights: bool
    ) -> None:
        try:
            async with programmatic_turn_lock:
                chat_ctx = phone_agent.chat_ctx.copy()
                chat_ctx.add_message(role="assistant", content=text)
                await phone_agent.update_chat_ctx(chat_ctx)
        except Exception:
            logger.exception("Unable to synchronize programmatic assistant turn")
        logger.info("Programmatic assistant context synchronized: source=%s", source)
        if persist_insights and managed_job and insights_session_id:
            try:
                await _insights_event(
                    managed_job,
                    insights_session_id,
                    "agent.response",
                    {"text": text, "source": source},
                )
            except Exception:
                logger.exception("Unable to persist programmatic assistant response")

    def schedule_programmatic_assistant_turn(
        text: str, *, source: str, persist_insights: bool = True
    ) -> None:
        task = asyncio.create_task(
            sync_programmatic_assistant_turn(
                text,
                source=source,
                persist_insights=persist_insights,
            ),
            name=f"sync-{source}",
        )
        programmatic_turn_tasks.add(task)
        task.add_done_callback(programmatic_turn_tasks.discard)

    async def persist_preseeded_assistant_turn(text: str, *, source: str) -> None:
        logger.info("Programmatic assistant response: source=%s text=%s", source, text)
        _append_local_transcript(
            room_name=ctx.room.name,
            role="assistant",
            text=text,
            source=source,
        )
        if managed_job and insights_session_id:
            try:
                await _insights_event(
                    managed_job,
                    insights_session_id,
                    "agent.response",
                    {"text": text, "source": source},
                )
            except Exception:
                logger.exception("Unable to persist preseeded assistant response")

    def schedule_preseeded_assistant_turn(text: str, *, source: str) -> None:
        task = asyncio.create_task(
            persist_preseeded_assistant_turn(text, source=source),
            name=f"persist-{source}",
        )
        programmatic_turn_tasks.add(task)
        task.add_done_callback(programmatic_turn_tasks.discard)

    async def play_wechat_added_notice() -> None:
        nonlocal awaiting_wechat_acknowledgement
        awaiting_wechat_acknowledgement = True
        started = perf_counter()
        try:
            await session.interrupt(force=True)
        except Exception:
            logger.debug("No active model response to interrupt before WeChat notice")
        audio = await fixed_realtime_audio(
            WECHAT_ADDED_NOTICE_TEXT,
            label="Realtime WeChat added notice",
        )
        await _play_wav_bytes_direct(
            ctx.room,
            audio,
            label="Realtime WeChat added notice",
            track_name="realtime-wechat-added-notice",
            on_first_frame_queued=lambda: schedule_preseeded_assistant_turn(
                WECHAT_ADDED_NOTICE_TEXT,
                source="programmatic_wechat_added_notice",
            ),
        )
        # The silence window is measured from the actual end of the notice,
        # independent of model-context or Insights network latency.
        _finish_wechat_notice_playout(
            awaiting_acknowledgement=awaiting_wechat_acknowledgement,
            start_close_timer=start_wechat_close_timer,
        )
        schedule_programmatic_assistant_turn(
            WECHAT_ADDED_NOTICE_TEXT,
            source="programmatic_wechat_added_notice",
            persist_insights=False,
        )
        logger.info(
            "Realtime WeChat added notice completed in %.3fs",
            perf_counter() - started,
        )

    async def run_final_goodbye(reason: str, *, interrupt_model: bool) -> None:
        nonlocal awaiting_wechat_acknowledgement, outbound_call_ended_normally
        awaiting_wechat_acknowledgement = False
        if interrupt_model:
            try:
                await session.interrupt(force=True)
            except Exception:
                logger.debug("No active model response to interrupt before final goodbye")
        logger.info("Playing final goodbye: %s", reason)
        audio = await fixed_realtime_audio(
            FINAL_GOODBYE_TEXT,
            label="Realtime final goodbye",
        )

        await _play_wav_bytes_direct(
            ctx.room,
            audio,
            label="Realtime final goodbye",
            track_name="realtime-final-goodbye",
            tail_silence_ms=max(
                0, _env_int("QWEN_AUDIO_REALTIME_PLAYOUT_TAIL_MS", 400)
            ),
            on_first_frame_queued=lambda: schedule_preseeded_assistant_turn(
                FINAL_GOODBYE_TEXT,
                source="programmatic_final_goodbye",
            ),
        )
        schedule_programmatic_assistant_turn(
            FINAL_GOODBYE_TEXT,
            source="programmatic_final_goodbye",
            persist_insights=False,
        )
        if not _customer_hangup_only(managed_job):
            # Mark the call before shutdown. The worker may otherwise surface
            # only a generic parent-process reason to the finalizer.
            if outbound_job and outbound_call_answered:
                outbound_call_ended_normally = True
            ctx.shutdown(reason=f"realtime programmatic goodbye completed: {reason}")

    async def play_final_goodbye(reason: str, *, interrupt_model: bool) -> None:
        nonlocal final_goodbye_task
        final_goodbye_task = _start_single_flight_task(
            final_goodbye_task,
            lambda: run_final_goodbye(reason, interrupt_model=interrupt_model),
            name="realtime-final-goodbye",
        )
        await asyncio.shield(final_goodbye_task)

    async def finish_after_customer_goodbye(text: str) -> None:
        """Persist transcript-derived facts and close without another model turn."""

        cancel_customer_silence_timer("customer said goodbye")
        if not phone_agent._business_result_saved:
            result = await PhoneAgent.save_call_result.__wrapped__(
                phone_agent,
                summary=text,
            )
            logger.info("Customer-goodbye result persistence: %s", result)
        await play_final_goodbye(
            "customer explicitly ended the conversation",
            interrupt_model=True,
        )

    async def interrupt_duplicate_assistant_response() -> None:
        try:
            await session.interrupt(force=True)
        except Exception:
            logger.debug("No active response to interrupt for duplicate suppression")

    async def close_wechat_followup_after_silence() -> None:
        timeout_seconds = _configured_float(
            "QWEN_REALTIME_WECHAT_CLOSE_TIMEOUT_SECONDS",
            3.0,
            minimum=1.0,
            maximum=10.0,
        )
        await asyncio.sleep(timeout_seconds)
        await play_final_goodbye(
            f"wechat follow-up received no reply for {timeout_seconds:.1f}s",
            interrupt_model=False,
        )

    def cancel_wechat_close_timer(reason: str, *, clear_waiting: bool = False) -> None:
        nonlocal awaiting_wechat_acknowledgement, wechat_close_task
        if wechat_close_task and not wechat_close_task.done():
            logger.info("Wechat follow-up close timer cancelled: %s", reason)
            wechat_close_task.cancel()
        wechat_close_task = None
        if clear_waiting:
            awaiting_wechat_acknowledgement = False

    def start_wechat_close_timer() -> None:
        nonlocal wechat_close_task
        cancel_wechat_close_timer("timer restarted")
        wechat_close_task = asyncio.create_task(
            close_wechat_followup_after_silence(),
            name="realtime-wechat-close-timeout",
        )

    def cancel_customer_silence_timer(reason: str) -> None:
        nonlocal customer_silence_task
        if customer_silence_task and not customer_silence_task.done():
            logger.info("Customer silence timer cancelled: %s", reason)
            customer_silence_task.cancel()
        customer_silence_task = None

    async def close_after_customer_silence() -> None:
        nonlocal outbound_call_ended_normally
        timeout_seconds = _configured_float(
            "QWEN_CUSTOMER_RESPONSE_TIMEOUT_SECONDS",
            5.0,
            minimum=1.0,
            maximum=30.0,
        )
        await asyncio.sleep(timeout_seconds)
        reason = f"客户连续{timeout_seconds:g}秒未回应，系统主动挂机"
        logger.info("%s: room=%s", reason, ctx.room.name)
        await phone_agent.persist_fallback_call_result(reason)
        try:
            await session.interrupt(force=True)
        except Exception:
            logger.debug("No active response to interrupt before silence hangup")
        # Room deletion can win the race against ctx.shutdown() and make the
        # framework report "parent process shutdown". Record that this is an
        # intentional, answered-call completion before deleting the room.
        if outbound_job and outbound_call_answered:
            outbound_call_ended_normally = True
        try:
            await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
        finally:
            ctx.shutdown(reason="customer response timeout")

    def start_customer_silence_timer() -> None:
        nonlocal customer_silence_task
        if not (task_prompt_override and outbound_job and assistant_business_turns > 0):
            return
        cancel_customer_silence_timer("timer restarted")
        customer_silence_task = asyncio.create_task(
            close_after_customer_silence(),
            name="customer-response-timeout",
        )

    async def finish_at_conversation_limit() -> None:
        await phone_agent.persist_fallback_call_result("已达到8轮对话上限")
        await play_final_goodbye(
            "maximum conversation turns reached",
            interrupt_model=True,
        )

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent) -> None:
        nonlocal metrics_event_count
        metrics.log_metrics(ev.metrics)
        metrics_event_count += 1
        if managed_job and insights_session_id and metrics_event_count % 10 == 1:
            sample_number = metrics_event_count
            sample = str(ev.metrics)[:4000]
            async def persist_metrics() -> None:
                try:
                    await _insights_event(
                        managed_job,
                        insights_session_id,
                        "agent.metrics",
                        {"sample": sample, "sample_number": sample_number},
                    )
                except Exception:
                    logger.exception("Unable to persist agent metrics sample")

            asyncio.create_task(persist_metrics())

    @session.on("speech_created")
    def _on_speech_created(ev) -> None:
        nonlocal current_speech_handle
        current_speech_handle = ev.speech_handle

    @session.on("function_tools_executed")
    def _on_function_tools_executed(ev) -> None:
        nonlocal wechat_notice_task
        if selected_pipeline != REALTIME_PIPELINE:
            return
        requested_tools = _programmatic_audio_tool_names(ev)
        if (
            requested_tools == {"end_call"}
            and task_prompt_override
            and outbound_job
            and not phone_agent._business_result_saved
        ):
            # Preserve the rejected end_call tool result so Qwen can continue
            # with the actual business request. Canceling it here would leave
            # the model unaware that identity confirmation was insufficient.
            logger.warning(
                "Leaving unsupported end_call result visible to model: call_id=%s",
                outbound_job.get("call_id"),
            )
            return
        programmatic_tools = _cancel_tool_reply_for_programmatic_audio(ev)
        if not programmatic_tools:
            return

        action = _programmatic_audio_action(programmatic_tools)
        if action == "wechat_notice":
            if "end_call" in programmatic_tools:
                logger.warning(
                    "Deferring batched end_call until after confirmed WeChat follow-up"
                )
            wechat_notice_task = _start_single_flight_task(
                wechat_notice_task,
                play_wechat_added_notice,
                name="realtime-wechat-added-notice",
            )
            return

        if action == "final_goodbye":
            if task_prompt_override and outbound_job and not phone_agent._business_result_saved:
                logger.warning(
                    "Ignoring unsupported end_call before evidence-backed result: call_id=%s",
                    outbound_job.get("call_id"),
                )
                return
            reason = "normal flow completed"
            for call in getattr(ev, "function_calls", []):
                if str(getattr(call, "name", "")) != "end_call":
                    continue
                try:
                    arguments = json.loads(str(getattr(call, "arguments", "") or "{}"))
                except json.JSONDecodeError:
                    arguments = {}
                if isinstance(arguments, dict) and str(arguments.get("reason") or "").strip():
                    reason = str(arguments["reason"]).strip()[:200]
                break
            asyncio.create_task(
                play_final_goodbye(reason, interrupt_model=True),
                name="realtime-end-call-goodbye",
            )
            return

    @session.on("user_state_changed")
    def _on_user_state_changed(ev) -> None:
        state = str(getattr(ev, "new_state", ""))
        if state == "speaking":
            cancel_customer_silence_timer("customer started speaking")
        if state == "speaking" and awaiting_wechat_acknowledgement:
            cancel_wechat_close_timer("customer started speaking")
        elif (
            state == "listening"
            and awaiting_wechat_acknowledgement
            and (wechat_close_task is None or wechat_close_task.done())
        ):
            start_wechat_close_timer()

    @session.on("agent_state_changed")
    def _on_agent_state_changed(ev) -> None:
        state = str(getattr(ev, "new_state", ""))
        if state == "listening":
            start_customer_silence_timer()
        elif state in {"thinking", "speaking"}:
            cancel_customer_silence_timer(f"agent state changed to {state}")

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev) -> None:
        nonlocal awaiting_wechat_acknowledgement, wechat_close_task
        nonlocal assistant_business_turns, customer_goodbye_task
        nonlocal last_assistant_utterance
        item = getattr(ev, "item", None)
        role = str(getattr(item, "role", ""))
        text = str(getattr(item, "text_content", "") or "").strip()
        if role not in {"user", "assistant"} or not text:
            return

        if role == "user":
            phone_agent.note_user_transcript(text)
            cancel_customer_silence_timer("customer transcript received")
            if (
                selected_pipeline == REALTIME_PIPELINE
                and task_prompt_override
                and outbound_job
                and _is_customer_goodbye(text)
            ):
                customer_goodbye_task = _start_single_flight_task(
                    customer_goodbye_task,
                    lambda: finish_after_customer_goodbye(text),
                    name="realtime-customer-goodbye",
                )
            if (
                task_prompt_override
                and assistant_business_turns >= max_conversation_turns - 1
            ):
                asyncio.create_task(
                    finish_at_conversation_limit(),
                    name="conversation-turn-limit",
                )
        else:
            normalized_assistant_text = _normalized_spoken_text(text)
            if (
                task_prompt_override
                and text != FINAL_GOODBYE_TEXT
                and normalized_assistant_text
                and normalized_assistant_text == last_assistant_utterance
            ):
                logger.warning(
                    "Suppressing duplicate assistant response: call_id=%s text=%s",
                    str((outbound_job or {}).get("call_id") or ""),
                    text,
                )
                asyncio.create_task(
                    interrupt_duplicate_assistant_response(),
                    name="interrupt-duplicate-assistant-response",
                )
                return
            phone_agent.note_assistant_transcript(text)
            if task_prompt_override and text != FINAL_GOODBYE_TEXT:
                last_assistant_utterance = normalized_assistant_text
                assistant_business_turns += 1
                if assistant_business_turns >= max_conversation_turns:
                    asyncio.create_task(
                        finish_at_conversation_limit(),
                        name="conversation-turn-limit",
                    )

        _append_local_transcript(
            room_name=ctx.room.name,
            role=role,
            text=text,
            item_id=str(getattr(item, "id", "") or ""),
        )

        if selected_pipeline == REALTIME_PIPELINE:
            if role == "user" and awaiting_wechat_acknowledgement:
                cancel_wechat_close_timer("customer replied")
                awaiting_wechat_acknowledgement = False
                if _is_short_wechat_acknowledgement(text):
                    async def goodbye_after_wechat_notice() -> None:
                        try:
                            await session.interrupt(force=True)
                        except Exception:
                            logger.debug(
                                "No active model response to interrupt after WeChat acknowledgement"
                            )
                        if wechat_notice_task and not wechat_notice_task.done():
                            await asyncio.shield(wechat_notice_task)
                        await play_final_goodbye(
                            "customer acknowledged the WeChat request",
                            interrupt_model=False,
                        )

                    wechat_close_task = asyncio.create_task(
                        goodbye_after_wechat_notice(),
                        name="realtime-wechat-ack-goodbye",
                    )
            elif role == "assistant" and _is_wechat_added_notice(text):
                # Transcript creation does not prove that remote SIP playout
                # completed. Only complete_wechat_followup may enter the timed
                # closing state; its handler interrupts and replays fixed audio.
                logger.warning(
                    "Model emitted reserved WeChat notice; awaiting deterministic tool path"
                )

        if not managed_job or not insights_session_id:
            return
        event_type = "user.transcript" if role == "user" else "agent.response"
        item_id = str(getattr(item, "id", "") or "")

        async def persist_conversation_item() -> None:
            try:
                await _insights_event(
                    managed_job,
                    insights_session_id,
                    event_type,
                    {"text": text, "item_id": item_id},
                )
            except Exception:
                logger.exception("Unable to persist conversation item")

        task = asyncio.create_task(
            persist_conversation_item(),
            name=f"persist-conversation-{item_id or role}",
        )
        # Terminal call state is written only after this set is drained by the
        # shutdown finalizer. The result forwarder can therefore read the full
        # database transcript as soon as it observes a completed call.
        phone_agent._pending_business_event_tasks.add(task)
        task.add_done_callback(phone_agent._pending_business_event_tasks.discard)

    async def cancel_wechat_timer_on_shutdown(_reason: str = "") -> None:
        cancel_wechat_close_timer("session shutdown", clear_waiting=True)
        cancel_customer_silence_timer("session shutdown")
        await _cancel_task(wechat_notice_task)

    shutdown_finalizers.append(cancel_wechat_timer_on_shutdown)

    async def log_usage(_reason: str = "") -> None:
        logger.info("Usage: %s", session.usage)

    shutdown_finalizers.append(log_usage)

    if managed_job and insights_session_id:
        async def finalize_insights(reason: str) -> None:
            await _cancel_task(console_task)
            try:
                insights_status = (
                    "failed"
                    if "failed" in reason.lower() or "rejected" in reason.lower()
                    else "completed"
                )
                if outbound_job:
                    shutdown_status, _ = _outbound_shutdown_transition(
                        answered=outbound_call_answered,
                        reason=reason,
                        normal_disconnect=outbound_call_ended_normally,
                    )
                    insights_status = (
                        "completed" if shutdown_status == "completed" else "failed"
                    )
                await _insights_record_session_usage(
                    managed_job, insights_session_id, str(session.usage)
                )
                await _insights_event(
                    managed_job,
                    insights_session_id,
                    "agent.stopped",
                    {"reason": reason[:1000]},
                )
                await _insights_close_session(
                    managed_job,
                    insights_session_id,
                    insights_status,
                )
            except Exception:
                logger.exception(
                    "Unable to finalize Insights session: session_id=%s",
                    insights_session_id,
                )

        shutdown_finalizers.append(finalize_insights)

    if managed_job:
        async def finalize_telephony_call(reason: str) -> None:
            nonlocal telephony_terminal
            await _cancel_task(heartbeat_task)
            await stop_managed_recording()
            if telephony_terminal:
                return
            try:
                final_status = "completed"
                failure_code = ""
                if str(managed_job.get("direction") or "") == "outbound":
                    final_status, failure_code = _outbound_shutdown_transition(
                        answered=outbound_call_answered,
                        reason=reason,
                        normal_disconnect=outbound_call_ended_normally,
                    )
                await _telephony_transition(
                    managed_job,
                    final_status,
                    failure_code=failure_code,
                    failure_detail=reason[:500],
                )
                telephony_terminal = True
            except Exception:
                logger.exception(
                    "Unable to finalize managed call: call_id=%s",
                    managed_job["call_id"],
                )

        shutdown_finalizers.append(finalize_telephony_call)

    async def finalize_job(reason: str) -> None:
        try:
            # Persist call state and stop the lease before slower Insights writes.
            for finalizer in reversed(shutdown_finalizers):
                await finalizer(reason)
        finally:
            await _close_telephony_control_client()

    ctx.add_shutdown_callback(finalize_job)

    initial_chat_ctx = _initial_realtime_chat_context(
        selected_pipeline=selected_pipeline,
        realtime_opening=realtime_opening,
        task_prompt_override=task_prompt_override,
    )

    phone_agent = PhoneAgent(
        ctx=ctx,
        managed_job=managed_job,
        sip_identity=managed_sip_identity,
        instructions=realtime_instructions or None,
        chat_ctx=initial_chat_ctx,
    )
    # The blind A/B tool uses this exact output object so its RoomIO arm is the
    # same production path used by Qwen Realtime responses.
    phone_agent._agent_session = session

    async def flush_pending_business_events(_reason: str = "") -> None:
        await phone_agent.wait_for_pending_business_events()

    shutdown_finalizers.append(flush_pending_business_events)

    async def flush_programmatic_turns(_reason: str = "") -> None:
        pending = [task for task in programmatic_turn_tasks if not task.done()]
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=5.0
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out waiting for %d programmatic assistant event(s)",
                len(pending),
            )

    shutdown_finalizers.append(flush_programmatic_turns)
    session_start_task = asyncio.create_task(
        session.start(
            agent=phone_agent,
            room=ctx.room,
            room_options=room_io.RoomOptions(
                audio_input=room_io.AudioInputOptions(),
                audio_output=room_io.AudioOutputOptions(
                    sample_rate=ROOM_AUDIO_SAMPLE_RATE,
                    num_channels=QwenTTS.num_channels_count,
                ),
            ),
        ),
        name="agent-session-start",
    )

    early_realtime_opening_task: asyncio.Task[str | None] | None = None
    if selected_pipeline == REALTIME_PIPELINE and not managed_job:
        async def play_opening_while_realtime_connects() -> str | None:
            try:
                # session.start connects the same JobContext. This idempotent
                # call waits only for the room connection, while the Realtime
                # WebSocket continues warming in session_start_task.
                await ctx.connect()
                await _wait_for_active_sip_participant(
                    ctx.room,
                    timeout=_configured_float(
                        "QWEN_SIP_OPENING_WAIT_SECONDS",
                        45.0,
                        minimum=1.0,
                        maximum=120.0,
                    ),
                )
                opening_text = realtime_opening
                audio = await fixed_realtime_audio(
                    opening_text,
                    label="Realtime opening",
                )
                await _play_wav_bytes_direct(
                    ctx.room,
                    audio,
                    label="Realtime opening",
                    track_name="realtime-opening",
                    on_first_frame_queued=lambda: schedule_preseeded_assistant_turn(
                        opening_text,
                        source="programmatic_opening_during_realtime_warmup",
                    ),
                )
                return opening_text
            except Exception:
                logger.exception(
                    "Unable to play opening while Realtime connects; using normal fallback"
                )
                return None

        early_realtime_opening_task = asyncio.create_task(
            play_opening_while_realtime_connects(),
            name="realtime-opening-during-session-start",
        )

    try:
        await session_start_task
    except Exception:
        await _cancel_task(early_realtime_opening_task)
        raise

    realtime_scene_apply_task: asyncio.Task[None] | None = None
    if selected_pipeline == REALTIME_PIPELINE and realtime_scene_task is not None:
        async def apply_realtime_scene_when_ready() -> None:
            scene = await realtime_scene_task
            if not scene:
                return
            updated_instructions = load_realtime_instructions(
                root=ROOT,
                session_id=ctx.room.name,
                scene_id=scene_id,
                prompt_override=task_prompt_override,
                customer_name=str((managed_job or {}).get("customer_name") or ""),
                customer_company=str((managed_job or {}).get("customer_company") or ""),
                customer_phone=str(
                    (managed_job or {}).get("phone_number")
                    or (managed_job or {}).get("source_number")
                    or ""
                ),
                customer_profile=str((managed_job or {}).get("customer_profile") or ""),
                scene=scene,
            )
            await phone_agent.update_instructions(updated_instructions)
            logger.info(
                "Realtime scene applied without blocking opening: scene=%s",
                scene_id,
            )

        realtime_scene_apply_task = asyncio.create_task(
            apply_realtime_scene_when_ready(),
            name=f"realtime-scene-apply-{scene_id}",
        )

        async def cancel_realtime_scene_apply(_reason: str = "") -> None:
            await _cancel_task(realtime_scene_apply_task)

        shutdown_finalizers.append(cancel_realtime_scene_apply)

    if managed_job and insights_session_id:
        console_task = asyncio.create_task(
            _console_command_loop(
                ctx,
                session,
                managed_job,
                insights_session_id,
                managed_sip_identity,
            )
        )

    async def handle_sip_disconnect() -> None:
        """Persist a fallback result, then release the disconnected room."""

        if outbound_call_answered:
            await phone_agent.persist_fallback_call_result("客户主动挂断")
        try:
            await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
            logger.info(
                "Released room immediately after customer disconnect: call_id=%s room=%s",
                str((managed_job or {}).get("call_id") or ""),
                ctx.room.name,
            )
        except Exception:
            logger.exception(
                "Unable to delete room after customer disconnect: room=%s",
                ctx.room.name,
            )
        # Deleting the room causes RoomCompositeEgress to finish. Do not issue
        # a redundant StopEgress RPC; only wait for completion.
        await stop_managed_recording(request_stop=False)
        await session.aclose()
        ctx.shutdown(reason="customer sip participant disconnected")

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        nonlocal sip_disconnect_task, outbound_call_ended_normally
        if participant.identity != managed_sip_identity or telephony_terminal:
            return
        if outbound_job and not outbound_call_answered:
            logger.info(
                "SIP participant disconnected before answer; dial result owns cleanup: "
                "call_id=%s identity=%s",
                str((managed_job or {}).get("call_id") or ""),
                participant.identity,
            )
            return
        if sip_disconnect_task is not None and not sip_disconnect_task.done():
            return
        logger.info(
            "Customer SIP participant disconnected; releasing room immediately: "
            "call_id=%s identity=%s",
            str((managed_job or {}).get("call_id") or ""),
            participant.identity,
        )
        outbound_call_ended_normally = True
        sip_disconnect_task = asyncio.create_task(handle_sip_disconnect())

    async def ensure_required_recording() -> bool:
        nonlocal recording_egress_ids, recording_storage_uri
        nonlocal telephony_terminal
        if not managed_job or str(managed_job.get("recording_mode") or "off") != "always":
            return True
        if recording_egress_ids:
            return True
        try:
            def capture_created_egress(
                egress_ids: list[str],
                storage_uri: str,
            ) -> None:
                nonlocal recording_egress_ids, recording_storage_uri
                recording_egress_ids = egress_ids
                recording_storage_uri = storage_uri

            await _start_managed_recording(
                ctx,
                managed_job,
                on_created=capture_created_egress,
            )
            return True
        except Exception as exc:
            logger.exception(
                "Mandatory call recording failed to start: call_id=%s",
                managed_job["call_id"],
            )
            try:
                await _telephony_record_recording(
                    managed_job,
                    egress_id=f"setup:{managed_job['call_id']}",
                    status="failed",
                    storage_uri="",
                )
                await _telephony_transition(
                    managed_job,
                    "failed",
                    failure_code="recording_start_failed",
                    failure_detail=type(exc).__name__,
                )
                telephony_terminal = True
            except Exception:
                logger.exception("Unable to persist mandatory recording failure")
            await _cancel_task(heartbeat_task)
            ctx.shutdown(reason="mandatory recording failed")
            return False

    outbound_identity_opening = ""
    outbound_identity_audio: bytes | None = None
    outbound_realtime_io_suspended = False
    if (
        outbound_job
        and task_prompt_override
        and selected_pipeline == REALTIME_PIPELINE
    ):
        # Keep the Realtime model from hearing line noise or its own fixed
        # opening and producing a duplicate response. Direct playout uses an
        # independent room track, so it remains audible while this IO is gated.
        _set_session_audio_io_enabled(session, False)
        outbound_realtime_io_suspended = True
        outbound_identity_opening = _outbound_identity_opening(
            str(outbound_job.get("customer_name") or "")
        )
        # Synthesize before the PSTN side effect so answer-to-audio latency is
        # only the room playout latency, not a model or TTS network round trip.
        outbound_identity_audio = await fixed_realtime_audio(
            outbound_identity_opening,
            label="Realtime outbound identity opening",
        )

    if outbound_job:
        call_active = False
        amd_category = ""
        ringing_timeout = _bounded_duration(
            "CLOUD_PARITY_TELEPHONY_RINGING_TIMEOUT_SECONDS",
            45,
            minimum=10,
            maximum=120,
        )
        try:
            # Persist dialing before causing the external PSTN side effect.
            await _telephony_transition(
                outbound_job,
                "dialing",
                room_name=ctx.room.name,
            )
            heartbeat_task = asyncio.create_task(_telephony_heartbeat(outbound_job))

            registration = await asyncio.to_thread(
                register_from_env,
                _provider_registration_env_prefix(
                    str(outbound_job.get("trunk_provider") or "")
                ),
            )
            if registration is not None:
                logger.info(
                    "SIP registration completed before outbound dial: "
                    "call_id=%s status=%s realm=%s expires=%s",
                    outbound_job["call_id"],
                    registration.status_code,
                    registration.realm,
                    registration.expires,
                )

            # Start Chrome RoomComposite before dialing.  The mandatory
            # recording must be active before disclosure and business audio.
            if str(outbound_job.get("recording_mode") or "off") == "always":
                recording_start_task = asyncio.create_task(ensure_required_recording())

            async def dial_and_activate() -> None:
                nonlocal call_active, outbound_call_answered
                participant = await ctx.api.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        room_name=ctx.room.name,
                        sip_trunk_id=str(outbound_job["livekit_trunk_id"]),
                        sip_call_to=_provider_dial_target(
                            str(outbound_job["phone_number"]),
                            str(outbound_job.get("trunk_provider") or ""),
                        ),
                        sip_number=str(outbound_job.get("source_number") or ""),
                        participant_identity=managed_sip_identity,
                        participant_name="AI voice call",
                        participant_metadata=json.dumps(
                            {"call_id": outbound_job["call_id"]}, separators=(",", ":")
                        ),
                        wait_until_answered=True,
                        ringing_timeout=ringing_timeout,
                        max_call_duration=_bounded_duration(
                            "CLOUD_PARITY_TELEPHONY_MAX_CALL_DURATION_SECONDS",
                            1800,
                            minimum=60,
                            maximum=14400,
                        ),
                        # PCMU sends a continuous RTP packet clock, including
                        # silence. If that clock disappears, release the call
                        # promptly; a real SIP BYE still disconnects immediately.
                        media=_outbound_sip_media_config(),
                    )
                )
                # wait_until_answered=True means a successful API return is the
                # authoritative answer boundary. Set this before any later
                # awaits so an immediate customer hangup is not misclassified.
                outbound_call_answered = True
                await ctx.wait_for_participant(identity=managed_sip_identity)
                await _telephony_transition(
                    outbound_job,
                    "active",
                    provider_call_id=str(participant.sip_call_id or ""),
                    room_name=ctx.room.name,
                )
                call_active = True

            amd_requested = os.getenv("QWEN_AMD_ENABLED", "true").strip().lower() in {
                "1", "true", "yes", "on"
            }
            # The current AMD flow must hear the callee before classifying the
            # answer. Managed task calls instead promise an immediate AI-first
            # opening, so do not hold their first sentence behind that listener.
            amd_enabled = amd_requested and not (
                task_prompt_override and selected_pipeline == REALTIME_PIPELINE
            )
            if amd_requested and not amd_enabled:
                logger.info(
                    "Skipping AMD to preserve AI-first managed task opening: call_id=%s",
                    outbound_job["call_id"],
                )
            if amd_enabled:
                detector = AMD(
                    session,
                    participant_identity=managed_sip_identity,
                    ivr_detection=os.getenv("QWEN_AMD_IVR_DETECTION", "true").strip().lower()
                    in {"1", "true", "yes", "on"},
                    wait_until_finished=os.getenv(
                        "QWEN_AMD_WAIT_UNTIL_FINISHED", "true"
                    ).strip().lower() in {"1", "true", "yes", "on"},
                )
                async with detector:
                    await dial_and_activate()
                    try:
                        amd_result = await detector.execute()
                        amd_category = str(
                            getattr(amd_result.category, "value", amd_result.category)
                        )
                    except Exception:
                        amd_category = "uncertain"
                        logger.exception(
                            "Answering machine detection failed; treating as uncertain: call_id=%s",
                            outbound_job["call_id"],
                        )
            else:
                await dial_and_activate()

            if amd_category:
                disposition = {
                    "machine-vm": "voicemail_detected",
                    "machine-unavailable": "mailbox_unavailable",
                    "machine-ivr": "ivr_detected",
                    "human": "human_answered",
                    "uncertain": "amd_uncertain",
                }.get(amd_category, "")
                await _telephony_record_result(
                    outbound_job,
                    answering_machine_category=amd_category,
                    disposition=disposition,
                )
                logger.info(
                    "Answering machine detection completed: call_id=%s category=%s",
                    outbound_job["call_id"],
                    amd_category,
                )
            if amd_category == "machine-vm":
                recording_ready = (
                    await recording_start_task
                    if recording_start_task is not None
                    else await ensure_required_recording()
                )
                if not recording_ready:
                    return
                await _play_recording_disclosure(session, outbound_job)
                voicemail = os.getenv(
                    "QWEN_AMD_VOICEMAIL_MESSAGE",
                    "您好，这里是智能语音服务。稍后我们会再次联系您，谢谢。",
                ).strip()
                speech = session.say(voicemail, allow_interruptions=False)
                await speech.wait_for_playout()
                await _telephony_transition(
                    outbound_job,
                    "completed",
                    failure_detail="amd:machine-vm",
                )
                telephony_terminal = True
                await _cancel_task(heartbeat_task)
                ctx.shutdown(reason="amd:machine-vm")
                return
            if amd_category == "machine-unavailable":
                await _telephony_transition(
                    outbound_job,
                    "completed",
                    failure_detail="amd:machine-unavailable",
                )
                telephony_terminal = True
                await _cancel_task(heartbeat_task)
                ctx.shutdown(reason="amd:machine-unavailable")
                return
        except SIPRegistrationError as exc:
            logger.error(
                "SIP registration failed before outbound dial: call_id=%s error=%s",
                outbound_job["call_id"],
                exc,
            )
            await _telephony_transition(
                outbound_job,
                "failed",
                failure_code="sip_registration_failed",
                failure_detail=str(exc)[:500],
                retryable=True,
            )
            telephony_terminal = True
            await _cancel_task(heartbeat_task)
            ctx.shutdown(reason="outbound SIP registration failed")
            return
        except api.SipCallError as exc:
            status, code, retryable = _sip_failure(exc)
            await _telephony_transition(
                outbound_job,
                status,
                failure_code=code,
                failure_detail=str(getattr(exc, "sip_status", ""))[:500],
                retryable=retryable,
                retry_delay_seconds=(
                    5 if retryable and re.fullmatch(r"sip_5[0-9]{2}", code) else 30
                ),
            )
            telephony_terminal = True
            await _cancel_task(heartbeat_task)
            ctx.shutdown(reason=f"outbound dial failed: {code}")
            return
        except Exception as exc:
            ringing_timeout_failure = _livekit_ringing_timeout_failure(exc)
            if ringing_timeout_failure is not None:
                status, code, retryable = ringing_timeout_failure
                await _telephony_transition(
                    outbound_job,
                    status,
                    failure_code=code,
                    failure_detail=(
                        f"no answer before {ringing_timeout.seconds}-second ringing timeout"
                    ),
                    retryable=retryable,
                )
                telephony_terminal = True
                await _cancel_task(heartbeat_task)
                ctx.shutdown(reason=f"outbound dial failed: {code}")
                return
            if call_active:
                logger.exception(
                    "Post-answer setup failed; continuing without AMD: call_id=%s",
                    outbound_job["call_id"],
                )
                try:
                    await _telephony_record_result(
                        outbound_job,
                        answering_machine_category="uncertain",
                        disposition="amd_error",
                    )
                except Exception:
                    logger.exception("Unable to persist AMD fallback result")
            else:
                logger.exception(
                    "Outbound call setup result is uncertain; scheduling reconciliation: "
                    "call_id=%s",
                    outbound_job["call_id"],
                )
                try:
                    await _telephony_transition(
                        outbound_job,
                        "reconciling",
                        failure_code="sip_setup_result_uncertain",
                        failure_detail=type(exc).__name__,
                    )
                    telephony_terminal = True
                except Exception:
                    logger.exception("Unable to persist outbound setup failure")
                await _cancel_task(heartbeat_task)
                ctx.shutdown(reason="outbound call setup failed")
                return

    recording_ready = (
        await recording_start_task
        if recording_start_task is not None
        else await ensure_required_recording()
    )
    if not recording_ready:
        return

    if managed_job:
        await _play_recording_disclosure(session, managed_job)

    if selected_pipeline == CLASSIC_PIPELINE:
        # Warm the classic LLM concurrently with the welcome message so the
        # first caller utterance uses an already-active model endpoint.
        start_llm_warmup_background_thread()
        opening = await fetch_dialogue_opening(ctx.room.name)
        if opening and opening.get("text"):
            audio = opening.get("audio") if isinstance(opening.get("audio"), dict) else {}
            audio_url = str(opening.get("audio_url") or audio.get("url") or "").strip()
            speech = session.say(str(opening["text"]), allow_interruptions=not audio_url)
            if audio_url:
                schedule_interrupt_gate(
                    speech,
                    text=str(opening["text"]),
                    audio_url=audio_url,
                    result=opening,
                )
            await speech.wait_for_playout()
        else:
            await play_greeting_audio_direct(ctx.room)
    else:
        if task_prompt_override:
            # The assistant must speak first. Use deterministic media instead
            # of response.create, which requires a real user message on Qwen.
            if outbound_identity_audio is None:
                outbound_identity_opening = _outbound_identity_opening(
                    str((managed_job or {}).get("customer_name") or "")
                )
                outbound_identity_audio = await fixed_realtime_audio(
                    outbound_identity_opening,
                    label="Realtime outbound identity opening",
                )
            try:
                try:
                    await session.interrupt(force=True)
                except Exception:
                    logger.debug("No active response before fixed outbound opening")
                await _play_wav_bytes_direct(
                    ctx.room,
                    outbound_identity_audio,
                    label="Realtime outbound identity opening",
                    track_name="realtime-outbound-identity-opening",
                    on_first_frame_queued=lambda: schedule_preseeded_assistant_turn(
                        outbound_identity_opening,
                        source="programmatic_outbound_identity_opening",
                    ),
                )
            finally:
                # Cancel any response created before the gate took effect,
                # then let Qwen hear the customer's first real reply.
                try:
                    await session.interrupt(force=True)
                except Exception:
                    logger.debug("No duplicate opening response to interrupt")
                if outbound_realtime_io_suspended:
                    _set_session_audio_io_enabled(session, True)
                    outbound_realtime_io_suspended = False
            phone_agent.note_assistant_transcript(outbound_identity_opening)
            assistant_business_turns += 1
            start_customer_silence_timer()
        else:
            # In direct/manual calls the fixed opening may already have played
            # on an independent track while session.start warmed Qwen. Once the
            # Realtime session is ready, synchronize only the assistant text.
            early_opening_text = (
                await early_realtime_opening_task
                if early_realtime_opening_task is not None
                else None
            )
            if early_opening_text:
                logger.info(
                    "realtime_opening_playout_completed context_preseeded=true "
                    "mode=parallel text=%s",
                    early_opening_text,
                )
            else:
                # The opening text is already in Qwen's initial context. Play only
                # deterministic media, avoiding a second generated/duplicated turn.
                started = perf_counter()
                opening_audio = await fixed_realtime_audio(
                    realtime_opening,
                    label="Realtime opening",
                )
                await _play_wav_bytes_direct(
                    ctx.room,
                    opening_audio,
                    label="Realtime opening",
                    track_name="realtime-opening",
                    on_first_frame_queued=lambda: schedule_preseeded_assistant_turn(
                        realtime_opening,
                        source="programmatic_opening_after_realtime_warmup",
                    ),
                )
                logger.info(
                    "realtime_opening_playout_completed context_preseeded=true "
                    "mode=post_start elapsed=%.3fs text=%s",
                    perf_counter() - started,
                    realtime_opening,
                )


if __name__ == "__main__":
    cli.run_app(server)
