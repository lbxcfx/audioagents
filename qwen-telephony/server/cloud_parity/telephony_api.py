from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .api import _translate_error
from .auth import require_user_id
from .telephony import (
    CapacityExceededError,
    ComplianceBlockedError,
    IdempotencyConflictError,
    InvalidCallTransitionError,
    LeaseConflictError,
    TelephonyService,
)


class TelephonyLimitsUpdate(BaseModel):
    max_concurrent_calls: int = Field(ge=1, le=10000)
    max_outbound_calls: int = Field(ge=1, le=10000)
    max_inbound_calls: int = Field(ge=1, le=10000)
    max_calls_per_minute: int = Field(ge=1, le=100000)
    lease_seconds: int = Field(default=30, ge=10, le=300)


class TelephonyPolicyUpdate(BaseModel):
    outbound_enabled: bool = True
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    allowed_weekdays: list[int] = Field(
        default_factory=lambda: [0, 1, 2, 3, 4], min_length=1, max_length=7
    )
    calling_window_start: str = Field(default="09:00", pattern=r"^[0-2][0-9]:[0-5][0-9]$")
    calling_window_end: str = Field(default="18:00", pattern=r"^[0-2][0-9]:[0-5][0-9]$")
    require_consent: bool = True
    consent_purpose: str = Field(default="outbound", min_length=1, max_length=200)
    max_attempts_per_number_per_day: int = Field(
        default=3,
        ge=0,
        le=100,
        description="Maximum daily attempts per number; 0 disables the limit.",
    )
    inbound_overflow_mode: Literal["reject", "transfer"] = "reject"
    inbound_overflow_destination_name: str = Field(default="", max_length=200)
    recording_mode: Literal["off", "always"] = "off"
    recording_disclosure_text: str = Field(default="", max_length=1000)


class DoNotCallUpsert(BaseModel):
    phone_number: str = Field(min_length=8, max_length=16)
    reason: str = Field(min_length=1, max_length=500)
    source: str = Field(min_length=1, max_length=120)
    expires_at: datetime | None = None


