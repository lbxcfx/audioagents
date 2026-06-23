from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import logging
import os
import json
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import websockets
from livekit import rtc
from livekit.agents import APIConnectOptions, stt, tts, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from openai import AsyncOpenAI


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DASHSCOPE_GENERATION_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)
DEFAULT_DASHSCOPE_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
logger = logging.getLogger("qwen-phone-agent.qwen")


def _dashscope_key() -> str:
    key = os.getenv("DASHSCOPE_API_KEY")
    if not key:
        raise ValueError("DASHSCOPE_API_KEY is required in .env")
    return key


def _base_url() -> str:
    return os.getenv("QWEN_OPENAI_BASE_URL", DEFAULT_BASE_URL)


@dataclass
class QwenASROptions:
    model: str
    language: str
    prompt: str


class QwenASR(stt.STT):
    """Batch Qwen ASR wrapped as a LiveKit STT provider.

    LiveKit's StreamAdapter + Silero VAD turns this into a usable real-time
    pipeline by sending each detected utterance here as one audio segment.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        language: str = "zh",
        prompt: str = "请将这段电话语音转写为简洁准确的中文文本。",
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                diarization=False,
                offline_recognize=True,
            )
        )
        self._opts = QwenASROptions(
            model=model or os.getenv("QWEN_ASR_MODEL", "qwen3-asr-flash"),
            language=language,
            prompt=prompt,
        )
        self._client = AsyncOpenAI(api_key=_dashscope_key(), base_url=_base_url())

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "dashscope-qwen"

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        frame = rtc.combine_audio_frames(buffer) if isinstance(buffer, list) else buffer
        wav = frame.to_wav_bytes()
        audio_url = "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")
        lang = language if language is not NOT_GIVEN else self._opts.language

        completion = await self._client.chat.completions.create(
            model=self._opts.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": audio_url},
                        },
                    ],
                }
            ],
            temperature=0,
            timeout=conn_options.timeout,
            extra_body={
                "asr_options": {
                    "language": lang or self._opts.language,
                    "enable_itn": False,
                }
            },
        )
        text = (completion.choices[0].message.content or "").strip()
        request_id = getattr(completion, "id", "") or str(uuid.uuid4())
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=request_id,
            alternatives=[
                stt.SpeechData(
                    language=lang or self._opts.language,
                    text=text,
                    confidence=1.0 if text else 0.0,
                )
            ],
        )

    async def aclose(self) -> None:
        await self._client.close()


@dataclass
class QwenRealtimeASROptions:
    model: str
    transcription_model: str
    endpoint: str
    language: str
    vad_threshold: float
    silence_duration_ms: int
    prefix_padding_ms: int


class QwenRealtimeASR(stt.STT):
    """DashScope Qwen realtime ASR via the Omni WebSocket API."""

    input_sample_rate_hz = 16000

    def __init__(
        self,
        *,
        model: str | None = None,
        transcription_model: str | None = None,
        language: str = "zh",
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                diarization=False,
                offline_recognize=False,
            )
        )
        self._opts = QwenRealtimeASROptions(
            model=model or os.getenv("QWEN_REALTIME_MODEL", "qwen3-omni-flash-realtime"),
            transcription_model=transcription_model
            or os.getenv("QWEN_REALTIME_ASR_MODEL", "qwen3-asr-flash-realtime"),
            endpoint=os.getenv("QWEN_REALTIME_ENDPOINT", DEFAULT_DASHSCOPE_REALTIME_URL),
            language=language,
            vad_threshold=float(os.getenv("QWEN_REALTIME_VAD_THRESHOLD", "0.45")),
            silence_duration_ms=int(os.getenv("QWEN_REALTIME_SILENCE_MS", "200")),
            prefix_padding_ms=int(os.getenv("QWEN_REALTIME_PREFIX_MS", "100")),
        )

    @property
    def model(self) -> str:
        return self._opts.transcription_model

    @property
    def provider(self) -> str:
        return "dashscope-qwen-realtime"

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        fallback = QwenASR(language=self._opts.language)
        try:
            return await fallback._recognize_impl(
                buffer,
                language=language,
                conn_options=conn_options,
            )
        finally:
            await fallback.aclose()

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        lang = language if language is not NOT_GIVEN else self._opts.language
        return _QwenRealtimeASRStream(
            stt=self,
            conn_options=conn_options,
            language=lang or self._opts.language,
            sample_rate=self.input_sample_rate_hz,
        )


class _QwenRealtimeASRStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt: QwenRealtimeASR,
        conn_options: APIConnectOptions,
        language: str,
        sample_rate: int,
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=sample_rate)
        self._qwen_stt = stt
        self._language = language

    async def _run(self) -> None:
        opts = self._qwen_stt._opts
        url = f"{opts.endpoint}?model={opts.model}"
        audio_duration = 0.0
        request_id = str(uuid.uuid4())

        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {_dashscope_key()}"},
            ping_interval=20,
            ping_timeout=20,
            open_timeout=self._conn_options.timeout,
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "event_id": str(uuid.uuid4()),
                        "type": "session.update",
                        "session": {
                            "modalities": ["text"],
                            "input_audio_format": "pcm",
                            "output_audio_format": "pcm",
                            "input_audio_transcription": {
                                "model": opts.transcription_model,
                            },
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": opts.vad_threshold,
                                "prefix_padding_ms": opts.prefix_padding_ms,
                                "silence_duration_ms": opts.silence_duration_ms,
                                "create_response": False,
                                "interrupt_response": False,
                            },
                        },
                    },
                    ensure_ascii=False,
                )
            )

            async def send_audio() -> None:
                nonlocal audio_duration
                async for item in self._input_ch:
                    if isinstance(item, stt.RecognizeStream._FlushSentinel):
                        await ws.send(
                            json.dumps(
                                {
                                    "event_id": str(uuid.uuid4()),
                                    "type": "input_audio_buffer.commit",
                                }
                            )
                        )
                        continue

                    frame = item
                    pcm_bytes = bytes(frame.data)
                    audio_duration += frame.samples_per_channel / frame.sample_rate
                    await ws.send(
                        json.dumps(
                            {
                                "event_id": str(uuid.uuid4()),
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm_bytes).decode("ascii"),
                            }
                        )
                    )

            send_task = asyncio.create_task(send_audio())
            try:
                async for message in ws:
                    event = json.loads(message)
                    event_type = event.get("type")
                    if event_type == "error":
                        raise RuntimeError(f"DashScope realtime ASR error: {event.get('error')}")

                    if event_type == "conversation.item.input_audio_transcription.delta":
                        text = (event.get("delta") or "").strip()
                        if text:
                            self._event_ch.send_nowait(
                                stt.SpeechEvent(
                                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                                    request_id=event.get("event_id") or request_id,
                                    alternatives=[
                                        stt.SpeechData(
                                            language=self._language,
                                            text=text,
                                            confidence=0.0,
                                        )
                                    ],
                                )
                            )
                        continue

                    if event_type == "conversation.item.input_audio_transcription.completed":
                        text = (event.get("transcript") or "").strip()
                        if not text:
                            continue
                        request_id = event.get("event_id") or request_id
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                request_id=request_id,
                                alternatives=[
                                    stt.SpeechData(
                                        language=self._language,
                                        text=text,
                                        confidence=1.0,
                                    )
                                ],
                            )
                        )
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.RECOGNITION_USAGE,
                                request_id=request_id,
                                recognition_usage=stt.RecognitionUsage(
                                    audio_duration=audio_duration
                                ),
                            )
                        )
            finally:
                send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await send_task


@dataclass
class QwenTTSOptions:
    model: str
    voice: str
    format: str
    endpoint: str
    language_type: str
    use_sse: bool


def _tts_cache_dir() -> Path:
    return Path(
        os.getenv(
            "QWEN_TTS_CACHE_DIR",
            str(Path(__file__).resolve().parents[1] / "cache" / "tts"),
        )
    )


def _tts_cache_enabled() -> bool:
    return os.getenv("QWEN_TTS_CACHE_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _tts_cache_key(text: str, opts: QwenTTSOptions) -> str:
    payload = json.dumps(
        {
            "model": opts.model,
            "voice": opts.voice,
            "format": opts.format,
            "language_type": opts.language_type,
            "text": text,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tts_cache_path(text: str, opts: QwenTTSOptions) -> Path:
    suffix = opts.format.lower().strip() or "wav"
    return _tts_cache_dir() / f"{_tts_cache_key(text, opts)}.{suffix}"


def _normalize_wav_container(audio_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(audio_bytes), "rb") as reader:
        num_channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        pcm = reader.readframes(reader.getnframes())

    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(num_channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    return output.getvalue()


def _maybe_normalize_tts_audio(audio_bytes: bytes, opts: QwenTTSOptions) -> bytes:
    if opts.format.lower().strip() != "wav":
        return audio_bytes
    try:
        return _normalize_wav_container(audio_bytes)
    except Exception:
        logger.exception("TTS WAV normalization failed; using original bytes")
        return audio_bytes


class QwenTTS(tts.TTS):
    """DashScope/Qwen TTS wrapped as a LiveKit TTS provider."""

    sample_rate_hz = 24000
    num_channels_count = 1

    def __init__(
        self,
        *,
        model: str | None = None,
        voice: str | None = None,
        response_format: str | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=self.sample_rate_hz,
            num_channels=self.num_channels_count,
        )
        self._opts = QwenTTSOptions(
            model=model or os.getenv("QWEN_TTS_MODEL", "qwen3-tts-flash"),
            voice=voice or os.getenv("QWEN_TTS_VOICE", "Cherry"),
            format=response_format or os.getenv("QWEN_TTS_FORMAT", "wav"),
            endpoint=os.getenv("QWEN_TTS_ENDPOINT", DEFAULT_DASHSCOPE_GENERATION_URL),
            language_type=os.getenv("QWEN_TTS_LANGUAGE_TYPE", "Chinese"),
            use_sse=os.getenv("QWEN_TTS_USE_SSE", "true").lower() in {"1", "true", "yes", "on"},
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=15.0),
            headers={
                "Authorization": f"Bearer {_dashscope_key()}",
                "Content-Type": "application/json",
            },
        )

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "dashscope-qwen"

    def cached_audio_bytes(self, text: str) -> tuple[bytes, str, str] | None:
        if not _tts_cache_enabled() or not text.strip():
            return None
        path = _tts_cache_path(text, self._opts)
        if not path.exists() or path.stat().st_size <= 44:
            return None
        original_bytes = path.read_bytes()
        audio_bytes = _maybe_normalize_tts_audio(original_bytes, self._opts)
        if audio_bytes != original_bytes:
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_bytes(audio_bytes)
            tmp_path.replace(path)
            logger.info("TTS cache normalized: key=%s bytes=%d", path.stem[:12], len(audio_bytes))
        request_id = f"tts_cache_{path.stem[:12]}"
        logger.info("TTS cache hit: key=%s bytes=%d", path.stem[:12], len(audio_bytes))
        return audio_bytes, _format_to_mime(self._opts.format), request_id

    def _write_audio_cache(self, text: str, audio_bytes: bytes) -> None:
        if not _tts_cache_enabled() or not text.strip() or not audio_bytes:
            return
        audio_bytes = _maybe_normalize_tts_audio(audio_bytes, self._opts)
        path = _tts_cache_path(text, self._opts)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_bytes(audio_bytes)
        tmp_path.replace(path)
        logger.info("TTS cache write: key=%s bytes=%d", path.stem[:12], len(audio_bytes))

    async def synthesize_audio_bytes(self, text: str) -> tuple[bytes, str, str]:
        cached = self.cached_audio_bytes(text)
        if cached:
            return cached

        payload = {
            "model": self._opts.model,
            "input": {
                "text": text,
                "voice": self._opts.voice,
                "language_type": self._opts.language_type,
            },
        }
        request_id = ""

        if self._opts.use_sse:
            audio_bytes = bytearray()
            async with self._client.stream(
                "POST",
                self._opts.endpoint,
                json=payload,
                headers={"X-DashScope-SSE": "enable"},
            ) as response:
                response.raise_for_status()
                async for event in _iter_sse_json(response):
                    request_id = _extract_request_id(event) or request_id
                    try:
                        audio = _extract_audio(event)
                    except RuntimeError:
                        continue
                    if "data" not in audio:
                        continue
                    encoded = audio["data"]
                    if encoded.startswith("data:"):
                        _, encoded = encoded.split(",", 1)
                    if encoded:
                        audio_bytes.extend(base64.b64decode(encoded))

            if audio_bytes:
                output = bytes(audio_bytes)
                self._write_audio_cache(text, output)
                return output, _format_to_mime(self._opts.format), request_id or str(uuid.uuid4())

        response = await self._client.post(self._opts.endpoint, json=payload)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        audio = _extract_audio(body)
        request_id = _extract_request_id(body) or response.headers.get("X-Request-Id") or str(uuid.uuid4())

        if "url" in audio:
            download_url = audio["url"].replace("http://", "https://", 1)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=30.0),
                follow_redirects=True,
            ) as download_client:
                audio_response = await download_client.get(download_url)
                audio_response.raise_for_status()
                content_type = audio_response.headers.get("content-type", "").split(";")[0].strip()
                self._write_audio_cache(text, audio_response.content)
                return audio_response.content, content_type or _format_to_mime(self._opts.format), request_id

        if "data" in audio:
            encoded = audio["data"]
            if encoded.startswith("data:"):
                header, encoded = encoded.split(",", 1)
                mime_type = header.split(":", 1)[1].split(";", 1)[0]
            else:
                mime_type = _format_to_mime(self._opts.format)
            output = base64.b64decode(encoded)
            self._write_audio_cache(text, output)
            return output, mime_type, request_id

        raise RuntimeError(f"DashScope TTS response did not contain audio url/data: {body}")

    async def stream_audio_chunks(self, text: str):
        payload = {
            "model": self._opts.model,
            "input": {
                "text": text,
                "voice": self._opts.voice,
                "language_type": self._opts.language_type,
            },
        }

        async with self._client.stream(
            "POST",
            self._opts.endpoint,
            json=payload,
            headers={"X-DashScope-SSE": "enable"},
        ) as response:
            response.raise_for_status()
            async for event in _iter_sse_json(response):
                try:
                    audio = _extract_audio(event)
                except RuntimeError:
                    continue
                if "data" not in audio:
                    continue
                encoded = audio["data"]
                if encoded.startswith("data:"):
                    _, encoded = encoded.split(",", 1)
                if encoded:
                    yield base64.b64decode(encoded)

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return _QwenTTSChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        await self._client.aclose()


class _QwenTTSChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts: QwenTTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._qwen_tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        cached = self._qwen_tts.cached_audio_bytes(self.input_text)
        if cached:
            audio_bytes, mime_type, request_id = cached
            output_emitter.initialize(
                request_id=request_id,
                sample_rate=QwenTTS.sample_rate_hz,
                num_channels=QwenTTS.num_channels_count,
                mime_type=mime_type,
            )
            output_emitter.push(audio_bytes)
            output_emitter.flush()
            return

        if self._qwen_tts._opts.use_sse:
            output_emitter.initialize(
                request_id=str(uuid.uuid4()),
                sample_rate=QwenTTS.sample_rate_hz,
                num_channels=QwenTTS.num_channels_count,
                mime_type=_format_to_mime(self._qwen_tts._opts.format),
            )
            pushed = False
            async for audio_chunk in self._qwen_tts.stream_audio_chunks(self.input_text):
                pushed = True
                output_emitter.push(audio_chunk)
            if pushed:
                output_emitter.flush()
                return

        audio_bytes, mime_type, request_id = await self._qwen_tts.synthesize_audio_bytes(self.input_text)
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=QwenTTS.sample_rate_hz,
            num_channels=QwenTTS.num_channels_count,
            mime_type=mime_type,
        )
        output_emitter.push(audio_bytes)
        output_emitter.flush()


def _format_to_mime(format_name: str) -> str:
    normalized = format_name.lower().strip()
    if normalized == "mp3":
        return "audio/mpeg"
    if normalized == "pcm":
        return "audio/pcm"
    return f"audio/{normalized}"


async def _iter_sse_json(response: httpx.Response):
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines.clear()
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())

    if data_lines:
        try:
            yield json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return


def _extract_request_id(body: dict[str, Any]) -> str:
    return str(
        body.get("request_id")
        or body.get("requestId")
        or body.get("output", {}).get("request_id")
        or ""
    )


def _extract_audio(body: dict[str, Any]) -> dict[str, str]:
    output = body.get("output", {})
    candidates: list[Any] = [
        output.get("audio"),
        output.get("choices", [{}])[0].get("message", {}).get("audio")
        if output.get("choices")
        else None,
    ]

    for choice in output.get("choices", []) or []:
        message = choice.get("message", {})
        candidates.append(message.get("audio"))
        for content in message.get("content", []) or []:
            if isinstance(content, dict):
                candidates.append(content.get("audio"))
                candidates.append(content)

    for candidate in candidates:
        if isinstance(candidate, dict) and (candidate.get("url") or candidate.get("data")):
            return {"url": candidate["url"]} if candidate.get("url") else {"data": candidate["data"]}
        if isinstance(candidate, str):
            if candidate.startswith("http://") or candidate.startswith("https://"):
                return {"url": candidate}
            return {"data": candidate}

    raise RuntimeError(f"DashScope TTS response did not contain audio url/data: {body}")
