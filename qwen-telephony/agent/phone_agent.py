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
import threading
from time import perf_counter
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
from livekit.agents.inference_runner import _InferenceRunner
from livekit.plugins import openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from openai import AsyncOpenAI

from dialogue_llm import ScriptFirstLLM
from qwen_audio_realtime import (
    CLASSIC_PIPELINE,
    QwenAudioRealtimeModel,
    load_realtime_instructions,
    voice_pipeline,
)
from qwen_providers import QwenASR, QwenRealtimeASR, QwenTTS, register_recorded_audio


# Importing livekit.plugins.turn_detector registers both its English and
# multilingual inference runners. This service only constructs
# MultilingualModel, so avoid loading a second, unused English ONNX model in
# every worker process. The multilingual runner remains registered.
_InferenceRunner.registered_runners.pop("lk_end_of_utterance_en", None)


def _provider_dial_target(phone_number: str) -> str:
    """Translate an E.164 contact number to the carrier's SIP dial string."""
    number = phone_number.strip()
    prefix = os.getenv("QWEN_SIP_DIAL_PREFIX", "").strip()
    if not prefix:
        return number
    if not prefix.isdigit():
        raise ValueError("QWEN_SIP_DIAL_PREFIX must contain digits only")
    normalized = number.removeprefix("+")
    if not normalized.isdigit():
        raise ValueError("outbound phone number must be E.164 digits")
    strip_country_code = os.getenv("QWEN_SIP_STRIP_COUNTRY_CODE", "").strip()
    if strip_country_code:
        if not strip_country_code.isdigit():
            raise ValueError("QWEN_SIP_STRIP_COUNTRY_CODE must contain digits only")
        if normalized.startswith(strip_country_code):
            normalized = normalized[len(strip_country_code) :]
    return f"{prefix}{normalized}"


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


def prewarm_process(proc: JobProcess) -> None:
    # Each LiveKit job process owns one preloaded VAD model. Streams created by
    # AgentSession and StreamAdapter keep their own state, while the ONNX model
    # is loaded only once per process.
    proc.userdata["vad"] = _build_session_vad()
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
            return body if isinstance(body, dict) else None
    except Exception:
        logger.exception("Realtime scene fetch failed; using default prompt: scene=%s", scene_id)
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
    session: AgentSession,
    job: dict[str, Any],
) -> tuple[str, str]:
    if str(job.get("recording_mode") or "off") != "always":
        return "", ""
    disclosure = str(job.get("recording_disclosure_text") or "").strip()
    if not disclosure:
        raise RuntimeError("recording disclosure text is missing")
    bucket = os.getenv("QWEN_RECORDING_S3_BUCKET", "").strip()
    region = os.getenv("QWEN_RECORDING_S3_REGION", "").strip()
    if not bucket or not region:
        raise RuntimeError("recording requires QWEN_RECORDING_S3_BUCKET and region")
    prefix = os.getenv("QWEN_RECORDING_S3_PREFIX", "telephony-recordings").strip(" /")
    if not prefix or ".." in prefix:
        raise RuntimeError("invalid QWEN_RECORDING_S3_PREFIX")

    notice = session.say(disclosure, allow_interruptions=False)
    await notice.wait_for_playout()
    filepath = f"{prefix}/{job['project_id']}/{job['call_id']}.mp3"
    upload = api.S3Upload(
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
            file_outputs=[
                api.EncodedFileOutput(
                    file_type=api.EncodedFileType.MP3,
                    filepath=filepath,
                    s3=upload,
                )
            ],
        )
    )
    egress_id = str(info.egress_id or "")
    if not egress_id:
        raise RuntimeError("LiveKit Egress did not return an egress id")
    storage_uri = f"s3://{bucket}/{filepath}"
    await _telephony_record_recording(
        job,
        egress_id=egress_id,
        status="active",
        storage_uri=storage_uri,
    )
    return egress_id, storage_uri


