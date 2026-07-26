from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import os
import re
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


_bearer = HTTPBearer(auto_error=False)
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")


@dataclass(frozen=True)
class AuthenticationSettings:
    mode: str
    oidc_issuer: str | None = None
    oidc_audiences: tuple[str, ...] = ()
    oidc_jwks_url: str | None = None
    oidc_algorithms: tuple[str, ...] = ("RS256",)
    oidc_subject_claim: str = "sub"
    oidc_leeway_seconds: float = 30.0
    oidc_jwks_cache_seconds: float = 300.0
    oidc_jwks_timeout_seconds: float = 5.0
    trusted_proxy_user_header: str = "X-Authenticated-User"
    trusted_proxy_secret_header: str = "X-Cloud-Parity-Proxy-Secret"
    trusted_proxy_secret: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, environment: str) -> "AuthenticationSettings":
        default_mode = "development" if environment in {"development", "test"} else "oidc"
        mode = os.getenv("CLOUD_PARITY_AUTH_MODE", default_mode).strip().lower()
        if mode not in {"development", "oidc", "trusted-proxy"}:
            raise ValueError(
                "CLOUD_PARITY_AUTH_MODE must be development, oidc, or trusted-proxy"
            )
        if environment in {"staging", "production"} and mode == "development":
            raise ValueError("development authentication is forbidden in staging and production")

        issuer = os.getenv("CLOUD_PARITY_OIDC_ISSUER", "").strip() or None
        audiences = tuple(
            item.strip()
            for item in os.getenv("CLOUD_PARITY_OIDC_AUDIENCE", "").split(",")
            if item.strip()
        )
        jwks_url = os.getenv("CLOUD_PARITY_OIDC_JWKS_URL", "").strip() or None
        algorithms = tuple(
            item.strip()
            for item in os.getenv("CLOUD_PARITY_OIDC_ALGORITHMS", "RS256").split(",")
            if item.strip()
        )
        asymmetric_algorithms = {
            "RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
            "ES256", "ES384", "ES512", "EdDSA",
        }
        if not algorithms or any(item not in asymmetric_algorithms for item in algorithms):
            raise ValueError("OIDC algorithms must be an asymmetric allow-list")

        subject_claim = os.getenv("CLOUD_PARITY_OIDC_SUBJECT_CLAIM", "sub").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", subject_claim):
            raise ValueError("invalid CLOUD_PARITY_OIDC_SUBJECT_CLAIM")

        leeway = float(os.getenv("CLOUD_PARITY_OIDC_LEEWAY_SECONDS", "30"))
        cache_seconds = float(os.getenv("CLOUD_PARITY_OIDC_JWKS_CACHE_SECONDS", "300"))
        timeout_seconds = float(os.getenv("CLOUD_PARITY_OIDC_JWKS_TIMEOUT_SECONDS", "5"))
        if not 0 <= leeway <= 300:
            raise ValueError("CLOUD_PARITY_OIDC_LEEWAY_SECONDS must be between 0 and 300")
        if cache_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("OIDC JWKS cache and timeout values must be positive")

        user_header = os.getenv(
            "CLOUD_PARITY_TRUSTED_PROXY_USER_HEADER", "X-Authenticated-User"
        ).strip()
        secret_header = os.getenv(
            "CLOUD_PARITY_TRUSTED_PROXY_SECRET_HEADER",
            "X-Cloud-Parity-Proxy-Secret",
        ).strip()
        if not _HEADER_NAME_RE.fullmatch(user_header) or not _HEADER_NAME_RE.fullmatch(
            secret_header
        ):
            raise ValueError("trusted proxy header names are invalid")
        proxy_secret = os.getenv("CLOUD_PARITY_TRUSTED_PROXY_SECRET", "").strip() or None

        if mode == "oidc":
            if not issuer or not audiences or not jwks_url:
                raise ValueError(
                    "OIDC mode requires CLOUD_PARITY_OIDC_ISSUER, "
                    "CLOUD_PARITY_OIDC_AUDIENCE, and CLOUD_PARITY_OIDC_JWKS_URL"
                )
            for label, value in (("issuer", issuer), ("JWKS URL", jwks_url)):
                parsed = urlsplit(value)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise ValueError(f"OIDC {label} must be an absolute HTTP(S) URL")
                if environment in {"staging", "production"} and parsed.scheme != "https":
                    raise ValueError(f"OIDC {label} must use HTTPS outside development")

        if mode == "trusted-proxy" and (not proxy_secret or len(proxy_secret) < 32):
            raise ValueError(
                "trusted-proxy mode requires CLOUD_PARITY_TRUSTED_PROXY_SECRET "
                "with at least 32 characters"
            )

        return cls(
            mode=mode,
            oidc_issuer=issuer,
            oidc_audiences=audiences,
            oidc_jwks_url=jwks_url,
            oidc_algorithms=algorithms,
            oidc_subject_claim=subject_claim,
            oidc_leeway_seconds=leeway,
            oidc_jwks_cache_seconds=cache_seconds,
            oidc_jwks_timeout_seconds=timeout_seconds,
            trusted_proxy_user_header=user_header,
            trusted_proxy_secret_header=secret_header,
            trusted_proxy_secret=proxy_secret,
        )


class Authenticator(Protocol):
    mode: str

    def authenticate(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None,
        development_user_id: str | None,
        *,
        optional: bool = False,
    ) -> str | None: ...


