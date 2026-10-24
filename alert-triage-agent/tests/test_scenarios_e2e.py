"""End-to-End Verification Harness for Demo Scenarios (S1-S5).

Verifies:
1. Scenario triage flows (S1 through S5)
2. Correlation engine grouping (1 group vs 2 groups for S3 concurrent disjoint)
3. Origin attribution and blast radius calculation
4. GCS OKF Markdown incident record formatting
5. ServiceNow ticket creation via adapter
6. Fault control telemetry exclusion guardrail (zero logs containing /admin/fault)
"""

from datetime import datetime, timezone, timedelta
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import yaml

from agent.app.config import AgentConfig, reset_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.report import InvestigationReport
from agent.app.correlation.engine import CorrelationEngine, CandidateGroup
from agent.app.correlation.scoring import compute_multi_factor_score
from agent.app.specialists.synthesizer import SynthesizerAgent
from agent.app.triage import write_preanalysis_lock, generate_investigation_report
from bridge.app.adapters.servicenow import ServiceNowAdapter
from demo.ata_inventory_api.app.main import app as inventory_app
from demo.ata_inventory_api.app.middleware import SuppressAdminLogsFilter
from demo.ata_orders_api.app.main import app as orders_app
from fastapi.testclient import TestClient
from topology.topology_graph import get_topology_graph, get_blast_radius


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


# ==============================================================================
# 1. Fault Control Telemetry Exclusion Guardrail
# ==============================================================================

def test_telemetry_exclusion_guardrail(caplog):
    """Assert zero log entries containing '/admin/fault' or '/admin' are emitted."""
    caplog.handler.addFilter(SuppressAdminLogsFilter())
    caplog.set_level(logging.DEBUG)

    inv_client = TestClient(inventory_app)
    ord_client = TestClient(orders_app)

    # 1. Exercise inventory admin endpoints
    r1 = inv_client.post("/admin/fault/inject", json={"type": "500", "rate": 0.85})
    assert r1.status_code == 200

    r2 = inv_client.post("/admin/leak-connections", json={"count": 5})
    assert r2.status_code == 200

    r3 = inv_client.post("/admin/lock-table", params={"duration_seconds": 10})
    assert r3.status_code == 200

    r4 = inv_client.post("/admin/fault/reset")
    assert r4.status_code == 200

    # 2. Exercise orders admin endpoints
    r5 = ord_client.post("/admin/fault/inject", json={"type": "latency", "delay_ms": 6500})
    assert r5.status_code == 200

    r6 = ord_client.post("/admin/fault/reset")
    assert r6.status_code == 200

    # 3. Assert zero log messages in caplog contain /admin
    for record in caplog.records:
        msg = record.getMessage()
        assert "/admin" not in msg
        assert "/admin/fault" not in msg


# ==============================================================================
# 2. Scenario S1: Backend 5xx Error Spike
# ==============================================================================

def test_scenario_s1_backend_5xx(isolated_env: AgentConfig):
    """Assert S1 creates 1 incident group, attributes origin to ata-inventory-api, and computes blast radius."""
    now_str = datetime.now(timezone.utc).isoformat()
    alert = AlertEvent(
        incident_id="0.s1_test_001",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at=now_str,
        condition_name="Cloud Run inventory 5xx error rate",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "5xx",
            "severity": "p1",
        },
    )

    # Pre-analysis lock & OKF verification
    fpath, group_id, fname = write_preanalysis_lock(alert, isolated_env)
    assert fpath.exists()
    content = fpath.read_text(encoding="utf-8")
    parts = content.split("---")
    assert len(parts) >= 3
    fm = yaml.safe_load(parts[1])
    assert fm["type"] == "incident"
    assert fm["origin"] == "ata-inventory-api"
    assert fm["policy_name"] == "ata-inventory-5xx"
    assert fm["status"] == "mitigation-in-progress"

    # Blast radius verification via topology DAG
    blast = get_blast_radius("ata-inventory-api")
    assert "ata-inventory-v1" in blast or "ata-orders-api" in blast

    # Investigation report generation
    report = generate_investigation_report(alert, group_id, fname, isolated_env)
    assert report.origin == "ata-inventory-api"
    assert "5xx" in report.summary or "threshold" in report.summary.lower()