def _telephony_heartbeat_interval(job: dict[str, Any]) -> float:
    requested = max(1.0, min(float(job.get("heartbeat_seconds") or 10), 60.0))
    lease_seconds = max(3.0, float(job.get("lease_seconds") or requested * 3))
    return max(1.0, min(requested, lease_seconds / 3.0))


def _outbound_shutdown_transition(
    *, answered: bool, reason: str
) -> tuple[str, str]:
    normalized = reason.strip().lower()
    if not answered:
        return "reconciling", "agent_shutdown_before_answer"
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
        return "no_answer", f"sip_{code}", True
    return "failed", f"sip_{code}" if code else "sip_error", code >= 500 or code == 0


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


class PhoneAgent(Agent):
    def __init__(
        self,
        *,
        ctx: JobContext | None = None,
        managed_job: dict[str, Any] | None = None,
        sip_identity: str = "",
        instructions: str | None = None,
    ) -> None:
        self._job_ctx = ctx
        self._managed_job = managed_job
        self._sip_identity = sip_identity
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
            ).strip()
        )

    async def on_enter(self) -> None:
        logger.info("PhoneAgent.on_enter: ready")

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

    @function_tool(description="Save a faithful summary before normally completing the call.")
    async def save_call_result(self, summary: str, intent_label: str = "") -> str:
        if not summary.strip():
            return "通话摘要为空，未保存。"
        saved = await self._record_realtime_business_event(
            "call.result",
            {
                "summary": summary.strip()[:4000],
                "intent_label": intent_label.strip().upper()[:10],
            },
        )
        return "通话结果已保存。" if saved else "通话结果暂时无法保存。"

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
            "End the current call after the final spoken sentence has had time to play. "
            "Use only for a completed flow or an explicit customer rejection."
        )
    )
    async def end_call(self, ctx: RunContext, reason: str) -> str:
        if not self._job_ctx:
            return "当前通话不支持自动挂机。"
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
            os.getenv("QWEN_AUDIO_REALTIME_MODEL", "qwen-audio-3.0-realtime-flash"),
        )

    hangup_task: asyncio.Task[None] | None = None
    current_speech_handle: Any = None
    recording_egress_id = ""
    recording_storage_uri = ""

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
        realtime_scene = await fetch_realtime_scene(scene_id)
        realtime_instructions = load_realtime_instructions(
            root=ROOT,
            session_id=ctx.room.name,
            scene_id=scene_id,
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

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev) -> None:
        if not managed_job or not insights_session_id:
            return
        item = getattr(ev, "item", None)
        role = str(getattr(item, "role", ""))
        text = str(getattr(item, "text_content", "") or "").strip()
        if role not in {"user", "assistant"} or not text:
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

        asyncio.create_task(persist_conversation_item())

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
            nonlocal telephony_terminal, recording_egress_id
            await _cancel_task(heartbeat_task)
            if recording_egress_id:
                recording_status = "stopping"
                try:
                    stopped = await ctx.api.egress.stop_egress(
                        api.StopEgressRequest(egress_id=recording_egress_id)
                    )
                    provider_status = api.EgressStatus.Name(int(stopped.status))
                    recording_status = {
                        "EGRESS_COMPLETE": "completed",
                        "EGRESS_FAILED": "failed",
                        "EGRESS_ABORTED": "failed",
                        "EGRESS_LIMIT_REACHED": "failed",
                    }.get(provider_status, "stopping")
                except Exception:
                    logger.exception(
                        "Recording stop result is uncertain; awaiting Egress webhook: egress_id=%s",
                        recording_egress_id,
                    )
                try:
                    await _telephony_record_recording(
                        managed_job,
                        egress_id=recording_egress_id,
                        status=recording_status,
                        storage_uri=recording_storage_uri,
                    )
                except Exception:
                    logger.exception(
                        "Unable to persist final recording status: call_id=%s",
                        managed_job["call_id"],
                    )
                recording_egress_id = ""
            if telephony_terminal:
                return
            try:
                final_status = "completed"
                failure_code = ""
                if str(managed_job.get("direction") or "") == "outbound":
                    final_status, failure_code = _outbound_shutdown_transition(
                        answered=outbound_call_answered,
                        reason=reason,
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

    await session.start(
        agent=PhoneAgent(
            ctx=ctx,
            managed_job=managed_job,
            sip_identity=managed_sip_identity,
            instructions=realtime_instructions or None,
        ),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(),
            audio_output=room_io.AudioOutputOptions(
                sample_rate=ROOM_AUDIO_SAMPLE_RATE,
                num_channels=QwenTTS.num_channels_count,
            ),
        ),
    )

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

    async def ensure_required_recording() -> bool:
        nonlocal recording_egress_id, recording_storage_uri, telephony_terminal
        if not managed_job or str(managed_job.get("recording_mode") or "off") != "always":
            return True
        if recording_egress_id:
            return True
        try:
            recording_egress_id, recording_storage_uri = await _start_managed_recording(
                ctx, session, managed_job
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

    if outbound_job:
        call_active = False
        amd_category = ""
        try:
            # Persist dialing before causing the external PSTN side effect.
            await _telephony_transition(
                outbound_job,
                "dialing",
                room_name=ctx.room.name,
            )
            heartbeat_task = asyncio.create_task(_telephony_heartbeat(outbound_job))

            async def dial_and_activate() -> None:
                nonlocal call_active, outbound_call_answered
                participant = await ctx.api.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        room_name=ctx.room.name,
                        sip_trunk_id=str(outbound_job["livekit_trunk_id"]),
                        sip_call_to=_provider_dial_target(
                            str(outbound_job["phone_number"])
                        ),
                        sip_number=str(outbound_job.get("source_number") or ""),
                        participant_identity=managed_sip_identity,
                        participant_name="AI voice call",
                        participant_metadata=json.dumps(
                            {"call_id": outbound_job["call_id"]}, separators=(",", ":")
                        ),
                        wait_until_answered=True,
                        ringing_timeout=_bounded_duration(
                            "CLOUD_PARITY_TELEPHONY_RINGING_TIMEOUT_SECONDS",
                            45,
                            minimum=10,
                            maximum=120,
                        ),
                        max_call_duration=_bounded_duration(
                            "CLOUD_PARITY_TELEPHONY_MAX_CALL_DURATION_SECONDS",
                            1800,
                            minimum=60,
                            maximum=14400,
                        ),
                    )
                )
                await ctx.wait_for_participant(identity=managed_sip_identity)
                await _telephony_transition(
                    outbound_job,
                    "active",
                    provider_call_id=str(participant.sip_call_id or ""),
                    room_name=ctx.room.name,
                )
                call_active = True
                outbound_call_answered = True

            amd_enabled = os.getenv("QWEN_AMD_ENABLED", "true").strip().lower() in {
                "1", "true", "yes", "on"
            }
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
                if not await ensure_required_recording():
                    return
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
        except api.SipCallError as exc:
            status, code, retryable = _sip_failure(exc)
            await _telephony_transition(
                outbound_job,
                status,
                failure_code=code,
                failure_detail=str(getattr(exc, "sip_status", ""))[:500],
                retryable=retryable,
            )
            telephony_terminal = True
            await _cancel_task(heartbeat_task)
            ctx.shutdown(reason=f"outbound dial failed: {code}")
            return
        except Exception as exc:
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

    if not await ensure_required_recording():
        return

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
        # In Realtime mode the model owns both generation and speech. Trigger
        # the start node only after the SIP participant is ready/recording is
        # active, so the caller cannot miss the opening sentence.
        speech = session.generate_reply(
            instructions=(
                "现在开始通话。严格只播报系统指令中定义的开场白，"
                "不要解释规则，不要提前进入下一节点。"
            ),
            allow_interruptions=True,
        )
        await speech.wait_for_playout()


if __name__ == "__main__":
    cli.run_app(server)
