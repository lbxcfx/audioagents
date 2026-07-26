from __future__ import annotations

from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.observability import render_prometheus_metrics
from server.cloud_parity.store import PlatformStore
from server.cloud_parity.telephony import TelephonyService


def test_prometheus_snapshot_is_tenant_scoped_and_contains_no_phone_numbers(
    tmp_path: Path,
) -> None:
    store = PlatformStore(tmp_path / "metrics.sqlite3")
    assert store.initialize() >= 13
    project = store.create_project(name="Metrics", slug="metrics", owner_id="owner")
    service = TelephonyService(store)
    service.update_policy(
        project_id=project["id"],
        user_id="owner",
        timezone_name="UTC",
        allowed_weekdays=range(7),
        calling_window_start="00:00",
        calling_window_end="23:59",
        require_consent=False,
        consent_purpose="outbound",
        max_attempts_per_number_per_day=100,
    )
    service.enqueue_outbound(
        project_id=project["id"],
        user_id="owner",
        idempotency_key="metrics-call",
        destination_number="+8613800000081",
        agent_name="metrics-agent",
    )
    leased = service.claim_outbound(
        project_id=project["id"], user_id="owner", worker_id="metrics-worker"
    )[0]
    service.record_call_recording(
        project_id=project["id"],
        user_id="owner",
        call_id=leased["id"],
        worker_id="metrics-worker",
        lease_token=leased["lease_token"],
        egress_id="setup:metrics-call",
        status="failed",
    )
    service.enqueue_outbound(
        project_id=project["id"],
        user_id="owner",
        idempotency_key="metrics-queued-call",
        destination_number="+8613800000082",
        agent_name="metrics-agent",
    )

    rendered = render_prometheus_metrics(store)

    assert "cloud_parity_up 1" in rendered
    assert f'telephony_queue_depth{{project_id="{project["id"]}"}} 1' in rendered
    assert "telephony_queue_oldest_age_seconds" in rendered
    assert "telephony_scheduled_calls" in rendered
    assert "# TYPE telephony_attempts_by_status gauge" in rendered
    assert "# TYPE telephony_transfers_by_status gauge" in rendered
    assert "# TYPE telephony_recordings_by_status gauge" in rendered
    assert "# TYPE telephony_answering_machine_results gauge" in rendered
    assert f'telephony_recording_failures_recent{{project_id="{project["id"]}"}} 1' in rendered
    assert f'telephony_recordings_by_status{{project_id="{project["id"]}",status="failed"}} 1' in rendered
    assert "telephony_stale_leases" in rendered
    assert f'telephony_active_calls{{project_id="{project["id"]}"}} 1' in rendered
    assert "+8613800000081" not in rendered
    assert "+8613800000082" not in rendered
    assert "destination_number" not in rendered
