"""Small dependency-free client for the AudioAgent control plane."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen


class AudioAgentError(RuntimeError):
    pass


class AudioAgentClient:
    def __init__(self) -> None:
        self.base_url = os.getenv(
            "AUDIOAGENT_BASE_URL", "http://127.0.0.1:8090"
        ).strip().rstrip("/")
        self.project_id = os.getenv("AUDIOAGENT_PROJECT_ID", "").strip()
        self.bearer_token = os.getenv("AUDIOAGENT_BEARER_TOKEN", "").strip()
        self.user_id = os.getenv("AUDIOAGENT_USER_ID", "dev-owner").strip()
        self.timeout = min(
            60.0,
            max(1.0, float(os.getenv("AUDIOAGENT_HTTP_TIMEOUT_SECONDS", "10"))),
        )
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AudioAgentError("AUDIOAGENT_BASE_URL must be an absolute HTTP(S) URL")
        if not self.project_id:
            raise AudioAgentError("AUDIOAGENT_PROJECT_ID is required")
        if not self.bearer_token and not self.user_id:
            raise AudioAgentError(
                "AUDIOAGENT_BEARER_TOKEN or AUDIOAGENT_USER_ID is required"
            )

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        else:
            headers["X-User-ID"] = self.user_id
        return headers

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None
        headers = self.headers
        if payload is not None:
            data = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method.upper(),
        )
        try:
            parsed = urlsplit(self.base_url)
            if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
                response_context = build_opener(ProxyHandler({})).open(
                    request, timeout=self.timeout
                )
            else:
                response_context = urlopen(request, timeout=self.timeout)
            with response_context as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise AudioAgentError(
                f"AudioAgent request failed with HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise AudioAgentError(
                f"AudioAgent is unavailable: {type(exc).__name__}: {exc}"
            ) from exc
        if not body:
            return {}
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise AudioAgentError("AudioAgent returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise AudioAgentError("AudioAgent returned a non-object response")
        return decoded

    def project_path(self, suffix: str) -> str:
        return (
            f"/api/platform/projects/{quote(self.project_id, safe='')}"
            f"{suffix}"
        )

    def download_to_path(
        self,
        url: str,
        target: Path,
        *,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> int:
        """Download a control-plane-issued recording URL to a local file."""

        parsed = urlsplit(str(url or "").strip())
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AudioAgentError("recording URL must be absolute HTTP(S)")
        if parsed.scheme == "http" and not loopback:
            raise AudioAgentError("recording URL must use HTTPS outside loopback development")
        if parsed.username or parsed.password or parsed.fragment:
            raise AudioAgentError("recording URL contains disallowed credentials or fragment")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

        request = Request(url, headers={"Accept": "audio/*,application/octet-stream"})
        timeout = min(
            180.0,
            max(
                5.0,
                float(
                    os.getenv(
                        "AUDIOAGENT_RECORDING_DOWNLOAD_TIMEOUT_SECONDS", "60"
                    )
                ),
            ),
        )
        try:
            opener = build_opener(ProxyHandler({})) if loopback else build_opener()
            with opener.open(request, timeout=timeout) as response:
                final_url = urlsplit(response.geturl())
                final_loopback = final_url.hostname in {
                    "127.0.0.1",
                    "localhost",
                    "::1",
                }
                if final_url.scheme not in {"http", "https"} or (
                    final_url.scheme == "http" and not final_loopback
                ):
                    raise AudioAgentError("recording download redirected to an unsafe URL")
                content_length = response.headers.get("Content-Length", "").strip()
                if content_length and int(content_length) > max_bytes:
                    raise AudioAgentError("recording exceeds the configured download limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                total = 0
                with target.open("wb") as output:
                    while True:
                        chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise AudioAgentError(
                                "recording exceeds the configured download limit"
                            )
                        output.write(chunk)
        except HTTPError as exc:
            raise AudioAgentError(
                f"recording download failed with HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise AudioAgentError(
                f"recording download failed: {type(exc).__name__}: {exc}"
            ) from exc
        if total <= 0:
            raise AudioAgentError("recording download returned an empty file")
        return total
