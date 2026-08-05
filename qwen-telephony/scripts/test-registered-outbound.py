#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import os
from pathlib import Path
import sys
import time
import uuid

from livekit import api

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from sip_registration import register_from_env  # noqa: E402


def load_root_env() -> None:
    root = Path(__file__).resolve().parents[2]
    for env_file in (root / ".env", root / "qwen-telephony/config/dev.env"):
        if not env_file.exists():
            continue
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


async def main() -> int:
    parser = argparse.ArgumentParser(description="REGISTER first, then place a LiveKit SIP test call")
    parser.add_argument("phone")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--trunk-id", default=os.getenv("QWEN_SIP_OUTBOUND_TRUNK_ID", ""))
    parser.add_argument(
        "--codec",
        choices=("PCMU", "PCMA", "G722", "AMR-WB"),
        help="Offer only this codec for an end-to-end negotiation test",
    )
    args = parser.parse_args()

    load_root_env()
    if not args.trunk_id:
        args.trunk_id = os.getenv("QWEN_SIP_OUTBOUND_TRUNK_ID", "")
    if not args.trunk_id:
        parser.error("--trunk-id or QWEN_SIP_OUTBOUND_TRUNK_ID is required")
    registration = await asyncio.to_thread(register_from_env)
    if registration is None:
        raise RuntimeError("QWEN_SIP_REGISTER_ENABLED is not enabled")
    print(f"REGISTER {registration.status_code} {registration.reason}; expires={registration.expires}s", flush=True)

    livekit = api.LiveKitAPI(
        os.getenv("LIVEKIT_HTTP_URL", "http://127.0.0.1:7880"),
        os.getenv("LIVEKIT_API_KEY", "devkey"),
        os.getenv("LIVEKIT_API_SECRET", "secret"),
    )
    room = f"registered-test-{uuid.uuid4().hex[:10]}"
    identity = "registered-test-callee"
    try:
        participant = await livekit.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=room,
                sip_trunk_id=args.trunk_id,
                sip_call_to=args.phone,
                sip_number=os.getenv("QWEN_SIP_SOURCE_NUMBER", "10745635"),
                participant_identity=identity,
                participant_name="Registered test call",
                wait_until_answered=False,
                ringing_timeout=timedelta(seconds=45),
                max_call_duration=timedelta(seconds=max(60, args.duration)),
                media=(
                    api.SIPMediaConfig(
                        codecs=[
                            api.SIPCodec(
                                name=args.codec,
                                rate=16_000 if args.codec == "AMR-WB" else 8_000,
                            )
                        ],
                        only_listed_codecs=True,
                        media_timeout=timedelta(seconds=60),
                    )
                    if args.codec
                    else None
                ),
            )
        )
        print(f"INVITE queued; call_id={participant.sip_call_id}; room={room}", flush=True)
        deadline = time.monotonic() + args.duration
        last_status = ""
        while time.monotonic() < deadline:
            try:
                info = await livekit.room.get_participant(
                    api.RoomParticipantIdentity(room=room, identity=identity)
                )
            except Exception as exc:
                print(f"participant ended: {exc}", flush=True)
                break
            status = info.attributes.get("sip.callStatus", "unknown")
            if status != last_status:
                print(f"SIP status: {status}", flush=True)
                last_status = status
            if status in {"disconnected", "hangup"}:
                break
            await asyncio.sleep(1)
    finally:
        try:
            await livekit.room.delete_room(api.DeleteRoomRequest(room=room))
        except Exception:
            pass
        await livekit.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
