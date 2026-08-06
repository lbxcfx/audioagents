#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

from dotenv import load_dotenv
from livekit import api
from minio import Minio


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "qwen-telephony"


def load_environment() -> None:
    load_dotenv(ROOT / ".env", override=True)
    load_dotenv(APP / "config" / "dev.env", override=True)
    load_dotenv(APP / "config" / "local.env", override=True)


async def wait_for_egress(
    livekit: api.LiveKitAPI,
    egress_id: str,
    *,
    wanted: set[int],
    timeout_seconds: float,
) -> api.EgressInfo:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = await livekit.egress.list_egress(api.ListEgressRequest(egress_id=egress_id))
        info = next((item for item in response.items if item.egress_id == egress_id), None)
        if info is not None:
            if int(info.status) in wanted:
                return info
            if info.status in {
                api.EgressStatus.EGRESS_FAILED,
                api.EgressStatus.EGRESS_ABORTED,
                api.EgressStatus.EGRESS_LIMIT_REACHED,
            }:
                name = api.EgressStatus.Name(int(info.status))
                raise RuntimeError(f"egress ended unsuccessfully: {name}: {info.error}")
        await asyncio.sleep(0.25)
    raise TimeoutError(f"egress {egress_id} did not reach the requested state")


def download_recording(bucket: str, object_name: str, destination: Path) -> None:
    endpoint = os.getenv("QWEN_RECORDING_S3_PUBLIC_ENDPOINT", "http://127.0.0.1:9000")
    parsed = urlsplit(endpoint)
    client = Minio(
        parsed.netloc or parsed.path,
        access_key=os.environ["QWEN_RECORDING_S3_ACCESS_KEY"],
        secret_key=os.environ["QWEN_RECORDING_S3_SECRET"],
        secure=parsed.scheme == "https",
        region=os.getenv("QWEN_RECORDING_S3_REGION", "us-east-1"),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    client.fget_object(bucket, object_name, str(destination))


async def main() -> int:
    parser = argparse.ArgumentParser(description="Place and archive a recorded blind audio A/B call")
    parser.add_argument("phone")
    parser.add_argument("--max-seconds", type=int, default=300)
    args = parser.parse_args()

    load_environment()
    livekit = api.LiveKitAPI(
        os.getenv("LIVEKIT_HTTP_URL", "http://127.0.0.1:7880"),
        os.getenv("LIVEKIT_API_KEY", "devkey"),
        os.getenv("LIVEKIT_API_SECRET", "secret"),
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    room = f"audio-ab-{uuid.uuid4().hex[:10]}"
    identity = "xiaoxu-dinner-callee"
    bucket = os.getenv("QWEN_RECORDING_S3_BUCKET", "audioagents-recordings")
    prefix = os.getenv("QWEN_RECORDING_S3_PREFIX", "telephony-recordings").strip("/")
    object_name = f"{prefix}/audio-ab-tests/{timestamp}-{room}.mp3"
    local_dir = APP / "data" / "recordings" / "audio-ab-tests"
    local_recording = local_dir / f"{room}.mp3"
    transcript = APP / "data" / "transcripts" / f"{room}.jsonl"
    metadata_path = local_dir / f"{room}.json"
    egress_id = ""
    call_id = ""
    outcome = "unknown"

    metadata: dict[str, object] = {
        "room": room,
        "target": args.phone,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "opening_greeting": "您好，请问是晓旭老师吗？",
        "experiment": "same-pcm-randomized-playback-path",
        "blinded": True,
        "recording_object": f"s3://{bucket}/{object_name}",
        "local_recording": str(local_recording),
        "transcript": str(transcript),
    }

    print(f"ROOM_CREATE room={room}", flush=True)
    try:
        await livekit.room.create_room(api.CreateRoomRequest(name=room))
        participant = await livekit.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=room,
                sip_trunk_id="ST_Dg6YGoNir6S5",
                sip_number="83450325",
                sip_call_to=args.phone,
                participant_identity=identity,
                participant_name="晓旭老师",
                wait_until_answered=False,
                ringing_timeout=timedelta(seconds=45),
                max_call_duration=timedelta(seconds=max(60, args.max_seconds)),
                media=api.SIPMediaConfig(
                    codecs=[api.SIPCodec(name="PCMU", rate=8000)],
                    only_listed_codecs=True,
                    media_timeout=timedelta(seconds=10),
                ),
            )
        )
        call_id = str(participant.sip_call_id)
        metadata["call_id"] = call_id
        print(f"CALL_QUEUED call_id={call_id}", flush=True)

        upload = api.S3Upload(
            access_key=os.environ["QWEN_RECORDING_S3_ACCESS_KEY"],
            secret=os.environ["QWEN_RECORDING_S3_SECRET"],
            region=os.getenv("QWEN_RECORDING_S3_REGION", "us-east-1"),
            endpoint=os.getenv("QWEN_RECORDING_S3_ENDPOINT", "http://minio:9000"),
            bucket=bucket,
            force_path_style=os.getenv("QWEN_RECORDING_S3_FORCE_PATH_STYLE", "true").lower()
            in {"1", "true", "yes", "on"},
        )
        egress = await livekit.egress.start_room_composite_egress(
            api.RoomCompositeEgressRequest(
                room_name=room,
                audio_only=True,
                audio_mixing=api.AudioMixing.DUAL_CHANNEL_AGENT,
                advanced=api.EncodingOptions(
                    audio_codec=api.AudioCodec.AC_MP3,
                    audio_frequency=16_000,
                    audio_bitrate=64,
                ),
                file_outputs=[
                    api.EncodedFileOutput(
                        file_type=api.EncodedFileType.MP3,
                        filepath=object_name,
                        s3=upload,
                    )
                ],
            )
        )
        egress_id = str(egress.egress_id)
        metadata["egress_id"] = egress_id
        print(f"RECORDING_START egress_id={egress_id}", flush=True)
        await wait_for_egress(
            livekit,
            egress_id,
            wanted={int(api.EgressStatus.EGRESS_ACTIVE)},
            timeout_seconds=30,
        )
        print("RECORDING_ACTIVE", flush=True)
        answer_deadline = time.monotonic() + 45
        while time.monotonic() < answer_deadline:
            try:
                info = await livekit.room.get_participant(
                    api.RoomParticipantIdentity(room=room, identity=identity)
                )
            except Exception:
                outcome = "ended_before_answer"
                raise RuntimeError("SIP participant ended before answer")
            status = info.attributes.get("sip.callStatus", "unknown")
            if status == "active":
                metadata["answered_at"] = datetime.now(timezone.utc).isoformat()
                print(f"CALL_ANSWERED call_id={call_id}", flush=True)
                break
            if status in {"disconnected", "hangup"}:
                outcome = status
                raise RuntimeError(f"SIP call ended before answer: {status}")
            await asyncio.sleep(0.25)
        else:
            outcome = "answer_timeout"
            raise TimeoutError("SIP call was not answered within 45 seconds")

        deadline = time.monotonic() + args.max_seconds
        while time.monotonic() < deadline:
            try:
                info = await livekit.room.get_participant(
                    api.RoomParticipantIdentity(room=room, identity=identity)
                )
            except Exception:
                outcome = "remote_disconnected"
                break
            status = info.attributes.get("sip.callStatus", "unknown")
            if status in {"disconnected", "hangup"}:
                outcome = status
                break
            await asyncio.sleep(1)
        else:
            outcome = "max_duration"
    except Exception as exc:
        outcome = "failed"
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        print(f"CALL_FAILED {metadata['error']}", flush=True)
        raise
    finally:
        metadata["outcome"] = outcome
        metadata["ended_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await livekit.room.delete_room(api.DeleteRoomRequest(room=room))
        except Exception:
            pass
        if egress_id:
            try:
                await wait_for_egress(
                    livekit,
                    egress_id,
                    wanted={int(api.EgressStatus.EGRESS_COMPLETE)},
                    timeout_seconds=60,
                )
                print("RECORDING_COMPLETE", flush=True)
                await asyncio.to_thread(download_recording, bucket, object_name, local_recording)
                print(f"RECORDING_SAVED {local_recording}", flush=True)
            except Exception as exc:
                metadata["recording_error"] = f"{type(exc).__name__}: {exc}"
                print(f"RECORDING_SAVE_FAILED {metadata['recording_error']}", flush=True)
        await livekit.aclose()
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"METADATA_SAVED {metadata_path}", flush=True)
        print(f"TRANSCRIPT_PATH {transcript}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
