from __future__ import annotations

import re
from pathlib import Path
import sys

import pytest


AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import sip_registration


def test_register_from_carrier_specific_environment(monkeypatch) -> None:
    captured = {}

    def fake_register(**kwargs):
        captured.update(kwargs)
        return sip_registration.SIPRegistrationResult(200, "OK", "backup.example", 300)

    prefix = "QWEN_SIP_BACKUP_REGISTER"
    values = {
        "ENABLED": "true",
        "HOST": "192.0.2.10",
        "PORT": "25060",
        "USERNAME": "backup-user",
        "AUTH_USERNAME": "backup-auth",
        "PASSWORD": "backup-password",
        "DOMAIN": "backup.example",
        "CONTACT_HOST": "198.51.100.20",
        "CONTACT_PORT": "5066",
        "EXPIRES": "300",
        "TIMEOUT_SECONDS": "7",
    }
    for suffix, value in values.items():
        monkeypatch.setenv(f"{prefix}_{suffix}", value)
    monkeypatch.setattr(sip_registration, "register", fake_register)

    result = sip_registration.register_from_env(prefix)

    assert result and result.status_code == 200
    assert captured == {
        "host": "192.0.2.10",
        "port": 25060,
        "sip_username": "backup-user",
        "auth_username": "backup-auth",
        "password": "backup-password",
        "domain": "backup.example",
        "contact_host": "198.51.100.20",
        "contact_port": 5066,
        "expires": 300,
        "timeout": 7.0,
    }


def test_register_completes_digest_challenge(monkeypatch) -> None:
    requests: list[str] = []
    responses = iter(
        [
            (
                "SIP/2.0 401 Unauthorized\r\n"
                'WWW-Authenticate: Digest realm="cc.qingchuanyun.cn", '
                'nonce="abc123", algorithm=MD5, qop="auth"\r\n\r\n'
            ),
            "SIP/2.0 200 OK\r\nExpires: 300\r\n\r\n",
        ]
    )

    def exchange(host: str, port: int, payload: str, timeout: float) -> str:
        requests.append(payload)
        return next(responses)

    monkeypatch.setattr(sip_registration, "_udp_exchange", exchange)
    result = sip_registration.register(
        host="47.98.241.177",
        port=2060,
        sip_username="10745635",
        auth_username="10745635",
        password="test-password",
        domain="cc.qingchuanyun.cn",
        contact_host="120.55.185.55",
        contact_port=5066,
    )

    assert result.status_code == 200
    assert result.realm == "cc.qingchuanyun.cn"
    assert len(requests) == 2
    assert "REGISTER sip:cc.qingchuanyun.cn SIP/2.0" in requests[0]
    assert "Contact: <sip:10745635@120.55.185.55:5066;transport=udp>" in requests[0]
    assert 'Authorization: Digest username="10745635"' in requests[1]
    assert 'realm="cc.qingchuanyun.cn"' in requests[1]
    assert re.search(r'response="[0-9a-f]{32}"', requests[1])
    assert "test-password" not in requests[1]


def test_register_rejects_failed_authenticated_request(monkeypatch) -> None:
    responses = iter(
        [
            (
                "SIP/2.0 401 Unauthorized\r\n"
                'WWW-Authenticate: Digest realm="cc.qingchuanyun.cn", '
                'nonce="abc123", qop="auth"\r\n\r\n'
            ),
            "SIP/2.0 403 Forbidden\r\n\r\n",
        ]
    )
    monkeypatch.setattr(
        sip_registration, "_udp_exchange", lambda *args, **kwargs: next(responses)
    )

    with pytest.raises(sip_registration.SIPRegistrationError, match="403 Forbidden"):
        sip_registration.register(
            host="47.98.241.177",
            port=2060,
            sip_username="10745635",
            auth_username="10745635",
            password="wrong",
            domain="cc.qingchuanyun.cn",
            contact_host="120.55.185.55",
            contact_port=5066,
        )
