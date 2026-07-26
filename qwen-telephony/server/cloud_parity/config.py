from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit

from .auth import AuthenticationSettings


@dataclass(frozen=True)
class PlatformSettings:
    """Validated runtime settings for the Cloud-parity control plane."""

    database_path: Path
    database_url: str | None = field(default=None, repr=False)
    default_retention_days: int = 30
    environment: str = "development"
    database_pool_min_size: int = 1
    database_pool_max_size: int = 10
    database_pool_timeout_seconds: float = 10.0
    database_connect_timeout_seconds: float = 10.0
    cors_allowed_origins: tuple[str, ...] = ()
    phone_hash_key: str = field(
        default="development-only-phone-hash-key-change-before-production",
        repr=False,
    )
    metrics_token: str = field(default="", repr=False)
    api_requests_per_minute: int = 600
    worker_api_requests_per_minute: int = 30_000
    worker_source_requests_per_minute: int = 12_000
    webhook_requests_per_minute: int = 6000
    embed_tokens_per_minute: int = 60
    authentication: AuthenticationSettings = field(
        default_factory=lambda: AuthenticationSettings(mode="development")
    )

    @classmethod
    def from_env(cls, project_root: Path) -> "PlatformSettings":
        environment = os.getenv("CLOUD_PARITY_ENV", "development").strip().lower()
        if environment not in {"development", "test", "staging", "production"}:
            raise ValueError(
                "CLOUD_PARITY_ENV must be development, test, staging, or production"
            )

        raw_path = os.getenv("CLOUD_PARITY_DATABASE_PATH", "").strip()
        database_path = (
            Path(raw_path).expanduser()
            if raw_path
            else project_root / "qwen-telephony" / "data" / "cloud-parity.sqlite3"
        )
        retention_days = int(os.getenv("CLOUD_PARITY_RETENTION_DAYS", "30"))
        if retention_days < 1:
            raise ValueError("CLOUD_PARITY_RETENTION_DAYS must be at least 1")

        database_url = os.getenv("CLOUD_PARITY_DATABASE_URL", "").strip() or None
        if database_url and not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError(
                "CLOUD_PARITY_DATABASE_URL must use postgresql:// or postgres://"
            )
        if environment in {"staging", "production"} and not database_url:
            raise ValueError(
                "CLOUD_PARITY_DATABASE_URL is required in staging and production"
            )
        master_key = os.getenv("CLOUD_PARITY_MASTER_KEY", "").strip()
        if environment == "production" and not master_key:
            raise ValueError("CLOUD_PARITY_MASTER_KEY is required in production")
        if master_key:
            try:
                from cryptography.fernet import Fernet

                Fernet(master_key.encode("ascii"))
            except Exception as exc:
                raise ValueError("CLOUD_PARITY_MASTER_KEY must be a valid Fernet key") from exc

        phone_hash_key = os.getenv("CLOUD_PARITY_PHONE_HASH_KEY", "").strip()
        if environment == "production" and len(phone_hash_key) < 32:
            raise ValueError(
                "CLOUD_PARITY_PHONE_HASH_KEY must contain at least 32 characters in production"
            )
        if not phone_hash_key:
            phone_hash_key = "development-only-phone-hash-key-change-before-production"
        metrics_token = os.getenv("CLOUD_PARITY_METRICS_TOKEN", "").strip()
        if environment == "production" and len(metrics_token) < 32:
            raise ValueError(
                "CLOUD_PARITY_METRICS_TOKEN must contain at least 32 characters in production"
            )
        api_requests_per_minute = int(
            os.getenv("CLOUD_PARITY_API_REQUESTS_PER_MINUTE", "600")
        )
        if not 0 <= api_requests_per_minute <= 1_000_000:
            raise ValueError(
                "CLOUD_PARITY_API_REQUESTS_PER_MINUTE must be between 0 and 1000000"
            )
        if environment == "production" and api_requests_per_minute == 0:
            raise ValueError("API rate limiting cannot be disabled in production")
        worker_api_requests_per_minute = int(
            os.getenv("CLOUD_PARITY_WORKER_API_REQUESTS_PER_MINUTE", "30000")
        )
        worker_source_requests_per_minute = int(
            os.getenv("CLOUD_PARITY_WORKER_SOURCE_REQUESTS_PER_MINUTE", "12000")
        )
        if not 1 <= worker_api_requests_per_minute <= 1_000_000:
            raise ValueError(
                "CLOUD_PARITY_WORKER_API_REQUESTS_PER_MINUTE must be between 1 and 1000000"
            )
        if not 1 <= worker_source_requests_per_minute <= 1_000_000:
            raise ValueError(
                "CLOUD_PARITY_WORKER_SOURCE_REQUESTS_PER_MINUTE must be between 1 and 1000000"
            )
        if worker_source_requests_per_minute > worker_api_requests_per_minute:
            raise ValueError(
                "CLOUD_PARITY_WORKER_SOURCE_REQUESTS_PER_MINUTE cannot exceed the worker API limit"
            )
        webhook_requests_per_minute = int(
            os.getenv("CLOUD_PARITY_WEBHOOK_REQUESTS_PER_MINUTE", "6000")
        )
        if not 1 <= webhook_requests_per_minute <= 1_000_000:
            raise ValueError(
                "CLOUD_PARITY_WEBHOOK_REQUESTS_PER_MINUTE must be between 1 and 1000000"
            )
        embed_tokens_per_minute = int(
            os.getenv("CLOUD_PARITY_EMBED_TOKENS_PER_MINUTE", "60")
        )
        if not 1 <= embed_tokens_per_minute <= 100_000:
            raise ValueError(
                "CLOUD_PARITY_EMBED_TOKENS_PER_MINUTE must be between 1 and 100000"
            )

        pool_min_size = int(os.getenv("CLOUD_PARITY_DB_POOL_MIN_SIZE", "1"))
        pool_max_size = int(os.getenv("CLOUD_PARITY_DB_POOL_MAX_SIZE", "10"))
        if pool_min_size < 0 or pool_max_size < 1 or pool_min_size > pool_max_size:
            raise ValueError("invalid Cloud-Parity database pool size")

        pool_timeout = float(os.getenv("CLOUD_PARITY_DB_POOL_TIMEOUT_SECONDS", "10"))
        connect_timeout = float(
            os.getenv("CLOUD_PARITY_DB_CONNECT_TIMEOUT_SECONDS", "10")
        )
        if pool_timeout <= 0 or connect_timeout <= 0:
            raise ValueError("Cloud-Parity database timeouts must be positive")

        raw_origins = os.getenv("CLOUD_PARITY_CORS_ALLOWED_ORIGINS", "").strip()
        if raw_origins:
            cors_allowed_origins = tuple(
                dict.fromkeys(
                    origin.strip().rstrip("/")
                    for origin in raw_origins.split(",")
                    if origin.strip()
                )
            )
        elif environment in {"development", "test"}:
            cors_allowed_origins = (
                "http://127.0.0.1:8090",
                "http://localhost:8090",
                "http://127.0.0.1:8091",
                "http://localhost:8091",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
                "http://127.0.0.1:5174",
                "http://localhost:5174",
            )
        else:
            # Same-origin deployments need no CORS entry.
            cors_allowed_origins = ()
        for origin in cors_allowed_origins:
            parsed = urlsplit(origin)
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError(
                    "CLOUD_PARITY_CORS_ALLOWED_ORIGINS must contain exact HTTP(S) origins"
                )
            if environment in {"staging", "production"} and parsed.scheme != "https":
                raise ValueError(
                    "CLOUD_PARITY_CORS_ALLOWED_ORIGINS must use HTTPS outside development"
                )

        return cls(
            database_path=database_path.resolve(),
            database_url=database_url,
            default_retention_days=retention_days,
            environment=environment,
            database_pool_min_size=pool_min_size,
            database_pool_max_size=pool_max_size,
            database_pool_timeout_seconds=pool_timeout,
            database_connect_timeout_seconds=connect_timeout,
            cors_allowed_origins=cors_allowed_origins,
            phone_hash_key=phone_hash_key,
            metrics_token=metrics_token,
            api_requests_per_minute=api_requests_per_minute,
            worker_api_requests_per_minute=worker_api_requests_per_minute,
            worker_source_requests_per_minute=worker_source_requests_per_minute,
            webhook_requests_per_minute=webhook_requests_per_minute,
            embed_tokens_per_minute=embed_tokens_per_minute,
            authentication=AuthenticationSettings.from_env(environment),
        )