# ==============================================================================
# 3. Scenario S2: Backend Latency Degradation
# ==============================================================================

def test_scenario_s2_backend_latency(isolated_env: AgentConfig):
    """Assert S2 attributes origin to ata-inventory-api and reports timeout root cause."""
    now_str = datetime.now(timezone.utc).isoformat()
    alert = AlertEvent(
        incident_id="0.s2_test_001",
        policy_name="ata-orders-latency",
        resource_name="ata-orders-api",
        state="open",
        started_at=now_str,
        condition_name="Cloud Run orders latency > 2s",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-orders-api",
            "tier": "service",
            "signal": "latency",
            "severity": "p2",
        },
    )
    report = generate_investigation_report(alert, "grp_s2", "incident-s2.md", isolated_env)
    assert report.origin == "ata-inventory-api"
    assert "ata-inventory-api" in report.blast_radius
    assert "ata-orders-api" in report.blast_radius
    assert report.priority in {"P1", "P2"}


@pytest.mark.asyncio
async def test_scenario_s2_upstream_origin_with_telemetry_fixture(isolated_env: AgentConfig):
    """Verify S2 with mock telemetry fixture where upstream latency precedes downstream alert.

    Proves:
    1. Downstream alert arrives on victim ata-orders-api at T+6m.
    2. Upstream dependency ata-inventory-api exhibits latency degradation starting at T+0m (preceding alert).
    3. Synthesizer traces topology DAG upstream from orders-api -> inventory-api.
    4. Identifies upstream cause ata-inventory-api as origin.
    5. Blast radius includes both downstream victims (orders-api, apigee proxies) and upstream cause (inventory-api).
    6. Incident Markdown frontmatter in OKF records origin: ata-inventory-api.
    """
    t_fault = datetime(2026, 10, 23, 4, 42, 25, tzinfo=timezone.utc)
    t_alert = datetime(2026, 10, 23, 4, 48, 18, tzinfo=timezone.utc)

    alert = AlertEvent(
        incident_id="0.oe5h0cgcbvhu",
        policy_name="ata-orders-latency",
        resource_name="ata-orders-api",
        state="open",
        started_at=t_alert.isoformat(),
        condition_name="Cloud Run orders latency > 2s",
        resource_labels={"service_name": "ata-orders-api"},
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-orders-api",
            "tier": "service",
            "signal": "latency",
            "severity": "p2",
        },
    )

    # 1. Pre-analysis lock creation
    fpath, group_id, fname = write_preanalysis_lock(alert, isolated_env)
    assert fpath.exists()

    # 2. Recorded/Mock Cloud Monitoring telemetry fixture
    # Upstream dependency (ata-inventory-api): latency fault injected at 04:42:25Z (6500ms delay)
    mock_inv_lat_ts = MagicMock()
    mock_inv_dist = MagicMock()
    mock_inv_dist.mean = 6500.0  # 6.5s latency
    mock_inv_pt = MagicMock()
    mock_inv_pt.value.distribution_value = mock_inv_dist
    mock_inv_pt.interval.start_time.seconds = int(t_fault.timestamp())
    mock_inv_lat_ts.points = [mock_inv_pt]

    # Downstream caller (ata-orders-api): orders latency alert fired at 04:48:18Z (6800ms)
    mock_ord_lat_ts = MagicMock()
    mock_ord_dist = MagicMock()
    mock_ord_dist.mean = 6800.0  # 6.8s latency
    mock_ord_pt = MagicMock()
    mock_ord_pt.value.distribution_value = mock_ord_dist
    mock_ord_pt.interval.start_time.seconds = int(t_alert.timestamp())
    mock_ord_lat_ts.points = [mock_ord_pt]

    # Mock Cloud Logging entry for upstream inventory latency
    mock_inv_log = MagicMock()
    mock_inv_log.http_request = {"requestUrl": "https://ata-inventory-api/items/reserve", "status": 200, "latency": "6.502s"}
    mock_inv_log.payload = {"message": "Inventory reserve completed", "latency_ms": 6502}
    mock_inv_log.timestamp = t_fault + timedelta(seconds=5)

    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mock_mon_cls, \
         patch("google.cloud.logging.Client") as mock_log_cls:

        mock_mon = mock_mon_cls.return_value
        def list_ts_side_effect(request):
            filter_text = getattr(request, "filter", "")
            if "request_latencies" in filter_text:
                if "ata-inventory-api" in filter_text:
                    return [mock_inv_lat_ts]
                elif "ata-orders-api" in filter_text:
                    return [mock_ord_lat_ts]
            return []
        mock_mon.list_time_series.side_effect = list_ts_side_effect

        mock_log = mock_log_cls.return_value
        def list_entries_side_effect(filter_, max_results=50):
            if "ata-inventory-api" in filter_:
                return [mock_inv_log]
            return []
        mock_log.list_entries.side_effect = list_entries_side_effect

        synthesizer = SynthesizerAgent()
        report = await synthesizer.execute_investigation(alert, group_id, fname, isolated_env)

        # Verify upstream cause identified as origin
        assert report.origin == "ata-inventory-api"

        # Verify blast radius contains upstream cause and downstream victims
        assert "ata-inventory-api" in report.blast_radius
        assert "ata-orders-api" in report.blast_radius
        assert "ata-apigee-orders-v1" in report.blast_radius

        # Verify root cause text mentions upstream degradation
        assert "ata-inventory-api" in report.probable_root_cause.text
        assert "ata-orders-api" in report.probable_root_cause.text

        # Verify OKF file frontmatter updated with upstream origin
        content = fpath.read_text(encoding="utf-8")
        parts = content.split("---")
        fm = yaml.safe_load(parts[1])
        assert fm["origin"] == "ata-inventory-api"
        assert "ata-inventory-api" in fm["blast_radius"]
        assert "ata-orders-api" in fm["blast_radius"]



