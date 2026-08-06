"""Small dependency-free client for the AudioAgent control plane."""

from __future__ import annotations

import json
import os
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
