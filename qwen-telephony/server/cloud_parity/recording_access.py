from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import os
from urllib.parse import quote, urlencode, urlparse


def _sign(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def validate_recording_storage_uri(storage_uri: str) -> str:
    uri = storage_uri.strip()
    if not uri:
        return ""
    if len(uri) > 1000 or any(ord(char) < 32 for char in uri):
        raise ValueError("invalid recording storage URI")
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        if not parsed.netloc or not parsed.path.strip("/") or parsed.query or parsed.fragment:
            raise ValueError("recording S3 URI must identify one object")
        return uri
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("recording storage URI must use https or s3")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("recording HTTPS URI must not contain credentials or fragments")
    allowed_hosts = {
        host.strip().lower()
        for host in os.getenv("CLOUD_PARITY_RECORDING_HTTPS_HOSTS", "").split(",")
        if host.strip()
    }
    environment = os.getenv("CLOUD_PARITY_ENV", "development").strip().lower()
    if environment in {"staging", "production"} and not allowed_hosts:
        raise ValueError(
            "direct HTTPS recording URIs require CLOUD_PARITY_RECORDING_HTTPS_HOSTS"
        )
    if allowed_hosts and parsed.hostname.lower() not in allowed_hosts:
        raise ValueError("recording HTTPS host is not allowlisted")
    return uri


def presign_recording_uri(
    storage_uri: str,
    *,
    ttl_seconds: int = 300,
    now: datetime | None = None,
) -> str:
    """Return a short-lived GET URL for HTTPS or an S3-compatible object URI."""

    uri = validate_recording_storage_uri(storage_uri)
    if uri.startswith("https://"):
        return uri
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError("recording storage URI must use https or s3")

    access_key = os.getenv(
        "QWEN_RECORDING_S3_ACCESS_KEY", os.getenv("AWS_ACCESS_KEY_ID", "")
    ).strip()
    secret_key = os.getenv(
        "QWEN_RECORDING_S3_SECRET", os.getenv("AWS_SECRET_ACCESS_KEY", "")
    ).strip()
    region = os.getenv(
        "QWEN_RECORDING_S3_REGION",
        os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1")),
    ).strip()
    endpoint = os.getenv(
        "QWEN_RECORDING_S3_ENDPOINT",
        os.getenv("CLOUD_PARITY_RECORDING_S3_ENDPOINT", ""),
    ).strip()
    if not access_key or not secret_key or not endpoint:
        raise RuntimeError(
            "recording access requires QWEN_RECORDING_S3_ACCESS_KEY, "
            "QWEN_RECORDING_S3_SECRET, and QWEN_RECORDING_S3_ENDPOINT "
            "(or the corresponding AWS environment variables)"
        )
    if not 30 <= int(ttl_seconds) <= 3600:
        raise ValueError("recording access TTL must be between 30 and 3600 seconds")

    endpoint_url = urlparse(endpoint.rstrip("/"))
    if endpoint_url.scheme != "https" or not endpoint_url.netloc:
        raise ValueError("recording S3 endpoint must be an https URL")
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = timestamp.strftime("%Y%m%d")
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    bucket = parsed.netloc
    object_key = parsed.path.lstrip("/")
    base_path = endpoint_url.path.rstrip("/")
    canonical_uri = quote(f"{base_path}/{bucket}/{object_key}" or "/", safe="/-_.~")
    query: dict[str, str] = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(ttl_seconds)),
        "X-Amz-SignedHeaders": "host",
    }
    session_token = os.getenv(
        "QWEN_RECORDING_S3_SESSION_TOKEN", os.getenv("AWS_SESSION_TOKEN", "")
    ).strip()
    if session_token:
        query["X-Amz-Security-Token"] = session_token
    canonical_query = urlencode(
        sorted(query.items()),
        quote_via=quote,
        safe="-_.~",
    )
    canonical_headers = f"host:{endpoint_url.netloc}\n"
    canonical_request = "\n".join(
        [
            "GET",
            canonical_uri,
            canonical_query,
            canonical_headers,
            "host",
            "UNSIGNED-PAYLOAD",
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    date_key = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    region_key = _sign(date_key, region)
    service_key = _sign(region_key, "s3")
    signing_key = _sign(service_key, "aws4_request")
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return (
        f"{endpoint_url.scheme}://{endpoint_url.netloc}{canonical_uri}"
        f"?{canonical_query}&X-Amz-Signature={signature}"
    )
