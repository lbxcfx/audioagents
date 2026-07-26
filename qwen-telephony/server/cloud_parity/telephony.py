from __future__ import annotations

import base64
from datetime import datetime, time as clock_time, timedelta, timezone
import hashlib
import hmac
import json
import re
from typing import Any, Iterable, Protocol
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import PlatformStore, ResourceNotFoundError, _row, _utc_now
from .recording_access import presign_recording_uri, validate_recording_storage_uri


ACTIVE_STATUSES = frozenset(
    {"leased", "dispatching", "dialing", "ringing", "active", "reconciling"}
)
TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "busy", "no_answer", "canceled", "blocked"}
)
RETRYABLE_STATUSES = frozenset({"failed", "busy", "no_answer"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"canceled"}),
    "leased": frozenset({"dispatching", "failed", "canceled"}),
    "dispatching": frozenset({"dialing", "reconciling", "failed", "canceled"}),
    "dialing": frozenset(
        {"ringing", "active", "reconciling", "failed", "busy", "no_answer", "canceled"}
    ),
    "ringing": frozenset(
        {"active", "reconciling", "failed", "busy", "no_answer", "canceled"}
    ),
    "active": frozenset({"completed", "failed", "canceled"}),
    "reconciling": frozenset({"active", "completed", "failed", "canceled"}),
}

DEFAULT_LIMITS = {
    "max_concurrent_calls": 100,
    "max_outbound_calls": 80,
    "max_inbound_calls": 80,
    "max_calls_per_minute": 60,
    "lease_seconds": 30,
}

DEFAULT_POLICY = {
    "outbound_enabled": True,
    "timezone": "Asia/Shanghai",
    "allowed_weekdays": [0, 1, 2, 3, 4],
    "calling_window_start": "09:00",
    "calling_window_end": "18:00",
    "require_consent": True,
    "consent_purpose": "outbound",
    "max_attempts_per_number_per_day": 3,
    "inbound_overflow_mode": "reject",
    "inbound_overflow_destination_name": "",
    "recording_mode": "off",
    "recording_disclosure_text": "",
}

_E164_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,200}$")
_SIP_URI_RE = re.compile(
    r"^sip:[A-Za-z0-9_.!~*'()%+\-]+@[A-Za-z0-9.-]+(?::[0-9]{1,5})?$"
)


class CapacityExceededError(RuntimeError):
    pass


class LeaseConflictError(RuntimeError):
    pass


class InvalidCallTransitionError(RuntimeError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


class ComplianceBlockedError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"outbound call blocked by compliance policy: {reason}")


class PhoneCipher(Protocol):
    def encrypt(self, value: str) -> str: ...
    def decrypt(self, value: str) -> str: ...


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return _now(value).isoformat().replace("+00:00", "Z")


def _validate_phone(value: str, *, optional: bool = False) -> str:
    normalized = value.strip()
    if optional and not normalized:
        return ""
    if not _E164_RE.fullmatch(normalized):
        raise ValueError("phone numbers must use E.164 format, for example +8613800138000")
    return normalized


def _validate_identifier(value: str, label: str) -> str:
    normalized = value.strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"invalid {label}")
    return normalized


