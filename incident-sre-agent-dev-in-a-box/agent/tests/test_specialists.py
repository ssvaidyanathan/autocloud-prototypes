from datetime import datetime, timezone
from unittest.mock import MagicMock

from agent.app.contracts.findings import Findings
from agent.app.contracts.incident import Incident
from agent.app.specialists.changes import ChangesSpecialist
from agent.app.specialists.logs import LogsSpecialist
from agent.app.specialists.metrics import MetricsSpecialist
from agent.app.specialists.resources import ResourcesSpecialist


def _make_incident() -> Incident:
    return Incident(
        id="inc-test-001",
        source="servicenow",
        external_id="INC0012345",
        external_key="INC0012345",
        external_url="https://instance.service-now.com/nav_to.do?uri=incident.do?sys_id=123",
        title="Spike in 500 errors on shop-api",
        description="Checkout endpoint is returning HTTP 500 intermittently since 02:00 UTC",
        severity="P2",
        service_hint="shop-api",
        labels=["production", "checkout"],
        opened_at=datetime(2026, 10, 9, 2, 10, 0, tzinfo=timezone.utc),
        reporter="sre-oncall@example.com",
    )


def test_logs_specialist_produces_valid_findings() -> None:
    mock_client = MagicMock()
    # Mock log entries: one ERROR, one WARNING
    entry_err = MagicMock()
    entry_err.http_request = None
    entry_err.httpRequest = None
    entry_err.timestamp = datetime(2026, 10, 9, 2, 5, 0, tzinfo=timezone.utc)
    entry_err.severity = "ERROR"
    entry_err.text_payload = "Database connection timed out"
    entry_err.payload = "Database connection timed out"
    entry_err.log_name = "projects/test-proj/logs/shop-api"

    entry_warn = MagicMock()
    entry_warn.http_request = None
    entry_warn.httpRequest = None
    entry_warn.timestamp = datetime(2026, 10, 9, 2, 7, 0, tzinfo=timezone.utc)
    entry_warn.severity = "WARNING"
    entry_warn.text_payload = "Scenario 2 (Latency Spike): Injecting 6.93s delay"
    entry_warn.payload = "Scenario 2 (Latency Spike): Injecting 6.93s delay"
    entry_warn.log_name = "projects/test-proj/logs/shop-api"

    mock_client.list_entries.return_value = [entry_err, entry_warn]

    # Mock error groups
    grp = MagicMock()
    grp.group_id = "grp-err-1"
    grp.message = "Database connection timed out"
    grp.count = 25
    mock_client.list_group_stats.return_value = [grp]

    specialist = LogsSpecialist(project_id="test-proj", client=mock_client)
    incident = _make_incident()

    findings = specialist.investigate(incident)

    assert isinstance(findings, Findings)
    assert findings.confidence > 0.5
    assert len(findings.evidence) > 0
    assert len(findings.anomalies) > 0
    assert len(findings.queries_run) > 0
    assert not findings.errors

    # Verify query filter uses severity >= WARNING
    filters = [c.kwargs.get("filter_", "") for c in mock_client.list_entries.call_args_list]
    assert any("severity >= WARNING" in f and "shop-api" in f for f in filters)

    # Verify WARNING entry captured with kind warning_log
    warning_items = [e for e in findings.evidence if e.kind == "warning_log"]
    assert len(warning_items) >= 1
    assert "Injecting 6.93s delay" in warning_items[0].excerpt

    # Verify latency warning anomaly extracted
    assert any("latency spike / processing delay" in a.lower() for a in findings.anomalies)


def test_metrics_specialist_produces_valid_findings() -> None:
    mock_client = MagicMock()

    # Alert incident
    inc = MagicMock()
    inc.incident_id = "alert-123"
    inc.policy_name = "5xx Spike Policy"
    inc.summary = "5xx error rate exceeded 5%"
    mock_client.list_incidents.return_value = [inc]

    # 5xx error series
    ts_curr_5xx = MagicMock()
    pt_5xx = MagicMock()
    pt_5xx.value.double_value = 85.0
    ts_curr_5xx.points = [pt_5xx]

    ts_base_5xx = MagicMock()
    pt_base_5xx = MagicMock()
    pt_base_5xx.value.double_value = 5.0
    ts_base_5xx.points = [pt_base_5xx]

    # Latency series (in seconds, e.g. 5.82s current vs 0.05s baseline)
    ts_curr_lat = MagicMock()
    pt_lat = MagicMock()
    pt_lat.value.double_value = 5.82
    ts_curr_lat.points = [pt_lat]

    ts_base_lat = MagicMock()
    pt_base_lat = MagicMock()
    pt_base_lat.value.double_value = 0.05
    ts_base_lat.points = [pt_base_lat]

    mock_client.list_time_series.side_effect = [
        [ts_curr_5xx],
        [ts_base_5xx],
        [ts_curr_lat],
        [ts_base_lat],
    ]

    specialist = MetricsSpecialist(project_id="test-proj", client=mock_client)
    incident = _make_incident()

    findings = specialist.investigate(incident)

    assert isinstance(findings, Findings)
    assert findings.confidence >= 0.8
    assert len(findings.evidence) >= 3
    assert len(findings.anomalies) >= 2
    assert not findings.errors

    # Verify MetricsSpecialist queries both 5xx request count and
    # request_latencies with ALIGN_PERCENTILE_95
    calls = mock_client.list_time_series.call_args_list
    assert len(calls) == 4

    filters_queried = []
    aligners_queried = []
    for call in calls:
        req = call.kwargs.get("request", {})
        filt = req.get("filter", "")
        filters_queried.append(filt)
        agg = req.get("aggregation", {})
        if agg:
            aligners_queried.append(agg.get("per_series_aligner"))

    assert any("run.googleapis.com/request_count" in f for f in filters_queried)
    assert any("run.googleapis.com/request_latencies" in f for f in filters_queried)
    assert "ALIGN_PERCENTILE_95" in aligners_queried


