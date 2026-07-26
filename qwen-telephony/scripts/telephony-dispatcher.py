from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hmac
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
import uuid
from typing import Any

from dotenv import load_dotenv
import httpx
from aiohttp import web
from livekit import api


ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "qwen-telephony" / "config" / "local.env", override=False)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("telephony-dispatcher")


class DispatcherSettings:
    def __init__(self) -> None:
        self.control_url = os.getenv("CLOUD_PARITY_CONTROL_URL", "").strip().rstrip("/")
        self.project_ids = tuple(
            item.strip()
            for item in os.getenv("CLOUD_PARITY_TELEPHONY_PROJECT_IDS", "").split(",")
            if item.strip()
        )
        self.bearer_token = os.getenv("CLOUD_PARITY_SERVICE_BEARER_TOKEN", "").strip()
        self.bearer_token_file = os.getenv(
            "CLOUD_PARITY_SERVICE_BEARER_TOKEN_FILE", ""
        ).strip()
        self.user_id = os.getenv("CLOUD_PARITY_SERVICE_USER_ID", "").strip()
        self.dispatch_metadata_key = os.getenv(
            "CLOUD_PARITY_DISPATCH_METADATA_KEY", ""
        ).strip()
        self.metrics_token = os.getenv("CLOUD_PARITY_METRICS_TOKEN", "").strip()
        self.worker_id = os.getenv(
            "CLOUD_PARITY_TELEPHONY_WORKER_ID",
            f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}",
        ).strip()
        self.batch_size = int(os.getenv("CLOUD_PARITY_TELEPHONY_CLAIM_BATCH", "10"))
        self.poll_seconds = float(os.getenv("CLOUD_PARITY_TELEPHONY_POLL_SECONDS", "1"))
        self.request_timeout = float(
            os.getenv("CLOUD_PARITY_CONTROL_TIMEOUT_SECONDS", "5")
        )
        self.heartbeat_seconds = float(
            os.getenv("CLOUD_PARITY_TELEPHONY_HEARTBEAT_SECONDS", "10")
        )
        self.reconciliation_grace_seconds = float(
            os.getenv("CLOUD_PARITY_TELEPHONY_RECONCILIATION_GRACE_SECONDS", "120")
        )
        self.health_host = os.getenv(
            "CLOUD_PARITY_TELEPHONY_HEALTH_HOST", "0.0.0.0"
        ).strip()
        self.health_port = int(
            os.getenv("CLOUD_PARITY_TELEPHONY_HEALTH_PORT", "9091")
        )
        if not self.control_url:
            raise ValueError("CLOUD_PARITY_CONTROL_URL is required")
        if not self.project_ids:
            raise ValueError("CLOUD_PARITY_TELEPHONY_PROJECT_IDS is required")
        if not self.bearer_token and not self.bearer_token_file and not self.user_id:
            raise ValueError(
                "CLOUD_PARITY_SERVICE_BEARER_TOKEN is required outside local development"
            )
        if (
            os.getenv("CLOUD_PARITY_ENV", "development").strip().lower() == "production"
            and not self.dispatch_metadata_key
        ):
            raise ValueError("CLOUD_PARITY_DISPATCH_METADATA_KEY is required in production")
        if (
            os.getenv("CLOUD_PARITY_ENV", "development").strip().lower() == "production"
            and self.dispatch_metadata_key
            and self.dispatch_metadata_key
            == os.getenv("CLOUD_PARITY_MASTER_KEY", "").strip()
        ):
            raise ValueError(
                "CLOUD_PARITY_DISPATCH_METADATA_KEY must differ from CLOUD_PARITY_MASTER_KEY"
            )
        if (
            os.getenv("CLOUD_PARITY_ENV", "development").strip().lower() == "production"
            and len(self.metrics_token) < 32
        ):
            raise ValueError(
                "CLOUD_PARITY_METRICS_TOKEN must contain at least 32 characters in production"
            )
        if self.dispatch_metadata_key:
            try:
                from cryptography.fernet import Fernet

                Fernet(self.dispatch_metadata_key.encode("ascii"))
            except Exception as exc:
                raise ValueError(
                    "CLOUD_PARITY_DISPATCH_METADATA_KEY must be a valid Fernet key"
                ) from exc
        if not 1 <= self.batch_size <= 100:
            raise ValueError("CLOUD_PARITY_TELEPHONY_CLAIM_BATCH must be between 1 and 100")
        if not 0.1 <= self.poll_seconds <= 60:
            raise ValueError("CLOUD_PARITY_TELEPHONY_POLL_SECONDS must be between 0.1 and 60")
        if not 30 <= self.reconciliation_grace_seconds <= 3600:
            raise ValueError(
                "CLOUD_PARITY_TELEPHONY_RECONCILIATION_GRACE_SECONDS must be between 30 and 3600"
            )
        if not 0 <= self.health_port <= 65535:
            raise ValueError("CLOUD_PARITY_TELEPHONY_HEALTH_PORT must be between 0 and 65535")

    @property
    def headers(self) -> dict[str, str]:
        token = self.bearer_token
        if self.bearer_token_file:
            try:
                token = Path(self.bearer_token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise RuntimeError("service bearer token file cannot be read") from exc
            if not token:
                raise RuntimeError("service bearer token file is empty")
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {"X-User-ID": self.user_id}


class ControlPlaneClient:
    def __init__(self, settings: DispatcherSettings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.control_url,
            timeout=settings.request_timeout,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(path, json=payload, headers=self.settings.headers)
        response.raise_for_status()
        return response.json()

    async def claim(self, project_id: str) -> list[dict[str, Any]]:
        result = await self._post(
            f"/api/platform/projects/{project_id}/telephony/dispatch/claim",
            {"worker_id": self.settings.worker_id, "limit": self.settings.batch_size},
        )
        return list(result.get("items") or [])

    async def materialize_campaigns(self, project_id: str) -> dict[str, Any]:
        return await self._post(
            f"/api/platform/projects/{project_id}/telephony/campaigns/materialize",
            {"limit": self.settings.batch_size},
        )

    async def claim_reconciliation(self, project_id: str) -> list[dict[str, Any]]:
        result = await self._post(
            f"/api/platform/projects/{project_id}/telephony/reconciliation/claim",
            {"worker_id": self.settings.worker_id, "limit": self.settings.batch_size},
        )
        return list(result.get("items") or [])

    async def heartbeat(self, project_id: str, call: dict[str, Any]) -> dict[str, Any]:
        return await self._post(
            f"/api/platform/projects/{project_id}/telephony/calls/{call['id']}/heartbeat",
            {
                "worker_id": self.settings.worker_id,
                "lease_token": call["lease_token"],
            },
        )

    async def observe(
        self,
        project_id: str,
        call: dict[str, Any],
        **fields: Any,
    ) -> dict[str, Any]:
        return await self._post(
            f"/api/platform/projects/{project_id}/telephony/calls/{call['id']}/observe",
            {
                "worker_id": self.settings.worker_id,
                "lease_token": call["lease_token"],
                **fields,
            },
        )

    async def transition(
        self,
        project_id: str,
        call: dict[str, Any],
        status: str,
        **fields: Any,
    ) -> dict[str, Any]:
        return await self._post(
            f"/api/platform/projects/{project_id}/telephony/calls/{call['id']}/transition",
            {
                "status": status,
                "worker_id": self.settings.worker_id,
                "lease_token": call["lease_token"],
                **fields,
            },
        )


class DispatcherRuntimeState:
    def __init__(self, poll_seconds: float) -> None:
        self.started_at = time.time()
        self.last_loop_at = 0.0
        self.last_success_at = 0.0
        self.consecutive_errors = 0
        self.project_polls_total = 0
        self.project_poll_errors_total = 0
        self.jobs_claimed_total = 0
        self.readiness_window = max(30.0, poll_seconds * 10)

    def success(self, claimed: int) -> None:
        now = time.time()
        self.last_loop_at = now
        self.last_success_at = now
        self.consecutive_errors = 0
        self.project_polls_total += 1
        self.jobs_claimed_total += claimed

    def failure(self) -> None:
        self.last_loop_at = time.time()
        self.consecutive_errors += 1
        self.project_polls_total += 1
        self.project_poll_errors_total += 1

    def ready(self) -> bool:
        return self.last_success_at > 0 and time.time() - self.last_success_at <= self.readiness_window

    def prometheus(self) -> str:
        values = {
            "telephony_dispatcher_up": 1,
            "telephony_dispatcher_ready": int(self.ready()),
            "telephony_dispatcher_started_timestamp_seconds": self.started_at,
            "telephony_dispatcher_last_success_timestamp_seconds": self.last_success_at,
            "telephony_dispatcher_consecutive_errors": self.consecutive_errors,
            "telephony_dispatcher_project_polls_total": self.project_polls_total,
            "telephony_dispatcher_project_poll_errors_total": self.project_poll_errors_total,
            "telephony_dispatcher_jobs_claimed_total": self.jobs_claimed_total,
        }
        return "\n".join(f"{name} {value}" for name, value in values.items()) + "\n"


def metrics_token_valid(authorization: str, expected: str) -> bool:
    """Authenticate Prometheus without exposing the token in a query string."""
    if not expected:
        return True
    supplied = (
        authorization[len("Bearer ") :]
        if authorization.startswith("Bearer ")
        else ""
    )
    return bool(supplied) and hmac.compare_digest(supplied, expected)


async def start_health_server(
    settings: DispatcherSettings, state: DispatcherRuntimeState
) -> web.AppRunner | None:
    if settings.health_port == 0:
        return None

    async def live(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def ready(_request: web.Request) -> web.Response:
        status = 200 if state.ready() else 503
        return web.json_response(
            {
                "status": "ready" if status == 200 else "not_ready",
                "last_success_at": state.last_success_at,
                "consecutive_errors": state.consecutive_errors,
            },
            status=status,
        )

    async def metrics(_request: web.Request) -> web.Response:
        if not metrics_token_valid(
            _request.headers.get("Authorization", ""), settings.metrics_token
        ):
            return web.json_response(
                {"detail": "invalid metrics credentials"},
                status=401,
                headers={"Cache-Control": "no-store"},
            )
        return web.Response(
            text=state.prometheus(),
            content_type="text/plain",
            headers={"Cache-Control": "no-store"},
        )

    application = web.Application()
    application.add_routes(
        [web.get("/live", live), web.get("/ready", ready), web.get("/metrics", metrics)]
    )
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.health_host, settings.health_port)
    await site.start()
    logger.info(
        "Dispatcher health server listening: host=%s port=%s",
        settings.health_host,
        settings.health_port,
    )
    return runner

async def dispatch_call(
    settings: DispatcherSettings,
    control: ControlPlaneClient,
    livekit: api.LiveKitAPI,
    project_id: str,
    call: dict[str, Any],
) -> None:
    trunk_id = str(call.get("livekit_trunk_id") or "").strip()
    if not trunk_id:
        await control.transition(
            project_id,
            call,
            "failed",
            failure_code="outbound_trunk_missing",
            failure_detail="call has no active LiveKit outbound trunk",
            retryable=False,
        )
        return
    room_name = str(call.get("room_name") or f"call-{call['id']}")
    # Move out of the requeueable lease state before scheduling the agent. If
    # this process dies after this point, reconciliation is required and the
    # system will not blindly place a duplicate PSTN call.
    dispatching = await control.transition(
        project_id,
        call,
        "dispatching",
        room_name=room_name,
    )
    metadata = json.dumps(
        {
            "kind": "telephony.outbound",
            "project_id": project_id,
            "call_id": call["id"],
            "worker_id": settings.worker_id,
            "lease_token": dispatching["lease_token"],
            "phone_number": call["destination_number"],
            "source_number": call.get("source_number") or "",
            "livekit_trunk_id": trunk_id,
            "heartbeat_seconds": settings.heartbeat_seconds,
            "recording_mode": call.get("recording_mode") or "off",
            "recording_disclosure_text": call.get("recording_disclosure_text") or "",
        },
        separators=(",", ":"),
    )
    metadata_key = str(getattr(settings, "dispatch_metadata_key", "") or "").strip()
    if metadata_key:
        from cryptography.fernet import Fernet

        metadata = "enc:v1:" + Fernet(metadata_key.encode("ascii")).encrypt(
            metadata.encode("utf-8")
        ).decode("ascii")
    try:
        await livekit.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=str(call["agent_name"]),
                room=room_name,
                metadata=metadata,
            )
        )
        logger.info(
            "Agent dispatch created: project_id=%s call_id=%s room=%s",
            project_id,
            call["id"],
            room_name,
        )
    except Exception as exc:
        logger.exception(
            "Agent dispatch result is uncertain; scheduling reconciliation: "
            "project_id=%s call_id=%s",
            project_id,
            call["id"],
        )
        await control.transition(
            project_id,
            dispatching,
            "reconciling",
            failure_code="agent_dispatch_result_uncertain",
            failure_detail=type(exc).__name__,
        )


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def reconcile_call(
    settings: DispatcherSettings,
    control: ControlPlaneClient,
    livekit: api.LiveKitAPI,
    project_id: str,
    call: dict[str, Any],
) -> None:
    room_name = str(call.get("room_name") or "").strip()
    if not room_name:
        await control.transition(
            project_id,
            call,
            "failed",
            failure_code="reconciliation_room_missing",
            failure_detail="call has no LiveKit room to reconcile",
        )
        return
    room_absent = False
    try:
        response = await livekit.room.list_participants(
            api.ListParticipantsRequest(room=room_name)
        )
    except api.TwirpError as exc:
        if exc.code != api.TwirpErrorCode.NOT_FOUND:
            # A LiveKit control-plane outage is not proof that the PSTN call ended.
            # Retain the reconciliation lease and try again without causing a redial.
            logger.exception(
                "LiveKit reconciliation query failed: project_id=%s call_id=%s",
                project_id,
                call["id"],
            )
            await control.heartbeat(project_id, call)
            return
        # RoomService returns NOT_FOUND after a room has closed. Treat that as an
        # empty room so the normal reconciliation grace period can terminalize it.
        room_absent = True
        response = None
    except Exception:
        # A LiveKit control-plane outage is not proof that the PSTN call ended.
        # Retain the reconciliation lease and try again without causing a redial.
        logger.exception(
            "LiveKit reconciliation query failed: project_id=%s call_id=%s",
            project_id,
            call["id"],
        )
        await control.heartbeat(project_id, call)
        return

    sip_participant = next(
        (
            participant
            for participant in (() if response is None else response.participants)
            if str((participant.attributes or {}).get("sip.callID") or "").strip()
            or str(participant.identity).startswith("sip-")
        ),
        None,
    )
    if sip_participant is not None:
        attributes = dict(sip_participant.attributes or {})
        await control.observe(
            project_id,
            call,
            provider="livekit-sip",
            provider_call_id=str(
                attributes.get("sip.callIDFull")
                or attributes.get("sip.twilio.callSid")
                or attributes.get("sip.callID")
                or ""
            ),
            sip_call_id=str(attributes.get("sip.callID") or ""),
            room_name=room_name,
            participant_identity=str(sip_participant.identity),
            sip_status=str(attributes.get("sip.callStatus") or ""),
            attributes=attributes,
        )
        await control.heartbeat(project_id, call)
        logger.info(
            "Reconciliation confirmed live SIP participant: project_id=%s call_id=%s room=%s",
            project_id,
            call["id"],
            room_name,
        )
        return

    started = _parse_timestamp(call.get("reconcile_started_at"))
    elapsed = (
        (datetime.now(timezone.utc) - started).total_seconds()
        if started is not None
        else settings.reconciliation_grace_seconds
    )
    if elapsed < settings.reconciliation_grace_seconds:
        await control.heartbeat(project_id, call)
        return
    answered = bool(call.get("answered_at"))
    await control.transition(
        project_id,
        call,
        "completed" if answered else "failed",
        failure_code="" if answered else "reconciliation_no_sip_participant",
        failure_detail=(
            "LiveKit room no longer exists after reconciliation grace period"
            if room_absent
            else "LiveKit room has no SIP participant after reconciliation grace period"
        ),
    )


