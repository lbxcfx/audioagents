from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
from typing import Mapping, Any

import jwt


class WorkerAuthenticationError(PermissionError):
    pass


def issue_worker_token(secret: str, *, subject: str, scopes: list[str], ttl_seconds: int = 60) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": "audioagents-inbound-worker",
            "aud": "audioagents-inbound-control",
            "sub": subject,
            "scope": scopes,
            "iat": now,
            "exp": now + timedelta(seconds=max(10, min(ttl_seconds, 300))),
            "jti": str(uuid.uuid4()),
        },
        secret,
        algorithm="HS256",
    )


def verify_worker_token(token: str, *, secrets: tuple[str, ...], required_scope: str) -> dict:
    if not token or not secrets:
        raise WorkerAuthenticationError("worker authentication failed")
    last_error: Exception | None = None
    for secret in secrets:
        if not secret:
            continue
        try:
            claims = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience="audioagents-inbound-control",
                issuer="audioagents-inbound-worker",
                options={"require": ["iss", "aud", "sub", "scope", "iat", "exp", "jti"]},
                leeway=5,
            )
            scopes = claims.get("scope")
            if not isinstance(scopes, list) or required_scope not in scopes:
                raise WorkerAuthenticationError("worker token scope is insufficient")
            return claims
        except WorkerAuthenticationError:
            raise
        except Exception as exc:
            last_error = exc
    raise WorkerAuthenticationError("worker authentication failed") from last_error


def verify_worker_identity_token(
    token: str,
    *,
    identities: Mapping[str, Mapping[str, Any]],
    required_scope: str,
) -> dict:
    """Verify against the key and fixed scope allowlist assigned to the token subject."""
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
        subject = str(unverified.get("sub") or "")
        identity = identities.get(subject)
        if not identity:
            raise WorkerAuthenticationError("worker identity is not registered")
        allowed = identity.get("scopes")
        secret = str(identity.get("secret") or "")
        if not isinstance(allowed, list) or required_scope not in allowed or len(secret) < 32:
            raise WorkerAuthenticationError("worker identity scope is insufficient")
        claims = verify_worker_token(token, secrets=(secret,), required_scope=required_scope)
        claimed = claims.get("scope")
        if not isinstance(claimed, list) or any(scope not in allowed for scope in claimed):
            raise WorkerAuthenticationError("worker token contains an unauthorized scope")
        return claims
    except WorkerAuthenticationError:
        raise
    except Exception as exc:
        raise WorkerAuthenticationError("worker authentication failed") from exc