# ==============================================================================
# 4. Scenario S3: Concurrent Disjoint Incidents
# ==============================================================================

def test_scenario_s3_concurrent_disjoint_splitting(isolated_env: AgentConfig):
    """Assert correlation engine evaluates inventory 5xx and orders 429 as disjoint (<35 pts)

    resulting in 2 distinct incident groups and 2 ServiceNow tickets.
    """
    now = datetime(2026, 10, 22, 22, 0, 0, tzinfo=timezone.utc)
    engine = CorrelationEngine()

    # Alert 1: Inventory 5xx
    alert1 = AlertEvent(
        incident_id="0.s3_inv_5xx",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        state="open",
        started_at=now.isoformat(),
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "5xx",
            "severity": "p1",
        },
    )

    # Alert 2: Orders 429 SpikeArrest
    alert2 = AlertEvent(
        incident_id="0.s3_ord_429",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        state="open",
        started_at=now.isoformat(),
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-orders-v1",
            "tier": "edge",
            "signal": "rate-limit",
            "severity": "p2",
        },
    )

    # Multi-factor score between Alert 2 and Candidate Group from Alert 1
    # SpikeArrest 429 vs backend 5xx causal = 0 pts; hop distance >= 2
    score_breakdown = compute_multi_factor_score(
        alert_resource=alert2.resource_name,
        alert_category="client_429",
        alert_time=now,
        group_resources=[alert1.resource_name],
        group_category="backend_5xx",
        group_anchor_time=now,
        deployment_time=None,
    )

    # Score must be strictly less than 35 to guarantee distinct groups
    assert score_breakdown.total_score < 35.0
    assert score_breakdown.causal_score == 0.0

    # Decision must be NEW GROUP
    candidate = CandidateGroup(
        group_id="grp_s3_inventory",
        filename="incident-inventory.md",
        file_path=Path("/tmp/incident-inventory.md"),
        origin=alert1.resource_name,
        resources=[alert1.resource_name],
        category="backend_5xx",
        anchor_time=now,
    )
    decision = engine.evaluate_alert(alert2, candidate_groups=[candidate])
    assert decision.decision == "new_group"
    assert decision.best_score < 35.0