class TelephonyService:
    """Tenant-isolated, transactional call admission and dispatch control plane.

    PostgreSQL workers serialize capacity decisions by locking the project row.
    SQLite uses BEGIN IMMEDIATE through PlatformStore, preserving equivalent
    behavior for local development and concurrency tests.
    """

    def __init__(
        self,
        store: PlatformStore,
        *,
        phone_hash_key: str | bytes | None = None,
        phone_cipher: PhoneCipher | None = None,
    ):
        self.store = store
        self._phone_cipher = phone_cipher
        raw_key = phone_hash_key or "development-only-phone-hash-key-change-before-production"
        self._phone_hash_key = raw_key.encode("utf-8") if isinstance(raw_key, str) else raw_key
        if len(self._phone_hash_key) < 32:
            raise ValueError("phone_hash_key must contain at least 32 bytes")

    def _protect_phone(self, value: str) -> str:
        if not value or self._phone_cipher is None or value.startswith("enc:v1:"):
            return value
        return "enc:v1:" + self._phone_cipher.encrypt(value)

    def _reveal_phone(self, value: Any) -> str:
        text = str(value or "")
        if not text.startswith("enc:v1:"):
            return text
        if self._phone_cipher is None:
            raise RuntimeError("encrypted phone data cannot be read without CLOUD_PARITY_MASTER_KEY")
        return self._phone_cipher.decrypt(text.removeprefix("enc:v1:"))

    def _protect_json(self, value: str) -> str:
        if self._phone_cipher is None or value.startswith("encjson:v1:"):
            return value
        return "encjson:v1:" + self._phone_cipher.encrypt(value)

    def _reveal_json(self, value: Any) -> dict[str, Any]:
        text = str(value or "{}")
        if text.startswith("encjson:v1:"):
            if self._phone_cipher is None:
                raise RuntimeError("encrypted metadata cannot be read without CLOUD_PARITY_MASTER_KEY")
            text = self._phone_cipher.decrypt(text.removeprefix("encjson:v1:"))
        decoded = json.loads(text or "{}")
        return decoded if isinstance(decoded, dict) else {}

    def protect_legacy_phone_data(self) -> dict[str, int]:
        """Encrypt pre-existing plaintext phone fields after enabling a master key."""
        if self._phone_cipher is None:
            return {"call_jobs": 0, "contacts": 0, "campaigns": 0}
        changed = {"call_jobs": 0, "contacts": 0, "campaigns": 0}
        with self.store.transaction() as conn:
            for table, id_column, fields, json_fields, result_key in (
                ("call_jobs", "id", ("source_number", "destination_number"), ("metadata_json",), "call_jobs"),
                ("telephony_contacts", "id", ("phone_number",), ("metadata_json",), "contacts"),
                ("telephony_campaigns", "id", ("source_number",), ("metadata_json",), "campaigns"),
            ):
                rows = conn.execute(
                    f"SELECT {id_column}, {', '.join((*fields, *json_fields))} FROM {table}"
                ).fetchall()
                for row in rows:
                    updates = {
                        field: self._protect_phone(str(row[field] or ""))
                        for field in fields
                        if row[field] and not str(row[field]).startswith("enc:v1:")
                    }
                    updates.update(
                        {
                            field: self._protect_json(str(row[field] or "{}"))
                            for field in json_fields
                            if not str(row[field] or "").startswith("encjson:v1:")
                        }
                    )
                    if not updates:
                        continue
                    assignments = ", ".join(f"{field} = ?" for field in updates)
                    conn.execute(
                        f"UPDATE {table} SET {assignments} WHERE {id_column} = ?",
                        (*updates.values(), row[id_column]),
                    )
                    changed[result_key] += 1
        return changed

    def _lock_project(self, conn: Any, project_id: str) -> None:
        suffix = " FOR UPDATE" if self.store.backend == "postgresql" else ""
        row = conn.execute(
            f"SELECT id FROM projects WHERE id = ?{suffix}", (project_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("project not found")

    def _ensure_limits(self, conn: Any, project_id: str, now: str) -> dict[str, Any]:
        conn.execute(
            """
            INSERT INTO telephony_limits (
                project_id, max_concurrent_calls, max_outbound_calls,
                max_inbound_calls, max_calls_per_minute, lease_seconds, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO NOTHING
            """,
            (
                project_id,
                DEFAULT_LIMITS["max_concurrent_calls"],
                DEFAULT_LIMITS["max_outbound_calls"],
                DEFAULT_LIMITS["max_inbound_calls"],
                DEFAULT_LIMITS["max_calls_per_minute"],
                DEFAULT_LIMITS["lease_seconds"],
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM telephony_limits WHERE project_id = ?", (project_id,)
        ).fetchone()
        return _row(row) or {"project_id": project_id, **DEFAULT_LIMITS, "updated_at": now}

    def get_limits(self, *, project_id: str, user_id: str) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.read")
        now = _utc_now()
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            return self._ensure_limits(conn, project_id, now)

    def update_limits(
        self,
        *,
        project_id: str,
        user_id: str,
        max_concurrent_calls: int,
        max_outbound_calls: int,
        max_inbound_calls: int,
        max_calls_per_minute: int,
        lease_seconds: int,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.manage")
        values = (
            max_concurrent_calls,
            max_outbound_calls,
            max_inbound_calls,
            max_calls_per_minute,
            lease_seconds,
        )
        if any(value < 1 for value in values):
            raise ValueError("telephony limits must be positive")
        if max_outbound_calls > max_concurrent_calls or max_inbound_calls > max_concurrent_calls:
            raise ValueError("direction limits cannot exceed max_concurrent_calls")
        if max_concurrent_calls > 10000 or max_calls_per_minute > 100000:
            raise ValueError("telephony limits exceed the supported safety bound")
        if not 10 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 10 and 300")
        now = _utc_now()
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            conn.execute(
                """
                INSERT INTO telephony_limits (
                    project_id, max_concurrent_calls, max_outbound_calls,
                    max_inbound_calls, max_calls_per_minute, lease_seconds, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    max_concurrent_calls = excluded.max_concurrent_calls,
                    max_outbound_calls = excluded.max_outbound_calls,
                    max_inbound_calls = excluded.max_inbound_calls,
                    max_calls_per_minute = excluded.max_calls_per_minute,
                    lease_seconds = excluded.lease_seconds,
                    updated_at = excluded.updated_at
                """,
                (project_id, *values, now),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action="telephony.limits.update",
                resource_type="telephony_limits",
                resource_id=project_id,
                payload={
                    "max_concurrent_calls": max_concurrent_calls,
                    "max_outbound_calls": max_outbound_calls,
                    "max_inbound_calls": max_inbound_calls,
                    "max_calls_per_minute": max_calls_per_minute,
                    "lease_seconds": lease_seconds,
                },
            )
            return self._ensure_limits(conn, project_id, now)

    def _phone_hash(self, phone_number: str) -> str:
        return hmac.new(
            self._phone_hash_key,
            phone_number.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _policy(row: Any) -> dict[str, Any]:
        record = _row(row) or {}
        record["allowed_weekdays"] = json.loads(
            record.pop("allowed_weekdays_json", "[0,1,2,3,4]")
        )
        record["require_consent"] = bool(record.get("require_consent"))
        record["outbound_enabled"] = bool(record.get("outbound_enabled"))
        return record

    def _ensure_policy(self, conn: Any, project_id: str, now: str) -> dict[str, Any]:
        conn.execute(
            """
            INSERT INTO telephony_policies (
                project_id, timezone, allowed_weekdays_json, calling_window_start,
                calling_window_end, require_consent, consent_purpose,
                max_attempts_per_number_per_day, inbound_overflow_mode,
                inbound_overflow_destination_name, outbound_enabled,
                recording_mode, recording_disclosure_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO NOTHING
            """,
            (
                project_id,
                DEFAULT_POLICY["timezone"],
                json.dumps(DEFAULT_POLICY["allowed_weekdays"], separators=(",", ":")),
                DEFAULT_POLICY["calling_window_start"],
                DEFAULT_POLICY["calling_window_end"],
                int(DEFAULT_POLICY["require_consent"]),
                DEFAULT_POLICY["consent_purpose"],
                DEFAULT_POLICY["max_attempts_per_number_per_day"],
                DEFAULT_POLICY["inbound_overflow_mode"],
                DEFAULT_POLICY["inbound_overflow_destination_name"],
                int(DEFAULT_POLICY["outbound_enabled"]),
                DEFAULT_POLICY["recording_mode"],
                DEFAULT_POLICY["recording_disclosure_text"],
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM telephony_policies WHERE project_id = ?", (project_id,)
        ).fetchone()
        return self._policy(row)

    def get_policy(self, *, project_id: str, user_id: str) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.read")
        now = _utc_now()
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            return self._ensure_policy(conn, project_id, now)

    def update_policy(
        self,
        *,
        project_id: str,
        user_id: str,
        timezone_name: str,
        allowed_weekdays: Iterable[int],
        calling_window_start: str,
        calling_window_end: str,
        require_consent: bool,
        consent_purpose: str,
        max_attempts_per_number_per_day: int,
        outbound_enabled: bool = True,
        inbound_overflow_mode: str = "reject",
        inbound_overflow_destination_name: str = "",
        recording_mode: str = "off",
        recording_disclosure_text: str = "",
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.manage")
        normalized_timezone = timezone_name.strip()
        try:
            ZoneInfo(normalized_timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("invalid IANA timezone") from exc
        weekdays = sorted(set(int(day) for day in allowed_weekdays))
        if not weekdays or any(day < 0 or day > 6 for day in weekdays):
            raise ValueError("allowed_weekdays must contain values from 0 (Monday) to 6")
        try:
            window_start = clock_time.fromisoformat(calling_window_start.strip())
            window_end = clock_time.fromisoformat(calling_window_end.strip())
        except ValueError as exc:
            raise ValueError("calling window must use HH:MM format") from exc
        if window_start.second or window_start.microsecond or window_end.second or window_end.microsecond:
            raise ValueError("calling window must use minute precision")
        if window_start >= window_end:
            raise ValueError("calling_window_start must be earlier than calling_window_end")
        purpose = _validate_identifier(consent_purpose, "consent_purpose")
        if not 1 <= max_attempts_per_number_per_day <= 100:
            raise ValueError("max_attempts_per_number_per_day must be between 1 and 100")
        if inbound_overflow_mode not in {"reject", "transfer"}:
            raise ValueError("inbound_overflow_mode must be reject or transfer")
        overflow_destination = inbound_overflow_destination_name.strip()
        if inbound_overflow_mode == "transfer":
            overflow_destination = _validate_identifier(
                overflow_destination, "inbound overflow destination name"
            )
        elif overflow_destination:
            raise ValueError("overflow destination requires inbound_overflow_mode=transfer")
        if recording_mode not in {"off", "always"}:
            raise ValueError("recording_mode must be off or always")
        disclosure = recording_disclosure_text.strip()
        if len(disclosure) > 1000:
            raise ValueError("recording_disclosure_text must not exceed 1000 characters")
        if recording_mode == "always" and not disclosure:
            raise ValueError("recording disclosure text is required when recording is enabled")
        start_text = window_start.strftime("%H:%M")
        end_text = window_end.strftime("%H:%M")
        now = _utc_now()
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            if inbound_overflow_mode == "transfer":
                destination = conn.execute(
                    """
                    SELECT id FROM transfer_destinations
                    WHERE project_id = ? AND name = ? AND status = 'active'
                    """,
                    (project_id, overflow_destination),
                ).fetchone()
                if destination is None:
                    raise ResourceNotFoundError(
                        "active inbound overflow transfer destination not found"
                    )
            conn.execute(
                """
                INSERT INTO telephony_policies (
                    project_id, timezone, allowed_weekdays_json, calling_window_start,
                    calling_window_end, require_consent, consent_purpose,
                    max_attempts_per_number_per_day, inbound_overflow_mode,
                    inbound_overflow_destination_name, outbound_enabled,
                    recording_mode, recording_disclosure_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    timezone = excluded.timezone,
                    allowed_weekdays_json = excluded.allowed_weekdays_json,
                    calling_window_start = excluded.calling_window_start,
                    calling_window_end = excluded.calling_window_end,
                    require_consent = excluded.require_consent,
                    consent_purpose = excluded.consent_purpose,
                    max_attempts_per_number_per_day = excluded.max_attempts_per_number_per_day,
                    inbound_overflow_mode = excluded.inbound_overflow_mode,
                    inbound_overflow_destination_name = excluded.inbound_overflow_destination_name,
                    outbound_enabled = excluded.outbound_enabled,
                    recording_mode = excluded.recording_mode,
                    recording_disclosure_text = excluded.recording_disclosure_text,
                    updated_at = excluded.updated_at
                """,
                (
                    project_id,
                    normalized_timezone,
                    json.dumps(weekdays, separators=(",", ":")),
                    start_text,
                    end_text,
                    int(require_consent),
                    purpose,
                    max_attempts_per_number_per_day,
                    inbound_overflow_mode,
                    overflow_destination,
                    int(outbound_enabled),
                    recording_mode,
                    disclosure,
                    now,
                    now,
                ),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action="telephony.policy.update",
                resource_type="telephony_policy",
                resource_id=project_id,
                payload={
                    "timezone": normalized_timezone,
                    "allowed_weekdays": weekdays,
                    "calling_window_start": start_text,
                    "calling_window_end": end_text,
                    "require_consent": require_consent,
                    "consent_purpose": purpose,
                    "max_attempts_per_number_per_day": max_attempts_per_number_per_day,
                    "outbound_enabled": outbound_enabled,
                    "inbound_overflow_mode": inbound_overflow_mode,
                    "inbound_overflow_destination_name": overflow_destination,
                    "recording_mode": recording_mode,
                },
            )
            return self._ensure_policy(conn, project_id, now)

    @staticmethod
    def _compliance_record(row: Any) -> dict[str, Any]:
        record = _row(row) or {}
        record.pop("phone_hash", None)
        return record

    def upsert_do_not_call(
        self,
        *,
        project_id: str,
        user_id: str,
        phone_number: str,
        reason: str,
        source: str,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.manage")
        phone = _validate_phone(phone_number)
        normalized_reason = reason.strip()
        normalized_source = source.strip()
        if not normalized_reason or len(normalized_reason) > 500:
            raise ValueError("DNC reason is required and must not exceed 500 characters")
        if not normalized_source or len(normalized_source) > 120:
            raise ValueError("DNC source is required and must not exceed 120 characters")
        expiry = _timestamp(expires_at) if expires_at else None
        if expires_at and _now(expires_at) <= _now():
            raise ValueError("DNC expiry must be in the future")
        phone_hash = self._phone_hash(phone)
        entry_id = str(uuid.uuid4())
        now = _utc_now()
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            conn.execute(
                """
                INSERT INTO do_not_call_entries (
                    id, project_id, phone_hash, phone_last4, reason, source,
                    expires_at, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, phone_hash) DO UPDATE SET
                    reason = excluded.reason, source = excluded.source,
                    expires_at = excluded.expires_at, updated_at = excluded.updated_at
                """,
                (
                    entry_id,
                    project_id,
                    phone_hash,
                    phone[-4:],
                    normalized_reason,
                    normalized_source,
                    expiry,
                    user_id,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM do_not_call_entries WHERE project_id = ? AND phone_hash = ?",
                (project_id, phone_hash),
            ).fetchone()
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action="telephony.dnc.upsert",
                resource_type="do_not_call_entry",
                resource_id=str(row["id"]),
                payload={"phone_last4": phone[-4:], "reason": normalized_reason},
            )
        return self._compliance_record(row)

    def list_do_not_call(
        self, *, project_id: str, user_id: str, active_only: bool = True, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "telephony.read")
        clause = " AND (expires_at IS NULL OR expires_at > ?)" if active_only else ""
        params: list[Any] = [project_id]
        if active_only:
            params.append(_utc_now())
        params.append(max(1, min(limit, 500)))
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM do_not_call_entries
                WHERE project_id = ?{clause}
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._compliance_record(row) for row in rows]

    def delete_do_not_call(
        self, *, project_id: str, user_id: str, entry_id: str
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.manage")
        now = _utc_now()
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            row = conn.execute(
                "SELECT * FROM do_not_call_entries WHERE id = ? AND project_id = ?",
                (entry_id, project_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("DNC entry not found")
            conn.execute(
                "DELETE FROM do_not_call_entries WHERE id = ? AND project_id = ?",
                (entry_id, project_id),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action="telephony.dnc.delete",
                resource_type="do_not_call_entry",
                resource_id=entry_id,
                payload={"phone_last4": str(row["phone_last4"]), "deleted_at": now},
            )
        return {"id": entry_id, "deleted": True}

    def record_consent(
        self,
        *,
        project_id: str,
        user_id: str,
        phone_number: str,
        purpose: str,
        status: str,
        evidence_ref: str,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.manage")
        phone = _validate_phone(phone_number)
        normalized_purpose = _validate_identifier(purpose, "consent purpose")
        if status not in {"granted", "revoked", "expired"}:
            raise ValueError("invalid consent status")
        evidence = evidence_ref.strip()
        if status == "granted" and not evidence:
            raise ValueError("evidence_ref is required for granted consent")
        if len(evidence) > 1000:
            raise ValueError("evidence_ref must not exceed 1000 characters")
        start = _now(valid_from)
        end = _now(valid_until) if valid_until else None
        if end and end <= start:
            raise ValueError("valid_until must be later than valid_from")
        phone_hash = self._phone_hash(phone)
        record_id = str(uuid.uuid4())
        now = _utc_now()
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            conn.execute(
                """
                INSERT INTO consent_records (
                    id, project_id, phone_hash, phone_last4, purpose, status,
                    evidence_ref, valid_from, valid_until, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    project_id,
                    phone_hash,
                    phone[-4:],
                    normalized_purpose,
                    status,
                    evidence,
                    _timestamp(start),
                    _timestamp(end) if end else None,
                    user_id,
                    now,
                ),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action=f"telephony.consent.{status}",
                resource_type="consent_record",
                resource_id=record_id,
                payload={"phone_last4": phone[-4:], "purpose": normalized_purpose},
            )
            row = conn.execute(
                "SELECT * FROM consent_records WHERE id = ?", (record_id,)
            ).fetchone()
        return self._compliance_record(row)

    def list_consents(
        self, *, project_id: str, user_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "telephony.read")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM consent_records WHERE project_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (project_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._compliance_record(row) for row in rows]

    def _contact(self, row: Any, *, redact_phone: bool = False) -> dict[str, Any]:
        record = _row(row) or {}
        record["metadata"] = self._reveal_json(record.pop("metadata_json", "{}"))
        record.pop("phone_hash", None)
        record["phone_number"] = self._reveal_phone(record.get("phone_number"))
        if redact_phone and record.get("phone_number"):
            value = str(record["phone_number"])
            record["phone_number"] = "+" + "*" * max(0, len(value) - 5) + value[-4:]
            record["metadata"] = {}
        return record

    def upsert_contact(
        self,
        *,
        project_id: str,
        user_id: str,
        external_id: str,
        phone_number: str,
        name: str = "",
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.operate")
        external = _validate_identifier(external_id, "contact external_id")
        phone = _validate_phone(phone_number)
        normalized_name = name.strip()[:200]
        if status not in {"active", "suppressed"}:
            raise ValueError("contact status must be active or suppressed")
        serialized = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 8192:
            raise ValueError("contact metadata exceeds 8 KiB")
        now = _utc_now()
        contact_id = str(uuid.uuid4())
        with self.store.transaction() as conn:
            existing = conn.execute(
                """
                SELECT id FROM telephony_contacts
                WHERE project_id = ? AND external_id = ?
                """,
                (project_id, external),
            ).fetchone()
            if existing is not None:
                contact_id = str(existing["id"])
            conn.execute(
                """
                INSERT INTO telephony_contacts (
                    id, project_id, external_id, name, phone_number, phone_hash,
                    status, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, external_id) DO UPDATE SET
                    name = excluded.name, phone_number = excluded.phone_number,
                    phone_hash = excluded.phone_hash, status = excluded.status,
                    metadata_json = excluded.metadata_json, updated_at = excluded.updated_at
                """,
                (
                    contact_id,
                    project_id,
                    external,
                    normalized_name,
                    self._protect_phone(phone),
                    self._phone_hash(phone),
                    status,
                    self._protect_json(serialized),
                    now,
                    now,
                ),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action="telephony.contact.upsert",
                resource_type="telephony_contact",
                resource_id=contact_id,
                payload={"external_id": external, "phone_last4": phone[-4:], "status": status},
            )
            row = conn.execute(
                "SELECT * FROM telephony_contacts WHERE id = ?", (contact_id,)
            ).fetchone()
        return self._contact(row)

    def list_contacts(
        self, *, project_id: str, user_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.list_contacts_page(
            project_id=project_id,
            user_id=user_id,
            limit=limit,
        )["items"]

    def list_contacts_page(
        self,
        *,
        project_id: str,
        user_id: str,
        limit: int = 100,
        search: str = "",
        status: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        role = self.store.require_permission(project_id, user_id, "telephony.read")
        safe_limit = max(1, min(limit, 1000))
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        query = search.strip().lower()
        if len(query) > 200:
            raise ValueError("contact search must not exceed 200 characters")
        if query:
            clauses.append("(LOWER(external_id) LIKE ? OR LOWER(name) LIKE ?)")
            pattern = f"%{query}%"
            params.extend([pattern, pattern])
        if status:
            if status not in {"active", "suppressed"}:
                raise ValueError("contact status must be active or suppressed")
            clauses.append("status = ?")
            params.append(status)
        if cursor:
            try:
                padded = cursor + "=" * (-len(cursor) % 4)
                updated_at, contact_id = json.loads(
                    base64.urlsafe_b64decode(padded).decode("utf-8")
                )
            except Exception as exc:
                raise ValueError("invalid contact cursor") from exc
            clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
            params.extend([str(updated_at), str(updated_at), str(contact_id)])
        params.append(safe_limit + 1)
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM telephony_contacts
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, id DESC LIMIT ?
                """,
                params,
            ).fetchall()
        has_more = len(rows) > safe_limit
        selected = rows[:safe_limit]
        items = [self._contact(row, redact_phone=role == "viewer") for row in selected]
        next_cursor = None
        if has_more and items:
            raw = json.dumps(
                [items[-1]["updated_at"], items[-1]["id"]], separators=(",", ":")
            ).encode("utf-8")
            next_cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return {"items": items, "next_cursor": next_cursor}

    def delete_contact(
        self, *, project_id: str, user_id: str, contact_id: str
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.manage")
        with self.store.transaction() as conn:
            contact = conn.execute(
                "SELECT * FROM telephony_contacts WHERE id = ? AND project_id = ?",
                (contact_id, project_id),
            ).fetchone()
            if contact is None:
                raise ResourceNotFoundError("telephony contact not found")
            active = conn.execute(
                """
                SELECT COUNT(*) AS count FROM call_jobs c
                JOIN telephony_campaign_contacts cc ON cc.call_id = c.id
                WHERE cc.contact_id = ? AND cc.project_id = ?
                  AND c.status IN ('queued','leased','dispatching','dialing','ringing','active','reconciling')
                """,
                (contact_id, project_id),
            ).fetchone()
            if int(active["count"] or 0) > 0:
                raise ValueError("contact has queued or active calls; cancel them before erasure")
            calls = conn.execute(
                """
                SELECT c.id FROM call_jobs c
                JOIN telephony_campaign_contacts cc ON cc.call_id = c.id
                WHERE cc.contact_id = ? AND cc.project_id = ?
                """,
                (contact_id, project_id),
            ).fetchall()
            for call in calls:
                conn.execute(
                    "DELETE FROM call_jobs WHERE id = ? AND project_id = ?",
                    (call["id"], project_id),
                )
            conn.execute(
                "DELETE FROM telephony_contacts WHERE id = ? AND project_id = ?",
                (contact_id, project_id),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action="telephony.contact.erase",
                resource_type="telephony_contact",
                resource_id=contact_id,
                payload={"external_id": str(contact["external_id"]), "erased_calls": len(calls)},
            )
        return {"id": contact_id, "deleted": True, "erased_calls": len(calls)}

    def _campaign(self, row: Any, *, redact_metadata: bool = False) -> dict[str, Any]:
        record = _row(row) or {}
        record["metadata"] = self._reveal_json(record.pop("metadata_json", "{}"))
        record["source_number"] = self._reveal_phone(record.get("source_number"))
        if redact_metadata:
            record["metadata"] = {}
        return record

    @staticmethod
    def _sync_campaign_call(
        conn: Any, *, call_id: str, status: str, timestamp: str, reason: str = ""
    ) -> None:
        row = conn.execute(
            "SELECT campaign_id, project_id FROM call_jobs WHERE id = ?", (call_id,)
        ).fetchone()
        if row is None or not row["campaign_id"]:
            return
        campaign_id = str(row["campaign_id"])
        project_id = str(row["project_id"])
        conn.execute(
            """
            UPDATE telephony_campaign_contacts SET status = ?, failure_reason = ?,
                updated_at = ? WHERE call_id = ? AND campaign_id = ?
            """,
            (status, reason[:500], timestamp, call_id, campaign_id),
        )
        remaining = conn.execute(
            """
            SELECT COUNT(*) AS count FROM telephony_campaign_contacts
            WHERE campaign_id = ? AND project_id = ?
              AND status IN ('pending','queued','leased','dispatching','dialing','ringing','active','reconciling')
            """,
            (campaign_id, project_id),
        ).fetchone()
        if int(remaining["count"] or 0) == 0:
            conn.execute(
                """
                UPDATE telephony_campaigns SET status = 'completed', updated_at = ?
                WHERE id = ? AND project_id = ? AND status = 'running'
                """,
                (timestamp, campaign_id, project_id),
            )

    def create_campaign(
        self,
        *,
        project_id: str,
        user_id: str,
        name: str,
        agent_name: str,
        trunk_id: str | None,
        source_number: str = "",
        priority: int = 100,
        max_attempts: int = 3,
        max_concurrent_calls: int = 10,
        scheduled_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.operate")
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 200:
            raise ValueError("campaign name is required and must not exceed 200 characters")
        agent = _validate_identifier(agent_name, "agent_name")
        source = _validate_phone(source_number, optional=True)
        if not 0 <= priority <= 1000 or not 1 <= max_attempts <= 10:
            raise ValueError("invalid campaign priority or retry limit")
        if not 1 <= max_concurrent_calls <= 10000:
            raise ValueError("max_concurrent_calls must be between 1 and 10000")
        serialized = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 32768:
            raise ValueError("campaign metadata exceeds 32 KiB")
        now = _utc_now()
        schedule = _timestamp(scheduled_at) if scheduled_at else now
        campaign_id = str(uuid.uuid4())
        with self.store.transaction() as conn:
            if trunk_id:
                trunk = conn.execute(
                    """
                    SELECT * FROM sip_trunks WHERE id = ? AND project_id = ?
                      AND direction IN ('outbound', 'bidirectional') AND status = 'active'
                    """,
                    (trunk_id, project_id),
                ).fetchone()
                if trunk is None:
                    raise ValueError("active outbound trunk not found")
                allowed_numbers = json.loads(str(trunk["numbers_json"] or "[]"))
                if source and "*" not in allowed_numbers and source not in allowed_numbers:
                    raise ValueError("source_number is not allowlisted on the outbound trunk")
            conn.execute(
                """
                INSERT INTO telephony_campaigns (
                    id, project_id, name, agent_name, trunk_id, source_number,
                    status, priority, max_attempts, max_concurrent_calls,
                    scheduled_at, metadata_json, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id, project_id, normalized_name, agent, trunk_id,
                    self._protect_phone(source),
                    priority, max_attempts, max_concurrent_calls, schedule,
                    self._protect_json(serialized),
                    user_id, now, now,
                ),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action="telephony.campaign.create",
                resource_type="telephony_campaign",
                resource_id=campaign_id,
                payload={"name": normalized_name},
            )
            row = conn.execute(
                "SELECT * FROM telephony_campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
        return self._campaign(row)

    def add_campaign_contacts(
        self,
        *,
        project_id: str,
        user_id: str,
        campaign_id: str,
        contact_ids: Iterable[str],
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.operate")
        unique_ids = list(dict.fromkeys(str(item).strip() for item in contact_ids if str(item).strip()))
        if not unique_ids or len(unique_ids) > 5000:
            raise ValueError("contact_ids must contain between 1 and 5000 contacts")
        now = _utc_now()
        added = 0
        with self.store.transaction() as conn:
            campaign = conn.execute(
                "SELECT status FROM telephony_campaigns WHERE id = ? AND project_id = ?",
                (campaign_id, project_id),
            ).fetchone()
            if campaign is None:
                raise ResourceNotFoundError("telephony campaign not found")
            if str(campaign["status"]) not in {"draft", "paused"}:
                raise ValueError("contacts can only be added to a draft or paused campaign")
            for contact_id in unique_ids:
                contact = conn.execute(
                    """
                    SELECT id FROM telephony_contacts
                    WHERE id = ? AND project_id = ? AND status = 'active'
                    """,
                    (contact_id, project_id),
                ).fetchone()
                if contact is None:
                    raise ResourceNotFoundError("active telephony contact not found")
                cursor = conn.execute(
                    """
                    INSERT INTO telephony_campaign_contacts (
                        campaign_id, contact_id, project_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT(campaign_id, contact_id) DO NOTHING
                    """,
                    (campaign_id, contact_id, project_id, now, now),
                )
                added += max(0, int(cursor.rowcount or 0))
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action="telephony.campaign.contacts.add",
                resource_type="telephony_campaign",
                resource_id=campaign_id,
                payload={"requested": len(unique_ids), "added": added},
            )
        return {"campaign_id": campaign_id, "requested": len(unique_ids), "added": added}

    def list_campaigns(
        self, *, project_id: str, user_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        role = self.store.require_permission(project_id, user_id, "telephony.read")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT cp.*,
                       COUNT(cc.contact_id) AS contact_count,
                       SUM(CASE WHEN cc.status = 'queued' THEN 1 ELSE 0 END) AS queued_count,
                       SUM(CASE WHEN cc.status = 'blocked' THEN 1 ELSE 0 END) AS blocked_count,
                       SUM(CASE WHEN cc.status IN ('completed','failed','busy','no_answer','canceled') THEN 1 ELSE 0 END) AS terminal_count
                FROM telephony_campaigns cp
                LEFT JOIN telephony_campaign_contacts cc ON cc.campaign_id = cp.id
                WHERE cp.project_id = ?
                GROUP BY cp.id
                ORDER BY cp.created_at DESC, cp.id DESC LIMIT ?
                """,
                (project_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._campaign(row, redact_metadata=role == "viewer") for row in rows]

    def set_campaign_status(
        self,
        *,
        project_id: str,
        user_id: str,
        campaign_id: str,
        status: str,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.operate")
        if status not in {"running", "paused", "canceled"}:
            raise ValueError("campaign status must be running, paused, or canceled")
        now = _utc_now()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM telephony_campaigns WHERE id = ? AND project_id = ?",
                (campaign_id, project_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("telephony campaign not found")
            current = str(row["status"])
            allowed = {
                "draft": {"running", "paused", "canceled"},
                "running": {"paused", "canceled"},
                "paused": {"running", "canceled"},
            }
            if status != current and status not in allowed.get(current, set()):
                raise InvalidCallTransitionError(
                    f"cannot transition campaign from {current} to {status}"
                )
            if status == "running":
                pending = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM telephony_campaign_contacts
                    WHERE project_id = ? AND campaign_id = ?
                      AND status IN ('pending','materializing')
                    """,
                    (project_id, campaign_id),
                ).fetchone()
                resumable = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM call_jobs
                    WHERE project_id = ? AND campaign_id = ?
                      AND status IN ('queued','leased','dispatching','dialing','ringing','active','reconciling')
                    """,
                    (project_id, campaign_id),
                ).fetchone()
                if int(pending["count"] or 0) + int(resumable["count"] or 0) == 0:
                    raise ValueError("campaign has no pending or active contacts")
            conn.execute(
                "UPDATE telephony_campaigns SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, campaign_id),
            )
            if status == "canceled":
                conn.execute(
                    """
                    UPDATE call_jobs SET status = 'canceled', ended_at = ?, updated_at = ?,
                        failure_code = 'campaign_canceled'
                    WHERE project_id = ? AND campaign_id = ? AND status = 'queued'
                    """,
                    (now, now, project_id, campaign_id),
                )
                conn.execute(
                    """
                    UPDATE telephony_campaign_contacts SET status = 'canceled',
                        failure_reason = 'campaign_canceled', updated_at = ?
                    WHERE project_id = ? AND campaign_id = ?
                      AND status IN ('pending','materializing','queued')
                    """,
                    (now, project_id, campaign_id),
                )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action=f"telephony.campaign.{status}",
                resource_type="telephony_campaign",
                resource_id=campaign_id,
                payload={"previous_status": current},
            )
            campaign = self._campaign(
                conn.execute(
                    "SELECT * FROM telephony_campaigns WHERE id = ?", (campaign_id,)
                ).fetchone()
            )

        enqueue_result = {"queued": 0, "blocked": 0}
        if status == "running":
            enqueue_result = self.materialize_campaigns(
                project_id=project_id,
                user_id=user_id,
                campaign_id=campaign_id,
                limit=100,
            )
            with self.store.connect() as conn:
                campaign = self._campaign(
                    conn.execute(
                        "SELECT * FROM telephony_campaigns WHERE id = ? AND project_id = ?",
                        (campaign_id, project_id),
                    ).fetchone()
                )
        campaign["enqueue_result"] = {
            "queued": enqueue_result["queued"],
            "blocked": enqueue_result["blocked"],
        }
        return campaign

    def materialize_campaigns(
        self,
        *,
        project_id: str,
        user_id: str,
        limit: int = 100,
        campaign_id: str | None = None,
    ) -> dict[str, int]:
        """Turn a bounded number of campaign contacts into durable call jobs."""
        self.store.require_any_permission(
            project_id, user_id, {"telephony.operate", "telephony.work"}
        )
        safe_limit = max(1, min(int(limit), 500))
        parameters: list[Any] = [project_id]
        campaign_filter = ""
        if campaign_id:
            campaign_filter = " AND cp.id = ?"
            parameters.append(campaign_id)
        parameters.append(safe_limit)
        stale_before = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat().replace("+00:00", "Z")
        lock_suffix = " FOR UPDATE OF cc SKIP LOCKED" if self.store.backend == "postgresql" else ""
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE telephony_campaign_contacts SET status = 'pending', updated_at = ?
                WHERE project_id = ? AND status = 'materializing' AND updated_at <= ?
                """,
                (_utc_now(), project_id, stale_before),
            )
            contacts = conn.execute(
                f"""
                SELECT cc.contact_id, c.phone_number, c.status AS contact_status,
                       cp.id AS campaign_id, cp.source_number, cp.agent_name,
                       cp.trunk_id, cp.priority, cp.max_attempts, cp.scheduled_at
                FROM telephony_campaign_contacts cc
                JOIN telephony_contacts c ON c.id = cc.contact_id
                JOIN telephony_campaigns cp ON cp.id = cc.campaign_id
                WHERE cc.project_id = ? AND cc.status = 'pending'
                  AND cp.status = 'running'{campaign_filter}
                ORDER BY cp.scheduled_at, cp.created_at, cc.created_at, cc.contact_id
                LIMIT ?{lock_suffix}
                """,
                tuple(parameters),
            ).fetchall()
            claimed_at = _utc_now()
            for contact in contacts:
                conn.execute(
                    """
                    UPDATE telephony_campaign_contacts
                    SET status = 'materializing', updated_at = ?
                    WHERE campaign_id = ? AND contact_id = ? AND project_id = ?
                      AND status = 'pending'
                    """,
                    (
                        claimed_at,
                        contact["campaign_id"],
                        contact["contact_id"],
                        project_id,
                    ),
                )

        result = {"scanned": len(contacts), "queued": 0, "blocked": 0, "pending": 0}
        touched_campaigns: set[str] = set()
        for contact in contacts:
            current_campaign_id = str(contact["campaign_id"])
            contact_id = str(contact["contact_id"])
            touched_campaigns.add(current_campaign_id)
            if str(contact["contact_status"]) != "active":
                reason = "contact_suppressed"
                call = None
            else:
                try:
                    scheduled_at = datetime.fromisoformat(
                        str(contact["scheduled_at"]).replace("Z", "+00:00")
                    )
                    call = self.enqueue_outbound(
                        project_id=project_id,
                        user_id=user_id,
                        idempotency_key=f"campaign:{current_campaign_id}:{contact_id}",
                        destination_number=self._reveal_phone(contact["phone_number"]),
                        source_number=self._reveal_phone(contact["source_number"]),
                        agent_name=str(contact["agent_name"]),
                        trunk_id=contact["trunk_id"],
                        campaign_id=current_campaign_id,
                        priority=int(contact["priority"]),
                        max_attempts=int(contact["max_attempts"]),
                        available_at=scheduled_at,
                        metadata={
                            "campaign_id": current_campaign_id,
                            "contact_id": contact_id,
                        },
                    )
                    reason = ""
                except (ComplianceBlockedError, ValueError, ResourceNotFoundError) as exc:
                    # A concurrent pause is not a compliance failure; retain the item
                    # so a later resume can materialize it.
                    if isinstance(exc, ValueError) and str(exc) == "running telephony campaign not found":
                        with self.store.transaction() as conn:
                            conn.execute(
                                """
                                UPDATE telephony_campaign_contacts
                                SET status = 'pending', updated_at = ?
                                WHERE campaign_id = ? AND contact_id = ? AND project_id = ?
                                  AND status = 'materializing'
                                """,
                                (_utc_now(), current_campaign_id, contact_id, project_id),
                            )
                        result["pending"] += 1
                        continue
                    reason = (
                        exc.reason
                        if isinstance(exc, ComplianceBlockedError)
                        else type(exc).__name__ + ": " + str(exc)
                    )
                    call = None
            with self.store.transaction() as conn:
                if call is not None:
                    updated = conn.execute(
                        """
                        UPDATE telephony_campaign_contacts SET status = 'queued',
                            call_id = ?, failure_reason = '', updated_at = ?
                        WHERE campaign_id = ? AND contact_id = ? AND project_id = ?
                          AND status = 'materializing'
                        """,
                        (
                            call["id"],
                            _utc_now(),
                            current_campaign_id,
                            contact_id,
                            project_id,
                        ),
                    )
                    result["queued"] += max(0, int(updated.rowcount or 0))
                else:
                    updated = conn.execute(
                        """
                        UPDATE telephony_campaign_contacts SET status = 'blocked',
                            failure_reason = ?, updated_at = ?
                        WHERE campaign_id = ? AND contact_id = ? AND project_id = ?
                          AND status = 'materializing'
                        """,
                        (
                            reason[:500],
                            _utc_now(),
                            current_campaign_id,
                            contact_id,
                            project_id,
                        ),
                    )
                    result["blocked"] += max(0, int(updated.rowcount or 0))

        if campaign_id:
            touched_campaigns.add(campaign_id)
        with self.store.transaction() as conn:
            for current_campaign_id in touched_campaigns:
                remaining = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM telephony_campaign_contacts
                    WHERE project_id = ? AND campaign_id = ?
                      AND status IN ('pending','materializing','queued')
                    """,
                    (project_id, current_campaign_id),
                ).fetchone()
                if int(remaining["count"] or 0) == 0:
                    conn.execute(
                        """
                        UPDATE telephony_campaigns SET status = 'completed', updated_at = ?
                        WHERE id = ? AND project_id = ? AND status = 'running'
                        """,
                        (_utc_now(), current_campaign_id, project_id),
                    )
            pending = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM telephony_campaign_contacts cc
                JOIN telephony_campaigns cp ON cp.id = cc.campaign_id
                WHERE cc.project_id = ? AND cc.status IN ('pending','materializing')
                  AND cp.status = 'running'{campaign_filter}
                """,
                tuple(parameters[:-1]),
            ).fetchone()
        result["pending"] = int(pending["count"] or 0)
        return result

    def upsert_trunk(
        self,
        *,
        project_id: str,
        user_id: str,
        name: str,
        direction: str,
        provider: str,
        livekit_trunk_id: str = "",
        secret_name: str = "",
        numbers: Iterable[str] = (),
        status: str = "active",
        max_concurrent_calls: int = 100,
        max_calls_per_second: int = 5,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.manage")
        if direction not in {"inbound", "outbound", "bidirectional"}:
            raise ValueError("invalid trunk direction")
        if status not in {"active", "disabled", "degraded"}:
            raise ValueError("invalid trunk status")
        normalized_name = name.strip()
        normalized_provider = provider.strip().lower()
        if not normalized_name or len(normalized_name) > 120:
            raise ValueError("trunk name is required")
        if not re.fullmatch(r"[a-z0-9_.-]{1,80}", normalized_provider):
            raise ValueError("invalid trunk provider")
        normalized_numbers = [
            item if item == "*" else _validate_phone(item) for item in numbers
        ]
        if not 1 <= max_concurrent_calls <= 10000:
            raise ValueError("max_concurrent_calls must be between 1 and 10000")
        if not 1 <= max_calls_per_second <= 1000:
            raise ValueError("max_calls_per_second must be between 1 and 1000")
        now = _utc_now()
        trunk_id = str(uuid.uuid4())
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            existing = conn.execute(
                "SELECT id FROM sip_trunks WHERE project_id = ? AND name = ?",
                (project_id, normalized_name),
            ).fetchone()
            if existing is not None:
                trunk_id = str(existing["id"])
            conn.execute(
                """
                INSERT INTO sip_trunks (
                    id, project_id, name, direction, provider, livekit_trunk_id,
                    secret_name, status, numbers_json, created_at, updated_at
                    , max_concurrent_calls, max_calls_per_second
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, name) DO UPDATE SET
                    direction = excluded.direction,
                    provider = excluded.provider,
                    livekit_trunk_id = excluded.livekit_trunk_id,
                    secret_name = excluded.secret_name,
                    status = excluded.status,
                    numbers_json = excluded.numbers_json,
                    max_concurrent_calls = excluded.max_concurrent_calls,
                    max_calls_per_second = excluded.max_calls_per_second,
                    updated_at = excluded.updated_at
                """,
                (
                    trunk_id,
                    project_id,
                    normalized_name,
                    direction,
                    normalized_provider,
                    livekit_trunk_id.strip(),
                    secret_name.strip(),
                    status,
                    json.dumps(normalized_numbers, separators=(",", ":")),
                    now,
                    now,
                    max_concurrent_calls,
                    max_calls_per_second,
                ),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action="telephony.trunk.upsert",
                resource_type="sip_trunk",
                resource_id=trunk_id,
                payload={"name": normalized_name, "direction": direction, "provider": normalized_provider},
            )
            row = conn.execute("SELECT * FROM sip_trunks WHERE id = ?", (trunk_id,)).fetchone()
        return self._trunk(row)

    def list_trunks(self, *, project_id: str, user_id: str) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "telephony.read")
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sip_trunks WHERE project_id = ? ORDER BY name, id",
                (project_id,),
            ).fetchall()
        return [self._trunk(row) for row in rows]

    @staticmethod
    def _validate_transfer_uri(value: str) -> str:
        uri = value.strip()
        if uri.startswith("tel:"):
            _validate_phone(uri[4:])
            return uri
        if _SIP_URI_RE.fullmatch(uri):
            port = uri.rsplit(":", 1)[-1]
            if port.isdigit() and not 1 <= int(port) <= 65535:
                raise ValueError("invalid SIP URI port")
            return uri
        raise ValueError("transfer target must be a tel:+E164 or sip:user@host URI")

    def upsert_transfer_destination(
        self,
        *,
        project_id: str,
        user_id: str,
        name: str,
        target_uri: str,
        mode: str = "cold",
        status: str = "active",
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.manage")
        normalized_name = _validate_identifier(name, "transfer destination name")
        target = self._validate_transfer_uri(target_uri)
        if mode != "cold":
            raise ValueError("only the production-verified cold transfer mode is enabled")
        if status not in {"active", "disabled"}:
            raise ValueError("invalid transfer destination status")
        now = _utc_now()
        destination_id = str(uuid.uuid4())
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            existing = conn.execute(
                "SELECT id FROM transfer_destinations WHERE project_id = ? AND name = ?",
                (project_id, normalized_name),
            ).fetchone()
            if existing is not None:
                destination_id = str(existing["id"])
            conn.execute(
                """
                INSERT INTO transfer_destinations (
                    id, project_id, name, target_uri, mode, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, name) DO UPDATE SET
                    target_uri = excluded.target_uri, mode = excluded.mode,
                    status = excluded.status, updated_at = excluded.updated_at
                """,
                (
                    destination_id,
                    project_id,
                    normalized_name,
                    target,
                    mode,
                    status,
                    now,
                    now,
                ),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=user_id,
                action="telephony.transfer_destination.upsert",
                resource_type="transfer_destination",
                resource_id=destination_id,
                payload={"name": normalized_name, "mode": mode, "status": status},
            )
            row = conn.execute(
                "SELECT * FROM transfer_destinations WHERE id = ?", (destination_id,)
            ).fetchone()
        return _row(row) or {}

    def list_transfer_destinations(
        self, *, project_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        role = self.store.require_permission(project_id, user_id, "telephony.read")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM transfer_destinations WHERE project_id = ?
                ORDER BY name, id
                """,
                (project_id,),
            ).fetchall()
        records = [_row(row) or {} for row in rows]
        if role == "viewer":
            for record in records:
                record["target_uri"] = "redacted"
        return records

    @staticmethod
    def _transfer(row: Any) -> dict[str, Any]:
        return _row(row) or {}

    def request_transfer(
        self,
        *,
        project_id: str,
        user_id: str,
        call_id: str,
        worker_id: str,
        lease_token: str,
        destination_name: str,
        idempotency_key: str,
        context_summary: str = "",
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.work")
        destination = _validate_identifier(destination_name, "transfer destination name")
        key = _validate_identifier(idempotency_key, "transfer idempotency_key")
        summary = context_summary.strip()
        if len(summary.encode("utf-8")) > 8192:
            raise ValueError("transfer context_summary exceeds 8 KiB")
        now = _utc_now()
        transfer_id = str(uuid.uuid4())
        with self.store.transaction() as conn:
            call = self._owned_call(conn, project_id, call_id, worker_id, lease_token)
            if str(call["status"]) != "active":
                raise InvalidCallTransitionError("only an active call can be transferred")
            target = conn.execute(
                """
                SELECT * FROM transfer_destinations
                WHERE project_id = ? AND name = ? AND status = 'active'
                """,
                (project_id, destination),
            ).fetchone()
            if target is None:
                raise ResourceNotFoundError("active transfer destination not found")
            conn.execute(
                """
                INSERT INTO call_transfers (
                    id, project_id, call_id, destination_id, idempotency_key,
                    mode, status, context_summary, requested_by, worker_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?)
                ON CONFLICT(call_id, idempotency_key) DO NOTHING
                """,
                (
                    transfer_id,
                    project_id,
                    call_id,
                    target["id"],
                    key,
                    target["mode"],
                    summary,
                    user_id,
                    worker_id,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT x.*, d.name AS destination_name, d.target_uri
                FROM call_transfers x
                JOIN transfer_destinations d ON d.id = x.destination_id
                WHERE x.call_id = ? AND x.idempotency_key = ?
                """,
                (call_id, key),
            ).fetchone()
            if str(row["destination_id"]) != str(target["id"]):
                raise IdempotencyConflictError(
                    "transfer idempotency key was already used for another destination"
                )
            if str(row["id"]) == transfer_id:
                self._event(
                    conn,
                    project_id,
                    call_id,
                    "call.transfer.requested",
                    {"transfer_id": transfer_id, "destination_name": destination},
                )
                self.store._append_audit(
                    conn,
                    project_id=project_id,
                    actor_id=user_id,
                    action="telephony.transfer.request",
                    resource_type="call_transfer",
                    resource_id=transfer_id,
                    payload={"call_id": call_id, "destination_name": destination},
                )
        return self._transfer(row)

    def transition_transfer(
        self,
        *,
        project_id: str,
        user_id: str,
        call_id: str,
        transfer_id: str,
        worker_id: str,
        lease_token: str,
        status: str,
        failure_code: str = "",
        failure_detail: str = "",
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.work")
        if status not in {"transferring", "completed", "failed", "canceled"}:
            raise ValueError("invalid transfer status")
        now = _utc_now()
        with self.store.transaction() as conn:
            self._owned_call(conn, project_id, call_id, worker_id, lease_token)
            lock_suffix = " FOR UPDATE" if self.store.backend == "postgresql" else ""
            row = conn.execute(
                f"""
                SELECT * FROM call_transfers
                WHERE id = ? AND call_id = ? AND project_id = ?{lock_suffix}
                """,
                (transfer_id, call_id, project_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("call transfer not found")
            current = str(row["status"])
            allowed = {
                "requested": {"transferring", "failed", "canceled"},
                "transferring": {"completed", "failed"},
            }
            if status not in allowed.get(current, set()):
                raise InvalidCallTransitionError(
                    f"cannot transition transfer from {current} to {status}"
                )
            completed_at = now if status in {"completed", "failed", "canceled"} else None
            conn.execute(
                """
                UPDATE call_transfers SET status = ?, failure_code = ?,
                    failure_detail = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    failure_code.strip()[:120],
                    failure_detail.strip()[:2000],
                    now,
                    completed_at,
                    transfer_id,
                ),
            )
            if status == "completed":
                conn.execute(
                    """
                    UPDATE call_jobs SET status = 'completed', ended_at = ?, updated_at = ?,
                        lease_owner = '', lease_token = '', lease_expires_at = NULL
                    WHERE id = ? AND project_id = ?
                    """,
                    (now, now, call_id, project_id),
                )
                conn.execute(
                    """
                    UPDATE call_attempts SET status = 'completed', ended_at = ?
                    WHERE call_id = ? AND ended_at IS NULL
                    """,
                    (now, call_id),
                )
                self._sync_campaign_call(
                    conn,
                    call_id=call_id,
                    status="completed",
                    timestamp=now,
                )
            self._event(
                conn,
                project_id,
                call_id,
                f"call.transfer.{status}",
                {"transfer_id": transfer_id, "failure_code": failure_code.strip()},
            )
            updated = conn.execute(
                """
                SELECT x.*, d.name AS destination_name, d.target_uri
                FROM call_transfers x JOIN transfer_destinations d ON d.id = x.destination_id
                WHERE x.id = ?
                """,
                (transfer_id,),
            ).fetchone()
        return self._transfer(updated)

    def list_transfers(
        self, *, project_id: str, user_id: str, call_id: str
    ) -> list[dict[str, Any]]:
        role = self.store.require_permission(project_id, user_id, "telephony.read")
        with self.store.connect() as conn:
            call = conn.execute(
                "SELECT id FROM call_jobs WHERE id = ? AND project_id = ?",
                (call_id, project_id),
            ).fetchone()
            if call is None:
                raise ResourceNotFoundError("call not found")
            rows = conn.execute(
                """
                SELECT x.*, d.name AS destination_name
                FROM call_transfers x JOIN transfer_destinations d ON d.id = x.destination_id
                WHERE x.call_id = ? AND x.project_id = ?
                ORDER BY x.created_at DESC, x.id DESC
                """,
                (call_id, project_id),
            ).fetchall()
        records = [self._transfer(row) for row in rows]
        if role == "viewer":
            for record in records:
                record["context_summary"] = ""
                record["failure_detail"] = ""
        return records

    @staticmethod
    def _trunk(row: Any) -> dict[str, Any]:
        record = _row(row) or {}
        record["numbers"] = json.loads(record.pop("numbers_json", "[]"))
        return record

    @staticmethod
    def _next_calling_window(
        policy: dict[str, Any], candidate: datetime
    ) -> tuple[datetime, str]:
        zone = ZoneInfo(str(policy["timezone"]))
        local = _now(candidate).astimezone(zone)
        start = clock_time.fromisoformat(str(policy["calling_window_start"]))
        end = clock_time.fromisoformat(str(policy["calling_window_end"]))
        weekdays = {int(day) for day in policy["allowed_weekdays"]}
        for offset in range(8):
            day = local.date() + timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            window_start = datetime.combine(day, start, tzinfo=zone)
            window_end = datetime.combine(day, end, tzinfo=zone)
            if offset == 0 and window_start <= local < window_end:
                return _now(candidate), "allowed"
            if offset > 0 or local < window_start:
                return window_start.astimezone(timezone.utc), "calling_window"
        raise RuntimeError("unable to resolve the next permitted calling window")

    def _compliance_check(
        self,
        conn: Any,
        *,
        project_id: str,
        phone_hash: str,
        current: datetime,
        requested_at: datetime,
    ) -> tuple[dict[str, Any], datetime, str]:
        timestamp = _timestamp(current)
        policy = self._ensure_policy(conn, project_id, timestamp)
        dnc = conn.execute(
            """
            SELECT id FROM do_not_call_entries
            WHERE project_id = ? AND phone_hash = ?
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (project_id, phone_hash, timestamp),
        ).fetchone()
        if dnc is not None:
            raise ComplianceBlockedError("do_not_call")

        if policy["require_consent"]:
            consent = conn.execute(
                """
                SELECT status, valid_from, valid_until FROM consent_records
                WHERE project_id = ? AND phone_hash = ? AND purpose = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (project_id, phone_hash, policy["consent_purpose"]),
            ).fetchone()
            if (
                consent is None
                or str(consent["status"]) != "granted"
                or str(consent["valid_from"]) > timestamp
                or (consent["valid_until"] is not None and str(consent["valid_until"]) <= timestamp)
            ):
                raise ComplianceBlockedError("consent_missing_or_inactive")

        zone = ZoneInfo(str(policy["timezone"]))
        local = current.astimezone(zone)
        local_start = datetime.combine(local.date(), clock_time.min, tzinfo=zone)
        local_end = local_start + timedelta(days=1)
        attempts = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM call_attempts a
            JOIN call_jobs c ON c.id = a.call_id AND c.project_id = a.project_id
            WHERE a.project_id = ? AND c.destination_hash = ?
              AND a.started_at >= ? AND a.started_at < ?
            """,
            (
                project_id,
                phone_hash,
                _timestamp(local_start.astimezone(timezone.utc)),
                _timestamp(local_end.astimezone(timezone.utc)),
            ),
        ).fetchone()
        if int(attempts["count"] or 0) >= int(policy["max_attempts_per_number_per_day"]):
            raise ComplianceBlockedError("daily_number_attempt_limit")

        candidate = max(_now(requested_at), current)
        ready_at, reason = self._next_calling_window(policy, candidate)
        return policy, ready_at, reason

    @staticmethod
    def _record_compliance_decision(
        conn: Any,
        *,
        project_id: str,
        call_id: str | None,
        phone_hash: str,
        decision: str,
        reason: str,
        policy: dict[str, Any],
    ) -> None:
        snapshot = {
            "timezone": policy["timezone"],
            "allowed_weekdays": policy["allowed_weekdays"],
            "calling_window_start": policy["calling_window_start"],
            "calling_window_end": policy["calling_window_end"],
            "require_consent": policy["require_consent"],
            "consent_purpose": policy["consent_purpose"],
            "max_attempts_per_number_per_day": policy[
                "max_attempts_per_number_per_day"
            ],
        }
        conn.execute(
            """
            INSERT INTO compliance_decisions (
                id, project_id, call_id, phone_hash, decision, reason,
                policy_snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                project_id,
                call_id,
                phone_hash,
                decision,
                reason,
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                _utc_now(),
            ),
        )

    def enqueue_outbound(
        self,
        *,
        project_id: str,
        user_id: str,
        idempotency_key: str,
        destination_number: str,
        agent_name: str,
        source_number: str = "",
        trunk_id: str | None = None,
        campaign_id: str | None = None,
        priority: int = 100,
        max_attempts: int = 3,
        metadata: dict[str, Any] | None = None,
        available_at: datetime | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.operate")
        key = _validate_identifier(idempotency_key, "idempotency_key")
        destination = _validate_phone(destination_number)
        source = _validate_phone(source_number, optional=True)
        agent = _validate_identifier(agent_name, "agent_name")
        if not 0 <= priority <= 1000:
            raise ValueError("priority must be between 0 and 1000")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        serialized_metadata = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
        if len(serialized_metadata.encode("utf-8")) > 32768:
            raise ValueError("call metadata exceeds 32 KiB")
        current = _now()
        now = _timestamp(current)
        call_id = str(uuid.uuid4())
        requested_at = _now(available_at) if available_at else current
        phone_hash = self._phone_hash(destination)
        blocked: ComplianceBlockedError | None = None
        record: dict[str, Any] = {}
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            existing = conn.execute(
                "SELECT * FROM call_jobs WHERE project_id = ? AND idempotency_key = ?",
                (project_id, key),
            ).fetchone()
            if existing is not None:
                record = self._call(existing, include_lease=True)
                if (
                    record.get("direction") != "outbound"
                    or record.get("destination_number") != destination
                    or record.get("agent_name") != agent
                ):
                    raise IdempotencyConflictError(
                        "idempotency key was already used for another call"
                    )
            else:
                if campaign_id:
                    campaign = conn.execute(
                        """
                        SELECT id FROM telephony_campaigns
                        WHERE id = ? AND project_id = ? AND status = 'running'
                        """,
                        (campaign_id, project_id),
                    ).fetchone()
                    if campaign is None:
                        raise ValueError("running telephony campaign not found")
                if trunk_id:
                    trunk = conn.execute(
                        """
                        SELECT * FROM sip_trunks
                        WHERE id = ? AND project_id = ?
                          AND direction IN ('outbound', 'bidirectional') AND status = 'active'
                        """,
                        (trunk_id, project_id),
                    ).fetchone()
                    if trunk is None:
                        raise ValueError("active outbound trunk not found")
                    allowed_numbers = json.loads(str(trunk["numbers_json"] or "[]"))
                    if source and "*" not in allowed_numbers and source not in allowed_numbers:
                        raise ValueError("source_number is not allowlisted on the outbound trunk")
                policy = self._ensure_policy(conn, project_id, now)
                if not policy["outbound_enabled"]:
                    raise ComplianceBlockedError("outbound_paused")
                try:
                    policy, ready, compliance_reason = self._compliance_check(
                        conn,
                        project_id=project_id,
                        phone_hash=phone_hash,
                        current=current,
                        requested_at=requested_at,
                    )
                except ComplianceBlockedError as exc:
                    blocked = exc
                    self._record_compliance_decision(
                        conn,
                        project_id=project_id,
                        call_id=None,
                        phone_hash=phone_hash,
                        decision="blocked",
                        reason=exc.reason,
                        policy=policy,
                    )
                if blocked is None:
                    ready_at = _timestamp(ready)
                    conn.execute(
                        """
                        INSERT INTO call_jobs (
                            id, project_id, direction, idempotency_key, source_number,
                            destination_number, destination_hash, agent_name, trunk_id, campaign_id,
                            status, priority, max_attempts, available_at, metadata_json,
                            recording_mode, recording_disclosure_text, created_at, updated_at
                        ) VALUES (?, ?, 'outbound', ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            call_id,
                            project_id,
                            key,
                            self._protect_phone(source),
                            self._protect_phone(destination),
                            phone_hash,
                            agent,
                            trunk_id,
                            campaign_id,
                            priority,
                            max_attempts,
                            ready_at,
                            self._protect_json(serialized_metadata),
                            str(policy["recording_mode"]),
                            str(policy["recording_disclosure_text"]),
                            now,
                            now,
                        ),
                    )
                    record = _row(
                        conn.execute(
                            "SELECT * FROM call_jobs WHERE id = ?", (call_id,)
                        ).fetchone()
                    ) or {}
                    self._record_compliance_decision(
                        conn,
                        project_id=project_id,
                        call_id=call_id,
                        phone_hash=phone_hash,
                        decision="approved" if compliance_reason == "allowed" else "scheduled",
                        reason=compliance_reason,
                        policy=policy,
                    )
                    self._event(
                        conn, project_id, call_id, "call.queued", {"actor_id": user_id}
                    )
                    self.store._append_audit(
                        conn,
                        project_id=project_id,
                        actor_id=user_id,
                        action="telephony.call.enqueue",
                        resource_type="call",
                        resource_id=call_id,
                        payload={
                            "direction": "outbound",
                            "destination_last4": destination[-4:],
                        },
                    )
        if blocked is not None:
            raise blocked
        return self.get_call(project_id=project_id, user_id=user_id, call_id=str(record["id"]))

    def admit_inbound(
        self,
        *,
        project_id: str,
        user_id: str,
        provider: str,
        provider_call_id: str,
        worker_id: str,
        source_number: str,
        destination_number: str,
        agent_name: str,
        room_name: str,
        trunk_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.work")
        provider_name = _validate_identifier(provider.lower(), "provider")
        external_id = _validate_identifier(provider_call_id, "provider_call_id")
        worker = _validate_identifier(worker_id, "worker_id")
        source = _validate_phone(source_number, optional=True)
        destination = _validate_phone(destination_number)
        agent = _validate_identifier(agent_name, "agent_name")
        room = _validate_identifier(room_name, "room_name")
        key = f"inbound:{provider_name}:{external_id}"
        current = _now(now)
        timestamp = _timestamp(current)
        metadata_payload = dict(metadata or {})
        serialized_metadata = json.dumps(metadata_payload, ensure_ascii=False, separators=(",", ":"))
        if len(serialized_metadata.encode("utf-8")) > 32768:
            raise ValueError("call metadata exceeds 32 KiB")
        call_id = str(uuid.uuid4())
        overflow_destination = ""
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            existing = conn.execute(
                "SELECT * FROM call_jobs WHERE project_id = ? AND idempotency_key = ?",
                (project_id, key),
            ).fetchone()
            if existing is not None:
                record = self._call(existing, include_lease=True)
                overflow_destination = str(
                    record["metadata"].get("inbound_overflow_destination_name") or ""
                )
                if overflow_destination:
                    record["overflow"] = {
                        "mode": "transfer",
                        "destination_name": overflow_destination,
                    }
                return record
            limits = self._ensure_limits(conn, project_id, timestamp)
            policy = self._ensure_policy(conn, project_id, timestamp)
            counts = self._active_counts(conn, project_id)
            capacity_exhausted = (
                counts["total"] >= int(limits["max_concurrent_calls"])
                or counts["inbound"] >= int(limits["max_inbound_calls"])
            )
            if capacity_exhausted:
                if policy["inbound_overflow_mode"] != "transfer":
                    raise CapacityExceededError(
                        "project inbound or total call capacity is exhausted"
                    )
                overflow_destination = str(
                    policy["inbound_overflow_destination_name"] or ""
                )
                target = conn.execute(
                    """
                    SELECT id FROM transfer_destinations
                    WHERE project_id = ? AND name = ? AND status = 'active'
                    """,
                    (project_id, overflow_destination),
                ).fetchone()
                if target is None:
                    raise CapacityExceededError(
                        "inbound overflow destination is unavailable"
                    )
                metadata_payload["inbound_overflow_destination_name"] = overflow_destination
                serialized_metadata = json.dumps(
                    metadata_payload, ensure_ascii=False, separators=(",", ":")
                )
            lease_token = uuid.uuid4().hex + uuid.uuid4().hex
            lease_expires = _timestamp(
                current + timedelta(seconds=int(limits["lease_seconds"]))
            )
            conn.execute(
                """
                INSERT INTO call_jobs (
                    id, project_id, direction, idempotency_key, source_number,
                    destination_number, agent_name, trunk_id, status, priority,
                    attempt_count, max_attempts, available_at, provider_call_id,
                    room_name, metadata_json, lease_owner, lease_token,
                    lease_expires_at, recording_mode, recording_disclosure_text,
                    created_at, updated_at, started_at
                ) VALUES (?, ?, 'inbound', ?, ?, ?, ?, ?, 'ringing', 0,
                          1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    project_id,
                    key,
                    self._protect_phone(source),
                    self._protect_phone(destination),
                    agent,
                    trunk_id,
                    timestamp,
                    external_id,
                    room,
                    self._protect_json(serialized_metadata),
                    worker,
                    lease_token,
                    lease_expires,
                    str(policy["recording_mode"]),
                    str(policy["recording_disclosure_text"]),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            self._event(
                conn,
                project_id,
                call_id,
                "call.inbound.overflow" if overflow_destination else "call.inbound.admitted",
                {
                    "provider": provider_name,
                    "provider_call_id": external_id,
                    "overflow_destination_name": overflow_destination,
                },
            )
            row = conn.execute("SELECT * FROM call_jobs WHERE id = ?", (call_id,)).fetchone()
        record = self._call(row, include_lease=True)
        if overflow_destination:
            record["overflow"] = {
                "mode": "transfer",
                "destination_name": overflow_destination,
            }
        return record

    def claim_outbound(
        self,
        *,
        project_id: str,
        user_id: str,
        worker_id: str,
        limit: int = 1,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "telephony.work")
        worker = _validate_identifier(worker_id, "worker_id")
        batch_limit = max(1, min(limit, 100))
        current = _now(now)
        timestamp = _timestamp(current)
        one_minute_ago = _timestamp(current - timedelta(minutes=1))
        one_second_ago = _timestamp(current - timedelta(seconds=1))
        claimed: list[dict[str, Any]] = []
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            limits = self._ensure_limits(conn, project_id, timestamp)
            policy = self._ensure_policy(conn, project_id, timestamp)
            if not policy["outbound_enabled"]:
                return []
            self._expire_leases(conn, project_id, current)
            counts = self._active_counts(conn, project_id)
            total_capacity = max(0, int(limits["max_concurrent_calls"]) - counts["total"])
            outbound_capacity = max(0, int(limits["max_outbound_calls"]) - counts["outbound"])
            rate_row = conn.execute(
                "SELECT COUNT(*) AS count FROM call_attempts WHERE project_id = ? AND started_at >= ?",
                (project_id, one_minute_ago),
            ).fetchone()
            rate_capacity = max(
                0, int(limits["max_calls_per_minute"]) - int(rate_row["count"] or 0)
            )
            capacity = min(batch_limit, total_capacity, outbound_capacity, rate_capacity)
            if capacity <= 0:
                return []
            candidate_limit = min(500, max(capacity * 5, capacity))
            rows = conn.execute(
                """
                SELECT c.*,
                       COALESCE(t.livekit_trunk_id, '') AS livekit_trunk_id,
                       COALESCE(t.max_concurrent_calls, 0) AS trunk_max_concurrent_calls,
                       COALESCE(t.max_calls_per_second, 0) AS trunk_max_calls_per_second,
                       COALESCE(t.numbers_json, '[]') AS trunk_numbers_json,
                       COALESCE(cp.max_concurrent_calls, 0) AS campaign_max_concurrent_calls
                FROM call_jobs c
                LEFT JOIN sip_trunks t ON t.id = c.trunk_id
                  AND t.project_id = c.project_id
                  AND t.direction IN ('outbound', 'bidirectional')
                  AND t.status = 'active'
                LEFT JOIN telephony_campaigns cp ON cp.id = c.campaign_id
                  AND cp.project_id = c.project_id
                WHERE c.project_id = ? AND c.direction = 'outbound' AND c.status = 'queued'
                  AND c.available_at <= ? AND c.attempt_count < c.max_attempts
                  AND (c.trunk_id IS NULL OR t.id IS NOT NULL)
                  AND (c.campaign_id IS NULL OR cp.status = 'running')
                ORDER BY c.priority ASC, c.available_at ASC, c.created_at ASC, c.id ASC
                LIMIT ?
                """,
                (project_id, timestamp, candidate_limit),
            ).fetchall()
            lease_expires = _timestamp(current + timedelta(seconds=int(limits["lease_seconds"])))
            for row in rows:
                if len(claimed) >= capacity:
                    break
                call_id = str(row["id"])
                row_trunk_id = str(row["trunk_id"] or "")
                if row_trunk_id:
                    source_number = self._reveal_phone(row["source_number"])
                    allowed_numbers = json.loads(str(row["trunk_numbers_json"] or "[]"))
                    if source_number and "*" not in allowed_numbers and source_number not in allowed_numbers:
                        conn.execute(
                            """
                            UPDATE call_jobs SET status = 'blocked',
                                failure_code = 'source_number_not_allowlisted',
                                failure_detail = 'source number is not allowlisted on the active trunk',
                                ended_at = ?, updated_at = ? WHERE id = ?
                            """,
                            (timestamp, timestamp, call_id),
                        )
                        self._event(
                            conn,
                            project_id,
                            call_id,
                            "call.blocked",
                            {"reason": "source_number_not_allowlisted"},
                        )
                        self._sync_campaign_call(
                            conn,
                            call_id=call_id,
                            status="blocked",
                            timestamp=timestamp,
                            reason="source_number_not_allowlisted",
                        )
                        continue
                    active_on_trunk = conn.execute(
                        """
                        SELECT COUNT(*) AS count FROM call_jobs
                        WHERE project_id = ? AND trunk_id = ?
                          AND status IN ('leased','dispatching','dialing','ringing','active','reconciling')
                        """,
                        (project_id, row_trunk_id),
                    ).fetchone()
                    if int(active_on_trunk["count"] or 0) >= int(
                        row["trunk_max_concurrent_calls"]
                    ):
                        continue
                    recent_on_trunk = conn.execute(
                        """
                        SELECT COUNT(*) AS count FROM call_attempts a
                        JOIN call_jobs c ON c.id = a.call_id
                        WHERE a.project_id = ? AND c.trunk_id = ? AND a.started_at >= ?
                        """,
                        (project_id, row_trunk_id, one_second_ago),
                    ).fetchone()
                    if int(recent_on_trunk["count"] or 0) >= int(
                        row["trunk_max_calls_per_second"]
                    ):
                        continue
                campaign_id = str(row["campaign_id"] or "")
                if campaign_id:
                    active_on_campaign = conn.execute(
                        """
                        SELECT COUNT(*) AS count FROM call_jobs
                        WHERE project_id = ? AND campaign_id = ?
                          AND status IN ('leased','dispatching','dialing','ringing','active','reconciling')
                        """,
                        (project_id, campaign_id),
                    ).fetchone()
                    if int(active_on_campaign["count"] or 0) >= int(
                        row["campaign_max_concurrent_calls"]
                    ):
                        continue
                phone_hash = str(row["destination_hash"] or "") or self._phone_hash(
                    self._reveal_phone(row["destination_number"])
                )
                try:
                    policy, compliant_at, compliance_reason = self._compliance_check(
                        conn,
                        project_id=project_id,
                        phone_hash=phone_hash,
                        current=current,
                        requested_at=current,
                    )
                except ComplianceBlockedError as exc:
                    conn.execute(
                        """
                        UPDATE call_jobs SET status = 'blocked', destination_hash = ?,
                            failure_code = ?, failure_detail = ?, ended_at = ?, updated_at = ?,
                            lease_owner = '', lease_token = '', lease_expires_at = NULL
                        WHERE id = ? AND project_id = ? AND status = 'queued'
                        """,
                        (
                            phone_hash,
                            f"compliance_{exc.reason}",
                            str(exc),
                            timestamp,
                            timestamp,
                            call_id,
                            project_id,
                        ),
                    )
                    self._record_compliance_decision(
                        conn,
                        project_id=project_id,
                        call_id=call_id,
                        phone_hash=phone_hash,
                        decision="blocked",
                        reason=exc.reason,
                        policy=policy,
                    )
                    self._event(
                        conn,
                        project_id,
                        call_id,
                        "call.blocked",
                        {"reason": exc.reason},
                    )
                    self._sync_campaign_call(
                        conn,
                        call_id=call_id,
                        status="blocked",
                        timestamp=timestamp,
                        reason=exc.reason,
                    )
                    continue
                if compliant_at > current:
                    conn.execute(
                        """
                        UPDATE call_jobs SET available_at = ?, destination_hash = ?, updated_at = ?
                        WHERE id = ? AND project_id = ? AND status = 'queued'
                        """,
                        (
                            _timestamp(compliant_at),
                            phone_hash,
                            timestamp,
                            call_id,
                            project_id,
                        ),
                    )
                    self._record_compliance_decision(
                        conn,
                        project_id=project_id,
                        call_id=call_id,
                        phone_hash=phone_hash,
                        decision="scheduled",
                        reason=compliance_reason,
                        policy=policy,
                    )
                    continue
                attempt_number = int(row["attempt_count"]) + 1
                token = uuid.uuid4().hex + uuid.uuid4().hex
                conn.execute(
                    """
                    UPDATE call_jobs SET
                        status = 'leased', attempt_count = ?, lease_owner = ?,
                        lease_token = ?, lease_expires_at = ?, updated_at = ?,
                        started_at = COALESCE(started_at, ?), failure_code = '',
                        failure_detail = '', destination_hash = ?
                    WHERE id = ? AND project_id = ? AND status = 'queued'
                    """,
                    (
                        attempt_number,
                        worker,
                        token,
                        lease_expires,
                        timestamp,
                        timestamp,
                        phone_hash,
                        call_id,
                        project_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO call_attempts (
                        id, project_id, call_id, attempt_number, worker_id,
                        lease_token, status, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'leased', ?)
                    """,
                    (str(uuid.uuid4()), project_id, call_id, attempt_number, worker, token, timestamp),
                )
                self._event(
                    conn,
                    project_id,
                    call_id,
                    "call.leased",
                    {"worker_id": worker, "attempt_number": attempt_number},
                )
                self._sync_campaign_call(
                    conn,
                    call_id=call_id,
                    status="leased",
                    timestamp=timestamp,
                )
                self._record_compliance_decision(
                    conn,
                    project_id=project_id,
                    call_id=call_id,
                    phone_hash=phone_hash,
                    decision="approved",
                    reason="dispatch_recheck",
                    policy=policy,
                )
                updated = conn.execute(
                    """
                    SELECT c.*, COALESCE(t.livekit_trunk_id, '') AS livekit_trunk_id
                    FROM call_jobs c
                    LEFT JOIN sip_trunks t ON t.id = c.trunk_id AND t.project_id = c.project_id
                      AND t.direction IN ('outbound', 'bidirectional') AND t.status = 'active'
                    WHERE c.id = ?
                    """,
                    (call_id,),
                ).fetchone()
                record = self._call(updated, include_lease=True)
                record["lease_seconds"] = int(limits["lease_seconds"])
                claimed.append(record)
        return claimed

    def claim_reconciliation(
        self,
        *,
        project_id: str,
        user_id: str,
        worker_id: str,
        limit: int = 10,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "telephony.work")
        worker = _validate_identifier(worker_id, "worker_id")
        batch_limit = max(1, min(limit, 100))
        current = _now(now)
        timestamp = _timestamp(current)
        claimed: list[dict[str, Any]] = []
        with self.store.transaction() as conn:
            self._lock_project(conn, project_id)
            limits = self._ensure_limits(conn, project_id, timestamp)
            self._expire_leases(conn, project_id, current)
            rows = conn.execute(
                """
                SELECT * FROM call_jobs
                WHERE project_id = ? AND status = 'reconciling'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY reconcile_started_at, updated_at, id LIMIT ?
                """,
                (project_id, timestamp, batch_limit),
            ).fetchall()
            lease_expires = _timestamp(
                current + timedelta(seconds=int(limits["lease_seconds"]))
            )
            for row in rows:
                token = uuid.uuid4().hex + uuid.uuid4().hex
                conn.execute(
                    """
                    UPDATE call_jobs SET lease_owner = ?, lease_token = ?,
                        lease_expires_at = ?, reconcile_attempt_count = reconcile_attempt_count + 1,
                        updated_at = ?
                    WHERE id = ? AND project_id = ? AND status = 'reconciling'
                      AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                    """,
                    (
                        worker,
                        token,
                        lease_expires,
                        timestamp,
                        row["id"],
                        project_id,
                        timestamp,
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM call_jobs WHERE id = ? AND lease_token = ?",
                    (row["id"], token),
                ).fetchone()
                if updated is None:
                    continue
                self._event(
                    conn,
                    project_id,
                    str(row["id"]),
                    "call.reconciliation.claimed",
                    {"worker_id": worker},
                )
                claimed.append(self._call(updated, include_lease=True))
        return claimed

    @staticmethod
    def _safe_sip_attributes(attributes: dict[str, Any] | None) -> dict[str, str]:
        allowed = {
            "sip.callID",
            "sip.callIDFull",
            "sip.callStatus",
            "sip.trunkID",
            "sip.ruleID",
            "sip.twilio.accountSid",
            "sip.twilio.callSid",
        }
        return {
            key: str(value)[:500]
            for key, value in (attributes or {}).items()
            if key in allowed and value is not None
        }

    def _upsert_cdr(
        self,
        conn: Any,
        *,
        project_id: str,
        call_id: str,
        provider: str,
        provider_call_id: str,
        sip_call_id: str,
        room_name: str,
        participant_identity: str,
        sip_status: str,
        disconnect_reason: str,
        attributes: dict[str, Any] | None,
        observed_at: str,
        ended: bool,
    ) -> dict[str, Any]:
        safe_attributes = self._safe_sip_attributes(attributes)
        conn.execute(
            """
            INSERT INTO call_cdrs (
                id, project_id, call_id, provider, provider_call_id, sip_call_id,
                room_name, participant_identity, sip_status, disconnect_reason,
                attributes_json, first_observed_at, last_observed_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(call_id) DO UPDATE SET
                provider = excluded.provider,
                provider_call_id = CASE WHEN excluded.provider_call_id <> ''
                    THEN excluded.provider_call_id ELSE call_cdrs.provider_call_id END,
                sip_call_id = CASE WHEN excluded.sip_call_id <> ''
                    THEN excluded.sip_call_id ELSE call_cdrs.sip_call_id END,
                room_name = CASE WHEN excluded.room_name <> ''
                    THEN excluded.room_name ELSE call_cdrs.room_name END,
                participant_identity = CASE WHEN excluded.participant_identity <> ''
                    THEN excluded.participant_identity ELSE call_cdrs.participant_identity END,
                sip_status = CASE WHEN excluded.sip_status <> ''
                    THEN excluded.sip_status ELSE call_cdrs.sip_status END,
                disconnect_reason = CASE WHEN excluded.disconnect_reason <> ''
                    THEN excluded.disconnect_reason ELSE call_cdrs.disconnect_reason END,
                attributes_json = excluded.attributes_json,
                last_observed_at = excluded.last_observed_at,
                ended_at = COALESCE(excluded.ended_at, call_cdrs.ended_at)
            """,
            (
                str(uuid.uuid4()),
                project_id,
                call_id,
                provider.strip()[:80] or "livekit-sip",
                provider_call_id.strip()[:200],
                sip_call_id.strip()[:200],
                room_name.strip()[:200],
                participant_identity.strip()[:200],
                sip_status.strip()[:80],
                disconnect_reason.strip()[:200],
                json.dumps(safe_attributes, ensure_ascii=False, separators=(",", ":")),
                observed_at,
                observed_at,
                observed_at if ended else None,
            ),
        )
        row = conn.execute("SELECT * FROM call_cdrs WHERE call_id = ?", (call_id,)).fetchone()
        record = _row(row) or {}
        record["attributes"] = json.loads(record.pop("attributes_json", "{}") or "{}")
        return record

    def observe_call(
        self,
        *,
        project_id: str,
        user_id: str,
        call_id: str,
        worker_id: str,
        lease_token: str,
        provider: str = "livekit-sip",
        provider_call_id: str = "",
        sip_call_id: str = "",
        room_name: str = "",
        participant_identity: str = "",
        sip_status: str = "",
        disconnect_reason: str = "",
        attributes: dict[str, Any] | None = None,
        ended: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.work")
        timestamp = _timestamp(now)
        with self.store.transaction() as conn:
            row = self._owned_call(conn, project_id, call_id, worker_id, lease_token)
            cdr = self._upsert_cdr(
                conn,
                project_id=project_id,
                call_id=call_id,
                provider=provider,
                provider_call_id=provider_call_id,
                sip_call_id=sip_call_id,
                room_name=room_name or str(row["room_name"] or ""),
                participant_identity=participant_identity,
                sip_status=sip_status,
                disconnect_reason=disconnect_reason,
                attributes=attributes,
                observed_at=timestamp,
                ended=ended,
            )
            conn.execute(
                """
                UPDATE call_jobs SET
                    provider_call_id = CASE WHEN ? <> '' THEN ? ELSE provider_call_id END,
                    room_name = CASE WHEN ? <> '' THEN ? ELSE room_name END,
                    updated_at = ? WHERE id = ? AND project_id = ?
                """,
                (
                    provider_call_id.strip(),
                    provider_call_id.strip(),
                    room_name.strip(),
                    room_name.strip(),
                    timestamp,
                    call_id,
                    project_id,
                ),
            )
            self._event(
                conn,
                project_id,
                call_id,
                "call.observed",
                {"sip_status": sip_status.strip(), "ended": ended},
            )
        return cdr

    def get_cdr(self, *, project_id: str, user_id: str, call_id: str) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.read")
        with self.store.connect() as conn:
            call = conn.execute(
                "SELECT id FROM call_jobs WHERE id = ? AND project_id = ?",
                (call_id, project_id),
            ).fetchone()
            if call is None:
                raise ResourceNotFoundError("call not found")
            row = conn.execute(
                "SELECT * FROM call_cdrs WHERE call_id = ? AND project_id = ?",
                (call_id, project_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("call CDR not found")
        record = _row(row) or {}
        record["attributes"] = json.loads(record.pop("attributes_json", "{}") or "{}")
        return record

    def get_recording_access(
        self,
        *,
        project_id: str,
        user_id: str,
        call_id: str,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.operate")
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM call_jobs WHERE id = ? AND project_id = ?",
                (call_id, project_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("call not found")
        call = self._call(row)
        storage_uri = str(call.get("recording_storage_uri") or "").strip()
        if str(call.get("recording_status") or "") != "completed" or not storage_uri:
            raise ResourceNotFoundError("completed call recording not found")
        temporary = storage_uri.startswith("s3://")
        return {
            "call_id": call_id,
            "status": "completed",
            "url": presign_recording_uri(storage_uri, ttl_seconds=ttl_seconds),
            "temporary": temporary,
            "ttl_seconds": ttl_seconds if temporary else None,
            "expires_at": _timestamp(_now() + timedelta(seconds=ttl_seconds)) if temporary else None,
        }

    def ingest_livekit_event(
        self,
        *,
        event_id: str,
        event_type: str,
        room_name: str = "",
        participant_identity: str = "",
        participant_kind: str = "",
        participant_metadata: str = "",
        attributes: dict[str, Any] | None = None,
        disconnect_reason: str = "",
        egress_id: str = "",
        egress_status: str = "",
        egress_error: str = "",
        egress_storage_uri: str = "",
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a verified LiveKit webhook and converge terminal call state."""
        normalized_event_id = _validate_identifier(event_id, "webhook event id")
        normalized_event_type = _validate_identifier(event_type, "webhook event type")
        timestamp = _timestamp(observed_at)
        safe_attributes = self._safe_sip_attributes(attributes)
        safe_egress_storage_uri = validate_recording_storage_uri(egress_storage_uri)
        provider_call_id = str(
            safe_attributes.get("sip.callIDFull")
            or safe_attributes.get("sip.twilio.callSid")
            or safe_attributes.get("sip.callID")
            or ""
        )
        sip_call_id = str(safe_attributes.get("sip.callID") or "")
        metadata_call_id = ""
        if participant_metadata.strip():
            try:
                metadata = json.loads(participant_metadata)
                if isinstance(metadata, dict):
                    metadata_call_id = str(metadata.get("call_id") or "")
            except json.JSONDecodeError:
                pass
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM telephony_webhook_events WHERE id = ?",
                (normalized_event_id,),
            ).fetchone()
            if existing is not None:
                return _row(existing) or {}

            call = None
            if metadata_call_id:
                call = conn.execute(
                    "SELECT * FROM call_jobs WHERE id = ?", (metadata_call_id,)
                ).fetchone()
            if call is None and provider_call_id:
                call = conn.execute(
                    """
                    SELECT c.* FROM call_jobs c
                    LEFT JOIN call_cdrs d ON d.call_id = c.id
                    WHERE c.provider_call_id = ? OR d.provider_call_id = ? OR d.sip_call_id = ?
                    ORDER BY c.created_at DESC, c.id DESC LIMIT 1
                    """,
                    (provider_call_id, provider_call_id, sip_call_id),
                ).fetchone()
            if call is None and room_name.strip():
                call = conn.execute(
                    """
                    SELECT * FROM call_jobs WHERE room_name = ?
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (room_name.strip(),),
                ).fetchone()
            if call is None and egress_id.strip():
                call = conn.execute(
                    """
                    SELECT * FROM call_jobs WHERE recording_egress_id = ?
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (egress_id.strip(),),
                ).fetchone()

            project_id = str(call["project_id"]) if call is not None else None
            call_id = str(call["id"]) if call is not None else None
            normalized_participant_kind = participant_kind.strip().upper()
            is_sip_participant = (
                normalized_participant_kind == "SIP"
                or bool(provider_call_id)
                or bool(sip_call_id)
            )
            is_terminal_event = normalized_event_type == "room_finished" or (
                normalized_event_type
                in {"participant_left", "participant_connection_aborted"}
                and is_sip_participant
            )
            outcome = "unmatched"
            if call is not None and call_id and project_id:
                self._upsert_cdr(
                    conn,
                    project_id=project_id,
                    call_id=call_id,
                    provider="livekit-sip",
                    provider_call_id=provider_call_id,
                    sip_call_id=sip_call_id,
                    room_name=room_name.strip() or str(call["room_name"] or ""),
                    participant_identity=participant_identity,
                    sip_status=str(safe_attributes.get("sip.callStatus") or ""),
                    disconnect_reason=disconnect_reason,
                    attributes=safe_attributes,
                    observed_at=timestamp,
                    ended=is_terminal_event,
                )
                conn.execute(
                    """
                    UPDATE call_jobs SET
                        provider_call_id = CASE WHEN ? <> '' THEN ? ELSE provider_call_id END,
                        room_name = CASE WHEN ? <> '' THEN ? ELSE room_name END,
                        updated_at = ? WHERE id = ?
                    """,
                    (
                        provider_call_id,
                        provider_call_id,
                        room_name.strip(),
                        room_name.strip(),
                        timestamp,
                        call_id,
                    ),
                )
                outcome = "observed"
                recording_status = {
                    "EGRESS_STARTING": "starting",
                    "EGRESS_ACTIVE": "active",
                    "EGRESS_ENDING": "stopping",
                    "EGRESS_COMPLETE": "completed",
                    "EGRESS_FAILED": "failed",
                    "EGRESS_ABORTED": "failed",
                    "EGRESS_LIMIT_REACHED": "failed",
                }.get(egress_status.strip().upper(), "")
                if egress_id.strip() and recording_status:
                    conn.execute(
                        """
                        UPDATE call_jobs SET recording_egress_id = ?,
                            recording_status = ?,
                            recording_storage_uri = CASE WHEN ? <> '' THEN ?
                                ELSE recording_storage_uri END,
                            updated_at = ? WHERE id = ?
                        """,
                        (
                            egress_id.strip(),
                            recording_status,
                            safe_egress_storage_uri,
                            safe_egress_storage_uri,
                            timestamp,
                            call_id,
                        ),
                    )
                    self._event(
                        conn,
                        project_id,
                        call_id,
                        f"call.recording.{recording_status}",
                        {
                            "egress_id": egress_id.strip(),
                            "provider_event": normalized_event_type,
                            "error": egress_error.strip()[:500]
                            if recording_status == "failed"
                            else "",
                        },
                    )
                    outcome = f"recording_{recording_status}"
                if is_terminal_event and str(call["status"]) in ACTIVE_STATUSES:
                    final_status = "completed" if call["answered_at"] else "failed"
                    failure_code = "" if final_status == "completed" else "call_ended_unanswered"
                    conn.execute(
                        """
                        UPDATE call_jobs SET status = ?, failure_code = ?, ended_at = ?,
                            updated_at = ?, lease_owner = '', lease_token = '',
                            lease_expires_at = NULL WHERE id = ?
                        """,
                        (final_status, failure_code, timestamp, timestamp, call_id),
                    )
                    conn.execute(
                        """
                        UPDATE call_attempts SET status = ?, ended_at = ?, failure_code = ?
                        WHERE call_id = ? AND ended_at IS NULL
                        """,
                        (final_status, timestamp, failure_code, call_id),
                    )
                    self._event(
                        conn,
                        project_id,
                        call_id,
                        f"call.{final_status}",
                        {
                            "source": "livekit_webhook",
                            "disconnect_reason": disconnect_reason,
                        },
                    )
                    self._sync_campaign_call(
                        conn,
                        call_id=call_id,
                        status=final_status,
                        timestamp=timestamp,
                        reason=failure_code,
                    )
                    outcome = f"terminalized_{final_status}"

            payload = {
                "room_name": room_name.strip()[:200],
                "participant_identity": participant_identity.strip()[:200],
                "participant_kind": normalized_participant_kind[:40],
                "disconnect_reason": disconnect_reason.strip()[:200],
                "attributes": safe_attributes,
                "egress_id": egress_id.strip()[:200],
                "egress_status": egress_status.strip()[:80],
                "egress_error": egress_error.strip()[:500],
            }
            conn.execute(
                """
                INSERT INTO telephony_webhook_events (
                    id, provider, event_type, project_id, call_id, outcome,
                    payload_json, received_at
                ) VALUES (?, 'livekit', ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_event_id,
                    normalized_event_type,
                    project_id,
                    call_id,
                    outcome,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                ),
            )
            row = conn.execute(
                "SELECT * FROM telephony_webhook_events WHERE id = ?",
                (normalized_event_id,),
            ).fetchone()
        return _row(row) or {}

    def heartbeat(
        self,
        *,
        project_id: str,
        user_id: str,
        call_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.work")
        current = _now(now)
        timestamp = _timestamp(current)
        with self.store.transaction() as conn:
            limits = conn.execute(
                "SELECT * FROM telephony_limits WHERE project_id = ?", (project_id,)
            ).fetchone()
            if limits is None:
                raise RuntimeError("telephony limits are missing for an active call")
            row = self._owned_call(conn, project_id, call_id, worker_id, lease_token)
            if row["status"] not in ACTIVE_STATUSES:
                raise LeaseConflictError("call is not lease-active")
            lease_expires = _timestamp(current + timedelta(seconds=int(limits["lease_seconds"])))
            conn.execute(
                "UPDATE call_jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (lease_expires, timestamp, call_id),
            )
            updated = conn.execute("SELECT * FROM call_jobs WHERE id = ?", (call_id,)).fetchone()
        return self._call(updated, include_lease=True)

    def record_call_result(
        self,
        *,
        project_id: str,
        user_id: str,
        call_id: str,
        worker_id: str,
        lease_token: str,
        answering_machine_category: str = "",
        disposition: str = "",
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.work")
        category = answering_machine_category.strip()
        allowed_categories = {
            "", "human", "machine-ivr", "machine-vm", "machine-unavailable", "uncertain"
        }
        if category not in allowed_categories:
            raise ValueError("invalid answering machine category")
        normalized_disposition = disposition.strip()
        if normalized_disposition and not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,120}", normalized_disposition
        ):
            raise ValueError("invalid call disposition")
        timestamp = _utc_now()
        with self.store.transaction() as conn:
            self._owned_call(conn, project_id, call_id, worker_id, lease_token)
            conn.execute(
                """
                UPDATE call_jobs SET answering_machine_category = ?, disposition = ?,
                    updated_at = ? WHERE id = ? AND project_id = ?
                """,
                (category, normalized_disposition, timestamp, call_id, project_id),
            )
            self._event(
                conn,
                project_id,
                call_id,
                "call.result.recorded",
                {
                    "answering_machine_category": category,
                    "disposition": normalized_disposition,
                },
            )
            row = conn.execute(
                "SELECT * FROM call_jobs WHERE id = ?", (call_id,)
            ).fetchone()
        return self._call(row, include_lease=True)

    def record_call_recording(
        self,
        *,
        project_id: str,
        user_id: str,
        call_id: str,
        worker_id: str,
        lease_token: str,
        egress_id: str,
        status: str,
        storage_uri: str = "",
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.work")
        normalized_egress_id = _validate_identifier(egress_id, "recording egress id")
        if status not in {"starting", "active", "stopping", "completed", "failed"}:
            raise ValueError("invalid recording status")
        uri = validate_recording_storage_uri(storage_uri)
        timestamp = _utc_now()
        with self.store.transaction() as conn:
            owned = self._owned_call(conn, project_id, call_id, worker_id, lease_token)
            if str(owned["recording_status"] or "") in {"completed", "failed"} and status in {
                "starting",
                "active",
                "stopping",
            }:
                return self._call(owned, include_lease=True)
            conn.execute(
                """
                UPDATE call_jobs SET recording_egress_id = ?, recording_status = ?,
                    recording_storage_uri = ?, updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (normalized_egress_id, status, uri, timestamp, call_id, project_id),
            )
            self._event(
                conn,
                project_id,
                call_id,
                f"call.recording.{status}",
                {"egress_id": normalized_egress_id},
            )
            row = conn.execute(
                "SELECT * FROM call_jobs WHERE id = ?", (call_id,)
            ).fetchone()
        return self._call(row, include_lease=True)

    def transition_call(
        self,
        *,
        project_id: str,
        user_id: str,
        call_id: str,
        status: str,
        worker_id: str = "",
        lease_token: str = "",
        provider_call_id: str | None = None,
        room_name: str | None = None,
        failure_code: str = "",
        failure_detail: str = "",
        retryable: bool = False,
        retry_delay_seconds: int = 30,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.work")
        if status not in set().union(*TRANSITIONS.values()):
            raise ValueError("invalid call status")
        if not 0 <= retry_delay_seconds <= 86400:
            raise ValueError("retry_delay_seconds must be between 0 and 86400")
        current = _now(now)
        timestamp = _timestamp(current)
        with self.store.transaction() as conn:
            lock_suffix = " FOR UPDATE" if self.store.backend == "postgresql" else ""
            row = conn.execute(
                f"SELECT * FROM call_jobs WHERE id = ? AND project_id = ?{lock_suffix}",
                (call_id, project_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("call not found")
            if row["lease_token"]:
                row = self._owned_call(conn, project_id, call_id, worker_id, lease_token)
            current_status = str(row["status"])
            if (
                str(row["direction"]) == "outbound"
                and current_status == "leased"
                and status == "dispatching"
            ):
                policy = self._ensure_policy(conn, project_id, timestamp)
                if not policy["outbound_enabled"]:
                    raise ComplianceBlockedError("outbound_paused")
                if row["campaign_id"]:
                    campaign = conn.execute(
                        """
                        SELECT status FROM telephony_campaigns
                        WHERE id = ? AND project_id = ?
                        """,
                        (row["campaign_id"], project_id),
                    ).fetchone()
                    if campaign is None or str(campaign["status"]) != "running":
                        raise ComplianceBlockedError("campaign_not_running")
                if row["trunk_id"]:
                    trunk = conn.execute(
                        """
                        SELECT * FROM sip_trunks
                        WHERE id = ? AND project_id = ?
                          AND direction IN ('outbound', 'bidirectional')
                          AND status = 'active'
                        """,
                        (row["trunk_id"], project_id),
                    ).fetchone()
                    if trunk is None or not str(trunk["livekit_trunk_id"] or "").strip():
                        raise ComplianceBlockedError("outbound_trunk_unavailable")
                    allowed_numbers = json.loads(str(trunk["numbers_json"] or "[]"))
                    source_number = self._reveal_phone(row["source_number"])
                    if source_number and "*" not in allowed_numbers and source_number not in allowed_numbers:
                        raise ComplianceBlockedError("source_number_not_allowlisted")
            if status not in TRANSITIONS.get(current_status, frozenset()):
                raise InvalidCallTransitionError(
                    f"cannot transition call from {current_status} to {status}"
                )
            final_status = status
            available_at = str(row["available_at"])
            if retryable and status in RETRYABLE_STATUSES and int(row["attempt_count"]) < int(row["max_attempts"]):
                final_status = "queued"
                available_at = _timestamp(current + timedelta(seconds=retry_delay_seconds))
            answered_at = timestamp if status == "active" and not row["answered_at"] else row["answered_at"]
            ended_at = timestamp if final_status in TERMINAL_STATUSES else None
            # An uncertain provider result is handed to a separate reconciler.
            # Release the business-operation lease immediately so no caller can
            # retry the external side effect while reconciliation is pending.
            clear_lease = (
                final_status in {"queued", "reconciling"}
                or final_status in TERMINAL_STATUSES
            )
            conn.execute(
                """
                UPDATE call_jobs SET
                    status = ?, available_at = ?,
                    provider_call_id = CASE WHEN ? <> '' THEN ? ELSE provider_call_id END,
                    room_name = CASE WHEN ? <> '' THEN ? ELSE room_name END,
                    failure_code = ?, failure_detail = ?, answered_at = ?, ended_at = ?,
                    reconcile_started_at = CASE WHEN ? = 'reconciling'
                        THEN COALESCE(reconcile_started_at, ?) ELSE reconcile_started_at END,
                    lease_owner = CASE WHEN ? = 1 THEN '' ELSE lease_owner END,
                    lease_token = CASE WHEN ? = 1 THEN '' ELSE lease_token END,
                    lease_expires_at = CASE WHEN ? = 1 THEN NULL ELSE lease_expires_at END,
                    updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (
                    final_status,
                    available_at,
                    (provider_call_id or "").strip(),
                    (provider_call_id or "").strip(),
                    (room_name or "").strip(),
                    (room_name or "").strip(),
                    failure_code.strip(),
                    failure_detail.strip()[:2000],
                    answered_at,
                    ended_at,
                    final_status,
                    timestamp,
                    int(clear_lease),
                    int(clear_lease),
                    int(clear_lease),
                    timestamp,
                    call_id,
                    project_id,
                ),
            )
            if row["lease_token"]:
                attempt_status = "retry_scheduled" if final_status == "queued" else final_status
                conn.execute(
                    """
                    UPDATE call_attempts SET status = ?, ended_at = ?,
                        failure_code = ?, failure_detail = ?
                    WHERE call_id = ? AND lease_token = ?
                    """,
                    (
                        attempt_status,
                        timestamp
                        if final_status in {"queued", "reconciling"}
                        or final_status in TERMINAL_STATUSES
                        else None,
                        failure_code.strip(),
                        failure_detail.strip()[:2000],
                        call_id,
                        row["lease_token"],
                    ),
                )
            self._event(
                conn,
                project_id,
                call_id,
                f"call.{final_status}",
                {
                    "requested_status": status,
                    "retryable": retryable,
                    "failure_code": failure_code.strip(),
                },
            )
            self._sync_campaign_call(
                conn,
                call_id=call_id,
                status=final_status,
                timestamp=timestamp,
                reason=failure_code.strip(),
            )
            updated = conn.execute("SELECT * FROM call_jobs WHERE id = ?", (call_id,)).fetchone()
        return self._call(updated, include_lease=not clear_lease)

    def get_call(self, *, project_id: str, user_id: str, call_id: str) -> dict[str, Any]:
        role = self.store.require_permission(project_id, user_id, "telephony.read")
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM call_jobs WHERE id = ? AND project_id = ?",
                (call_id, project_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("call not found")
        return self._call(row, redact_numbers=role == "viewer")

    def list_calls(
        self,
        *,
        project_id: str,
        user_id: str,
        direction: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        role = self.store.require_permission(project_id, user_id, "telephony.read")
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if direction:
            if direction not in {"inbound", "outbound"}:
                raise ValueError("invalid call direction")
            clauses.append("direction = ?")
            params.append(direction)
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.append(max(1, min(limit, 500)))
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM call_jobs WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._call(row, redact_numbers=role == "viewer") for row in rows]

    def metrics(self, *, project_id: str, user_id: str) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "telephony.read")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT direction, status, COUNT(*) AS count
                FROM call_jobs WHERE project_id = ? GROUP BY direction, status
                """,
                (project_id,),
            ).fetchall()
            attempts = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status IN ('failed', 'busy', 'no_answer') THEN 1 ELSE 0 END) AS failed
                FROM call_attempts WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            stale = conn.execute(
                """
                SELECT COUNT(*) AS count FROM call_jobs
                WHERE project_id = ? AND status IN ('leased', 'dispatching', 'dialing', 'ringing', 'active')
                  AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (project_id, _utc_now()),
            ).fetchone()
        states = {f"{row['direction']}.{row['status']}": int(row["count"]) for row in rows}
        active = sum(count for key, count in states.items() if key.split(".", 1)[1] in ACTIVE_STATUSES)
        return {
            "states": states,
            "queue_depth": sum(count for key, count in states.items() if key.endswith(".queued")),
            "active_calls": active,
            "stale_leases": int(stale["count"] or 0),
            "attempts": {
                "total": int(attempts["total"] or 0),
                "completed": int(attempts["completed"] or 0),
                "failed": int(attempts["failed"] or 0),
            },
        }

    def _call(
        self, row: Any, *, include_lease: bool = False, redact_numbers: bool = False
    ) -> dict[str, Any]:
        record = _row(row) or {}
        record["metadata"] = self._reveal_json(record.pop("metadata_json", "{}"))
        record.pop("destination_hash", None)
        for field in ("source_number", "destination_number"):
            record[field] = self._reveal_phone(record.get(field))
        if redact_numbers:
            for field in ("source_number", "destination_number"):
                value = str(record.get(field) or "")
                if value:
                    record[field] = "+" + "*" * max(0, len(value) - 5) + value[-4:]
            record["metadata"] = {}
            record["recording_storage_uri"] = ""
        if not include_lease:
            record.pop("lease_token", None)
        return record

    @staticmethod
    def _event(
        conn: Any,
        project_id: str,
        call_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO call_events (id, project_id, call_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                project_id,
                call_id,
                event_type,
                json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                _utc_now(),
            ),
        )

    @staticmethod
    def _active_counts(conn: Any, project_id: str) -> dict[str, int]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        statuses = sorted(ACTIVE_STATUSES)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) AS outbound,
                   SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) AS inbound
            FROM call_jobs WHERE project_id = ? AND status IN ({placeholders})
            """,
            [project_id, *statuses],
        ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "outbound": int(row["outbound"] or 0),
            "inbound": int(row["inbound"] or 0),
        }

    def _owned_call(
        self,
        conn: Any,
        project_id: str,
        call_id: str,
        worker_id: str,
        lease_token: str,
    ) -> Any:
        lock_suffix = " FOR UPDATE" if self.store.backend == "postgresql" else ""
        row = conn.execute(
            f"SELECT * FROM call_jobs WHERE id = ? AND project_id = ?{lock_suffix}",
            (call_id, project_id),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("call not found")
        expected_owner = str(row["lease_owner"] or "")
        expected_token = str(row["lease_token"] or "")
        if (
            not worker_id
            or not lease_token
            or not hmac.compare_digest(worker_id, expected_owner)
            or not hmac.compare_digest(lease_token, expected_token)
        ):
            raise LeaseConflictError("call lease ownership mismatch")
        return row

    def _expire_leases(self, conn: Any, project_id: str, current: datetime) -> None:
        timestamp = _timestamp(current)
        rows = conn.execute(
            """
            SELECT * FROM call_jobs
            WHERE project_id = ? AND status IN ('leased', 'dispatching', 'dialing', 'ringing', 'active')
              AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
            """,
            (project_id, timestamp),
        ).fetchall()
        for row in rows:
            call_id = str(row["id"])
            if row["status"] == "leased":
                next_status = (
                    "queued" if int(row["attempt_count"]) < int(row["max_attempts"]) else "failed"
                )
                conn.execute(
                    """
                    UPDATE call_jobs SET status = ?, available_at = ?, lease_owner = '',
                        lease_token = '', lease_expires_at = NULL, updated_at = ?,
                        failure_code = 'lease_expired',
                        ended_at = CASE WHEN ? = 'failed' THEN ? ELSE ended_at END
                    WHERE id = ?
                    """,
                    (next_status, timestamp, timestamp, next_status, timestamp, call_id),
                )
            else:
                next_status = "reconciling"
                conn.execute(
                    """
                    UPDATE call_jobs SET status = 'reconciling', updated_at = ?,
                        failure_code = 'lease_expired',
                        reconcile_started_at = COALESCE(reconcile_started_at, ?)
                    WHERE id = ?
                    """,
                    (timestamp, timestamp, call_id),
                )
            conn.execute(
                """
                UPDATE call_attempts SET status = 'lease_expired', ended_at = ?,
                    failure_code = 'lease_expired'
                WHERE call_id = ? AND lease_token = ? AND ended_at IS NULL
                """,
                (timestamp, call_id, row["lease_token"]),
            )
            self._event(
                conn,
                project_id,
                call_id,
                f"call.{next_status}",
                {"reason": "lease_expired"},
            )
