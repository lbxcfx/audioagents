from __future__ import annotations

import asyncio
import audioop
import io
import logging
import os
from pathlib import Path
import threading
from time import perf_counter
import wave

from dotenv import load_dotenv
import httpx
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    AutoSubscribe,
    JobContext,
    MetricsCollectedEvent,
    TurnHandlingOptions,
    cli,
    metrics,
    room_io,
    stt,
    utils,
)
from livekit.plugins import openai, silero
from openai import AsyncOpenAI

from dialogue_llm import ScriptFirstLLM
from qwen_providers import QwenASR, QwenRealtimeASR, QwenTTS


ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "qwen-telephony" / "config" / "local.env", override=False)

logger = logging.getLogger("qwen-phone-agent")

GREETING_TEXT = "Hello, I am your voice assistant. We can start the call now."
GREETING_AUDIO_PATH = ROOT / "qwen-telephony" / "cache" / "greeting.wav"
GREETING_ROOM_AUDIO_PATH = ROOT / "qwen-telephony" / "cache" / "greeting_24k.wav"
GREETING_AUDIO_LOCK_PATH = ROOT / "qwen-telephony" / "cache" / "greeting.wav.lock"
ROOM_AUDIO_SAMPLE_RATE = int(os.getenv("QWEN_ROOM_AUDIO_SAMPLE_RATE", str(QwenTTS.sample_rate_hz)))


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


def prewarm_process(_proc) -> None:
    try:
        asyncio.run(ensure_greeting_audio_cache())
    except Exception:
        logger.exception("Greeting audio cache prewarm failed")


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


async def fetch_dialogue_opening_text(session_id: str) -> str | None:
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
            response = await client.post(start_url, json=payload)
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
        return str(body["text"])
    logger.info("Dialogue opening unavailable, route=%s reason=%s", body.get("route_type"), body.get("reason"))
    return None


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
    await asyncio.sleep(0.5)
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


async def warm_up_qwen_llm() -> None:
    if os.getenv("QWEN_LLM_WARMUP", "true").lower() not in {"1", "true", "yes", "on"}:
        return

    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    if not dashscope_key:
        logger.warning("skip LLM warm-up: DASHSCOPE_API_KEY is missing")
        return

    client = AsyncOpenAI(
        api_key=dashscope_key,
        base_url=os.getenv("QWEN_OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    started = perf_counter()
    try:
        await client.chat.completions.create(
            model=os.getenv("QWEN_LLM_MODEL", "qwen-plus"),
            messages=[{"role": "user", "content": "just warm up"}],
            temperature=0,
            max_tokens=1,
            timeout=10,
        )
        logger.info("LLM warm-up completed in %.2fs", perf_counter() - started)
    except Exception:
        logger.exception("LLM warm-up failed")
    finally:
        await client.close()


class PhoneAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "你是一个中文语音电话助手，负责直接回答用户问题。"
                "回答要准确、简洁、自然，适合电话语音播报。"
                "优先给出结论，再补充必要说明。"
                "如果没有听清用户问题，只回答：我没有听清，请再说一遍。"
                "通常不超过三句话，除非用户明确要求详细解释。"
            )
        )

    async def on_enter(self) -> None:
        logger.info("PhoneAgent.on_enter: ready")


server = AgentServer(
    port=int(os.getenv("QWEN_AGENT_PORT", "18081")),
    http_proxy=None,
    setup_fnc=prewarm_process,
    load_threshold=float(os.getenv("QWEN_AGENT_LOAD_THRESHOLD", "0.95")),
)


@server.rtc_session(agent_name=os.getenv("QWEN_AGENT_EXPLICIT_NAME", os.getenv("LIVEKIT_AGENT_NAME", "")))
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    ctx.log_context_fields = {"room": ctx.room.name}

    start_llm_warmup_background_thread()

    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    qwen_base_url = os.getenv(
        "QWEN_OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )

    use_realtime_asr = os.getenv("QWEN_USE_REALTIME_ASR", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    asr_provider = (
        QwenRealtimeASR()
        if use_realtime_asr
        else stt.StreamAdapter(
            stt=QwenASR(),
            vad=silero.VAD.load(
                min_speech_duration=0.05,
                min_silence_duration=0.25,
                prefix_padding_duration=0.2,
                activation_threshold=0.45,
                sample_rate=8000,
            ),
        )
    )
    logger.info(
        "ASR provider selected: %s",
        "qwen-realtime-websocket" if use_realtime_asr else "qwen-http-vad-adapter",
    )

    session = AgentSession(
        stt=asr_provider,
        llm=ScriptFirstLLM(
            upstream=openai.LLM(
                model=os.getenv("QWEN_LLM_MODEL", "qwen-plus"),
                api_key=dashscope_key,
                base_url=qwen_base_url,
            ),
            session_id=ctx.room.name,
            scene_id=int(os.getenv("QWEN_DIALOGUE_SCENE_ID", "0")) or None,
            dialogue_url=os.getenv("QWEN_DIALOGUE_URL", "http://127.0.0.1:8090/api/dialogue/turn"),
            timeout=float(os.getenv("QWEN_DIALOGUE_TIMEOUT", "0.8")),
        ),
        tts=QwenTTS(),
        turn_handling=TurnHandlingOptions(
            endpointing={
                "mode": "fixed",
                "min_delay": 0.1,
                "max_delay": 0.6,
            },
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

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent) -> None:
        metrics.log_metrics(ev.metrics)

    async def log_usage() -> None:
        logger.info("Usage: %s", session.usage)

    ctx.add_shutdown_callback(log_usage)

    await session.start(
        agent=PhoneAgent(),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(),
            audio_output=room_io.AudioOutputOptions(
                sample_rate=ROOM_AUDIO_SAMPLE_RATE,
                num_channels=QwenTTS.num_channels_count,
            ),
        ),
    )

    opening_text = await fetch_dialogue_opening_text(ctx.room.name)
    if opening_text:
        await play_text_audio_direct(ctx.room, opening_text)
    else:
        await play_greeting_audio_direct(ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