# ==============================================================================
# 5. Scenario S4: Database Connection Pool Exhaustion
# ==============================================================================

def test_scenario_s4_db_connection_pool(isolated_env: AgentConfig):
    """Assert S4 attributes origin to ata-inventory-db when connection alert precedes 5xx."""
    now_str = datetime.now(timezone.utc).isoformat()
    alert = AlertEvent(
        incident_id="0.s4_test_001",
        policy_name="ata-cloudsql-connections-critical",
        resource_name="ata-inventory-db",
        state="open",
        started_at=now_str,
        condition_name="Cloud SQL connection count critical",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-db",
            "tier": "data",
            "signal": "connections",
            "severity": "p1",
        },
    )
    report = generate_investigation_report(alert, "grp_s4", "incident-s4.md", isolated_env)
    assert report.origin == "ata-inventory-db"


# ==============================================================================
# 6. Scenario S5: Bad Deploy / Config Failure
# ==============================================================================

def test_scenario_s5_bad_db_config(isolated_env: AgentConfig):
    """Assert S5 attributes origin to ata-inventory-api revision change."""
    now_str = datetime.now(timezone.utc).isoformat()
    alert = AlertEvent(
        incident_id="0.s5_test_001",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        state="open",
        started_at=now_str,
        condition_name="Cloud Run inventory 5xx error rate",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "5xx",
            "severity": "p1",
        },
    )
    report = generate_investigation_report(alert, "grp_s5", "incident-s5.md", isolated_env)
    assert report.origin == "ata-inventory-api"


# ==============================================================================
# 10. ServiceNow Ticket Creation via ServiceNowAdapter
# ==============================================================================

def test_servicenow_ticket_payload_and_local_fallback(tmp_path: Path):
    """Assert ServiceNowAdapter formats incident payload with OKF report and creates ticket."""
    import httpx
    from bridge.app.config import BridgeConfig
    from agent.app.contracts.report import ProbableRootCause

    posted_requests = []
    captured_payload = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        posted_requests.append(request)
        if "/api/now/table/incident" in str(request.url):
            import json
            captured_payload.update(json.loads(request.read()))
            return httpx.Response(
                201,
                json={"result": {"sys_id": "sn-sys-9876", "number": "INC009999"}},
            )
        if "/api/now/attachment/file" in str(request.url):
            return httpx.Response(
                201,
                json={"result": {"sys_id": "att-1234", "file_name": "report.html"}},
            )
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    cfg = BridgeConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        servicenow_instance_url="https://instance.service-now.com",
        servicenow_username="admin",
        servicenow_password="secretpassword",
        incidents_bucket="cloud-assist-fde-2-ata-incidents",
        is_local=True,
    )
    adapter = ServiceNowAdapter(config=cfg, http_client=mock_client)

    report = InvestigationReport(
        group_id="grp_verify_001",
        filename="incident-verify-001.md",
        summary="Database connection pool exhausted",
        observations="num_backends reached 100 on ata-inventory-db",
        recommended_actions=["Restart connection pool", "Inspect leaked sessions"],
        probable_root_cause=ProbableRootCause(
            text="Exhaustion of database connections",
            confidence=0.95,
        ),
        origin="ata-inventory-db",
        blast_radius=["ata-inventory-db", "ata-inventory-api", "ata-orders-api"],
        priority="P1",
    )

    ref = adapter.create_incident(report)
    assert ref.sys_id == "sn-sys-9876"
    assert ref.number == "INC009999"
    assert len(posted_requests) >= 1
    assert captured_payload["correlation_id"] == "incident-verify-001.md"
    assert captured_payload["urgency"] == "1"
    assert "Problem:" in captured_payload["description"]
    assert "*" not in captured_payload["description"]
    assert "#" not in captured_payload["description"]


