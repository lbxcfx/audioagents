from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import random
import re
import socket
import uuid


class SIPRegistrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SIPRegistrationResult:
    status_code: int
    reason: str
    realm: str
    expires: int


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _digest_params(value: str) -> dict[str, str]:
    value = re.sub(r"^Digest\s+", "", value.strip(), flags=re.I)
    return {
        key.lower(): (quoted if quoted is not None else token.strip())
        for key, quoted, token in re.findall(
            r'(\w+)\s*=\s*(?:"([^"]*)"|([^,]+))', value
        )
    }


def _digest_authorization(
    challenge: dict[str, str], username: str, password: str, uri: str
) -> str:
    try:
        realm = challenge["realm"]
        nonce = challenge["nonce"]
    except KeyError as exc:
        raise SIPRegistrationError("REGISTER challenge is missing realm or nonce") from exc
    algorithm = challenge.get("algorithm", "MD5") or "MD5"
    if algorithm.lower() not in {"md5", "md5-sess"}:
        raise SIPRegistrationError(f"unsupported REGISTER digest algorithm: {algorithm}")
    qop_options = {
        item.strip().lower() for item in challenge.get("qop", "").split(",") if item.strip()
    }
    if qop_options and "auth" not in qop_options:
        raise SIPRegistrationError("REGISTER challenge does not support qop=auth")
    qop = "auth" if "auth" in qop_options else ""
    cnonce = uuid.uuid4().hex[:16]
    nc = "00000001"
    ha1 = _md5(f"{username}:{realm}:{password}")
    if algorithm.lower() == "md5-sess":
        ha1 = _md5(f"{ha1}:{nonce}:{cnonce}")
    ha2 = _md5(f"REGISTER:{uri}")
    response = (
        _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        if qop
        else _md5(f"{ha1}:{nonce}:{ha2}")
    )
    fields = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
        f"algorithm={algorithm}",
    ]
    if challenge.get("opaque"):
        fields.append(f'opaque="{challenge["opaque"]}"')
    if qop:
        fields.extend([f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"'])
    return "Digest " + ", ".join(fields)


def _status(response: str) -> tuple[int, str]:
    first = response.splitlines()[0] if response else ""
    match = re.match(r"SIP/2.0\s+(\d+)\s*(.*)", first)
    if not match:
        raise SIPRegistrationError(f"invalid REGISTER response: {first or 'empty'}")
    return int(match.group(1)), match.group(2).strip()


def _challenge(response: str) -> tuple[str, dict[str, str]]:
    match = re.search(r"^WWW-Authenticate:\s*(.+)$", response, re.I | re.M)
    if match:
        return "Authorization", _digest_params(match.group(1))
    match = re.search(r"^Proxy-Authenticate:\s*(.+)$", response, re.I | re.M)
    if match:
        return "Proxy-Authorization", _digest_params(match.group(1))
    raise SIPRegistrationError("REGISTER authentication challenge header is missing")


def _udp_exchange(host: str, port: int, payload: str, timeout: float) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(payload.encode(), (host, port))
            data, _ = sock.recvfrom(65535)
        except (OSError, TimeoutError) as exc:
            raise SIPRegistrationError(f"REGISTER transport failed: {type(exc).__name__}") from exc
    return data.decode(errors="replace")


def _request(
    *,
    register_uri: str,
    sip_username: str,
    domain: str,
    contact_host: str,
    contact_port: int,
    call_id: str,
    cseq: int,
    expires: int,
    auth_header: tuple[str, str] | None = None,
) -> str:
    branch = "z9hG4bK" + uuid.uuid4().hex[:16]
    tag = f"{random.randrange(10**8):08d}"
    lines = [
        f"REGISTER {register_uri} SIP/2.0",
        f"Via: SIP/2.0/UDP {contact_host}:{contact_port};branch={branch};rport",
        "Max-Forwards: 70",
        f"From: <sip:{sip_username}@{domain}>;tag={tag}",
        f"To: <sip:{sip_username}@{domain}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} REGISTER",
        (
            f"Contact: <sip:{sip_username}@{contact_host}:{contact_port};transport=udp>"
            f";expires={expires}"
        ),
        f"Expires: {expires}",
        "User-Agent: audioagents-register/1.0",
    ]
    if auth_header:
        lines.append(f"{auth_header[0]}: {auth_header[1]}")
    lines.extend(["Content-Length: 0", "", ""])
    return "\r\n".join(lines)


def register(
    *,
    host: str,
    port: int,
    sip_username: str,
    auth_username: str,
    password: str,
    domain: str,
    contact_host: str,
    contact_port: int,
    expires: int = 300,
    timeout: float = 5.0,
) -> SIPRegistrationResult:
    if not all((host, sip_username, auth_username, password, domain, contact_host)):
        raise SIPRegistrationError("REGISTER configuration is incomplete")
    if not 1 <= port <= 65535 or not 1 <= contact_port <= 65535:
        raise SIPRegistrationError("REGISTER port is invalid")
    if not 30 <= expires <= 3600:
        raise SIPRegistrationError("REGISTER expiry must be between 30 and 3600 seconds")
    register_uri = f"sip:{domain}"
    call_id = f"{uuid.uuid4().hex}@{contact_host}"
    initial = _udp_exchange(
        host,
        port,
        _request(
            register_uri=register_uri,
            sip_username=sip_username,
            domain=domain,
            contact_host=contact_host,
            contact_port=contact_port,
            call_id=call_id,
            cseq=1,
            expires=expires,
        ),
        timeout,
    )
    initial_code, initial_reason = _status(initial)
    if initial_code == 200:
        return SIPRegistrationResult(initial_code, initial_reason, domain, expires)
    if initial_code not in {401, 407}:
        raise SIPRegistrationError(
            f"REGISTER rejected before authentication: {initial_code} {initial_reason}"
        )
    header_name, challenge = _challenge(initial)
    auth = _digest_authorization(challenge, auth_username, password, register_uri)
    authenticated = _udp_exchange(
        host,
        port,
        _request(
            register_uri=register_uri,
            sip_username=sip_username,
            domain=domain,
            contact_host=contact_host,
            contact_port=contact_port,
            call_id=call_id,
            cseq=2,
            expires=expires,
            auth_header=(header_name, auth),
        ),
        timeout,
    )
    status_code, reason = _status(authenticated)
    if status_code != 200:
        raise SIPRegistrationError(f"authenticated REGISTER failed: {status_code} {reason}")
    return SIPRegistrationResult(
        status_code=status_code,
        reason=reason,
        realm=challenge.get("realm", domain),
        expires=expires,
    )


def register_from_env(env_prefix: str = "QWEN_SIP_REGISTER") -> SIPRegistrationResult | None:
    """Register using one isolated environment-variable profile.

    ``env_prefix`` defaults to the legacy global profile.  Carrier-specific
    profiles (for example ``QWEN_SIP_QINGSHANYUN_REGISTER``) let an outbound
    worker select the credentials that belong to the chosen trunk without
    changing the primary carrier configuration.
    """

    enabled = os.getenv(f"{env_prefix}_ENABLED", "false").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    return register(
        host=os.getenv(f"{env_prefix}_HOST", "").strip(),
        port=int(os.getenv(f"{env_prefix}_PORT", "5060")),
        sip_username=os.getenv(f"{env_prefix}_USERNAME", "").strip(),
        auth_username=os.getenv(
            f"{env_prefix}_AUTH_USERNAME",
            os.getenv(f"{env_prefix}_USERNAME", ""),
        ).strip(),
        password=os.getenv(f"{env_prefix}_PASSWORD", ""),
        domain=os.getenv(f"{env_prefix}_DOMAIN", "").strip(),
        contact_host=os.getenv(f"{env_prefix}_CONTACT_HOST", "").strip(),
        contact_port=int(
            os.getenv(f"{env_prefix}_CONTACT_PORT", os.getenv("SIP_PORT", "5065"))
        ),
        expires=int(os.getenv(f"{env_prefix}_EXPIRES", "300")),
        timeout=float(os.getenv(f"{env_prefix}_TIMEOUT_SECONDS", "5")),
    )
