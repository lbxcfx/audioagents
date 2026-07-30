from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Mapping

from .metadata import InboundMetadataSigner
from .store import InboundAgentStore


class InboundAgentService:
    def __init__(
        self,
        store: InboundAgentStore,
        signer: InboundMetadataSigner,
        *,
        public_hash_key: str,
        public_calls_per_day: int = 3,
        public_seconds_per_day: int = 600,
        public_session_seconds: int = 180,
        public_max_concurrent_sessions: int = 20,
        enterprise_max_concurrent_sessions: int = 100,
    ):
        if len(public_hash_key) < 16:
            raise ValueError("public hash key must contain at least 16 characters")
        self.store = store
        self.signer = signer
        self.public_hash_key = public_hash_key
        self.public_calls_per_day = public_calls_per_day
        self.public_seconds_per_day = public_seconds_per_day
        self.public_session_seconds = public_session_seconds
        self.public_max_concurrent_sessions = public_max_concurrent_sessions
        self.enterprise_max_concurrent_sessions = enterprise_max_concurrent_sessions

    def subject_hash(self, subject: str) -> str:
        return hashlib.sha256(f"{self.public_hash_key}:{subject}".encode("utf-8")).hexdigest()

    def public_demo_info(self) -> dict[str, Any]:
        binding = self.store.find_public_binding()
        phone = self.store.find_public_phone_binding()
        return {
            "available": True,
            "name": binding["name"],
            "description": binding["description"],
            "max_duration_seconds": self.public_session_seconds,
            "recording_enabled": False,
            "browser_enabled": True,
            "public_number": phone["destination"] if phone else "",
            "tel_uri": f"tel:{phone['destination']}" if phone else "",
            "notice": "您正在与智能语音助手通话。请勿提供密码、验证码或其他敏感信息。",
        }

    def prepare_public_web_session(self, *, session_id: str, room_name: str) -> dict[str, Any]:
        binding = self.store.find_public_binding()
        claims = {
            "kind": "public_demo",
            "binding_id": binding["id"],
            "project_id": binding["project_id"],
            "agent_version_id": binding["agent_version_id"],
            "session_id": session_id,
            "room_name": room_name,
        }
        return {
            "dispatch_metadata": self.signer.sign(claims),
            "binding": {**binding, "session_id": session_id},
            "max_duration_seconds": self.public_session_seconds,
        }

    def commit_public_web_session(
        self,
        *,
        source: str,
        binding: Mapping[str, Any],
        room_name: str,
        provider_call_id: str,
    ) -> dict[str, Any]:
        return self.store.create_public_session(
            subject_hash=self.subject_hash(source),
            max_calls_per_day=self.public_calls_per_day,
            max_total_seconds=self.public_seconds_per_day,
            binding=binding,
            room_name=room_name,
            provider_call_id=provider_call_id,
            max_concurrent_sessions=self.public_max_concurrent_sessions,
        )

    def resolve_runtime(
        self,
        token: str,
        *,
        observed_room_name: str = "",
        provider_call_id: str = "",
    ) -> dict[str, Any]:
        claims = self.signer.verify(token)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=self.signer.max_age_seconds)
        ).isoformat().replace("+00:00", "Z")
        self.store.consume_nonce(nonce=str(claims["nonce"]), expires_at=expires_at)
        binding = self.store.resolve_binding(binding_id=str(claims["binding_id"]))
        for field in ("project_id", "agent_version_id", "kind"):
            if str(binding[field]) != str(claims[field]):
                raise ValueError("metadata does not match the active binding")
        claimed_room = str(claims.get("room_name") or "")
        if claimed_room and observed_room_name and claimed_room != observed_room_name:
            raise ValueError("metadata room does not match the observed room")
        session = None
        if claims.get("session_id"):
            session = self.store.activate_session(
                session_id=str(claims["session_id"]),
                binding_id=str(binding["id"]),
                room_name=observed_room_name or claimed_room,
                provider_call_id=provider_call_id,
            )
        version = self.store.get_runtime_version(
            project_id=str(binding["project_id"]),
            version_id=str(binding["agent_version_id"]),
        )
        config = dict(version["config"])
        if binding["kind"] == "public_demo":
            config["tools"] = []
            config["knowledge_sources"] = []
            config["content_sources"] = []
        return {
            "project_id": binding["project_id"],
            "agent_id": binding["agent_id"],
            "agent_version_id": binding["agent_version_id"],
            "binding_id": binding["id"],
            "kind": binding["kind"],
            "config_sha256": version["config_sha256"],
            "config": config,
            "session_id": session["id"] if session else "",
            "max_duration_seconds": min(
                int(config.get("max_duration_seconds") or self.public_session_seconds),
                self.public_session_seconds if binding["kind"] == "public_demo" else 7_200,
            ),
        }

    def admit_sip(
        self,
        *,
        trunk_id: str,
        called_number: str,
        caller_number: str,
        room_name: str,
        provider_call_id: str,
    ) -> dict[str, Any]:
        binding = self.store.resolve_sip_binding(trunk_id=trunk_id, called_number=called_number)
        caller_digits = "".join(filter(str.isdigit, caller_number))
        caller_hash = self.subject_hash(caller_number or provider_call_id)
        if binding["kind"] == "public_demo":
            session = self.store.create_public_session(
                subject_hash=caller_hash,
                max_calls_per_day=self.public_calls_per_day,
                max_total_seconds=self.public_seconds_per_day,
                binding=binding,
                room_name=room_name,
                provider_call_id=provider_call_id,
                max_concurrent_sessions=self.public_max_concurrent_sessions,
            )
            session_id = session["session_id"]
            self.store.activate_session(
                session_id=session_id,
                binding_id=str(binding["id"]),
                room_name=room_name,
                provider_call_id=provider_call_id,
            )
        else:
            session = self.store.create_enterprise_session(
                binding=binding,
                room_name=room_name,
                provider_call_id=provider_call_id,
                caller_hash=caller_hash,
                caller_last4=caller_digits[-4:],
                max_concurrent_sessions=self.enterprise_max_concurrent_sessions,
            )
            session_id = session["id"]
        version = self.store.get_runtime_version(
            project_id=str(binding["project_id"]), version_id=str(binding["agent_version_id"])
        )
        config = dict(version["config"])
        if binding["kind"] == "public_demo":
            config["tools"] = []
            config["knowledge_sources"] = []
            config["content_sources"] = []
        return {
            "project_id": binding["project_id"],
            "agent_id": binding["agent_id"],
            "agent_version_id": binding["agent_version_id"],
            "binding_id": binding["id"],
            "kind": binding["kind"],
            "session_id": session_id,
            "config_sha256": version["config_sha256"],
            "config": config,
            "max_duration_seconds": min(
                int(config.get("max_duration_seconds") or 600),
                self.public_session_seconds if binding["kind"] == "public_demo" else 7_200,
            ),
        }