def _validated_user_id(value: Any) -> str:
    user_id = str(value or "").strip()
    if not user_id or len(user_id) > 200 or any(ord(char) < 32 for char in user_id):
        raise _unauthorized()
    return user_id


def _unauthorized(detail: str = "authentication required") -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


class DevelopmentAuthenticator:
    mode = "development"

    def authenticate(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None,
        development_user_id: str | None,
        *,
        optional: bool = False,
    ) -> str | None:
        if development_user_id and development_user_id.strip():
            return _validated_user_id(development_user_id)
        if optional:
            return None
        # Preserve the local API's previous validation status for existing tools.
        raise HTTPException(status_code=422, detail="X-User-ID header is required")


class OIDCAuthenticator:
    mode = "oidc"

    def __init__(
        self,
        settings: AuthenticationSettings,
        *,
        jwk_client: Any | None = None,
        revocation_checker: Callable[[str], bool] | None = None,
    ):
        import jwt

        self.settings = settings
        self._jwt = jwt
        self._jwk_client = jwk_client or jwt.PyJWKClient(
            settings.oidc_jwks_url,
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=settings.oidc_jwks_cache_seconds,
            timeout=settings.oidc_jwks_timeout_seconds,
        )
        self._revocation_checker = revocation_checker

    def authenticate(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None,
        development_user_id: str | None,
        *,
        optional: bool = False,
    ) -> str:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise _unauthorized()
        token = credentials.credentials.strip()
        if not token:
            raise _unauthorized()
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = self._jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.settings.oidc_algorithms),
                audience=list(self.settings.oidc_audiences),
                issuer=self.settings.oidc_issuer,
                leeway=self.settings.oidc_leeway_seconds,
                options={
                    "require": ["exp", "iat", self.settings.oidc_subject_claim],
                },
            )
            if self._revocation_checker and self._revocation_checker(token):
                raise _unauthorized("access token has been revoked")
            return _validated_user_id(claims.get(self.settings.oidc_subject_claim))
        except HTTPException:
            raise
        except Exception as exc:
            # Do not expose token, key IDs, issuer internals, or validation detail.
            raise _unauthorized("invalid access token") from exc


class TrustedProxyAuthenticator:
    mode = "trusted-proxy"

    def __init__(self, settings: AuthenticationSettings):
        self.settings = settings

    def authenticate(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None,
        development_user_id: str | None,
        *,
        optional: bool = False,
    ) -> str:
        supplied_secret = request.headers.get(
            self.settings.trusted_proxy_secret_header, ""
        )
        expected_secret = self.settings.trusted_proxy_secret or ""
        if not supplied_secret or not hmac.compare_digest(
            supplied_secret.encode("utf-8"), expected_secret.encode("utf-8")
        ):
            raise _unauthorized("trusted proxy authentication failed")
        return _validated_user_id(
            request.headers.get(self.settings.trusted_proxy_user_header)
        )


def create_authenticator(
    settings: AuthenticationSettings,
    *,
    revocation_checker: Callable[[str], bool] | None = None,
) -> Authenticator:
    if settings.mode == "oidc":
        return OIDCAuthenticator(settings, revocation_checker=revocation_checker)
    if settings.mode == "trusted-proxy":
        return TrustedProxyAuthenticator(settings)
    return DevelopmentAuthenticator()


def install_authenticator(app: Any, authenticator: Authenticator) -> None:
    app.state.cloud_parity_authenticator = authenticator


def install_legacy_api_auth_boundary(app: Any) -> None:
    """Protect the pre-platform operations API with the configured authenticator.

    Platform routes keep their explicit dependencies and LiveKit webhooks keep
    their provider signature verification. The legacy health endpoint remains
    public for local launch scripts and external liveness probes.
    """

    @app.middleware("http")
    async def legacy_api_auth_boundary(request: Request, call_next):
        path = request.url.path
        protected = (
            path.startswith("/api/")
            and not path.startswith("/api/platform/")
            and not path.startswith("/api/embed/")
            and path != "/api/health"
        )
        if not protected:
            return await call_next(request)

        raw_authorization = request.headers.get("Authorization", "").strip()
        credentials: HTTPAuthorizationCredentials | None = None
        if raw_authorization:
            scheme, separator, token = raw_authorization.partition(" ")
            if separator and token.strip():
                credentials = HTTPAuthorizationCredentials(
                    scheme=scheme,
                    credentials=token.strip(),
                )
        try:
            user_id = _request_authenticator(request).authenticate(
                request,
                credentials,
                request.headers.get("X-User-ID"),
            )
            if user_id is None:
                raise _unauthorized()
            request.state.authenticated_user_id = user_id
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers or {},
            )
        return await call_next(request)


def _request_authenticator(request: Request) -> Authenticator:
    authenticator = getattr(
        request.app.state,
        "cloud_parity_authenticator",
        None,
    )
    if authenticator is None:
        # Router reuse must never silently downgrade to the development header.
        raise HTTPException(status_code=500, detail="authentication is not configured")
    return authenticator


def require_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
) -> str:
    value = _request_authenticator(request).authenticate(
        request,
        credentials,
        x_user_id,
    )
    if value is None:
        raise _unauthorized()
    return value


def optional_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
) -> str | None:
    return _request_authenticator(request).authenticate(
        request,
        credentials,
        x_user_id,
        optional=True,
    )
