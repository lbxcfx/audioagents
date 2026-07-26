from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .store import PlatformStore, _utc_now


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**values: Any) -> str:
    return "{" + ",".join(f'{key}="{_escape(value)}"' for key, value in values.items()) + "}"


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _family(
    output: list[str],
    name: str,
    help_text: str,
    metric_type: str,
    samples: Iterable[tuple[str, int | float]],
) -> None:
    output.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"))
    output.extend(f"{name}{labels} {value}" for labels, value in samples)


def render_prometheus_metrics(store: PlatformStore) -> str:
    """Render a bounded, phone-number-free Prometheus snapshot from durable state."""
    health = store.healthcheck()
    now = datetime.now(timezone.utc)
    with store.connect() as conn:
        projects = conn.execute(
            "SELECT id FROM projects WHERE status = 'active' ORDER BY id"
        ).fetchall()
        states = conn.execute(
            """
            SELECT project_id, direction, status, COUNT(*) AS count
            FROM call_jobs GROUP BY project_id, direction, status
            ORDER BY project_id, direction, status
            """
        ).fetchall()
        queued = conn.execute(
            """
            SELECT project_id, COUNT(*) AS depth, MIN(created_at) AS oldest
            FROM call_jobs WHERE status = 'queued' AND available_at <= ?
            GROUP BY project_id ORDER BY project_id
            """,
            (_utc_now(),),
        ).fetchall()
        scheduled = conn.execute(
            """
            SELECT project_id, COUNT(*) AS depth FROM call_jobs
            WHERE status = 'queued' AND available_at > ?
            GROUP BY project_id ORDER BY project_id
            """,
            (_utc_now(),),
        ).fetchall()
        stale = conn.execute(
            """
            SELECT project_id, COUNT(*) AS count FROM call_jobs
            WHERE status IN ('leased','dispatching','dialing','ringing','active','reconciling')
              AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
            GROUP BY project_id ORDER BY project_id
            """,
            (_utc_now(),),
        ).fetchall()
        attempts = conn.execute(
            """
            SELECT project_id, status, COUNT(*) AS count FROM call_attempts
            GROUP BY project_id, status ORDER BY project_id, status
            """
        ).fetchall()
        transfers = conn.execute(
            """
            SELECT project_id, status, COUNT(*) AS count FROM call_transfers
            GROUP BY project_id, status ORDER BY project_id, status
            """
        ).fetchall()
        recent_transfer_failures = conn.execute(
            """
            SELECT project_id, COUNT(*) AS count FROM call_transfers
            WHERE status = 'failed' AND updated_at >= ?
            GROUP BY project_id ORDER BY project_id
            """,
            ((now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),),
        ).fetchall()
        recordings = conn.execute(
            """
            SELECT project_id, recording_status AS status, COUNT(*) AS count
            FROM call_jobs WHERE recording_status <> ''
            GROUP BY project_id, recording_status ORDER BY project_id, recording_status
            """
        ).fetchall()
        recent_recording_failures = conn.execute(
            """
            SELECT project_id, COUNT(*) AS count FROM call_jobs
            WHERE recording_status = 'failed' AND updated_at >= ?
            GROUP BY project_id ORDER BY project_id
            """,
            ((now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),),
        ).fetchall()
        answering_machine_results = conn.execute(
            """
            SELECT project_id, answering_machine_category AS category, COUNT(*) AS count
            FROM call_jobs WHERE answering_machine_category <> ''
            GROUP BY project_id, answering_machine_category
            ORDER BY project_id, answering_machine_category
            """
        ).fetchall()
        compliance = conn.execute(
            """
            SELECT project_id, decision, reason, COUNT(*) AS count
            FROM compliance_decisions GROUP BY project_id, decision, reason
            ORDER BY project_id, decision, reason
            """
        ).fetchall()
        webhooks = conn.execute(
            """
            SELECT outcome, COUNT(*) AS count FROM telephony_webhook_events
            GROUP BY outcome ORDER BY outcome
            """
        ).fetchall()

    output: list[str] = []
    _family(output, "cloud_parity_up", "Control plane process and database are available.", "gauge", [("", 1)])
    _family(
        output,
        "cloud_parity_database_latency_milliseconds",
        "Latency of the control-plane database health query.",
        "gauge",
        [("", float(health["latency_ms"]))],
    )
    _family(
        output,
        "cloud_parity_schema_version",
        "Applied control-plane schema version.",
        "gauge",
        [("", int(health["schema_version"]))],
    )
    _family(
        output,
        "cloud_parity_projects",
        "Number of active tenant projects.",
        "gauge",
        [("", len(projects))],
    )
    _family(
        output,
        "telephony_calls",
        "Durable calls by tenant, direction, and state.",
        "gauge",
        [
            (
                _labels(
                    project_id=row["project_id"],
                    direction=row["direction"],
                    status=row["status"],
                ),
                int(row["count"]),
            )
            for row in states
        ],
    )
    active_by_project: dict[str, int] = {str(row["id"]): 0 for row in projects}
    for row in states:
        if str(row["status"]) in {
            "leased", "dispatching", "dialing", "ringing", "active", "reconciling"
        }:
            project_id = str(row["project_id"])
            active_by_project[project_id] = active_by_project.get(project_id, 0) + int(
                row["count"]
            )
    _family(
        output,
        "telephony_active_calls",
        "Current lease-active inbound and outbound calls by tenant.",
        "gauge",
        [
            (_labels(project_id=row["id"]), active_by_project.get(str(row["id"]), 0))
            for row in projects
        ],
    )
    queue_by_project = {str(row["project_id"]): dict(row) for row in queued}
    _family(
        output,
        "telephony_queue_depth",
        "Number of eligible outbound calls waiting for dispatch.",
        "gauge",
        [
            (
                _labels(project_id=row["id"]),
                int(queue_by_project.get(str(row["id"]), {}).get("depth", 0)),
            )
            for row in projects
        ],
    )
    scheduled_by_project = {str(row["project_id"]): int(row["depth"]) for row in scheduled}
    _family(
        output,
        "telephony_scheduled_calls",
        "Number of queued outbound calls scheduled for the future.",
        "gauge",
        [
            (_labels(project_id=row["id"]), scheduled_by_project.get(str(row["id"]), 0))
            for row in projects
        ],
    )
    oldest_samples: list[tuple[str, float]] = []
    for project in projects:
        row = queue_by_project.get(str(project["id"]))
        oldest = _timestamp(row["oldest"]) if row else None
        age = max(0.0, (now - oldest).total_seconds()) if oldest else 0.0
        oldest_samples.append((_labels(project_id=project["id"]), round(age, 3)))
    _family(
        output,
        "telephony_queue_oldest_age_seconds",
        "Age of the oldest queued outbound call.",
        "gauge",
        oldest_samples,
    )
    stale_by_project = {str(row["project_id"]): int(row["count"]) for row in stale}
    _family(
        output,
        "telephony_stale_leases",
        "Expired leases that require dispatcher recovery.",
        "gauge",
        [
            (
                _labels(project_id=row["id"]),
                stale_by_project.get(str(row["id"]), 0),
            )
            for row in projects
        ],
    )
    _family(
        output,
        "telephony_attempts_by_status",
        "Durable outbound call attempts by final/current state.",
        "gauge",
        [
            (
                _labels(project_id=row["project_id"], status=row["status"]),
                int(row["count"]),
            )
            for row in attempts
        ],
    )
    _family(
        output,
        "telephony_transfers_by_status",
        "Human transfer requests by state.",
        "gauge",
        [
            (
                _labels(project_id=row["project_id"], status=row["status"]),
                int(row["count"]),
            )
            for row in transfers
        ],
    )
    recent_failures_by_project = {
        str(row["project_id"]): int(row["count"]) for row in recent_transfer_failures
    }
    _family(
        output,
        "telephony_transfer_failures_recent",
        "Human transfer failures observed in the last ten minutes.",
        "gauge",
        [
            (_labels(project_id=row["id"]), recent_failures_by_project.get(str(row["id"]), 0))
            for row in projects
        ],
    )
    _family(
        output,
        "telephony_recordings_by_status",
        "Managed call recordings by durable state.",
        "gauge",
        [
            (
                _labels(project_id=row["project_id"], status=row["status"]),
                int(row["count"]),
            )
            for row in recordings
        ],
    )
    recent_recording_failures_by_project = {
        str(row["project_id"]): int(row["count"])
        for row in recent_recording_failures
    }
    _family(
        output,
        "telephony_recording_failures_recent",
        "Managed recording failures observed in the last ten minutes.",
        "gauge",
        [
            (
                _labels(project_id=row["id"]),
                recent_recording_failures_by_project.get(str(row["id"]), 0),
            )
            for row in projects
        ],
    )
    _family(
        output,
        "telephony_answering_machine_results",
        "Answering-machine detection results by durable category.",
        "gauge",
        [
            (
                _labels(project_id=row["project_id"], category=row["category"]),
                int(row["count"]),
            )
            for row in answering_machine_results
        ],
    )
    _family(
        output,
        "telephony_compliance_decisions_total",
        "Outbound compliance decisions by tenant and reason.",
        "counter",
        [
            (
                _labels(
                    project_id=row["project_id"],
                    decision=row["decision"],
                    reason=row["reason"],
                ),
                int(row["count"]),
            )
            for row in compliance
        ],
    )
    _family(
        output,
        "telephony_webhook_events_total",
        "Verified LiveKit webhook events by processing outcome.",
        "counter",
        [(_labels(outcome=row["outcome"]), int(row["count"])) for row in webhooks],
    )
    return "\n".join(output) + "\n"
