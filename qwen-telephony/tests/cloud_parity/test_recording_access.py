from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.recording_access import presign_recording_uri


def test_https_recording_uri_is_already_browser_accessible() -> None:
    assert (
        presign_recording_uri("https://media.example.com/calls/1.ogg")
        == "https://media.example.com/calls/1.ogg"
    )


def test_s3_recording_uri_gets_short_lived_sigv4_url(monkeypatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "cn-north-1")
    monkeypatch.setenv(
        "CLOUD_PARITY_RECORDING_S3_ENDPOINT", "https://objects.example.com/s3"
    )

    signed = presign_recording_uri(
        "s3://voice-recordings/2026/07/call 1.ogg",
        ttl_seconds=300,
        now=datetime(2026, 7, 26, 1, 2, 3, tzinfo=timezone.utc),
    )
    parsed = urlparse(signed)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "objects.example.com"
    assert parsed.path == "/s3/voice-recordings/2026/07/call%201.ogg"
    assert query["X-Amz-Expires"] == ["300"]
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert len(query["X-Amz-Signature"][0]) == 64


def test_s3_recording_uri_requires_a_https_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("CLOUD_PARITY_RECORDING_S3_ENDPOINT", "http://objects.local")

    with pytest.raises(ValueError, match="https"):
        presign_recording_uri("s3://bucket/call.ogg")