class ConsentRecordCreate(BaseModel):
    phone_number: str = Field(min_length=8, max_length=16)
    purpose: str = Field(default="outbound", min_length=1, max_length=200)
    status: Literal["granted", "revoked", "expired"]
    evidence_ref: str = Field(default="", max_length=1000)
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class TelephonyContactUpsert(BaseModel):
    external_id: str = Field(min_length=1, max_length=200)
    phone_number: str = Field(min_length=8, max_length=16)
    name: str = Field(default="", max_length=200)
    status: Literal["active", "suppressed"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class TelephonyContactImport(BaseModel):
    contacts: list[TelephonyContactUpsert] = Field(min_length=1, max_length=1000)


class TelephonyAddressBookUpsert(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    phone_number: str = Field(min_length=8, max_length=16)
    source: str = Field(default="automatic", max_length=80)


class TelephonyCampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agent_name: str = Field(min_length=1, max_length=200)
    trunk_id: str | None = None
    source_number: str = Field(default="", max_length=16)
    priority: int = Field(default=100, ge=0, le=1000)
    max_attempts: int = Field(default=3, ge=1, le=10)
    max_concurrent_calls: int = Field(default=10, ge=1, le=10000)
    scheduled_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TelephonyCampaignContactsAdd(BaseModel):
    contact_ids: list[str] = Field(min_length=1, max_length=5000)


class TelephonyCampaignStatusUpdate(BaseModel):
    status: Literal["running", "paused", "canceled"]


class TelephonyCampaignMaterialize(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)


class TrunkUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    direction: Literal["inbound", "outbound", "bidirectional"]
    provider: str = Field(min_length=1, max_length=80)
    livekit_trunk_id: str = Field(default="", max_length=200)
    secret_name: str = Field(default="", max_length=200)
    status: Literal["active", "disabled", "degraded"] = "active"
    numbers: list[str] = Field(default_factory=list, max_length=100)
    max_concurrent_calls: int = Field(default=100, ge=1, le=10000)
    max_calls_per_second: int = Field(default=5, ge=1, le=1000)


class TransferDestinationUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    target_uri: str = Field(min_length=8, max_length=300)
    mode: Literal["cold"] = "cold"
    status: Literal["active", "disabled"] = "active"


class OutboundCallCreate(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    destination_number: str = Field(min_length=8, max_length=16)
    source_number: str = Field(default="", max_length=16)
    agent_name: str = Field(min_length=1, max_length=200)
    trunk_id: str | None = None
    priority: int = Field(default=100, ge=0, le=1000)
    max_attempts: int = Field(default=3, ge=1, le=10)
    available_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InboundCallAdmission(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    provider_call_id: str = Field(min_length=1, max_length=200)
    worker_id: str = Field(min_length=1, max_length=200)
    source_number: str = Field(default="", max_length=16)
    destination_number: str = Field(min_length=8, max_length=16)
    agent_name: str = Field(min_length=1, max_length=200)
    room_name: str = Field(min_length=1, max_length=200)
    trunk_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DispatchClaim(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=1, ge=1, le=100)


class CallObservation(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=32, max_length=200)
    provider: str = Field(default="livekit-sip", min_length=1, max_length=80)
    provider_call_id: str = Field(default="", max_length=200)
    sip_call_id: str = Field(default="", max_length=200)
    room_name: str = Field(default="", max_length=200)
    participant_identity: str = Field(default="", max_length=200)
    sip_status: str = Field(default="", max_length=80)
    disconnect_reason: str = Field(default="", max_length=200)
    attributes: dict[str, Any] = Field(default_factory=dict)
    ended: bool = False


class CallTransferCreate(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=32, max_length=200)
    destination_name: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    context_summary: str = Field(default="", max_length=8192)


class CallTransferTransition(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=32, max_length=200)
    status: Literal["transferring", "completed", "failed", "canceled"]
    failure_code: str = Field(default="", max_length=120)
    failure_detail: str = Field(default="", max_length=2000)


class LeaseHeartbeat(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=32, max_length=200)


class CallResultUpdate(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=32, max_length=200)
    answering_machine_category: Literal[
        "", "human", "machine-ivr", "machine-vm", "machine-unavailable", "uncertain"
    ] = ""
    disposition: str = Field(default="", max_length=120)


class CallRecordingUpdate(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=32, max_length=200)
    egress_id: str = Field(min_length=1, max_length=200)
    status: Literal["starting", "active", "stopping", "completed", "failed"]
    storage_uri: str = Field(default="", max_length=1000)


class CallTransition(BaseModel):
    status: Literal[
        "dispatching", "dialing", "ringing", "active", "completed", "failed", "busy",
        "no_answer", "canceled", "reconciling"
    ]
    worker_id: str = Field(default="", max_length=200)
    lease_token: str = Field(default="", max_length=200)
    provider_call_id: str | None = Field(default=None, max_length=200)
    room_name: str | None = Field(default=None, max_length=200)
    failure_code: str = Field(default="", max_length=120)
    failure_detail: str = Field(default="", max_length=2000)
    retryable: bool = False
    retry_delay_seconds: int = Field(default=30, ge=0, le=86400)


def _telephony_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CapacityExceededError):
        return HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "1"})
    if isinstance(exc, ComplianceBlockedError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(
        exc,
        (IdempotencyConflictError, InvalidCallTransitionError, LeaseConflictError),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    return _translate_error(exc)


def create_telephony_router(service: TelephonyService) -> APIRouter:
    router = APIRouter(prefix="/api/platform", tags=["cloud-parity-telephony"])

    def require_worker_user_id(
        project_id: str,
        user_id: str = Depends(require_user_id),
    ) -> str:
        try:
            service.store.require_role(project_id, user_id, {"worker"})
        except Exception as exc:
            raise _telephony_error(exc) from exc
        return user_id

    @router.get("/projects/{project_id}/telephony/limits")
    def get_limits(
        project_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.get_limits(project_id=project_id, user_id=user_id)
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.put("/projects/{project_id}/telephony/limits")
    def update_limits(
        project_id: str,
        payload: TelephonyLimitsUpdate,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.update_limits(project_id=project_id, user_id=user_id, **payload.model_dump())
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/policy")
    def get_policy(
        project_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.get_policy(project_id=project_id, user_id=user_id)
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.put("/projects/{project_id}/telephony/policy")
    def update_policy(
        project_id: str,
        payload: TelephonyPolicyUpdate,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            values = payload.model_dump()
            values["timezone_name"] = values.pop("timezone")
            return service.update_policy(project_id=project_id, user_id=user_id, **values)
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/do-not-call", status_code=201)
    def put_do_not_call(
        project_id: str,
        payload: DoNotCallUpsert,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.upsert_do_not_call(
                project_id=project_id, user_id=user_id, **payload.model_dump()
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/do-not-call")
    def list_do_not_call(
        project_id: str,
        active_only: bool = True,
        limit: int = Query(default=100, ge=1, le=500),
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return {
                "items": service.list_do_not_call(
                    project_id=project_id,
                    user_id=user_id,
                    active_only=active_only,
                    limit=limit,
                )
            }
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.delete("/projects/{project_id}/telephony/do-not-call/{entry_id}")
    def delete_do_not_call(
        project_id: str,
        entry_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.delete_do_not_call(
                project_id=project_id, user_id=user_id, entry_id=entry_id
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/consents", status_code=201)
    def record_consent(
        project_id: str,
        payload: ConsentRecordCreate,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.record_consent(
                project_id=project_id, user_id=user_id, **payload.model_dump()
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.put("/projects/{project_id}/telephony/contacts/{external_id}")
    def upsert_contact(
        project_id: str,
        external_id: str,
        payload: TelephonyContactUpsert,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        if payload.external_id != external_id:
            raise HTTPException(status_code=422, detail="path and payload external_id must match")
        try:
            return service.upsert_contact(
                project_id=project_id,
                user_id=user_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/contacts/import")
    def import_contacts(
        project_id: str,
        payload: TelephonyContactImport,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            items = [
                service.upsert_contact(
                    project_id=project_id,
                    user_id=user_id,
                    **contact.model_dump(),
                )
                for contact in payload.contacts
            ]
            return {"items": items, "count": len(items)}
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/contacts")
    def list_contacts(
        project_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        search: str = Query(default="", max_length=200),
        status: Literal["active", "suppressed"] | None = None,
        cursor: str | None = None,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.list_contacts_page(
                project_id=project_id,
                user_id=user_id,
                limit=limit,
                search=search,
                status=status,
                cursor=cursor,
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/address-book/sync")
    def sync_address_book(
        project_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.sync_address_book_from_contacts(
                project_id=project_id,
                user_id=user_id,
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/address-book")
    def upsert_address_book(
        project_id: str,
        payload: TelephonyAddressBookUpsert,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.upsert_address_book(
                project_id=project_id,
                user_id=user_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/address-book/lookup")
    def resolve_address_book(
        project_id: str,
        query: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=3, ge=1, le=10),
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.resolve_address_book(
                project_id=project_id,
                user_id=user_id,
                query=query,
                limit=limit,
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.delete("/projects/{project_id}/telephony/contacts/{contact_id}")
    def delete_contact(
        project_id: str,
        contact_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.delete_contact(
                project_id=project_id, user_id=user_id, contact_id=contact_id
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/campaigns", status_code=201)
    def create_campaign(
        project_id: str,
        payload: TelephonyCampaignCreate,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.create_campaign(
                project_id=project_id,
                user_id=user_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/campaigns")
    def list_campaigns(
        project_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return {"items": service.list_campaigns(
                project_id=project_id, user_id=user_id, limit=limit
            )}
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/campaigns/{campaign_id}/contacts")
    def list_campaign_contacts(
        project_id: str,
        campaign_id: str,
        limit: int = Query(default=5000, ge=1, le=5000),
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return {
                "items": service.list_campaign_contacts(
                    project_id=project_id,
                    user_id=user_id,
                    campaign_id=campaign_id,
                    limit=limit,
                )
            }
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/campaigns/{campaign_id}/contacts")
    def add_campaign_contacts(
        project_id: str,
        campaign_id: str,
        payload: TelephonyCampaignContactsAdd,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.add_campaign_contacts(
                project_id=project_id,
                user_id=user_id,
                campaign_id=campaign_id,
                contact_ids=payload.contact_ids,
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.put("/projects/{project_id}/telephony/campaigns/{campaign_id}/status")
    def update_campaign_status(
        project_id: str,
        campaign_id: str,
        payload: TelephonyCampaignStatusUpdate,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.set_campaign_status(
                project_id=project_id,
                user_id=user_id,
                campaign_id=campaign_id,
                status=payload.status,
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/campaigns/materialize")
    def materialize_campaigns(
        project_id: str,
        payload: TelephonyCampaignMaterialize,
        user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return service.materialize_campaigns(
                project_id=project_id,
                user_id=user_id,
                limit=payload.limit,
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/consents")
    def list_consents(
        project_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return {
                "items": service.list_consents(
                    project_id=project_id, user_id=user_id, limit=limit
                )
            }
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.put("/projects/{project_id}/telephony/trunks/{name}")
    def put_trunk(
        project_id: str,
        name: str,
        payload: TrunkUpsert,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        if payload.name != name:
            raise HTTPException(status_code=422, detail="path and payload trunk name must match")
        try:
            return service.upsert_trunk(
                project_id=project_id,
                user_id=user_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/trunks")
    def list_trunks(
        project_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return {"items": service.list_trunks(project_id=project_id, user_id=user_id)}
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.put("/projects/{project_id}/telephony/transfer-destinations/{name}")
    def put_transfer_destination(
        project_id: str,
        name: str,
        payload: TransferDestinationUpsert,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        if payload.name != name:
            raise HTTPException(
                status_code=422,
                detail="path and payload transfer destination name must match",
            )
        try:
            return service.upsert_transfer_destination(
                project_id=project_id, user_id=user_id, **payload.model_dump()
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/transfer-destinations")
    def list_transfer_destinations(
        project_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return {
                "items": service.list_transfer_destinations(
                    project_id=project_id, user_id=user_id
                )
            }
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/calls/outbound", status_code=202)
    def create_outbound_call(
        project_id: str,
        payload: OutboundCallCreate,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.enqueue_outbound(
                project_id=project_id,
                user_id=user_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/calls/inbound", status_code=201)
    def admit_inbound_call(
        project_id: str,
        payload: InboundCallAdmission,
        user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return service.admit_inbound(
                project_id=project_id,
                user_id=user_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/dispatch/claim")
    def claim_dispatch(
        project_id: str,
        payload: DispatchClaim,
        user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return {
                "items": service.claim_outbound(
                    project_id=project_id,
                    user_id=user_id,
                    **payload.model_dump(),
                )
            }
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/reconciliation/claim")
    def claim_reconciliation(
        project_id: str,
        payload: DispatchClaim,
        user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return {
                "items": service.claim_reconciliation(
                    project_id=project_id,
                    user_id=user_id,
                    **payload.model_dump(),
                )
            }
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/calls/{call_id}/observe")
    def observe_call(
        project_id: str,
        call_id: str,
        payload: CallObservation,
        user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return service.observe_call(
                project_id=project_id,
                user_id=user_id,
                call_id=call_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/calls/{call_id}/transfers", status_code=201)
    def request_transfer(
        project_id: str,
        call_id: str,
        payload: CallTransferCreate,
        user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return service.request_transfer(
                project_id=project_id,
                user_id=user_id,
                call_id=call_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post(
        "/projects/{project_id}/telephony/calls/{call_id}/transfers/{transfer_id}/transition"
    )
    def transition_transfer(
        project_id: str,
        call_id: str,
        transfer_id: str,
        payload: CallTransferTransition,
        user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return service.transition_transfer(
                project_id=project_id,
                user_id=user_id,
                call_id=call_id,
                transfer_id=transfer_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/calls/{call_id}/transfers")
    def list_transfers(
        project_id: str,
        call_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return {
                "items": service.list_transfers(
                    project_id=project_id, user_id=user_id, call_id=call_id
                )
            }
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/calls/{call_id}/heartbeat")
    def heartbeat(
        project_id: str,
        call_id: str,
        payload: LeaseHeartbeat,
        user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return service.heartbeat(
                project_id=project_id,
                user_id=user_id,
                call_id=call_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/calls/{call_id}/transition")
    def transition(
        project_id: str,
        call_id: str,
        payload: CallTransition,
        user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return service.transition_call(
                project_id=project_id,
                user_id=user_id,
                call_id=call_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/metrics")
    def metrics(
        project_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.metrics(project_id=project_id, user_id=user_id)
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/calls")
    def list_calls(
        project_id: str,
        user_id: str = Depends(require_user_id),
        direction: Literal["inbound", "outbound"] | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        try:
            return {
                "items": service.list_calls(
                    project_id=project_id,
                    user_id=user_id,
                    direction=direction,
                    status=status,
                    limit=limit,
                )
            }
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/calls/{call_id}")
    def get_call(
        project_id: str,
        call_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.get_call(project_id=project_id, user_id=user_id, call_id=call_id)
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/calls/{call_id}/result")
    def update_call_result(
        project_id: str,
        call_id: str,
        payload: CallResultUpdate,
        user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return service.record_call_result(
                project_id=project_id,
                user_id=user_id,
                call_id=call_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/projects/{project_id}/telephony/calls/{call_id}/recording")
    def update_call_recording(
        project_id: str,
        call_id: str,
        payload: CallRecordingUpdate,
        user_id: str = Depends(require_worker_user_id),
    ) -> dict:
        try:
            return service.record_call_recording(
                project_id=project_id,
                user_id=user_id,
                call_id=call_id,
                **payload.model_dump(),
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/calls/{call_id}/cdr")
    def get_cdr(
        project_id: str,
        call_id: str,
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.get_cdr(
                project_id=project_id, user_id=user_id, call_id=call_id
            )
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.get("/projects/{project_id}/telephony/calls/{call_id}/recording-access")
    def get_recording_access(
        project_id: str,
        call_id: str,
        ttl_seconds: int = Query(default=300, ge=30, le=3600),
        user_id: str = Depends(require_user_id),
    ) -> dict:
        try:
            return service.get_recording_access(
                project_id=project_id,
                user_id=user_id,
                call_id=call_id,
                ttl_seconds=ttl_seconds,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise _telephony_error(exc) from exc

    @router.post("/telephony/webhooks/livekit", include_in_schema=False)
    async def livekit_webhook(request: Request) -> dict:
        api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
        api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
        if not api_key or not api_secret:
            raise HTTPException(status_code=503, detail="LiveKit webhook verification is not configured")
        authorization = request.headers.get("Authorization", "")
        max_body_bytes = 1024 * 1024
        content_length = request.headers.get("Content-Length", "").strip()
        if content_length:
            try:
                if int(content_length) > max_body_bytes:
                    raise HTTPException(status_code=413, detail="webhook payload is too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        raw_body = await request.body()
        if len(raw_body) > max_body_bytes:
            raise HTTPException(status_code=413, detail="webhook payload is too large")
        try:
            body = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="webhook payload must be UTF-8") from exc
        try:
            from livekit import api as livekit_api

            receiver = livekit_api.WebhookReceiver(
                livekit_api.TokenVerifier(api_key, api_secret)
            )
            event = receiver.receive(body, authorization)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="invalid LiveKit webhook") from exc

        participant = event.participant
        room = event.room
        attributes = dict(participant.attributes) if participant is not None else {}
        participant_kind = ""
        if participant is not None:
            try:
                participant_kind = livekit_api.ParticipantInfo.Kind.Name(
                    int(participant.kind)
                )
            except (TypeError, ValueError):
                participant_kind = str(participant.kind)
        created_at = (
            datetime.fromtimestamp(int(event.created_at), tz=timezone.utc)
            if int(event.created_at or 0) > 0
            else datetime.now(timezone.utc)
        )
        return await asyncio.to_thread(
            service.ingest_livekit_event,
            event_id=str(event.id),
            event_type=str(event.event),
            room_name=str(room.name) if room is not None else "",
            participant_identity=str(participant.identity) if participant is not None else "",
            participant_kind=participant_kind,
            participant_metadata=str(participant.metadata) if participant is not None else "",
            attributes=attributes,
            disconnect_reason=(
                str(participant.disconnect_reason) if participant is not None else ""
            ),
            egress_id=(
                str(event.egress_info.egress_id)
                if event.egress_info is not None
                else ""
            ),
            egress_status=(
                livekit_api.EgressStatus.Name(int(event.egress_info.status))
                if event.egress_info is not None and event.egress_info.egress_id
                else ""
            ),
            egress_error=(
                str(event.egress_info.error)
                if event.egress_info is not None
                else ""
            ),
            egress_storage_uri=(
                str(event.egress_info.file_results[0].location)
                if event.egress_info is not None and event.egress_info.file_results
                else ""
            ),
            observed_at=created_at,
        )

    return router
