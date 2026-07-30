from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
from typing import Any, Mapping


class MetadataValidationError(ValueError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class InboundMetadataSigner:
    """Small, dependency-free signed envelope for LiveKit job metadata."""

    def __init__(self, secret: str | bytes, *, max_age_seconds: int = 300):
        material = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(material) < 32:
            raise ValueError("inbound metadata secret must contain at least 32 bytes")
        if max_age_seconds < 1:
            raise ValueError("max_age_seconds must be positive")
        self._secret = material
        self.max_age_seconds = max_age_seconds

    def sign(self, claims: Mapping[str, Any], *, issued_at: int | None = None) -> str:
        now = issued_at if issued_at is not None else int(datetime.now(timezone.utc).timestamp())
        body = {
            **dict(claims),
            "iat": now,
            "nonce": str(claims.get("nonce") or secrets.token_urlsafe(12)),
            "v": 1,
        }
        encoded = _b64encode(
            json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = _b64encode(hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(self, token: str, *, now: int | None = None) -> dict[str, Any]:
        try:
            encoded, supplied = token.split(".", 1)
            expected = _b64encode(
                hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied, expected):
                raise MetadataValidationError("metadata signature is invalid")
            payload = json.loads(_b64decode(encoded))
        except MetadataValidationError:
            raise
        except Exception as exc:
            raise MetadataValidationError("metadata envelope is malformed") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise MetadataValidationError("metadata version is unsupported")
        issued_at = payload.get("iat")
        if not isinstance(issued_at, int):
            raise MetadataValidationError("metadata issued-at is missing")
        current = now if now is not None else int(datetime.now(timezone.utc).timestamp())
        if issued_at > current + 30 or current - issued_at > self.max_age_seconds:
            raise MetadataValidationError("metadata has expired")
        required = {"kind", "binding_id", "agent_version_id", "project_id", "nonce"}
        if any(not str(payload.get(field) or "").strip() for field in required):
            raise MetadataValidationError("metadata is missing required claims")
        if payload["kind"] not in {"public_demo", "enterprise"}:
            raise MetadataValidationError("metadata kind is invalid")
        return payload
