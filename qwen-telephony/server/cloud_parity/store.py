from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
import uuid

from .database import ConnectionLike, create_database_adapter


class ResourceNotFoundError(LookupError):
    pass


class AccessDeniedError(PermissionError):
    pass


class MigrationDriftError(RuntimeError):
    pass


ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset(
        {
            "project.read", "project.manage", "audit.read", "session.read",
            "session.write", "console.control", "console.observe", "agent.read", "agent.write",
            "inference.invoke", "inference.manage", "analytics.read", "analytics.export",
            "telephony.read", "telephony.manage", "telephony.operate",
        }
    ),
    "admin": frozenset(
        {
            "project.read", "project.manage", "audit.read", "session.read",
            "session.write", "console.control", "console.observe", "agent.read", "agent.write",
            "inference.invoke", "inference.manage", "analytics.read", "analytics.export",
            "telephony.read", "telephony.manage", "telephony.operate",
        }
    ),
    "member": frozenset(
        {
            "project.read", "session.read", "session.write", "console.control",
            "agent.read", "agent.write", "inference.invoke",
            "analytics.read",
            "telephony.read", "telephony.operate",
        }
    ),
    "viewer": frozenset(
        {"project.read", "session.read", "agent.read", "analytics.read", "telephony.read"}
    ),
}


MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            retention_days INTEGER NOT NULL DEFAULT 30,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_memberships (
            project_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (project_id, user_id),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_memberships_user
            ON project_memberships(user_id, project_id);
        CREATE INDEX IF NOT EXISTS idx_audit_project_created
            ON audit_logs(project_id, created_at DESC, id DESC);
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS platform_resources (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            spec_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, kind, name)
        );

        CREATE INDEX IF NOT EXISTS idx_resources_project_kind
            ON platform_resources(project_id, kind, updated_at DESC);
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS agent_sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            room_name TEXT NOT NULL,
            agent_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            ended_at TEXT,
            retention_until TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS session_events (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
            UNIQUE(session_id, sequence)
        );

        CREATE TABLE IF NOT EXISTS usage_records (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            category TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            quantity REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL,
            cost_usd REAL NOT NULL DEFAULT 0,
            latency_ms REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_project_started
            ON agent_sessions(project_id, started_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_events_session_sequence
            ON session_events(session_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_usage_session_created
            ON usage_records(session_id, created_at, id);
        """,
    ),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS console_commands (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            command_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            result_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_console_commands_session
            ON console_commands(session_id, created_at, id);
        """,
    ),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS agent_definitions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, name)
        );

        CREATE TABLE IF NOT EXISTS agent_builds (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            image_ref TEXT NOT NULL,
            status TEXT NOT NULL,
            logs TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(agent_id) REFERENCES agent_definitions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS agent_versions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            build_id TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            image_ref TEXT NOT NULL,
            spec_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(agent_id) REFERENCES agent_definitions(id) ON DELETE CASCADE,
            FOREIGN KEY(build_id) REFERENCES agent_builds(id) ON DELETE CASCADE,
            UNIQUE(agent_id, version_number)
        );

        CREATE TABLE IF NOT EXISTS agent_deployments (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            active_version_id TEXT,
            previous_version_id TEXT,
            desired_replicas INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(agent_id) REFERENCES agent_definitions(id) ON DELETE CASCADE,
            FOREIGN KEY(active_version_id) REFERENCES agent_versions(id),
            FOREIGN KEY(previous_version_id) REFERENCES agent_versions(id),
            UNIQUE(project_id, name)
        );

        CREATE TABLE IF NOT EXISTS deployment_revisions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            deployment_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(deployment_id) REFERENCES agent_deployments(id) ON DELETE CASCADE,
            FOREIGN KEY(version_id) REFERENCES agent_versions(id)
        );

        CREATE TABLE IF NOT EXISTS encrypted_secrets (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            ciphertext TEXT NOT NULL,
            value_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, name)
        );

        CREATE INDEX IF NOT EXISTS idx_builds_agent_created
            ON agent_builds(agent_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_versions_agent_number
            ON agent_versions(agent_id, version_number DESC);
        CREATE INDEX IF NOT EXISTS idx_revisions_deployment_created
            ON deployment_revisions(deployment_id, created_at DESC);
        """,
    ),
    (
        6,
        """
        CREATE TABLE IF NOT EXISTS agent_specs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'draft',
            spec_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, name)
        );

        CREATE TABLE IF NOT EXISTS agent_spec_revisions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            agent_spec_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            spec_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(agent_spec_id) REFERENCES agent_specs(id) ON DELETE CASCADE,
            UNIQUE(agent_spec_id, revision)
        );

        CREATE INDEX IF NOT EXISTS idx_agent_specs_project_updated
            ON agent_specs(project_id, updated_at DESC, id DESC);
        """,
    ),
    (
        7,
        """
        CREATE TABLE IF NOT EXISTS embed_configs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            room_prefix TEXT NOT NULL,
            allowed_origins_json TEXT NOT NULL,
            capabilities_json TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, name)
        );

        CREATE INDEX IF NOT EXISTS idx_embed_configs_project
            ON embed_configs(project_id, updated_at DESC);
        """,
    ),
    (
        8,
        """
        CREATE TABLE IF NOT EXISTS model_routes (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            descriptor TEXT NOT NULL,
            modality TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_model TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 100,
            timeout_seconds REAL NOT NULL DEFAULT 30,
            enabled INTEGER NOT NULL DEFAULT 1,
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, descriptor, provider, provider_model)
        );

        CREATE TABLE IF NOT EXISTS inference_attempts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_id TEXT,
            descriptor TEXT NOT NULL,
            modality TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_model TEXT NOT NULL,
            status TEXT NOT NULL,
            latency_ms REAL NOT NULL,
            error_type TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_model_routes_resolve
            ON model_routes(project_id, descriptor, modality, enabled, priority);
        CREATE INDEX IF NOT EXISTS idx_inference_attempts_project_created
            ON inference_attempts(project_id, created_at DESC);
        """,
    ),
    (
        9,
        """
        CREATE TABLE IF NOT EXISTS runtime_instances (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            deployment_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            replica_index INTEGER NOT NULL,
            runtime_kind TEXT NOT NULL,
            runtime_name TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            ready_at TEXT,
            stopped_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(deployment_id) REFERENCES agent_deployments(id) ON DELETE CASCADE,
            FOREIGN KEY(version_id) REFERENCES agent_versions(id),
            UNIQUE(runtime_kind, runtime_name)
        );

        CREATE TABLE IF NOT EXISTS runtime_logs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            deployment_id TEXT NOT NULL,
            instance_id TEXT,
            sequence INTEGER NOT NULL,
            stream TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(deployment_id) REFERENCES agent_deployments(id) ON DELETE CASCADE,
            FOREIGN KEY(instance_id) REFERENCES runtime_instances(id) ON DELETE SET NULL,
            UNIQUE(deployment_id, sequence)
        );

        CREATE INDEX IF NOT EXISTS idx_runtime_instances_deployment
            ON runtime_instances(deployment_id, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_runtime_logs_cursor
            ON runtime_logs(deployment_id, sequence);
        """,
    ),
    (
        10,
        """
        CREATE TABLE IF NOT EXISTS telephony_limits (
            project_id TEXT PRIMARY KEY,
            max_concurrent_calls INTEGER NOT NULL DEFAULT 100,
            max_outbound_calls INTEGER NOT NULL DEFAULT 80,
            max_inbound_calls INTEGER NOT NULL DEFAULT 80,
            max_calls_per_minute INTEGER NOT NULL DEFAULT 60,
            lease_seconds INTEGER NOT NULL DEFAULT 30,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS sip_trunks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            direction TEXT NOT NULL,
            provider TEXT NOT NULL,
            livekit_trunk_id TEXT NOT NULL DEFAULT '',
            secret_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            numbers_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, name)
        );

        CREATE TABLE IF NOT EXISTS call_jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            source_number TEXT NOT NULL DEFAULT '',
            destination_number TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            trunk_id TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            priority INTEGER NOT NULL DEFAULT 100,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            available_at TEXT NOT NULL,
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_token TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT,
            provider_call_id TEXT NOT NULL DEFAULT '',
            room_name TEXT NOT NULL DEFAULT '',
            failure_code TEXT NOT NULL DEFAULT '',
            failure_detail TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            answered_at TEXT,
            ended_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(trunk_id) REFERENCES sip_trunks(id) ON DELETE SET NULL,
            UNIQUE(project_id, idempotency_key)
        );

        CREATE TABLE IF NOT EXISTS call_attempts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            lease_token TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            failure_code TEXT NOT NULL DEFAULT '',
            failure_detail TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(call_id) REFERENCES call_jobs(id) ON DELETE CASCADE,
            UNIQUE(call_id, attempt_number)
        );

        CREATE TABLE IF NOT EXISTS call_events (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(call_id) REFERENCES call_jobs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_call_jobs_dispatch
            ON call_jobs(project_id, direction, status, available_at, priority, created_at);
        CREATE INDEX IF NOT EXISTS idx_call_jobs_active
            ON call_jobs(project_id, direction, status, lease_expires_at);
        CREATE INDEX IF NOT EXISTS idx_call_attempts_rate
            ON call_attempts(project_id, started_at);
        CREATE INDEX IF NOT EXISTS idx_call_events_call
            ON call_events(call_id, created_at, id);
        CREATE INDEX IF NOT EXISTS idx_sip_trunks_project
            ON sip_trunks(project_id, direction, status);
        """,
    ),
    (
        11,
        """
        ALTER TABLE call_jobs
            ADD COLUMN destination_hash TEXT NOT NULL DEFAULT '';

        CREATE TABLE IF NOT EXISTS telephony_policies (
            project_id TEXT PRIMARY KEY,
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            allowed_weekdays_json TEXT NOT NULL DEFAULT '[0,1,2,3,4]',
            calling_window_start TEXT NOT NULL DEFAULT '09:00',
            calling_window_end TEXT NOT NULL DEFAULT '18:00',
            require_consent INTEGER NOT NULL DEFAULT 1,
            consent_purpose TEXT NOT NULL DEFAULT 'outbound',
            max_attempts_per_number_per_day INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS do_not_call_entries (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            phone_hash TEXT NOT NULL,
            phone_last4 TEXT NOT NULL,
            reason TEXT NOT NULL,
            source TEXT NOT NULL,
            expires_at TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, phone_hash)
        );

        CREATE TABLE IF NOT EXISTS consent_records (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            phone_hash TEXT NOT NULL,
            phone_last4 TEXT NOT NULL,
            purpose TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_ref TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS compliance_decisions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            call_id TEXT,
            phone_hash TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(call_id) REFERENCES call_jobs(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_call_jobs_destination_hash
            ON call_jobs(project_id, destination_hash, created_at);
        CREATE INDEX IF NOT EXISTS idx_dnc_project_created
            ON do_not_call_entries(project_id, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_consent_lookup
            ON consent_records(project_id, phone_hash, purpose, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_compliance_call
            ON compliance_decisions(call_id, created_at, id);
        """,
    ),
    (
        12,
        """
        ALTER TABLE call_jobs
            ADD COLUMN reconcile_started_at TEXT;
        ALTER TABLE call_jobs
            ADD COLUMN reconcile_attempt_count INTEGER NOT NULL DEFAULT 0;

        CREATE TABLE IF NOT EXISTS call_cdrs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'livekit-sip',
            provider_call_id TEXT NOT NULL DEFAULT '',
            sip_call_id TEXT NOT NULL DEFAULT '',
            room_name TEXT NOT NULL DEFAULT '',
            participant_identity TEXT NOT NULL DEFAULT '',
            sip_status TEXT NOT NULL DEFAULT '',
            disconnect_reason TEXT NOT NULL DEFAULT '',
            attributes_json TEXT NOT NULL DEFAULT '{}',
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            ended_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(call_id) REFERENCES call_jobs(id) ON DELETE CASCADE,
            UNIQUE(call_id)
        );

        CREATE TABLE IF NOT EXISTS telephony_webhook_events (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            event_type TEXT NOT NULL,
            project_id TEXT,
            call_id TEXT,
            outcome TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            received_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY(call_id) REFERENCES call_jobs(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_call_jobs_reconcile
            ON call_jobs(project_id, status, lease_expires_at, reconcile_started_at);
        CREATE INDEX IF NOT EXISTS idx_call_cdr_provider_call
            ON call_cdrs(project_id, provider_call_id);
        CREATE INDEX IF NOT EXISTS idx_call_cdr_room
            ON call_cdrs(project_id, room_name);
        CREATE INDEX IF NOT EXISTS idx_webhook_events_received
            ON telephony_webhook_events(received_at, id);
        """,
    ),
    (
        13,
        """
        CREATE TABLE IF NOT EXISTS transfer_destinations (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            target_uri TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'cold',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, name)
        );

        CREATE TABLE IF NOT EXISTS call_transfers (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            destination_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'requested',
            context_summary TEXT NOT NULL DEFAULT '',
            requested_by TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            failure_code TEXT NOT NULL DEFAULT '',
            failure_detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(call_id) REFERENCES call_jobs(id) ON DELETE CASCADE,
            FOREIGN KEY(destination_id) REFERENCES transfer_destinations(id),
            UNIQUE(call_id, idempotency_key)
        );

        CREATE INDEX IF NOT EXISTS idx_transfer_destinations_project
            ON transfer_destinations(project_id, status, name);
        CREATE INDEX IF NOT EXISTS idx_call_transfers_call
            ON call_transfers(call_id, created_at DESC, id DESC);
        """,
    ),
    (
        14,
        """
        ALTER TABLE telephony_policies
            ADD COLUMN inbound_overflow_mode TEXT NOT NULL DEFAULT 'reject';
        ALTER TABLE telephony_policies
            ADD COLUMN inbound_overflow_destination_name TEXT NOT NULL DEFAULT '';
        """,
    ),
    (
        15,
        """
        CREATE TABLE IF NOT EXISTS revoked_access_tokens (
            token_sha256 TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            reason TEXT NOT NULL,
            expires_at TEXT,
            revoked_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_rate_limit_windows (
            key_sha256 TEXT PRIMARY KEY,
            window_started_at TEXT NOT NULL,
            request_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expiry
            ON revoked_access_tokens(expires_at);
        CREATE INDEX IF NOT EXISTS idx_rate_windows_updated
            ON api_rate_limit_windows(updated_at);
        """,
    ),
    (
        16,
        """
        ALTER TABLE telephony_policies
            ADD COLUMN outbound_enabled INTEGER NOT NULL DEFAULT 1;

        ALTER TABLE sip_trunks
            ADD COLUMN max_concurrent_calls INTEGER NOT NULL DEFAULT 100;
        ALTER TABLE sip_trunks
            ADD COLUMN max_calls_per_second INTEGER NOT NULL DEFAULT 5;

        ALTER TABLE call_jobs
            ADD COLUMN campaign_id TEXT;

        CREATE TABLE IF NOT EXISTS telephony_contacts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            phone_number TEXT NOT NULL,
            phone_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, external_id)
        );

        CREATE TABLE IF NOT EXISTS telephony_campaigns (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            trunk_id TEXT,
            source_number TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            priority INTEGER NOT NULL DEFAULT 100,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            max_concurrent_calls INTEGER NOT NULL DEFAULT 10,
            scheduled_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(trunk_id) REFERENCES sip_trunks(id) ON DELETE SET NULL,
            UNIQUE(project_id, name)
        );

        CREATE TABLE IF NOT EXISTS telephony_campaign_contacts (
            campaign_id TEXT NOT NULL,
            contact_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            call_id TEXT,
            failure_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(campaign_id, contact_id),
            FOREIGN KEY(campaign_id) REFERENCES telephony_campaigns(id) ON DELETE CASCADE,
            FOREIGN KEY(contact_id) REFERENCES telephony_contacts(id) ON DELETE CASCADE,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(call_id) REFERENCES call_jobs(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_telephony_contacts_project
            ON telephony_contacts(project_id, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_telephony_campaigns_project
            ON telephony_campaigns(project_id, status, scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_campaign_contacts_status
            ON telephony_campaign_contacts(campaign_id, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_call_jobs_campaign
            ON call_jobs(project_id, campaign_id, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_call_jobs_trunk_active
            ON call_jobs(project_id, trunk_id, status, created_at);
        """,
    ),
    (
        17,
        """
        ALTER TABLE call_jobs
            ADD COLUMN answering_machine_category TEXT NOT NULL DEFAULT '';
        ALTER TABLE call_jobs
            ADD COLUMN disposition TEXT NOT NULL DEFAULT '';
        """,
    ),
    (
        18,
        """
        ALTER TABLE telephony_policies
            ADD COLUMN recording_mode TEXT NOT NULL DEFAULT 'off';
        ALTER TABLE telephony_policies
            ADD COLUMN recording_disclosure_text TEXT NOT NULL DEFAULT '';
        ALTER TABLE call_jobs
            ADD COLUMN recording_mode TEXT NOT NULL DEFAULT 'off';
        ALTER TABLE call_jobs
            ADD COLUMN recording_disclosure_text TEXT NOT NULL DEFAULT '';
        ALTER TABLE call_jobs
            ADD COLUMN recording_egress_id TEXT NOT NULL DEFAULT '';
        ALTER TABLE call_jobs
            ADD COLUMN recording_status TEXT NOT NULL DEFAULT '';
        ALTER TABLE call_jobs
            ADD COLUMN recording_storage_uri TEXT NOT NULL DEFAULT '';
        """,
    ),
    (
        19,
        """
        ALTER TABLE console_commands
            ADD COLUMN claimed_by TEXT NOT NULL DEFAULT '';
        ALTER TABLE console_commands
            ADD COLUMN lease_expires_at TEXT;

        CREATE INDEX IF NOT EXISTS idx_console_commands_claim
            ON console_commands(project_id, session_id, status, lease_expires_at, created_at);
        """,
    ),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _row(row: Mapping[str, Any] | Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not normalized:
        raise ValueError("project slug must contain a letter or number")
    return normalized


class PlatformStore:
    """Transactional control-plane store with an explicit tenant boundary."""

    def __init__(
        self,
        database_path: str | Path,
        default_retention_days: int = 30,
        *,
        database_url: str | None = None,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        pool_timeout_seconds: float = 10.0,
        connect_timeout_seconds: float = 10.0,
    ):
        self.database_path = Path(database_path).expanduser().resolve()
        self.default_retention_days = default_retention_days
        self._database = create_database_adapter(
            database_path=self.database_path,
            database_url=database_url,
            min_pool_size=min_pool_size,
            max_pool_size=max_pool_size,
            pool_timeout_seconds=pool_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
        )

    @property
    def backend(self) -> str:
        return self._database.backend

    def connect(self):
        return self._database.connection()

    def transaction(self):
        return self._database.transaction()

    def close(self) -> None:
        self._database.close()

    def initialize(self) -> int:
        with self.transaction() as conn:
            self._database.acquire_migration_lock(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migration_metadata (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(version) REFERENCES schema_migrations(version)
                        ON DELETE CASCADE
                )
                """
            )
            applied = {
                item["version"]
                for item in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            metadata = {
                item["version"]: item["checksum"]
                for item in conn.execute(
                    "SELECT version, checksum FROM schema_migration_metadata"
                ).fetchall()
            }
            for version, script in MIGRATIONS:
                checksum = hashlib.sha256(script.strip().encode("utf-8")).hexdigest()
                if version in applied:
                    recorded_checksum = metadata.get(version)
                    if recorded_checksum is None:
                        conn.execute(
                            """
                            INSERT INTO schema_migration_metadata (
                                version, name, checksum, recorded_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (version, f"cloud-parity-{version:04d}", checksum, _utc_now()),
                        )
                    elif recorded_checksum != checksum:
                        raise MigrationDriftError(
                            f"migration {version} checksum mismatch; create a new migration instead of editing history"
                        )
                    continue
                conn.executescript(script)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, _utc_now()),
                )
                conn.execute(
                    """
                    INSERT INTO schema_migration_metadata (
                        version, name, checksum, recorded_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (version, f"cloud-parity-{version:04d}", checksum, _utc_now()),
                )
            row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
            return int(row["version"] or 0)

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
            return int(row["version"] or 0)

    def healthcheck(self) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        with self.connect() as conn:
            row = conn.execute("SELECT 1 AS ok").fetchone()
            if row is None or int(row["ok"]) != 1:
                raise RuntimeError("database health query failed")
            version = conn.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
        elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        return {
            "status": "ok",
            "backend": self.backend,
            "schema_version": int(version["version"] or 0),
            "latency_ms": round(elapsed_ms, 2),
        }

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def revoke_access_token(
        self,
        *,
        token: str,
        subject: str,
        revoked_by: str,
        reason: str,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        if not token.strip() or not subject.strip() or not revoked_by.strip():
            raise ValueError("token, subject, and revoked_by are required")
        normalized_reason = reason.strip()
        if not normalized_reason or len(normalized_reason) > 500:
            raise ValueError("revocation reason is required and must not exceed 500 characters")
        token_hash = self._token_hash(token.strip())
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO revoked_access_tokens (
                    token_sha256, subject, reason, expires_at, revoked_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(token_sha256) DO UPDATE SET
                    reason = excluded.reason, expires_at = excluded.expires_at,
                    revoked_by = excluded.revoked_by
                """,
                (
                    token_hash,
                    subject.strip(),
                    normalized_reason,
                    expires_at,
                    revoked_by.strip(),
                    now,
                ),
            )
        return {
            "revoked": True,
            "subject": subject.strip(),
            "reason": normalized_reason,
            "expires_at": expires_at,
        }

    def is_access_token_revoked(self, token: str) -> bool:
        if not token.strip():
            return False
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT expires_at FROM revoked_access_tokens
                WHERE token_sha256 = ?
                """,
                (self._token_hash(token.strip()),),
            ).fetchone()
        if row is None:
            return False
        expiry = row["expires_at"]
        return expiry is None or str(expiry) > _utc_now()

    def consume_api_rate_limit(
        self,
        *,
        key: str,
        limit: int,
        window_seconds: int = 60,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if limit < 1 or window_seconds < 1:
            raise ValueError("rate limit and window must be positive")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        timestamp = current.isoformat().replace("+00:00", "Z")
        stale_before = (
            current - timedelta(seconds=window_seconds * 2)
        ).isoformat().replace("+00:00", "Z")
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        lock_suffix = " FOR UPDATE" if self.backend == "postgresql" else ""
        with self.transaction() as conn:
            # Bound storage even when an attacker rotates arbitrary bearer tokens.
            conn.execute(
                "DELETE FROM api_rate_limit_windows WHERE updated_at < ?",
                (stale_before,),
            )
            conn.execute(
                """
                INSERT INTO api_rate_limit_windows (
                    key_sha256, window_started_at, request_count, updated_at
                ) VALUES (?, ?, 0, ?)
                ON CONFLICT(key_sha256) DO NOTHING
                """,
                (key_hash, timestamp, timestamp),
            )
            row = conn.execute(
                f"""
                SELECT * FROM api_rate_limit_windows
                WHERE key_sha256 = ?{lock_suffix}
                """,
                (key_hash,),
            ).fetchone()
            started = datetime.fromisoformat(
                str(row["window_started_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            elapsed = max(0.0, (current - started).total_seconds())
            if elapsed >= window_seconds:
                count = 1
                started = current
                allowed = True
            else:
                count = int(row["request_count"])
                allowed = count < limit
                if allowed:
                    count += 1
            conn.execute(
                """
                UPDATE api_rate_limit_windows SET window_started_at = ?,
                    request_count = ?, updated_at = ? WHERE key_sha256 = ?
                """,
                (
                    started.isoformat().replace("+00:00", "Z"),
                    count,
                    timestamp,
                    key_hash,
                ),
            )
        retry_after = max(1, int(window_seconds - min(elapsed, window_seconds)))
        return {
            "allowed": allowed,
            "limit": limit,
            "remaining": max(0, limit - count),
            "retry_after": retry_after,
        }

    def _purge_project_rows(
        self,
        conn: ConnectionLike,
        *,
        project_id: str,
        cutoff: str,
        now: str,
        actor_id: str,
    ) -> dict[str, int]:
        sessions = conn.execute(
            "DELETE FROM agent_sessions WHERE project_id = ? AND retention_until <= ?",
            (project_id, now),
        )
        calls = conn.execute(
            """
            DELETE FROM call_jobs WHERE project_id = ?
              AND status IN ('completed','failed','busy','no_answer','canceled','blocked')
              AND ended_at IS NOT NULL AND ended_at <= ?
            """,
            (project_id, cutoff),
        )
        compliance = conn.execute(
            "DELETE FROM compliance_decisions WHERE project_id = ? AND created_at <= ?",
            (project_id, cutoff),
        )
        webhooks = conn.execute(
            "DELETE FROM telephony_webhook_events WHERE project_id = ? AND received_at <= ?",
            (project_id, cutoff),
        )
        result = {
            "sessions": max(0, int(sessions.rowcount or 0)),
            "calls": max(0, int(calls.rowcount or 0)),
            "compliance_decisions": max(0, int(compliance.rowcount or 0)),
            "webhook_events": max(0, int(webhooks.rowcount or 0)),
        }
        self._append_audit(
            conn,
            project_id=project_id,
            actor_id=actor_id,
            action="retention.purge",
            resource_type="project",
            resource_id=project_id,
            payload={"cutoff": cutoff, **result},
        )
        return result

    def purge_expired_project_data(
        self,
        *,
        project_id: str,
        actor_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Apply the tenant retention policy without deleting legal DNC/consent evidence."""
        self.require_permission(project_id, actor_id, "project.manage")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        timestamp = current.isoformat().replace("+00:00", "Z")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT retention_days FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("project not found")
            cutoff = (
                current - timedelta(days=int(row["retention_days"]))
            ).isoformat().replace("+00:00", "Z")
            result = self._purge_project_rows(
                conn,
                project_id=project_id,
                cutoff=cutoff,
                now=timestamp,
                actor_id=actor_id,
            )
        return {"project_id": project_id, "cutoff": cutoff, **result}

    def run_retention_maintenance(
        self, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Internal scheduler entry point covering every active tenant."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        timestamp = current.isoformat().replace("+00:00", "Z")
        results: list[dict[str, Any]] = []
        with self.transaction() as conn:
            projects = conn.execute(
                "SELECT id, retention_days FROM projects WHERE status = 'active' ORDER BY id"
            ).fetchall()
            for project in projects:
                project_id = str(project["id"])
                cutoff = (
                    current - timedelta(days=int(project["retention_days"]))
                ).isoformat().replace("+00:00", "Z")
                purged = self._purge_project_rows(
                    conn,
                    project_id=project_id,
                    cutoff=cutoff,
                    now=timestamp,
                    actor_id="system:retention",
                )
                results.append({"project_id": project_id, "cutoff": cutoff, **purged})
            global_cutoff = (current - timedelta(days=2)).isoformat().replace(
                "+00:00", "Z"
            )
            conn.execute(
                "DELETE FROM api_rate_limit_windows WHERE updated_at < ?", (global_cutoff,)
            )
            conn.execute(
                "DELETE FROM revoked_access_tokens WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (timestamp,),
            )
            conn.execute(
                "DELETE FROM telephony_webhook_events WHERE project_id IS NULL AND received_at < ?",
                (global_cutoff,),
            )
        return results

    def create_project(
        self,
        *,
        name: str,
        slug: str,
        owner_id: str,
        retention_days: int | None = None,
    ) -> dict[str, Any]:
        if not name.strip() or not owner_id.strip():
            raise ValueError("name and owner_id are required")
        days = retention_days or self.default_retention_days
        if days < 1:
            raise ValueError("retention_days must be at least 1")
        project_id = str(uuid.uuid4())
        created_at = _utc_now()
        project_slug = _slug(slug)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO projects (
                    id, slug, name, status, retention_days, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (project_id, project_slug, name.strip(), days, created_at, created_at),
            )
            conn.execute(
                """
                INSERT INTO project_memberships (project_id, user_id, role, created_at)
                VALUES (?, ?, 'owner', ?)
                """,
                (project_id, owner_id.strip(), created_at),
            )
            self._append_audit(
                conn,
                project_id=project_id,
                actor_id=owner_id.strip(),
                action="project.create",
                resource_type="project",
                resource_id=project_id,
                payload={"name": name.strip(), "slug": project_slug},
            )
        return self.get_project(project_id, owner_id)

    def list_projects(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*, m.role
                FROM projects p
                JOIN project_memberships m ON m.project_id = p.id
                WHERE m.user_id = ?
                ORDER BY p.created_at, p.id
                """,
                (user_id,),
            ).fetchall()
        return [_row(item) for item in rows if item is not None]

    def get_project(self, project_id: str, user_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT p.*, m.role
                FROM projects p
                JOIN project_memberships m ON m.project_id = p.id
                WHERE p.id = ? AND m.user_id = ?
                """,
                (project_id, user_id),
            ).fetchone()
            if row is not None:
                return _row(row) or {}
            exists = conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
        if exists:
            raise AccessDeniedError("project access denied")
        raise ResourceNotFoundError("project not found")

    def require_permission(self, project_id: str, user_id: str, permission: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT role FROM project_memberships
                WHERE project_id = ? AND user_id = ?
                """,
                (project_id, user_id),
            ).fetchone()
        if row is None or permission not in ROLE_PERMISSIONS.get(row["role"], frozenset()):
            raise AccessDeniedError(f"missing permission: {permission}")
        return str(row["role"])

    def _lock_project_and_require_permission(
        self,
        conn: ConnectionLike,
        *,
        project_id: str,
        user_id: str,
        permission: str,
    ) -> str:
        """Serialize project administration and authorize against the locked state."""
        lock_suffix = " FOR UPDATE" if self.backend == "postgresql" else ""
        project = conn.execute(
            f"SELECT id FROM projects WHERE id = ?{lock_suffix}",
            (project_id,),
        ).fetchone()
        if project is None:
            raise ResourceNotFoundError("project not found")
        row = conn.execute(
            """
            SELECT role FROM project_memberships
            WHERE project_id = ? AND user_id = ?
            """,
            (project_id, user_id),
        ).fetchone()
        if row is None or permission not in ROLE_PERMISSIONS.get(
            str(row["role"]), frozenset()
        ):
            raise AccessDeniedError(f"missing permission: {permission}")
        return str(row["role"])

    def add_membership(
        self,
        *,
        project_id: str,
        actor_id: str,
        user_id: str,
        role: str,
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValueError(f"unsupported role: {role}")
        created_at = _utc_now()
        with self.transaction() as conn:
            actor_role = self._lock_project_and_require_permission(
                conn,
                project_id=project_id,
                user_id=actor_id,
                permission="project.manage",
            )
            existing = conn.execute(
                """
                SELECT role FROM project_memberships
                WHERE project_id = ? AND user_id = ?
                """,
                (project_id, user_id),
            ).fetchone()
            existing_role = str(existing["role"]) if existing is not None else ""
            if actor_role != "owner" and (role == "owner" or existing_role == "owner"):
                raise AccessDeniedError("only an owner can change owner membership")
            if existing_role == "owner" and role != "owner":
                owners = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM project_memberships
                    WHERE project_id = ? AND role = 'owner'
                    """,
                    (project_id,),
                ).fetchone()
                if int(owners["count"] or 0) <= 1:
                    raise ValueError("a project must retain at least one owner")
            conn.execute(
                """
                INSERT INTO project_memberships (project_id, user_id, role, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, user_id) DO UPDATE SET role = excluded.role
                """,
                (project_id, user_id, role, created_at),
            )
            self._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="membership.upsert",
                resource_type="membership",
                resource_id=user_id,
                payload={"role": role},
            )
        return {"project_id": project_id, "user_id": user_id, "role": role}

    def list_memberships(
        self, *, project_id: str, actor_id: str
    ) -> list[dict[str, Any]]:
        self.require_permission(project_id, actor_id, "project.manage")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, user_id, role, created_at
                FROM project_memberships WHERE project_id = ?
                ORDER BY role, user_id
                """,
                (project_id,),
            ).fetchall()
        return [_row(row) or {} for row in rows]

    def remove_membership(
        self, *, project_id: str, actor_id: str, user_id: str
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            actor_role = self._lock_project_and_require_permission(
                conn,
                project_id=project_id,
                user_id=actor_id,
                permission="project.manage",
            )
            existing = conn.execute(
                """
                SELECT role FROM project_memberships
                WHERE project_id = ? AND user_id = ?
                """,
                (project_id, user_id),
            ).fetchone()
            if existing is None:
                raise ResourceNotFoundError("project membership not found")
            target_role = str(existing["role"])
            if target_role == "owner":
                if actor_role != "owner":
                    raise AccessDeniedError("only an owner can remove an owner")
                owners = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM project_memberships
                    WHERE project_id = ? AND role = 'owner'
                    """,
                    (project_id,),
                ).fetchone()
                if int(owners["count"] or 0) <= 1:
                    raise ValueError("a project must retain at least one owner")
            conn.execute(
                "DELETE FROM project_memberships WHERE project_id = ? AND user_id = ?",
                (project_id, user_id),
            )
            self._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="membership.remove",
                resource_type="membership",
                resource_id=user_id,
                payload={"previous_role": target_role},
            )
        return {"removed": True, "project_id": project_id, "user_id": user_id}

    def list_audit_logs(
        self,
        *,
        project_id: str,
        user_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.require_permission(project_id, user_id, "audit.read")
        safe_limit = max(1, min(limit, 500))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM audit_logs
                WHERE project_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (project_id, safe_limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for item in rows:
            record = _row(item) or {}
            record["payload"] = json.loads(record.pop("payload_json"))
            result.append(record)
        return result

    def _append_audit(
        self,
        conn: ConnectionLike,
        *,
        project_id: str,
        actor_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        audit_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO audit_logs (
                id, project_id, actor_id, action, resource_type,
                resource_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                project_id,
                actor_id,
                action,
                resource_type,
                resource_id,
                json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                _utc_now(),
            ),
        )
        return audit_id
