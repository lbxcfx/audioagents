from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import logging
import os
import time
from typing import Any, Protocol
import uuid

import httpx

from .deployment import DeploymentService
from .insights import InsightsService
from .store import PlatformStore, ResourceNotFoundError, _row, _utc_now


logger = logging.getLogger("cloud-parity.inference")


@dataclass(frozen=True)
class InferenceResult:
    output: dict[str, Any]
    quantity: float
    unit: str
    cost_usd: float = 0


class InferenceAdapter(Protocol):
    async def invoke(
        self,
        *,
        modality: str,
        model: str,
        input_data: dict[str, Any],
        parameters: dict[str, Any],
        credentials: dict[str, str],
    ) -> InferenceResult: ...


class QwenInferenceAdapter:
    """DashScope OpenAI-compatible LLM/ASR and HTTP TTS adapter."""

    async def invoke(
        self,
        *,
        modality: str,
        model: str,
        input_data: dict[str, Any],
        parameters: dict[str, Any],
        credentials: dict[str, str],
    ) -> InferenceResult:
        api_key = credentials.get("DASHSCOPE_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required")
        if modality == "tts":
            return await self._tts(api_key, model, input_data, parameters)
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=credentials.get("QWEN_OPENAI_BASE_URL")
            or os.getenv("QWEN_OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )
        try:
            if modality == "llm":
                messages = input_data.get("messages")
                if not isinstance(messages, list) or not messages:
                    raise ValueError("LLM input requires a non-empty messages list")
                completion = await client.chat.completions.create(
                    model=model, messages=messages, **parameters
                )
                message = completion.choices[0].message
                usage = completion.usage
                return InferenceResult(
                    output={
                        "text": message.content or "",
                        "finish_reason": completion.choices[0].finish_reason,
                        "request_id": completion.id,
                    },
                    quantity=float(getattr(usage, "total_tokens", 0) or 0),
                    unit="tokens",
                )
            if modality == "stt":
                encoded = str(input_data.get("audio_base64", ""))
                if not encoded:
                    raise ValueError("STT input requires audio_base64")
                mime = str(input_data.get("mime_type", "audio/wav"))
                completion = await client.chat.completions.create(
                    model=model,
                    messages=[{
                        "role": "user",
                        "content": [{
                            "type": "input_audio",
                            "input_audio": {"data": f"data:{mime};base64,{encoded}"},
                        }],
                    }],
                    temperature=0,
                    **parameters,
                )
                duration = float(input_data.get("duration_seconds", 0))
                return InferenceResult(
                    output={"text": completion.choices[0].message.content or "", "request_id": completion.id},
                    quantity=duration,
                    unit="audio_seconds",
                )
            raise ValueError(f"unsupported modality: {modality}")
        finally:
            await client.close()

    async def _tts(
        self,
        api_key: str,
        model: str,
        input_data: dict[str, Any],
        parameters: dict[str, Any],
    ) -> InferenceResult:
        text = str(input_data.get("text", ""))
        if not text:
            raise ValueError("TTS input requires text")
        endpoint = os.getenv(
            "QWEN_TTS_ENDPOINT",
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        )
        payload = {
            "model": model,
            "input": {
                "text": text,
                "voice": parameters.pop("voice", "Cherry"),
                "language_type": parameters.pop("language_type", "Chinese"),
            },
            **parameters,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            body = response.json()
            audio = body.get("output", {}).get("audio") or body.get("audio") or {}
            if isinstance(audio, str):
                audio = {"url": audio}
            if audio.get("data"):
                encoded = str(audio["data"]).split(",", 1)[-1]
            elif audio.get("url"):
                download = await client.get(str(audio["url"]).replace("http://", "https://", 1))
                download.raise_for_status()
                encoded = base64.b64encode(download.content).decode("ascii")
            else:
                raise RuntimeError("Qwen TTS returned no audio")
        return InferenceResult(
            output={"audio_base64": encoded, "mime_type": "audio/wav"},
            quantity=float(len(text)),
            unit="characters",
        )


class InferenceGateway:
    def __init__(
        self,
        store: PlatformStore,
        deployment: DeploymentService,
        insights: InsightsService,
        adapters: dict[str, InferenceAdapter] | None = None,
    ):
        self.store = store
        self.deployment = deployment
        self.insights = insights
        self.adapters = adapters or {"qwen": QwenInferenceAdapter()}

    def put_route(
        self,
        *,
        project_id: str,
        actor_id: str,
        descriptor: str,
        modality: str,
        provider: str,
        provider_model: str,
        priority: int = 100,
        timeout_seconds: float = 30,
        enabled: bool = True,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "inference.manage")
        if modality not in {"stt", "llm", "tts"}:
            raise ValueError("modality must be stt, llm, or tts")
        if provider not in self.adapters:
            raise ValueError(f"inference provider is not installed: {provider}")
        if not descriptor.strip() or not provider_model.strip():
            raise ValueError("descriptor and provider_model are required")
        route_id = str(uuid.uuid4())
        now = _utc_now()
        with self.store.transaction() as conn:
            existing = conn.execute(
                """
                SELECT id FROM model_routes
                WHERE project_id = ? AND descriptor = ? AND provider = ? AND provider_model = ?
                """,
                (project_id, descriptor, provider, provider_model),
            ).fetchone()
            if existing:
                route_id = existing["id"]
                conn.execute(
                    """
                    UPDATE model_routes
                    SET modality = ?, priority = ?, timeout_seconds = ?, enabled = ?,
                        config_json = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        modality, priority, timeout_seconds, int(enabled),
                        json.dumps(config or {}, separators=(",", ":")), now, route_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO model_routes (
                        id, project_id, descriptor, modality, provider, provider_model,
                        priority, timeout_seconds, enabled, config_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        route_id, project_id, descriptor, modality, provider, provider_model,
                        priority, timeout_seconds, int(enabled),
                        json.dumps(config or {}, separators=(",", ":")), now, now,
                    ),
                )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="inference.route.upsert",
                resource_type="model_route",
                resource_id=route_id,
                payload={"descriptor": descriptor, "provider": provider},
            )
            row = conn.execute("SELECT * FROM model_routes WHERE id = ?", (route_id,)).fetchone()
        return self._route_record(row)

    def list_routes(self, *, project_id: str, user_id: str) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "project.read")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM model_routes WHERE project_id = ?
                ORDER BY descriptor, modality, priority, id
                """,
                (project_id,),
            ).fetchall()
        return [self._route_record(item) for item in rows]

    async def invoke(
        self,
        *,
        project_id: str,
        actor_id: str,
        descriptor: str,
        modality: str,
        input_data: dict[str, Any],
        parameters: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "inference.invoke")
        routes = self._resolve_routes(project_id, descriptor, modality)
        credentials = self.deployment.resolve_secrets(project_id)
        errors: list[dict[str, str]] = []
        for route in routes:
            adapter = self.adapters[route["provider"]]
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    adapter.invoke(
                        modality=modality,
                        model=route["provider_model"],
                        input_data=input_data,
                        parameters={**route["config"], **(parameters or {})},
                        credentials=credentials,
                    ),
                    timeout=float(route["timeout_seconds"]),
                )
                latency_ms = (time.perf_counter() - started) * 1000
                self._record_attempt(
                    project_id, session_id, descriptor, modality, route, "succeeded", latency_ms
                )
                if session_id:
                    self.insights.record_usage(
                        project_id=project_id,
                        actor_id=actor_id,
                        session_id=session_id,
                        category=modality,
                        provider=route["provider"],
                        model=route["provider_model"],
                        quantity=result.quantity,
                        unit=result.unit,
                        cost_usd=result.cost_usd,
                        latency_ms=latency_ms,
                    )
                return {
                    "descriptor": descriptor,
                    "modality": modality,
                    "provider": route["provider"],
                    "model": route["provider_model"],
                    "output": result.output,
                    "usage": {
                        "quantity": result.quantity,
                        "unit": result.unit,
                        "cost_usd": result.cost_usd,
                    },
                    "latency_ms": round(latency_ms, 3),
                    "fallbacks": errors,
                }
            except Exception as exc:
                latency_ms = (time.perf_counter() - started) * 1000
                error_type = type(exc).__name__
                self._record_attempt(
                    project_id, session_id, descriptor, modality, route, "failed", latency_ms, error_type
                )
                errors.append({"provider": route["provider"], "error_type": error_type})
                logger.warning(
                    "inference route failed project=%s descriptor=%s provider=%s error=%s",
                    project_id, descriptor, route["provider"], error_type,
                )
        raise RuntimeError(f"all inference routes failed: {errors}")

    def _resolve_routes(self, project_id: str, descriptor: str, modality: str) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM model_routes
                WHERE project_id = ? AND descriptor = ? AND modality = ? AND enabled = 1
                ORDER BY priority, created_at, id
                """,
                (project_id, descriptor, modality),
            ).fetchall()
        if not rows:
            raise ResourceNotFoundError("no enabled inference route found")
        return [self._route_record(item) for item in rows]

    def _record_attempt(
        self, project_id, session_id, descriptor, modality, route, status, latency_ms, error_type=""
    ) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO inference_attempts (
                    id, project_id, session_id, descriptor, modality, provider,
                    provider_model, status, latency_ms, error_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), project_id, session_id, descriptor, modality,
                    route["provider"], route["provider_model"], status, latency_ms,
                    error_type, _utc_now(),
                ),
            )

    @staticmethod
    def _route_record(row: Any) -> dict[str, Any]:
        record = _row(row) or {}
        record["config"] = json.loads(record.pop("config_json") or "{}")
        record["enabled"] = bool(record["enabled"])
        return record
