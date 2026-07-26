from __future__ import annotations

from pathlib import Path
import sys

import pytest
from cryptography.fernet import Fernet


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.config import PlatformSettings


DATABASE_ENV = (
    "CLOUD_PARITY_ENV",
    "CLOUD_PARITY_DATABASE_PATH",
    "CLOUD_PARITY_DATABASE_URL",
    "CLOUD_PARITY_MASTER_KEY",
    "CLOUD_PARITY_PHONE_HASH_KEY",
    "CLOUD_PARITY_METRICS_TOKEN",
    "CLOUD_PARITY_API_REQUESTS_PER_MINUTE",
    "CLOUD_PARITY_EMBED_TOKENS_PER_MINUTE",
    "CLOUD_PARITY_RETENTION_DAYS",
    "CLOUD_PARITY_DB_POOL_MIN_SIZE",
    "CLOUD_PARITY_DB_POOL_MAX_SIZE",
    "CLOUD_PARITY_DB_POOL_TIMEOUT_SECONDS",
    "CLOUD_PARITY_DB_CONNECT_TIMEOUT_SECONDS",
    "CLOUD_PARITY_CORS_ALLOWED_ORIGINS",
    "CLOUD_PARITY_AUTH_MODE",
    "CLOUD_PARITY_OIDC_ISSUER",
    "CLOUD_PARITY_OIDC_AUDIENCE",
    "CLOUD_PARITY_OIDC_JWKS_URL",
    "CLOUD_PARITY_OIDC_ALGORITHMS",
    "CLOUD_PARITY_OIDC_SUBJECT_CLAIM",
    "CLOUD_PARITY_OIDC_LEEWAY_SECONDS",
    "CLOUD_PARITY_OIDC_JWKS_CACHE_SECONDS",
    "CLOUD_PARITY_OIDC_JWKS_TIMEOUT_SECONDS",
    "CLOUD_PARITY_TRUSTED_PROXY_USER_HEADER",
    "CLOUD_PARITY_TRUSTED_PROXY_SECRET_HEADER",
    "CLOUD_PARITY_TRUSTED_PROXY_SECRET",
)


def _clear_database_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DATABASE_ENV:
        monkeypatch.delenv(name, raising=False)


def test_development_defaults_to_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_database_env(monkeypatch)

    settings = PlatformSettings.from_env(tmp_path)

    assert settings.environment == "development"
    assert settings.database_url is None
    assert settings.database_path == (
        tmp_path / "qwen-telephony" / "data" / "cloud-parity.sqlite3"
    ).resolve()
    assert settings.authentication.mode == "development"
    assert "http://127.0.0.1:8090" in settings.cors_allowed_origins


def test_staging_rejects_development_authentication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("CLOUD_PARITY_ENV", "staging")
    monkeypatch.setenv(
        "CLOUD_PARITY_DATABASE_URL", "postgresql://cloud_parity@db/cloud_parity"
    )
    monkeypatch.setenv("CLOUD_PARITY_AUTH_MODE", "development")

    with pytest.raises(ValueError, match="development authentication is forbidden"):
        PlatformSettings.from_env(tmp_path)


def test_oidc_rejects_symmetric_signing_algorithms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("CLOUD_PARITY_AUTH_MODE", "oidc")
    monkeypatch.setenv("CLOUD_PARITY_OIDC_ISSUER", "https://identity.example.test/")
    monkeypatch.setenv("CLOUD_PARITY_OIDC_AUDIENCE", "cloud-parity-api")
    monkeypatch.setenv(
        "CLOUD_PARITY_OIDC_JWKS_URL",
        "https://identity.example.test/.well-known/jwks.json",
    )
    monkeypatch.setenv("CLOUD_PARITY_OIDC_ALGORITHMS", "HS256")

    with pytest.raises(ValueError, match="asymmetric allow-list"):
        PlatformSettings.from_env(tmp_path)


def test_cors_requires_exact_origins_and_https_in_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("CLOUD_PARITY_CORS_ALLOWED_ORIGINS", "*")
    with pytest.raises(ValueError, match="exact HTTP"):
        PlatformSettings.from_env(tmp_path)

    _clear_database_env(monkeypatch)
    monkeypatch.setenv("CLOUD_PARITY_ENV", "staging")
    monkeypatch.setenv(
        "CLOUD_PARITY_DATABASE_URL", "postgresql://cloud_parity@db/cloud_parity"
    )
    monkeypatch.setenv("CLOUD_PARITY_AUTH_MODE", "trusted-proxy")
    monkeypatch.setenv("CLOUD_PARITY_TRUSTED_PROXY_SECRET", "s" * 32)
    monkeypatch.setenv(
        "CLOUD_PARITY_CORS_ALLOWED_ORIGINS", "http://console.example.test"
    )
    with pytest.raises(ValueError, match="must use HTTPS"):
        PlatformSettings.from_env(tmp_path)


def test_production_fails_closed_without_database_or_master_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("CLOUD_PARITY_ENV", "production")

    with pytest.raises(ValueError, match="DATABASE_URL"):
        PlatformSettings.from_env(tmp_path)

    secret_url = "postgresql://cloud_parity:do-not-log@db/cloud_parity"
    monkeypatch.setenv("CLOUD_PARITY_DATABASE_URL", secret_url)
    with pytest.raises(ValueError, match="MASTER_KEY"):
        PlatformSettings.from_env(tmp_path)

    monkeypatch.setenv(
        "CLOUD_PARITY_MASTER_KEY", Fernet.generate_key().decode("ascii")
    )
    with pytest.raises(ValueError, match="PHONE_HASH_KEY"):
        PlatformSettings.from_env(tmp_path)

    monkeypatch.setenv("CLOUD_PARITY_PHONE_HASH_KEY", "p" * 32)
    with pytest.raises(ValueError, match="METRICS_TOKEN"):
        PlatformSettings.from_env(tmp_path)

    monkeypatch.setenv("CLOUD_PARITY_METRICS_TOKEN", "m" * 32)
    with pytest.raises(ValueError, match="OIDC mode requires"):
        PlatformSettings.from_env(tmp_path)

    monkeypatch.setenv("CLOUD_PARITY_OIDC_ISSUER", "https://identity.example.test/")
    monkeypatch.setenv("CLOUD_PARITY_OIDC_AUDIENCE", "cloud-parity-api")
    monkeypatch.setenv(
        "CLOUD_PARITY_OIDC_JWKS_URL",
        "https://identity.example.test/.well-known/jwks.json",
    )
    settings = PlatformSettings.from_env(tmp_path)
    assert settings.database_url == secret_url
    assert settings.authentication.mode == "oidc"
    assert "do-not-log" not in repr(settings)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [("-1", "10"), ("2", "1"), ("0", "0")],
)
def test_invalid_pool_sizes_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minimum: str,
    maximum: str,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("CLOUD_PARITY_DB_POOL_MIN_SIZE", minimum)
    monkeypatch.setenv("CLOUD_PARITY_DB_POOL_MAX_SIZE", maximum)

    with pytest.raises(ValueError, match="pool size"):
        PlatformSettings.from_env(tmp_path)