async def run() -> None:
    settings = DispatcherSettings()
    control = ControlPlaneClient(settings)
    runtime_state = DispatcherRuntimeState(settings.poll_seconds)
    health_runner = await start_health_server(settings, runtime_state)
    logger.info(
        "Dispatcher started: worker_id=%s projects=%s batch=%s",
        settings.worker_id,
        len(settings.project_ids),
        settings.batch_size,
    )
    try:
        async with api.LiveKitAPI() as livekit:
            while True:
                claimed = 0
                for project_id in settings.project_ids:
                    try:
                        await control.materialize_campaigns(project_id)
                        calls = await control.claim(project_id)
                        reconciliation = await control.claim_reconciliation(project_id)
                        claimed += len(calls) + len(reconciliation)
                        if calls:
                            await asyncio.gather(
                                *(
                                    dispatch_call(settings, control, livekit, project_id, call)
                                    for call in calls
                                )
                            )
                        if reconciliation:
                            await asyncio.gather(
                                *(
                                    reconcile_call(
                                        settings, control, livekit, project_id, call
                                    )
                                    for call in reconciliation
                                )
                            )
                        runtime_state.success(len(calls) + len(reconciliation))
                    except httpx.HTTPStatusError as exc:
                        runtime_state.failure()
                        logger.error(
                            "Control plane rejected dispatcher request: project_id=%s status=%s",
                            project_id,
                            exc.response.status_code,
                        )
                    except Exception:
                        runtime_state.failure()
                        logger.exception("Dispatcher project loop failed: project_id=%s", project_id)
                await asyncio.sleep(0 if claimed else settings.poll_seconds)
    finally:
        await control.close()
        if health_runner is not None:
            await health_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Dispatcher stopped")
    except Exception as exc:
        logger.error("Dispatcher configuration failed: %s", exc)
        sys.exit(1)
