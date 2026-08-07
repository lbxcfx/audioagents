"""Qwen3-ASR-Flash transcription provider for Hermes.

The implementation intentionally uses only Python's standard library so that
receiving a WeChat voice note never installs a local Whisper runtime, NumPy, or
audio-capture packages. Hermes converts WeChat SILK files to WAV before this
provider is called.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3-asr-flash"
MAX_ENCODED_AUDIO_BYTES = 10 * 1024 * 1024
SUPPORTED_LANGUAGES = {
    "ar",
    "cs",
    "da",
    "de",
    "en",
    "es",
    "fi",
    "fil",
    "fr",
    "hi",
    "id",
    "is",
    "it",
    "ja",
    "ko",
    "ms",
    "no",
    "pl",
    "pt",
    "ru",
    "sv",
    "th",
    "tr",
    "uk",
    "vi",
    "yue",
    "zh",
}
MIME_TYPES = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
}


def _completion_url(base_url: str) -> str:
    """Return a safe DashScope OpenAI-compatible chat-completions URL."""
    parsed = urlsplit(base_url.strip().rstrip("/"))
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not hostname.endswith(".aliyuncs.com")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "QWEN_ASR_BASE_URL must be an HTTPS aliyuncs.com endpoint"
        )

    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path = f"{path}/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _response_text(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Qwen ASR response did not contain a transcript") from exc

    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts).strip()
    raise ValueError("Qwen ASR returned an unsupported transcript format")


def _error_message(exc: HTTPError) -> str:
    """Extract a bounded provider error without including credentials."""
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        candidate = (
            payload.get("error", payload) if isinstance(payload, dict) else payload
        )
        if isinstance(candidate, dict):
            candidate = candidate.get("message") or candidate.get("code")
        if isinstance(candidate, str):
            detail = candidate.strip()[:300]
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    suffix = f": {detail}" if detail else ""
    return f"Qwen ASR HTTP {exc.code}{suffix}"


class QwenASRClient:
    """Small dependency-free client for Qwen's synchronous ASR endpoint."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("DASHSCOPE_API_KEY", "")).strip()
        configured_base_url = (
            base_url
            if base_url is not None
            else os.getenv("QWEN_ASR_BASE_URL", "")
        )
        self.endpoint = _completion_url(configured_base_url or DEFAULT_BASE_URL)
        timeout_value = (
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("QWEN_ASR_TIMEOUT_SECONDS", "60")
        )
        self.timeout_seconds = float(timeout_value)
        if self.timeout_seconds <= 0:
            raise ValueError("QWEN_ASR_TIMEOUT_SECONDS must be greater than zero")

    def transcribe(
        self,
        file_path: str,
        *,
        model: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY is not configured")

        path = Path(file_path)
        audio = path.read_bytes()
        mime_type = MIME_TYPES.get(path.suffix.lower())
        if mime_type is None:
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data_uri = f"data:{mime_type};base64,{base64.b64encode(audio).decode('ascii')}"
        if len(data_uri.encode("ascii")) > MAX_ENCODED_AUDIO_BYTES:
            raise ValueError("Qwen ASR Base64 audio exceeds the 10 MB API limit")

        selected_language = (language or "").strip().lower()
        if selected_language and selected_language not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"Qwen ASR does not support language '{selected_language}'"
            )

        asr_options: dict[str, Any] = {"enable_itn": True}
        if selected_language:
            asr_options["language"] = selected_language
        body = {
            "model": model or os.getenv("QWEN_ASR_MODEL", "") or DEFAULT_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": data_uri},
                        }
                    ],
                }
            ],
            "stream": False,
            "asr_options": asr_options,
        }
        request = Request(
            self.endpoint,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(_error_message(exc)) from exc
        except URLError as exc:
            raise RuntimeError(f"Qwen ASR request failed: {exc.reason}") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Qwen ASR returned invalid JSON") from exc
        return {"transcript": _response_text(payload), "raw": payload}


def create_provider():
    """Build the Hermes provider lazily so project tests need no Hermes import."""
    from agent.transcription_provider import TranscriptionProvider

    class QwenASRProvider(TranscriptionProvider):
        @property
        def name(self) -> str:
            return "qwen"

        @property
        def display_name(self) -> str:
            return "Qwen3 ASR Flash"

        def is_available(self) -> bool:
            return bool(os.getenv("DASHSCOPE_API_KEY", "").strip())

        def list_models(self) -> list[dict[str, Any]]:
            return [
                {
                    "id": DEFAULT_MODEL,
                    "display": "Qwen3 ASR Flash",
                    "languages": sorted(SUPPORTED_LANGUAGES),
                    "max_audio_seconds": 300,
                }
            ]

        def get_setup_schema(self) -> dict[str, Any]:
            return {
                "name": self.display_name,
                "badge": "cloud",
                "tag": "Qwen3-ASR-Flash via Alibaba Cloud Model Studio",
                "env_vars": [
                    {
                        "key": "DASHSCOPE_API_KEY",
                        "prompt": "Alibaba Cloud Model Studio API key",
                        "url": "https://bailian.console.aliyun.com/",
                    }
                ],
            }

        def transcribe(
            self,
            file_path: str,
            *,
            model: str | None = None,
            language: str | None = None,
            **extra: Any,
        ) -> dict[str, Any]:
            del extra
            try:
                result = QwenASRClient().transcribe(
                    file_path,
                    model=model,
                    language=language,
                )
                transcript = result["transcript"]
                if not transcript:
                    raise ValueError("Qwen ASR returned an empty transcript")
                return {
                    "success": True,
                    "transcript": transcript,
                    "provider": self.name,
                }
            except Exception as exc:  # Hermes providers return error envelopes.
                return {
                    "success": False,
                    "transcript": "",
                    "error": str(exc),
                    "provider": self.name,
                }

    return QwenASRProvider()
