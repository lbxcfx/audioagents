from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import jwt


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.api import create_platform_router
from server.cloud_parity.auth import (
    AuthenticationSettings,
    DevelopmentAuthenticator,
    OIDCAuthenticator,
    TrustedProxyAuthenticator,
    install_authenticator,
    install_legacy_api_auth_boundary,
    optional_user_id,
    require_user_id,
)
from server.cloud_parity.store import PlatformStore


ISSUER = "https://identity.example.test/"
AUDIENCE = "cloud-parity-api"


class StaticJWKClient:
    def __init__(self, signing_key):
        self.signing_key = signing_key

    def get_signing_key_from_jwt(self, token: str):
        return self.signing_key


def _oidc_stack(revocation_checker=None):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    settings = AuthenticationSettings(
        mode="oidc",
        oidc_issuer=ISSUER,
        oidc_audiences=(AUDIENCE,),
        oidc_jwks_url="https://identity.example.test/jwks.json",
        oidc_algorithms=("RS256",),
        oidc_leeway_seconds=0,
    )
    authenticator = OIDCAuthenticator(
        settings,
        jwk_client=StaticJWKClient(jwt.PyJWK.from_dict(jwk)),
        revocation_checker=revocation_checker,
    )
    return private_key, authenticator


def _token(private_key, **overrides) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "oidc-user-123",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def _identity_client(authenticator) -> TestClient:
    app = FastAPI()
    install_authenticator(app, authenticator)

    @app.get("/protected")
    def protected(user_id: str = Depends(require_user_id)) -> dict:
        return {"user_id": user_id}

    @app.get("/optional")
    def optional(user_id: str | None = Depends(optional_user_id)) -> dict:
        return {"user_id": user_id}

    return TestClient(app)


def test_development_mode_preserves_local_header_behavior() -> None:
    client = _identity_client(DevelopmentAuthenticator())

    assert client.get("/protected").status_code == 422
    assert client.get("/optional").json() == {"user_id": None}
    assert client.get("/protected", headers={"X-User-ID": "local-admin"}).json() == {
        "user_id": "local-admin"
    }


def test_legacy_operations_api_is_authenticated_but_health_remains_public() -> None:
    app = FastAPI()
    install_authenticator(app, DevelopmentAuthenticator())
    install_legacy_api_auth_boundary(app)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/api/campaigns")
    def create_campaign() -> dict:
        return {"created": True}

    @app.post("/api/embed/widget-1/token")
    def public_embed_token() -> dict:
        return {"token": "public"}

    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    assert client.post("/api/campaigns").status_code == 422
    allowed = client.post(
        "/api/campaigns", headers={"X-User-ID": "local-admin"}
    )
    assert allowed.status_code == 200
    assert client.post("/api/embed/widget-1/token").json() == {"token": "public"}


def test_unconfigured_router_fails_closed() -> None:
    app = FastAPI()

    @app.get("/protected")
    def protected(user_id: str = Depends(require_user_id)) -> dict:
        return {"user_id": user_id}

    response = TestClient(app).get(
        "/protected", headers={"X-User-ID": "spoofed-admin"}
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "authentication is not configured"}


def test_oidc_accepts_valid_token_and_ignores_spoofed_local_header() -> None:
    private_key, authenticator = _oidc_stack()
    client = _identity_client(authenticator)
    token = _token(private_key)

    response = client.get(
        "/protected",
        headers={
            "Authorization": f"Bearer {token}",
            "X-User-ID": "spoofed-admin",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": "oidc-user-123"}


def test_oidc_rejects_missing_expired_and_wrong_audience_tokens() -> None:
    private_key, authenticator = _oidc_stack()
    client = _identity_client(authenticator)
    now = datetime.now(timezone.utc)

    assert client.get("/protected").status_code == 401
    expired = _token(
        private_key,
        iat=now - timedelta(minutes=10),
        exp=now - timedelta(minutes=5),
    )
    assert client.get(
        "/protected", headers={"Authorization": f"Bearer {expired}"}
    ).status_code == 401
    wrong_audience = _token(private_key, aud="some-other-api")
    response = client.get(
        "/protected", headers={"Authorization": f"Bearer {wrong_audience}"}
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid access token"}


def test_trusted_proxy_requires_secret_and_ignores_x_user_id() -> None:
    settings = AuthenticationSettings(
        mode="trusted-proxy",
        trusted_proxy_secret="a" * 32,
    )
    client = _identity_client(TrustedProxyAuthenticator(settings))

    assert client.get(
        "/protected", headers={"X-Authenticated-User": "proxy-user"}
    ).status_code == 401
    response = client.get(
        "/protected",
        headers={
            "X-Authenticated-User": "proxy-user",
            "X-Cloud-Parity-Proxy-Secret": "a" * 32,
            "X-User-ID": "spoofed-admin",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"user_id": "proxy-user"}


def test_authenticated_subject_owns_new_project_even_if_body_is_spoofed(
    tmp_path: Path,
) -> None:
    private_key, authenticator = _oidc_stack()
    store = PlatformStore(tmp_path / "auth.sqlite3")
    store.initialize()
    app = FastAPI()
    install_authenticator(app, authenticator)
    app.include_router(create_platform_router(store))
    client = TestClient(app)

    response = client.post(
        "/api/platform/projects",
        headers={"Authorization": f"Bearer {_token(private_key)}"},
        json={"name": "Secure", "slug": "secure", "owner_id": "attacker-choice"},
    )

    assert response.status_code == 201
    assert response.json()["role"] == "owner"
    assert len(store.list_projects("oidc-user-123")) == 1
    assert store.list_projects("attacker-choice") == []


def test_oidc_user_can_revoke_current_access_token_immediately(tmp_path: Path) -> None:
    store = PlatformStore(tmp_path / "revocation.sqlite3")
    store.initialize()
    private_key, authenticator = _oidc_stack(store.is_access_token_revoked)
    app = FastAPI()
    install_authenticator(app, authenticator)
    app.include_router(create_platform_router(store))
    client = TestClient(app)
    token = _token(private_key)
    headers = {"Authorization": f"Bearer {token}"}

    revoked = client.post(
        "/api/platform/auth/revoke",
        headers=headers,
        json={"reason": "interactive logout"},
    )

    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert store.is_access_token_revoked(token) is True
    denied = client.get("/api/platform/projects", headers=headers)
    assert denied.status_code == 401
    assert denied.json() == {"detail": "access token has been revoked"}