def test_changes_specialist_produces_valid_findings() -> None:
    mock_client = MagicMock()

    # Audit log
    audit_entry = MagicMock()
    audit_entry.timestamp = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    audit_entry.proto_payload = {
        "methodName": "google.cloud.run.v1.Services.ReplaceService",
        "authenticationInfo": {"principalEmail": "deployer@example.com"},
        "resourceName": "services/shop-api",
        "status": {"code": 0},
    }
    mock_client.list_entries.return_value = [audit_entry]

    # Revision
    rev = MagicMock()
    rev.name = "shop-api-rev-2"
    rev.create_time = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    rev.traffic_percent = 100
    rev.template.containers = [MagicMock(image="gcr.io/test-proj/shop-api:v2")]
    mock_client.list_revisions.return_value = [rev]

    specialist = ChangesSpecialist(project_id="test-proj", client=mock_client)
    incident = _make_incident()

    findings = specialist.investigate(incident)

    assert isinstance(findings, Findings)
    assert findings.confidence >= 0.7
    assert len(findings.evidence) >= 2
    assert len(findings.anomalies) >= 1
    assert not findings.errors


def test_resources_specialist_produces_valid_findings() -> None:
    mock_client = MagicMock()

    # Cloud Run
    svc = MagicMock()
    svc.uri = "https://shop-api.run.app"
    template = MagicMock()
    container = MagicMock()
    container.resources.limits = {"cpu": "1", "memory": "256Mi"}
    container.env = [{"name": "DATABASE_URL"}, {"name": "PORT"}]
    template.containers = [container]
    template.max_instance_request_concurrency = 80
    svc.template = template
    mock_client.get_service.return_value = svc

    # Cloud SQL
    sql_inst = MagicMock()
    sql_inst.name = "shop-db"
    sql_inst.database_version = "POSTGRES_15"
    sql_inst.state = "RUNNABLE"
    mock_client.list_instances.return_value = [sql_inst]

    specialist = ResourcesSpecialist(project_id="test-proj", client=mock_client)
    incident = _make_incident()

    findings = specialist.investigate(incident)

    assert isinstance(findings, Findings)
    assert len(findings.evidence) >= 1
    # 256Mi memory limit triggers anomaly flag
    assert any("Constrained memory" in a for a in findings.anomalies)


def test_specialist_error_containment() -> None:
    mock_client = MagicMock()
    # Force client to explode
    mock_client.list_entries.side_effect = RuntimeError("Fatal GCP network timeout")
    mock_client.list_group_stats.side_effect = RuntimeError("Fatal GCP network timeout")

    specialist = LogsSpecialist(project_id="test-proj", client=mock_client)
    incident = _make_incident()

    # Investigation should not raise, but return graceful Findings with error captured
    findings = specialist.investigate(incident)

    assert isinstance(findings, Findings)
    assert len(findings.errors) > 0
    assert "Fatal GCP network timeout" in findings.errors[0]


def test_specialists_get_tools_includes_new_tools() -> None:
    logs_spec = LogsSpecialist(project_id="test-proj")
    changes_spec = ChangesSpecialist(project_id="test-proj")
    resources_spec = ResourcesSpecialist(project_id="test-proj")

    logs_tools = [t.name for t in logs_spec.get_tools()]
    assert "query_request_logs" in logs_tools
    assert "aggregate_requests" in logs_tools

    changes_tools = [t.name for t in changes_spec.get_tools()]
    assert "diff_revisions" in changes_tools

    resources_tools = [t.name for t in resources_spec.get_tools()]
    assert "get_platform_events" in resources_tools
