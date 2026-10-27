"""Tests for F4 alert policies: inventory latency, calibrated Cloud SQL connections, and Apigee timeout."""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
import pytest

from agent.app.config import AgentConfig, reset_agent_settings
from agent.app.contracts.alert import AlertEvent, is_ata_alert_payload
from agent.app.correlation.scoring import classify_anomaly_category
from agent.app.triage import generate_investigation_report

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path):
    """Set up temporary local paths and reset settings."""
    cfg = AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        region="us-west1",
        incident_store_path=str(tmp_path / "knowledge/incident-manager"),
        fallback_store_path=str(tmp_path / "fallback"),
        is_local=True,
    )
    reset_agent_settings(cfg)
    yield cfg
    reset_agent_settings(None)


def test_terraform_observability_contains_all_policies():
    """Verify infra/terraform/observability defines all 8 expected alert policies and variables."""
    main_tf = (REPO_ROOT / "infra/terraform/observability/main.tf").read_text()
    vars_tf = (REPO_ROOT / "infra/terraform/observability/variables.tf").read_text()
    outputs_tf = (REPO_ROOT / "infra/terraform/observability/outputs.tf").read_text()

    # 1. Cloud SQL connections variable
    assert "variable \"cloudsql_connections_threshold\"" in vars_tf
    assert "default     = 20" in vars_tf

    # 2. Inventory latency policy defined
    assert "resource \"google_monitoring_alert_policy\" \"inventory_latency\"" in main_tf
    assert "${var.name_prefix}-inventory-latency" in main_tf
    assert "${var.name_prefix}-inventory-api" in main_tf
    assert "run.googleapis.com/request_latencies" in main_tf
    # p99 per service: latency distributions merged across series (variables.tf defaults).
    assert "cross_series_reducer = var.latency_cross_series_reducer" in main_tf

    # 3. Cloud SQL connections threshold uses variable
    assert "threshold_value = var.cloudsql_connections_threshold" in main_tf

    # 4. User labels on alert policies
    assert "user_labels = {" in main_tf
    assert re.search(r'app\s+=\s+"alert-triage-agent"', main_tf)

    # 5. Outputs include inventory_latency
    assert "google_monitoring_alert_policy.inventory_latency.display_name" in outputs_tf


def test_terraform_observability_channel_migration():
    """Observability notifies its own stack's foundation channel, without secret access or a state-bucket dependency."""
    main_tf = (REPO_ROOT / "infra/terraform/observability/main.tf").read_text()
    vars_tf = (REPO_ROOT / "infra/terraform/observability/variables.tf").read_text()
    outputs_tf = (REPO_ROOT / "infra/terraform/observability/outputs.tf").read_text()

    # 1. No secret manager access or channel creation in observability
    assert "google_secret_manager_secret_version" not in main_tf
    assert 'resource "google_monitoring_notification_channel"' not in main_tf

    # 2. Lookup of this stack's channel (display name derived from name_prefix), no remote state
    assert "terraform_remote_state" not in main_tf
    assert 'data "google_monitoring_notification_channel" "bridge_webhook"' in main_tf
    assert 'display_name = "${var.name_prefix}-bridge-webhook"' in main_tf

    # 3. Optional variable override defined
    assert 'variable "notification_channel_id"' in vars_tf
    assert "default     = null" in vars_tf

    # 4. Local resolves to the override or the looked-up channel
    assert "local.notification_channel_id" in main_tf
    assert "data.google_monitoring_notification_channel.bridge_webhook[0].name" in main_tf

    # 5. All 8 alert policies use local.notification_channel_id
    matches = re.findall(r"notification_channels\s*=\s*\[local\.notification_channel_id\]", main_tf)
    assert len(matches) == 8, f"Expected 8 policies with local.notification_channel_id, found {len(matches)}"

    # 6. Output uses local.notification_channel_id
    assert "value       = local.notification_channel_id" in outputs_tf


def test_apigee_proxy_target_timeouts_configured():
    """Verify Apigee proxy target endpoints specify 5000ms io.timeout.millis and connect.timeout.millis."""
    orders_target = (REPO_ROOT / "demo/apigee/ata-orders-v1/apiproxy/targets/default.xml").read_text()
    inv_target = (REPO_ROOT / "demo/apigee/ata-inventory-v1/apiproxy/targets/default.xml").read_text()

    for content, name in [(orders_target, "orders"), (inv_target, "inventory")]:
        root = ET.fromstring(content)
        props = root.findall(".//HTTPTargetConnection/Properties/Property")
        prop_map = {p.attrib.get("name"): p.text for p in props}
        assert prop_map.get("io.timeout.millis") == "5000", f"{name} target missing 5000ms io.timeout"
        assert prop_map.get("connect.timeout.millis") == "5000", f"{name} target missing 5000ms connect.timeout"


def test_ata_inventory_latency_scoping_and_classification():
    """Verify ata-inventory-latency alert scopes properly and classifies as latency_504."""
    raw_payload = {
        "incident": {
            "incident_id": "0.inv_lat_001",
            "policy_name": "projects/cloud-assist-fde-2/alertPolicies/ata-inventory-latency",
            "policy": {"displayName": "ata-inventory-latency"},
            "resource": {
                "type": "cloud_run_revision",
                "labels": {
                    "project_id": "cloud-assist-fde-2",
                    "service_name": "ata-inventory-api",
                },
            },
            "started_at": "2026-10-23T05:30:00Z",
            "state": "open",
        }
    }
    assert is_ata_alert_payload(raw_payload) is True

    event = AlertEvent.from_monitoring_payload(raw_payload)
    assert event.resource_name == "ata-inventory-api"
    assert event.policy_name == "projects/cloud-assist-fde-2/alertPolicies/ata-inventory-latency"

    category = classify_anomaly_category(event.summary or event.condition_name or "ata-inventory-latency")
    assert category == "latency_504"


def test_ata_inventory_latency_investigation_report(isolated_env: AgentConfig):
    """Verify investigation report generation when ata-inventory-latency alert fires."""
    now_str = datetime.now(timezone.utc).isoformat()
    alert = AlertEvent(
        incident_id="0.s2_inv_lat_test",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        state="open",
        started_at=now_str,
        condition_name="Cloud Run inventory latency > 2s",
    )
    report = generate_investigation_report(alert, "grp_s2_inv", "incident-s2-inv.md", isolated_env)
    assert report.origin == "ata-inventory-api"
    assert "latency" in report.summary.lower() or "timeout" in report.summary.lower()
    assert report.priority in {"P1", "P2"}