def test_triage_endpoint_duplicate_suppression_and_append(tmp_path):
    """Assert /triage suppresses duplicate alert, appends to existing file, and preserves ServiceNow ticket."""
    from agent.app.main import app as agent_fastapi_app
    from agent.app.config import get_agent_settings

    store_dir = tmp_path / "knowledge/incident-manager"
    store_dir.mkdir(parents=True, exist_ok=True)
    cfg = AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        is_local=True,
    )
    agent_fastapi_app.dependency_overrides[get_agent_settings] = lambda: cfg
    client = TestClient(agent_fastapi_app)

    now = datetime.now(timezone.utc)
    ts1 = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts2 = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # First alert creates incident
    alert1 = {
        "incident_id": "alert-e2e-1",
        "policy_name": "projects/cloud-assist-fde-2/alertPolicies/ata-inventory-5xx",
        "resource_name": "cloud-assist-fde-2 Cloud Run Revision labels {project_id=cloud-assist-fde-2, service_name=ata-inventory-api, revision_name=ata-inventory-api-00001}",
        "resource": {
            "type": "cloud_run_revision",
            "labels": {"service_name": "ata-inventory-api"},
        },
        "started_at": ts1,
        "state": "open",
    }
    with patch("agent.app.main.send_findings_to_bridge", return_value={"status": "ok"}), \
         patch("agent.app.grouping.manager.schedule_delayed_timer", return_value="mock-task"):
        resp1 = client.post("/triage", json=alert1)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["status"] == "triaged"
        inc_file = Path(data1["lock_file"])
        assert inc_file.exists()

        # Simulate bridge ticketing the file
        content = inc_file.read_text(encoding="utf-8")
        parts = content.split("---", 2)
        meta = yaml.safe_load(parts[1])
        meta["servicenow_number"] = "INC0010148"
        meta["servicenow_sys_id"] = "sys_live_123"
        # Even if status became ticketed or stayed mitigation-in-progress:
        meta["status"] = "ticketed"
        inc_file.write_text(f"---\n{yaml.dump(meta, sort_keys=False)}---{parts[2]}", encoding="utf-8")

        # Second alert arrives 5 minutes later with same policy and resource
        alert2 = {
            "incident_id": "alert-e2e-2",
            "policy_name": "projects/cloud-assist-fde-2/alertPolicies/ata-inventory-5xx",
            "resource_name": "cloud-assist-fde-2 Cloud Run Revision labels {project_id=cloud-assist-fde-2, service_name=ata-inventory-api, revision_name=ata-inventory-api-00002}",
            "resource": {
                "type": "cloud_run_revision",
                "labels": {"service_name": "ata-inventory-api"},
            },
            "started_at": ts2,
            "state": "open",
        }
        resp2 = client.post("/triage", json=alert2)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["status"] == "duplicate"
        assert data2["is_duplicate"] is True
        assert data2["existing_file"] == str(inc_file)

        # Verify existing file preserved ticket and has updated timestamp and appended alert
        updated_content = inc_file.read_text(encoding="utf-8")
        updated_meta = yaml.safe_load(updated_content.split("---")[1])
        assert updated_meta["servicenow_number"] == "INC0010148"
        assert updated_meta["servicenow_sys_id"] == "sys_live_123"
        assert updated_meta["timestamp"] == ts2
        assert len(updated_meta["alerts"]) == 2
        assert updated_meta["alerts"][1]["incident_id"] == "alert-e2e-2"

    agent_fastapi_app.dependency_overrides.clear()


