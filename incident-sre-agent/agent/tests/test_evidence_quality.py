"""Regression tests for Phase 6R-Q Evidence Quality Fixes.

Validates:
1. Logs tool and specialist: robust extraction across httpRequest, jsonPayload, protoPayload.
   Never emits empty '[ERROR] {}'.
2. Changes tool and specialist: audit log extraction across ProtobufEntry and dict shapes.
   Includes principalEmail and resourceName. Never emits 'Action  on  by '.
3. Cloud Run revisions: joins Service traffic statuses with Revision author/creator annotations.
   Accurately maps 100% traffic to active serving revision.
4. Cloud Monitoring: delta metric aggregation with ALIGN_RATE, REDUCE_SUM, and alignment_period.
   Captures non-zero rates.
5. Synthesizer grounding and confidence post-check: caps confidence and appends uncertainty notes
   when evidence is empty, broken, or conflicting.
"""

import json
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

from agent.app.contracts.findings import EvidenceItem, Findings
from agent.app.contracts.incident import Incident
from agent.app.contracts.report import ProbableRootCause, Report
from agent.app.specialists.changes import ChangesSpecialist
from agent.app.specialists.logs import LogsSpecialist
from agent.app.synthesizer import calibrate_report_confidence
from agent.app.tools.changes import list_cloud_run_revisions, query_audit_logs
from agent.app.tools.logs import _extract_entry_excerpt, query_logs, summarize_error_groups
from agent.app.tools.metrics import compare_to_baseline, query_time_series
from google.cloud.logging_v2.entries import ProtobufEntry

# ============================================================================
# Defect 1: Cloud Run Request Logs & Error Grouping
# ============================================================================


def test_cloud_run_request_log_empty_payload_extracted_informatively() -> None:
    """Regression test for Defect 1: Cloud Run request log has httpRequest and empty payload."""
    raw_api_repr = {
        "logName": "projects/test-proj/logs/run.googleapis.com%2Frequests",
        "severity": "ERROR",
        "timestamp": "2026-10-09T05:36:27.828498Z",
        "httpRequest": {
            "requestMethod": "GET",
            "requestUrl": "https://test-app-uc.a.run.app/api/v1/sync-tenant-data",
            "requestSize": "1070",
            "status": 500,
            "responseSize": "329",
            "userAgent": "curl/7.88.1",
            "remoteIp": "136.112.197.168",
            "serverIp": "34.143.76.2",
            "latency": "0.037559992s",
            "protocol": "HTTP/1.1",
        },
        "resource": {
            "type": "cloud_run_revision",
            "labels": {
                "service_name": "test-app",
                "revision_name": "test-app-mv0j7owe",
                "location": "us-central1",
            },
        },
    }

    mock_entry = MagicMock()
    mock_entry.log_name = raw_api_repr["logName"]
    mock_entry.severity = raw_api_repr["severity"]
    mock_entry.timestamp = datetime(2026, 10, 9, 5, 36, 27, tzinfo=timezone.utc)
    mock_entry.http_request = raw_api_repr["httpRequest"]
    mock_entry.payload = None
    mock_entry.text_payload = None
    mock_entry.json_payload = {}
    mock_entry.proto_payload = None
    mock_entry.resource.type = "cloud_run_revision"

    # Test direct extractor
    excerpt = _extract_entry_excerpt(mock_entry)
    assert "[ERROR] {}" not in excerpt
    assert "{}" not in excerpt
    assert "HTTP 500" in excerpt
    assert "GET" in excerpt
    assert "/api/v1/sync-tenant-data" in excerpt
    assert "0.037559992s" in excerpt

    # Test query_logs tool output
    mock_client = MagicMock()
    mock_client.list_entries.return_value = [mock_entry]

    result = query_logs(
        filter_str="severity >= ERROR",
        resource_names=["projects/test-proj"],
        max_entries=10,
        client=mock_client,
    )

    assert result["total_returned"] == 1
    assert len(result["entries"]) == 1
    entry_res = result["entries"][0]
    assert entry_res["severity"] == "ERROR"
    assert "HTTP 500 GET" in entry_res["payload_excerpt"]
    assert entry_res["payload_excerpt"] != "{}"

    # Test LogsSpecialist evidence generation
    specialist = LogsSpecialist(project_id="test-proj", client=mock_client)
    incident = Incident(
        id="inc-test-1",
        source="servicenow",
        external_id="INC001",
        external_key="INC001",
        title="HTTP 500 on test-app",
        description="Tenant sync returning 500",
        severity="P1",
        service_hint="test-app",
        opened_at=datetime(2026, 10, 9, 5, 40, 0, tzinfo=timezone.utc),
    )
    findings = specialist.run_investigation(
        incident=incident,
        start_time=datetime(2026, 10, 9, 5, 30, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 10, 9, 5, 45, 0, tzinfo=timezone.utc),
    )
    assert len(findings.evidence) >= 1
    log_evidence = [e for e in findings.evidence if e.kind == "log_entry"]
    assert len(log_evidence) == 1
    assert "[ERROR] HTTP 500 GET" in log_evidence[0].excerpt
    assert "[ERROR] {}" not in log_evidence[0].excerpt


def test_summarize_error_groups_fallback_groups_on_real_fields() -> None:
    """Validate that summarize_error_groups groups on real status codes,
    never on empty string or literal '{}'.
    """
    mock_entry1 = MagicMock()
    mock_entry1.timestamp = datetime(2026, 10, 9, 5, 36, 27, tzinfo=timezone.utc)
    mock_entry1.severity = "ERROR"
    mock_entry1.http_request = {
        "status": 500,
        "requestMethod": "GET",
        "requestUrl": "/api/sync",
        "latency": "0.03s",
    }
    mock_entry1.payload = None
    mock_entry1.resource.labels = {"service_name": "test-app"}

    # standard logging client without Error Reporting
    mock_client = MagicMock(spec=["list_entries"])
    mock_client.list_entries.return_value = [mock_entry1, mock_entry1]

    res = summarize_error_groups(project_id="test-proj", time_window_hours=1, client=mock_client)
    assert res["total_groups"] == 1
    grp = res["error_groups"][0]
    assert "HTTP 500 GET /api/sync" in grp["message"]
    assert grp["message"] != "{}"
    assert grp["count"] == 2
    assert "test-app" in grp["affected_services"]


# ============================================================================
# Defect 2: Audit Logs ProtobufEntry & Action Extraction
# ============================================================================


def test_audit_logs_extracts_protobuf_entry_attributes() -> None:
    """Regression test for Defect 2: Audit log excerpts must extract methodName,
    resourceName, and principalEmail.
    """
    raw_audit_api_repr = {
        "logName": "projects/test-proj/logs/cloudaudit.googleapis.com%2Factivity",
        "timestamp": "2026-10-09T05:36:13.616022Z",
        "severity": "NOTICE",
        "protoPayload": {
            "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
            "status": {},
            "authenticationInfo": {
                "principalEmail": "deploy-sa@test-proj.iam.gserviceaccount.com",
                "principalSubject": "serviceAccount:deploy-sa@test-proj.iam.gserviceaccount.com",
            },
            "serviceName": "run.googleapis.com",
            "methodName": "google.cloud.run.v1.Services.ReplaceService",
            "resourceName": "namespaces/test-proj/services/test-app",
        },
    }

    mock_client = MagicMock()
    mock_client.project = "test-proj"
    # Construct a real google.cloud.logging_v2 ProtobufEntry
    real_proto_entry = ProtobufEntry.from_api_repr(  # type: ignore[no-untyped-call]
        raw_audit_api_repr, mock_client
    )
    mock_client.list_entries.return_value = [real_proto_entry]

    start = datetime(2026, 10, 9, 5, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 9, 6, 0, 0, tzinfo=timezone.utc)

    # 1. Test query_audit_logs tool
    audit_events = query_audit_logs(
        project_id="test-proj",
        start_time=start,
        end_time=end,
        service_name="test-app",
        client=mock_client,
    )

    assert len(audit_events) == 1
    evt = audit_events[0]
    assert evt["method_name"] == "google.cloud.run.v1.Services.ReplaceService"
    assert evt["resource_name"] == "namespaces/test-proj/services/test-app"
    assert evt["principal_email"] == "deploy-sa@test-proj.iam.gserviceaccount.com"

    # 2. Test ChangesSpecialist excerpt generation
    specialist = ChangesSpecialist(project_id="test-proj", client=mock_client)
    incident = Incident(
        id="inc-test-2",
        source="servicenow",
        external_id="INC002",
        external_key="INC002",
        title="Admin deploy on test-app",
        description="Investigate deployment",
        severity="P2",
        service_hint="test-app",
        opened_at=end,
    )
    findings = specialist.run_investigation(incident=incident, start_time=start, end_time=end)

    audit_evidence = [e for e in findings.evidence if e.kind == "audit_log"]
    assert len(audit_evidence) == 1
    assert "Action google.cloud.run.v1.Services.ReplaceService" in audit_evidence[0].excerpt
    assert "namespaces/test-proj/services/test-app" in audit_evidence[0].excerpt
    assert "deploy-sa@test-proj.iam.gserviceaccount.com" in audit_evidence[0].excerpt
    assert "Action  on  by " not in audit_evidence[0].excerpt


# ============================================================================
# Defect 3: Cloud Run Revision Author & Service Traffic Split Join
# ============================================================================


def test_cloud_run_revisions_join_service_traffic_statuses_and_author() -> None:
    """Regression test for Defect 3: joins service.traffic_statuses with
    revision author annotations.
    """
    mock_rev_client = MagicMock()
    mock_svc_client = MagicMock()

    # Recorded Service with traffic statuses
    mock_service = MagicMock()
    target_traffic = MagicMock()
    target_traffic.revision = "test-app-mv0lfmh1"
    target_traffic.percent = 100
    mock_service.traffic_statuses = [target_traffic]
    mock_svc_client.get_service.return_value = mock_service

    # Recorded Revisions
    prefix = "projects/test-proj/locations/us-central1/services/test-app/revisions"
    rev1 = MagicMock()
    rev1.name = f"{prefix}/test-app-mv0lfmh1"
    rev1.creator = "deploy-sa@test-proj.iam.gserviceaccount.com"
    rev1.annotations = {}
    rev1.create_time = datetime(2026, 10, 9, 6, 38, 25, tzinfo=timezone.utc)
    rev1.template.containers = [MagicMock(image="us-central1-docker.pkg.dev/repo/app:v2")]

    rev2 = MagicMock()
    rev2.name = f"{prefix}/test-app-mv0j7owe"
    rev2.creator = "deploy-sa@test-proj.iam.gserviceaccount.com"
    rev2.annotations = {}
    rev2.create_time = datetime(2026, 10, 9, 5, 36, 13, tzinfo=timezone.utc)
    rev2.template.containers = [MagicMock(image="us-central1-docker.pkg.dev/repo/app:v1")]

    mock_rev_client.list_revisions.return_value = [rev1, rev2]

    # Call tool with both clients
    revisions = list_cloud_run_revisions(
        project_id="test-proj",
        service_name="test-app",
        region="us-central1",
        client=mock_rev_client,
        services_client=mock_svc_client,
    )

    assert len(revisions) == 2
    r1 = next(r for r in revisions if r["name"] == "test-app-mv0lfmh1")
    r2 = next(r for r in revisions if r["name"] == "test-app-mv0j7owe")

    # Verify joined traffic percentages
    assert r1["traffic_percent"] == 100
    assert r2["traffic_percent"] == 0

    # Verify author
    assert r1["author"] == "deploy-sa@test-proj.iam.gserviceaccount.com"
    assert r2["author"] == "deploy-sa@test-proj.iam.gserviceaccount.com"


# ============================================================================
# Defect 4: Cloud Monitoring Delta Metric Aggregation
# ============================================================================


def test_compare_to_baseline_delta_metric_aggregation() -> None:
    """Regression test for Defect 4: delta metrics require ALIGN_RATE aggregation
    to capture non-zero rates.
    """
    mock_client = MagicMock()

    # Mock time series with non-zero ALIGN_RATE points
    pt1 = MagicMock()
    pt1.value.double_value = 0.15
    pt2 = MagicMock()
    pt2.value.double_value = 0.25
    series_curr = MagicMock()
    series_curr.metric.type = "run.googleapis.com/request_count"
    series_curr.points = [pt1, pt2]

    pt_base = MagicMock()
    pt_base.value.double_value = 0.0
    series_base = MagicMock()
    series_base.metric.type = "run.googleapis.com/request_count"
    series_base.points = [pt_base]

    mock_client.list_time_series.side_effect = [[series_curr], [series_base]]

    now = datetime(2026, 10, 9, 6, 0, 0, tzinfo=timezone.utc)
    filter_str = (
        'metric.type = "run.googleapis.com/request_count" AND '
        'metric.labels.response_code_class = "5xx"'
    )

    res = compare_to_baseline(
        filter_str=filter_str,
        project_id="test-proj",
        current_start=now,
        current_end=now,
        baseline_start=now,
        baseline_end=now,
        alignment_period_seconds=60,
        per_series_aligner="ALIGN_RATE",
        cross_series_reducer="REDUCE_SUM",
        client=mock_client,
    )

    # Verify aggregation was passed to list_time_series
    calls = mock_client.list_time_series.call_args_list
    assert len(calls) == 2
    req = calls[0].kwargs.get("request", {})
    assert "aggregation" in req
    assert req["aggregation"]["per_series_aligner"] == "ALIGN_RATE"
    assert req["aggregation"]["cross_series_reducer"] == "REDUCE_SUM"
    assert req["aggregation"]["alignment_period"] == {"seconds": 60}

    # Verify calculated mean is non-zero
    assert res["current_period"]["mean"] == 0.2
    assert res["current_period"]["sample_count"] == 2
    assert res["baseline_period"]["mean"] == 0.0


def test_query_time_series_supports_aggregation_parameters() -> None:
    """Verify query_time_series builds aggregation for monitoring query."""
    mock_client = MagicMock()
    mock_client.list_time_series.return_value = []

    now = datetime(2026, 10, 9, 6, 0, 0, tzinfo=timezone.utc)
    query_time_series(
        filter_str='metric.type = "run.googleapis.com/request_count"',
        project_id="test-proj",
        start_time=now,
        end_time=now,
        alignment_period_seconds=120,
        per_series_aligner="ALIGN_DELTA",
        cross_series_reducer="REDUCE_SUM",
        client=mock_client,
    )

    calls = mock_client.list_time_series.call_args_list
    assert len(calls) == 1
    req = calls[0].kwargs.get("request", {})
    assert req["aggregation"]["alignment_period"] == {"seconds": 120}
    assert req["aggregation"]["per_series_aligner"] == "ALIGN_DELTA"


# ============================================================================
# Defect 5: Synthesizer Grounding & Confidence Post-Check
# ============================================================================


def test_synthesizer_post_check_caps_confidence_on_empty_evidence() -> None:
    """Regression test for Defect 5: zero evidence items must cap confidence at <= 0.25."""
    uncalibrated_report = Report(
        summary="Claimed major outage with root cause",
        impact="High",
        timeline=[],
        evidence_by_area={},
        probable_root_cause=ProbableRootCause(
            text="Service failed due to ungrounded regression",
            confidence=0.95,
        ),
        recent_changes=[],
        recommended_actions=[],
        specialists_status=[],
        skills_used=[],
        queries_appendix={},
        metadata={},
    )

    empty_findings = {
        "logs": Findings(
            summary="No logs",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.4,
            errors=[],
        )
    }

    calibrated = calibrate_report_confidence(uncalibrated_report, empty_findings)
    assert calibrated.probable_root_cause is not None
    assert calibrated.probable_root_cause.confidence <= 0.25
    assert "Calibrated from" not in calibrated.probable_root_cause.text
    assert any("zero evidence items" in c for c in calibrated.caveats)


def test_synthesizer_post_check_caps_confidence_on_phase6r_broken_evidence() -> None:
    """Regression test for Defect 5: empty log payloads, zero metrics, and 0% traffic
    must cap confidence at <= 0.45.
    """
    broken_findings = {
        "logs": Findings(
            summary="Logs completed",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt="[ERROR] {} (log: run.googleapis.com%2Frequests)",
                )
            ],
            anomalies=[],
            queries_run=["query_logs"],
            confidence=0.85,
            errors=[],
        ),
        "metrics": Findings(
            summary="Metrics completed",
            evidence=[
                EvidenceItem(
                    kind="baseline_comparison",
                    excerpt=(
                        "Metric run.googleapis.com/request_count: "
                        "current mean=0.0 vs baseline mean=0.0"
                    ),
                )
            ],
            anomalies=[],
            queries_run=["compare_to_baseline"],
            confidence=0.45,
            errors=[],
        ),
        "changes": Findings(
            summary="Revisions completed",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision test-app-mv0j7owe (author: , traffic: 0%)",
                )
            ],
            anomalies=[],
            queries_run=["list_cloud_run_revisions"],
            confidence=0.70,
            errors=[],
        ),
    }

    overconfident_report = Report(
        summary="Service experienced HTTP 500 errors post-deployment",
        impact="Critical",
        timeline=[],
        evidence_by_area={},
        probable_root_cause=ProbableRootCause(
            text=(
                "Deployment introduced HTTP 500 errors. "
                "Remediation revision mitigated the incident."
            ),
            confidence=0.85,
        ),
        recent_changes=[],
        recommended_actions=[],
        specialists_status=[],
        skills_used=[],
        queries_appendix={},
        metadata={},
    )

    calibrated = calibrate_report_confidence(overconfident_report, broken_findings)
    assert calibrated.probable_root_cause is not None
    assert calibrated.probable_root_cause.confidence <= 0.40
    assert "Calibrated from" not in calibrated.probable_root_cause.text
    assert any(
        "HTTP 500/5xx asserted without corroborating status" in c for c in calibrated.caveats
    )


# ============================================================================
# Phase 6R-Q2 Regression Tests
# ============================================================================


class _FakeProtobufMapComposite:
    """Mock representing a protobuf MapComposite that is not an instance of dict."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data


class _FakeProtobufItemAccessOnly:
    """Mock representing a protobuf mapping with only item access (__getitem__, __contains__)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data


def test_describe_cloud_run_service_protobuf_map_composite_limits() -> None:
    """Regression test: MapComposite in resources.limits extracted instead of empty cpu=, mem=."""
    from agent.app.tools.resources import describe_cloud_run_service

    mock_client = MagicMock()
    mock_svc = MagicMock()
    mock_container = MagicMock()
    mock_container.image = "us-central1-docker.pkg.dev/proj/repo/app:123"
    mock_container.env = []
    mock_container.resources.limits = _FakeProtobufMapComposite({"cpu": "1", "memory": "512Mi"})
    mock_svc.template.containers = [mock_container]
    mock_client.get_service.return_value = mock_svc

    res = describe_cloud_run_service(
        project_id="test-proj",
        service_name="test-app",
        region="us-central1",
        client=mock_client,
    )

    assert res["cpu_limit"] == "1"
    assert res["memory_limit"] == "512Mi"

    # Also test mapping with only item access (no .get() method)
    mock_container.resources.limits = _FakeProtobufItemAccessOnly({"cpu": "2", "memory": "1Gi"})
    res2 = describe_cloud_run_service(
        project_id="test-proj",
        service_name="test-app",
        region="us-central1",
        client=mock_client,
    )
    assert res2["cpu_limit"] == "2"
    assert res2["memory_limit"] == "1Gi"


def test_compare_to_baseline_zero_baseline_new_signal_and_rate_unit() -> None:
    """Regression test: zero baseline with non-zero current sets is_anomalous=True,
    mean_change_percent=None, and specialist formats rate unit /s.
    """
    from agent.app.specialists.metrics import MetricsSpecialist

    mock_client = MagicMock()

    # Current period: rate points averaging 0.0011
    pt_curr = MagicMock()
    pt_curr.value.double_value = 0.0011
    ts_curr = MagicMock()
    ts_curr.metric.type = "run.googleapis.com/request_count"
    ts_curr.points = [pt_curr]

    # Baseline period: zero points
    pt_base = MagicMock()
    pt_base.value.double_value = 0.0
    ts_base = MagicMock()
    ts_base.metric.type = "run.googleapis.com/request_count"
    ts_base.points = [pt_base]

    mock_client.list_time_series.side_effect = [[ts_curr], [ts_base]]

    t1 = datetime(2026, 10, 9, 5, 15, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 9, 6, 30, 0, tzinfo=timezone.utc)

    # 1. Test compare_to_baseline tool directly
    res = compare_to_baseline(
        filter_str='metric.type = "run.googleapis.com/request_count"',
        project_id="test-proj",
        current_start=t1,
        current_end=t2,
        baseline_start=t1,
        baseline_end=t2,
        client=mock_client,
    )

    assert res["is_anomalous"] is True
    assert res["mean_change_percent"] is None
    assert res["p95_change_percent"] is None
    assert res["current_period"]["mean"] == 0.0011
    assert res["baseline_period"]["mean"] == 0.0

    # 2. Test MetricsSpecialist formatting with /s rate unit
    mock_client.list_time_series.side_effect = [[ts_curr], [ts_base]]
    mock_client.list_alert_policies.return_value = []
    spec = MetricsSpecialist(project_id="test-proj", client=mock_client)
    inc = Incident(
        id="inc-1",
        source="servicenow",
        external_id="INC001",
        external_key="INC001",
        title="Elevated 5xx rate",
        description="5xx errors detected",
        severity="P1",
        opened_at=t1,
        service_hint="test-app",
    )
    findings = spec.run_investigation(incident=inc, start_time=t1, end_time=t2)

    assert len(findings.evidence) >= 1
    ev = findings.evidence[0]
    assert "current mean=0.0011/s vs baseline mean=0.0/s (new signal, baseline zero)" in ev.excerpt
    assert "+0.0% shift" not in ev.excerpt
    assert any(
        "New error signal detected" in a and "0.0011/s over 0.0/s baseline" in a
        for a in findings.anomalies
    )


def test_calibrate_report_confidence_unverified_mitigation_adds_caveats() -> None:
    """Regression test: unverified mitigation without post-deployment telemetry
    populates caveats[], softens mitigation claims, and caps confidence at <= 0.65.
    """
    from agent.app.contracts.report import RecentChangeItem

    findings = {
        "logs": Findings(
            summary="Logs completed",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt="[ERROR] HTTP 500 GET /api/v1/sync-tenant-data (latency: 2.16s)",
                    timestamp=datetime(2026, 10, 9, 5, 36, 27, tzinfo=timezone.utc),
                )
            ],
            anomalies=["Elevated HTTP 500 error rate"],
            queries_run=["query_logs"],
            confidence=0.85,
            errors=[],
        ),
        "changes": Findings(
            summary="Changes completed",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision test-app-mv0lfmh1 (author: deploy-sa, traffic: 100%)",
                    timestamp=datetime(2026, 10, 9, 6, 38, 25, tzinfo=timezone.utc),
                )
            ],
            anomalies=[],
            queries_run=["list_cloud_run_revisions"],
            confidence=0.85,
            errors=[],
        ),
    }

    report = Report(
        summary=(
            "Service test-app experienced HTTP 500 errors. "
            "The incident was mitigated following subsequent revision deployment test-app-mv0lfmh1."
        ),
        impact="Critical tenant degradation.",
        timeline=[],
        evidence_by_area={},
        probable_root_cause=ProbableRootCause(
            text=(
                "Revision test-app-mv0j7owe caused regression. "
                "Revision test-app-mv0lfmh1 mitigated the incident."
            ),
            confidence=0.85,
        ),
        recent_changes=[
            RecentChangeItem(
                timestamp=datetime(2026, 10, 9, 6, 38, 25, tzinfo=timezone.utc),
                description="Revision test-app-mv0lfmh1 (traffic: 100%) deployed",
            )
        ],
        recommended_actions=["Verify endpoint logic."],
        specialists_status=[],
        skills_used=[],
        queries_appendix={},
        metadata={
            "window_start": "2026-10-09T05:15:00Z",
            "window_end": "2026-10-09T06:30:00Z",
        },
    )

    calibrated = calibrate_report_confidence(report, findings)

    # 1. Confidence capped at <= 0.65
    assert calibrated.probable_root_cause is not None
    assert calibrated.probable_root_cause.confidence <= 0.65

    # 2. Caveats populated
    assert len(calibrated.caveats) >= 1
    assert any(
        "Mitigation via revision test-app-mv0lfmh1 is provisional: no post-deployment telemetry"
        in c
        for c in calibrated.caveats
    )

    # 3. Claims softened in summary and probable root cause
    assert "is provisionally mitigated" in calibrated.summary
    assert "provisionally mitigated the incident" in calibrated.probable_root_cause.text


def test_report_contract_caveats_parity() -> None:
    """Regression test: caveats field is present and byte-identical
    across agent and bridge contracts.
    """
    from agent.app.contracts.report import Report as AgentReport
    from bridge.app.contracts.report import Report as BridgeReport

    agent_rep = AgentReport(
        summary="Test summary",
        impact="Test impact",
        caveats=["Caveat 1", "Caveat 2"],
    )
    bridge_rep = BridgeReport(
        summary="Test summary",
        impact="Test impact",
        caveats=["Caveat 1", "Caveat 2"],
    )

    assert agent_rep.caveats == ["Caveat 1", "Caveat 2"]
    assert bridge_rep.caveats == ["Caveat 1", "Caveat 2"]
    assert agent_rep.model_dump() == bridge_rep.model_dump()


# ============================================================================
# Phase 6R-Q3 Regression Tests
# ============================================================================


def test_summarize_error_groups_filters_probe_noise_and_respects_window() -> None:
    """Regression test for Defect 2: summarize_error_groups must enforce incident window
    and filter out infrastructure probe noise (169.254.x, 200 OK health checks, cilium noise).
    """
    t_start = datetime(2026, 10, 9, 5, 15, 0, tzinfo=timezone.utc)
    t_end = datetime(2026, 10, 9, 6, 30, 0, tzinfo=timezone.utc)

    class FakeLogEntry:
        def __init__(
            self,
            timestamp: datetime,
            severity: str,
            text_payload: Optional[str] = None,
            http_request: Optional[dict[str, Any]] = None,
            service_name: str = "test-app",
        ) -> None:
            self.timestamp = timestamp
            self.severity = severity
            self.text_payload = text_payload
            self.textPayload = None
            self.http_request = http_request
            self.httpRequest = None
            self.payload = None
            self.json_payload = None
            self.jsonPayload = None
            self.proto_payload = None
            self.protoPayload = None
            self.log_name = "projects/test-proj/logs/run.googleapis.com%2Frequests"
            self.resource = MagicMock()
            self.resource.labels = {"service_name": service_name}
            self.resource.type = "cloud_run_revision"

    # 1. Probe entry: link-local IP 169.254.x GET / 200
    mock_probe1 = FakeLogEntry(
        timestamp=datetime(2026, 10, 9, 7, 34, 48, tzinfo=timezone.utc),
        severity="ERROR",
        text_payload='169.254.4.6 - - [09/Oct/2026 07:34:48] "GET / HTTP/1.1" 200 -',
    )

    # 2. Probe entry: health check 200 OK
    mock_probe2 = FakeLogEntry(
        timestamp=datetime(2026, 10, 9, 7, 34, 49, tzinfo=timezone.utc),
        severity="ERROR",
        text_payload='10.204.1.1 - - [09/Oct/2026 07:34:49] "GET /health HTTP/1.1" 200 -',
    )

    # 3. Infra daemon noise: cilium / kube-system
    mock_probe3 = FakeLogEntry(
        timestamp=datetime(2026, 10, 9, 7, 34, 50, tzinfo=timezone.utc),
        severity="ERROR",
        text_payload=(
            "failed to list <unspecified>: the server could not find the requested resource "
            "(get ciliumpodippools.cilium.io)"
        ),
    )

    # 4. Real application error: HTTP 500 on application endpoint
    mock_app_error = FakeLogEntry(
        timestamp=datetime(2026, 10, 9, 5, 36, 27, tzinfo=timezone.utc),
        severity="ERROR",
        http_request={
            "status": 500,
            "requestMethod": "GET",
            "requestUrl": "https://test-app.run.app/api/v1/sync-tenant-data",
            "latency": "2.21s",
        },
    )

    mock_client = MagicMock(spec=["list_entries"])
    mock_client.list_entries.return_value = [
        mock_probe1,
        mock_probe2,
        mock_probe3,
        mock_app_error,
    ]

    res = summarize_error_groups(
        project_id="test-proj",
        start_time=t_start,
        end_time=t_end,
        service_name="test-app",
        client=mock_client,
    )

    # Verify query filter strictly enforced window and service name
    calls = mock_client.list_entries.call_args_list
    assert len(calls) == 1
    filter_arg = calls[0].kwargs.get("filter_")
    assert 'timestamp >= "2026-10-09T05:15:00+00:00"' in filter_arg
    assert 'timestamp <= "2026-10-09T06:30:00+00:00"' in filter_arg
    assert 'resource.labels.service_name = "test-app"' in filter_arg

    # Verify all 3 probe noise items were filtered out
    assert res["total_groups"] == 1
    grp = res["error_groups"][0]
    assert "HTTP 500 GET https://test-app.run.app/api/v1/sync-tenant-data" in grp["message"]
    assert "169.254." not in grp["message"]
    assert "GET /health" not in grp["message"]
    assert "cilium" not in grp["message"]


def test_probable_root_cause_pure_llm_prose_no_calibration_notes() -> None:
    """Regression test for Defect 1: calibrate_report_confidence must never
    concatenate [Calibrated from ...] notes into probable_root_cause.text.
    """
    initial_text = (
        "Deployment of revision test-app-mv0j7owe at 05:36:13Z introduced an unhandled "
        "exception in tenant synchronization causing HTTP 500 responses."
    )
    report = Report(
        summary=(
            "Service degradation observed. "
            "The incident was mitigated following subsequent revision deployment test-app-mv0lfmh1."
        ),
        impact="Critical tenant degradation.",
        probable_root_cause=ProbableRootCause(
            text=initial_text,
            confidence=0.88,
        ),
        recent_changes=[],
        metadata={"window_end": "2026-10-09T06:30:00Z"},
    )
    # Findings indicating unverified mitigation
    findings = {
        "changes": Findings(
            summary="Changes",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision test-app-mv0lfmh1 (traffic: 100%)",
                    timestamp=datetime(2026, 10, 9, 6, 38, 25, tzinfo=timezone.utc),
                )
            ],
            anomalies=[],
            queries_run=[],
            confidence=0.8,
            errors=[],
        )
    }

    calibrated = calibrate_report_confidence(report, findings)

    # Confidence is adjusted downwards
    assert calibrated.probable_root_cause is not None
    assert calibrated.probable_root_cause.confidence <= 0.65

    # Text must remain pure prose without [Calibrated from ...]
    assert "[Calibrated from" not in calibrated.probable_root_cause.text
    assert initial_text in calibrated.probable_root_cause.text

    # Assert that probable_root_cause.text does NOT start with or contain
    # formulaic template fragments
    assert not calibrated.probable_root_cause.text.startswith(
        "Regression introduced by recent change ("
    )
    assert "resulting in Error group grp-" not in calibrated.probable_root_cause.text
    # Assert that probable_root_cause.text contains fluent SRE prose
    assert "unhandled exception in tenant synchronization" in calibrated.probable_root_cause.text
    assert "causing HTTP 500 responses" in calibrated.probable_root_cause.text

    # Calibration uncertainty reasons must be in caveats instead
    assert len(calibrated.caveats) >= 1
    assert any("provisional" in c.lower() for c in calibrated.caveats)


def test_determine_probable_root_cause_pure_grounded_prose_no_formulaic_templates() -> None:
    """Regression test for Defect 1: _determine_probable_root_cause must output pure
    grounded SRE prose without formulaic parenthetical templates.
    """
    from agent.app.contracts.report import RecentChangeItem
    from agent.app.synthesizer import SynthesizerAgent

    synth = SynthesizerAgent()
    incident = Incident(
        id="inc-test-01",
        source="servicenow",
        external_id="INC0091244",
        external_key="INC0091244",
        title="Service degradation and 5xx errors",
        description="HTTP 500 errors on sync-tenant-data",
        severity="P1",
        service_hint="example-svc",
        opened_at=datetime(2026, 10, 9, 6, 15, tzinfo=timezone.utc),
    )
    findings = {
        "logs": Findings(
            summary="Logs findings",
            evidence=[
                EvidenceItem(
                    kind="error_group",
                    excerpt=(
                        "Error group grp-610137: HTTP 500 GET "
                        "https://example-svc-xyz123-uc.a.run.app"
                        "/api/v1/sync-tenant-data (count: 5)"
                    ),
                    timestamp=None,
                )
            ],
            anomalies=["Elevated HTTP 500 response rate"],
            queries_run=[],
            confidence=0.9,
            errors=[],
        ),
        "changes": Findings(
            summary="Changes findings",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision example-svc-rev01 (traffic: 100%)",
                    timestamp=datetime(2026, 10, 9, 6, 38, 25, tzinfo=timezone.utc),
                ),
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision example-svc-rev02 (traffic: 0%)",
                    timestamp=datetime(2026, 10, 9, 5, 36, 13, tzinfo=timezone.utc),
                ),
            ],
            anomalies=[],
            queries_run=[],
            confidence=0.8,
            errors=[],
        ),
    }
    recent_changes = [
        RecentChangeItem(
            timestamp=datetime(2026, 10, 9, 5, 36, 13, tzinfo=timezone.utc),
            description=(
                "Action google.cloud.run.v1.Services.ReplaceService on "
                "namespaces/example-project/services/example-svc "
                "by deployer@example-project.iam.gserviceaccount.com"
            ),
            author="deployer@example-project.iam.gserviceaccount.com",
        )
    ]

    prc = synth._determine_probable_root_cause(incident, findings, recent_changes)
    assert prc is not None

    # Labeled fallback mode properties
    assert prc.text.startswith("Automated fallback (LLM unavailable):")
    assert prc.confidence <= 0.30
    assert "example-svc" in prc.text

    # Never start with or contain formulaic template fragments
    assert not prc.text.startswith("Regression introduced by recent change (")
    assert "resulting in Error group grp-" not in prc.text


def test_recent_changes_populated_from_changes_findings() -> None:
    """Regression test for Defect 3: report.recent_changes must be populated
    from changes specialist evidence when empty.
    """
    from agent.app.contracts.incident import Incident
    from agent.app.contracts.report import RecentChangeItem
    from agent.app.llm import GeminiLlmClient
    from agent.app.triage import InvestigationPlan

    ts1 = datetime(2026, 10, 9, 5, 36, 13, tzinfo=timezone.utc)
    ts2 = datetime(2026, 10, 9, 6, 38, 25, tzinfo=timezone.utc)

    findings = {
        "changes": Findings(
            summary="Changes found",
            evidence=[
                EvidenceItem(
                    kind="audit_log",
                    excerpt=(
                        "Action google.cloud.run.v1.Services.ReplaceService on "
                        "namespaces/test-proj/services/test-app by "
                        "deploy-sa@test-proj.iam.gserviceaccount.com"
                    ),
                    timestamp=ts1,
                    console_url="https://console.cloud.google.com/logs",
                ),
                EvidenceItem(
                    kind="revision",
                    excerpt=(
                        "Revision test-app-mv0lfmh1 "
                        "(author: deploy-sa@test-proj.iam.gserviceaccount.com, traffic: 100%)"
                    ),
                    timestamp=ts2,
                    console_url="https://console.cloud.google.com/run",
                ),
            ],
            anomalies=[],
            queries_run=["query_audit_logs"],
            confidence=0.85,
            errors=[],
        )
    }

    # Mock Gemini response that omitted recent_changes ([])
    mock_genai_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "summary": "Service test-app experienced errors.",
            "impact": "Tenant sync degraded.",
            "timeline": [],
            "evidence_by_area": {},
            "probable_root_cause": {
                "text": "Revision test-app-mv0j7owe triggered regression.",
                "confidence": 0.85,
            },
            "recent_changes": [],
            "recommended_actions": ["Investigate deployment."],
            "specialists_status": [],
            "skills_used": [],
            "caveats": [],
            "queries_appendix": {},
            "metadata": {},
        }
    )
    mock_genai_client.models.generate_content.return_value = mock_response

    llm_client = GeminiLlmClient(
        project_id="test-proj",
        location="global",
        client=mock_genai_client,
    )
    incident = Incident(
        id="inc-1",
        source="servicenow",
        external_id="INC001",
        external_key="INC001",
        title="Test incident",
        description="Test incident",
        severity="P1",
        opened_at=ts1,
        service_hint="test-app",
    )
    plan = InvestigationPlan(
        affected_service="test-app",
        symptoms=["500 errors"],
        start_time=ts1,
        end_time=ts2,
        selected_specialists=["changes"],
    )

    report = llm_client.generate_report(incident=incident, plan=plan, findings=findings)

    # recent_changes must be backfilled
    assert len(report.recent_changes) == 2
    rc1 = report.recent_changes[0]
    assert isinstance(rc1, RecentChangeItem)
    assert rc1.author == "deploy-sa@test-proj.iam.gserviceaccount.com"
    assert "ReplaceService" in rc1.description
    assert rc1.console_url == "https://console.cloud.google.com/logs"

    rc2 = report.recent_changes[1]
    assert rc2.author == "deploy-sa@test-proj.iam.gserviceaccount.com"
    assert "test-app-mv0lfmh1" in rc2.description


def test_recommended_actions_evidence_specific() -> None:
    """Regression test for Defect 4: recommended_actions must be specific
    to the service, revisions, and endpoints from the evidence.
    """
    from agent.app.contracts.incident import Incident
    from agent.app.contracts.report import RecentChangeItem
    from agent.app.synthesizer import SynthesizerAgent

    incident = Incident(
        id="inc-1",
        source="servicenow",
        external_id="INC001",
        external_key="INC001",
        title="5xx errors on test-app",
        description="Tenant sync failing",
        severity="P1",
        opened_at=datetime(2026, 10, 9, 6, 0, 0, tzinfo=timezone.utc),
        service_hint="test-app",
    )
    recent_changes = [
        RecentChangeItem(
            timestamp=datetime(2026, 10, 9, 6, 38, 25, tzinfo=timezone.utc),
            description="Revision test-app-mv0lfmh1 (traffic: 100%) deployed",
            author="deploy-sa@test-proj.iam.gserviceaccount.com",
        ),
        RecentChangeItem(
            timestamp=datetime(2026, 10, 9, 5, 36, 13, tzinfo=timezone.utc),
            description="Revision test-app-mv0j7owe (traffic: 0%) deployed",
            author="deploy-sa@test-proj.iam.gserviceaccount.com",
        ),
        RecentChangeItem(
            timestamp=datetime(2026, 10, 8, 6, 37, 18, tzinfo=timezone.utc),
            description="Revision test-app-muz5y5qr (traffic: 0%) deployed",
            author="deploy-sa@test-proj.iam.gserviceaccount.com",
        ),
    ]
    findings = {
        "logs": Findings(
            summary="Logs",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt=(
                        "[ERROR] HTTP 500 GET https://test-app.run.app/api/v1/sync-tenant-data "
                        "(latency: 2.21s)"
                    ),
                )
            ],
            anomalies=[],
            queries_run=[],
            confidence=0.85,
            errors=[],
        ),
        "changes": Findings(
            summary="Changes",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision test-app-mv0lfmh1 (traffic: 100%)",
                ),
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision test-app-mv0j7owe (traffic: 0%)",
                ),
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision test-app-muz5y5qr (traffic: 0%)",
                ),
            ],
            anomalies=[],
            queries_run=[],
            confidence=0.85,
            errors=[],
        ),
        "metrics": Findings(
            summary="Metrics",
            evidence=[
                EvidenceItem(
                    kind="baseline_comparison",
                    excerpt="Metric run.googleapis.com/request_count (5xx): current mean=0.0011/s",
                )
            ],
            anomalies=["Elevated 5xx rate"],
            queries_run=[],
            confidence=0.85,
            errors=[],
        ),
    }

    synth = SynthesizerAgent()
    actions = synth._generate_recommendations(
        incident=incident,
        probable_root_cause=None,
        recent_changes=recent_changes,
        findings=findings,
    )

    assert len(actions) >= 3
    # 1. Compares faulty vs baseline revision
    assert any(
        "test-app-mv0j7owe" in a and "test-app-muz5y5qr" in a and "test-app" in a for a in actions
    )
    # 2. Cites specific endpoint
    assert any("/api/v1/sync-tenant-data" in a and "test-app" in a for a in actions)
    # 3. Post-mitigation telemetry check on 100% traffic revision
    assert any("test-app-mv0lfmh1" in a and "100% traffic" in a for a in actions)
    # 4. Prohibits generic stock phrases
    assert not any("Inspect error group stack traces in Google Cloud" in a for a in actions)
    assert not any(
        "Review alert thresholds and telemetry dashboards in Cloud Monitoring." == a
        for a in actions
    )


def test_caveats_deduplicated() -> None:
    """Regression test for Defect 5: caveats must not contain near-duplicate entries."""
    report = Report(
        summary=(
            "Service test-app experienced errors. "
            "The incident was mitigated following revision deploy."
        ),
        impact="Critical tenant degradation.",
        probable_root_cause=ProbableRootCause(
            text="Revision test-app-mv0j7owe caused regression.",
            confidence=0.85,
        ),
        caveats=[
            (
                "Mitigation via revision test-app-mv0lfmh1 is provisional: "
                "no post-deployment telemetry is available in the investigation "
                "window to verify error cessation."
            )
        ],
        metadata={
            "window_start": "2026-10-09T05:15:00Z",
            "window_end": "2026-10-09T06:30:00Z",
        },
    )
    findings = {
        "changes": Findings(
            summary="Changes",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision test-app-mv0lfmh1 (traffic: 100%)",
                    timestamp=datetime(2026, 10, 9, 6, 38, 25, tzinfo=timezone.utc),
                )
            ],
            anomalies=[],
            queries_run=[],
            confidence=0.85,
            errors=[],
        )
    }

    calibrated = calibrate_report_confidence(report, findings)

    # Must contain only one provisional mitigation caveat, NOT two near-duplicates
    prov_caveats = [c for c in calibrated.caveats if "provisional" in c.lower()]
    assert len(prov_caveats) == 1


def test_skills_used_truthfulness_preservation() -> None:
    """Verify calibrate_report_confidence preserves skills_used truthfully
    without injecting unconsulted fake pipeline skills.
    """
    report = Report(
        summary="Summary",
        impact="Impact",
        skills_used=["cloud-logging-queries"],
    )
    calibrated = calibrate_report_confidence(report, {})

    # Must preserve exactly the provided skills without force-injecting unconsulted skills
    assert calibrated.skills_used == ["cloud-logging-queries"]


def test_llm_generate_report_replaces_formulaic_probable_root_cause_with_pure_prose() -> None:
    """Regression test: GeminiLlmClient.generate_report must replace formulaic
    interpolated root causes with pure grounded prose.
    """
    from datetime import timedelta
    from unittest.mock import MagicMock

    from agent.app.contracts.incident import Incident
    from agent.app.contracts.report import ProbableRootCause, Report
    from agent.app.llm import GeminiLlmClient
    from agent.app.triage import InvestigationPlan

    now = datetime(2026, 10, 9, 6, 0, 0, tzinfo=timezone.utc)
    incident = Incident(
        id="inc-formulaic-test",
        source="servicenow",
        external_id="INC0091244",
        external_key="INC0091244",
        title="Degradation test",
        description="500 errors",
        severity="P1",
        service_hint="example-svc",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="example-svc",
        symptoms=["HTTP 500 errors"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs", "changes"],
    )
    findings = {
        "logs": Findings(
            summary="Error logs",
            evidence=[
                EvidenceItem(
                    kind="error_group",
                    excerpt=(
                        "Error group grp-610137: HTTP 500 GET "
                        "https://example-svc-xyz123-uc.a.run.app"
                        "/api/v1/sync-tenant-data (count: 5)"
                    ),
                    timestamp=None,
                )
            ],
            anomalies=["HTTP 500 responses"],
            queries_run=[],
            confidence=0.9,
            errors=[],
        ),
        "changes": Findings(
            summary="Changes",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision example-svc-rev02 (traffic: 0%)",
                    timestamp=now - timedelta(minutes=20),
                )
            ],
            anomalies=[],
            queries_run=[],
            confidence=0.8,
            errors=[],
        ),
    }

    # Model returned the old formulaic template string
    templated_report = Report(
        summary="Service degradation observed.",
        impact="Critical tenant degradation.",
        probable_root_cause=ProbableRootCause(
            text=(
                "Regression introduced by recent change ("
                "Action ReplaceService on namespaces/example-project/services/"
                "example-svc by deployer@example-project.iam.gserviceaccount.com), resulting in "
                "Error group grp-610137: HTTP 500 (count: 5)."
            ),
            confidence=0.88,
        ),
        recent_changes=[],
    )

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = templated_report.model_dump_json()
    mock_prc_resp = MagicMock()
    mock_prc_resp.text = (
        "Following deployment of revision example-svc-rev02, "
        "application exceptions caused HTTP 500 errors on GET /api/v1/sync-tenant-data."
    )
    # First call returns templated_report, second call (lightweight) returns pure prose
    mock_client.models.generate_content.side_effect = [mock_resp, mock_prc_resp]

    llm_client = GeminiLlmClient(
        project_id="test-project",
        client=mock_client,
    )

    report = llm_client.generate_report(incident, plan, findings)
    assert report.probable_root_cause is not None
    assert not report.probable_root_cause.text.startswith(
        "Regression introduced by recent change ("
    )
    assert "resulting in Error group grp-" not in report.probable_root_cause.text
    assert "example-svc" in report.probable_root_cause.text
    assert "GET /api/v1/sync-tenant-data" in report.probable_root_cause.text
    assert report.probable_root_cause.confidence <= 0.5
    assert (
        "Root cause was regenerated in secondary LLM pass due to missing initial field."
        in report.caveats
    )


# ============================================================================
# Phase 6R-Q4 Regression Tests: Cloud Build Status & Service Correlation
# ============================================================================


def test_cloud_build_status_mapping_integers_and_enums() -> None:
    """Regression test for Phase 6R-Q4: integer and enum statuses map to standard names."""
    from enum import Enum

    from agent.app.tools.changes import normalize_cloud_build_status

    # 1. Standard integer mapping 0..8
    expected_int_map = {
        0: "STATUS_UNKNOWN",
        1: "QUEUED",
        2: "WORKING",
        3: "SUCCESS",
        4: "FAILURE",
        5: "INTERNAL_ERROR",
        6: "TIMEOUT",
        7: "CANCELLED",
        8: "EXPIRED",
    }
    for int_val, name in expected_int_map.items():
        assert normalize_cloud_build_status(int_val) == name
        assert normalize_cloud_build_status(str(int_val)) == name

    # 2. String status names
    assert normalize_cloud_build_status("SUCCESS") == "SUCCESS"
    assert normalize_cloud_build_status("FAILURE") == "FAILURE"
    assert normalize_cloud_build_status("Build.Status.SUCCESS") == "SUCCESS"

    # 3. Enum object with .name attribute
    class MockStatusEnum(Enum):
        SUCCESS = 3
        FAILURE = 4

    assert normalize_cloud_build_status(MockStatusEnum.SUCCESS) == "SUCCESS"
    assert normalize_cloud_build_status(MockStatusEnum.FAILURE) == "FAILURE"

    # 4. Unknown or None
    assert normalize_cloud_build_status(None) == "STATUS_UNKNOWN"
    assert normalize_cloud_build_status(99) == "STATUS_99"


def test_list_cloud_builds_extracts_status_images_substitutions_tags() -> None:
    """Regression test for Phase 6R-Q4: list_cloud_builds maps integer status
    and extracts images, substitutions, and tags.
    """
    from agent.app.tools.changes import list_cloud_builds

    mock_client = MagicMock()

    # Build 1: object with status=3 (int), images, substitutions, tags
    b1 = MagicMock()
    b1.id = "build-001"
    b1.status = 3
    b1.create_time = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    b1.finish_time = datetime(2026, 10, 9, 2, 5, 0, tzinfo=timezone.utc)
    b1.source_provenance.resolved_repo_source.commit_sha = "abcdef123456"
    b1.log_url = "https://storage.googleapis.com/logs/build-001.txt"
    b1.images = ["us-central1-docker.pkg.dev/test-proj/repo/app:v1"]
    b1.substitutions = {"_SERVICE_NAME": "test-app", "COMMIT_SHA": "abcdef123456"}
    b1.tags = ["test-app", "release-v1"]
    b1.results = None

    # Build 2: dict format with status=4 (int)
    b2 = {
        "id": "build-002",
        "status": 4,
        "create_time": datetime(2026, 10, 9, 1, 0, 0, tzinfo=timezone.utc),
        "finish_time": datetime(2026, 10, 9, 1, 5, 0, tzinfo=timezone.utc),
        "commit_sha": "fedcba654321",
        "log_url": "https://storage.googleapis.com/logs/build-002.txt",
        "images": ["us-central1-docker.pkg.dev/test-proj/repo/other-svc:v1"],
        "substitutions": {"_SERVICE_NAME": "other-svc"},
        "tags": ["other-svc"],
    }

    mock_client.list_builds.return_value = [b1, b2]

    builds = list_cloud_builds(project_id="test-proj", limit=5, client=mock_client)
    assert len(builds) == 2

    # Verify build 1
    assert builds[0]["build_id"] == "build-001"
    assert builds[0]["status"] == "SUCCESS"
    assert builds[0]["commit_sha"] == "abcdef123456"
    assert builds[0]["images"] == ["us-central1-docker.pkg.dev/test-proj/repo/app:v1"]
    assert builds[0]["substitutions"] == {
        "_SERVICE_NAME": "test-app",
        "COMMIT_SHA": "abcdef123456",
    }
    assert builds[0]["tags"] == ["test-app", "release-v1"]

    # Verify build 2
    assert builds[1]["build_id"] == "build-002"
    assert builds[1]["status"] == "FAILURE"
    assert builds[1]["commit_sha"] == "fedcba654321"
    assert builds[1]["images"] == ["us-central1-docker.pkg.dev/test-proj/repo/other-svc:v1"]
    assert builds[1]["substitutions"] == {"_SERVICE_NAME": "other-svc"}
    assert builds[1]["tags"] == ["other-svc"]


def test_is_build_related_correlation_logic() -> None:
    """Regression test for Phase 6R-Q4: verifies build correlation logic."""
    from agent.app.specialists.changes import is_build_related

    # 1. Related via image name
    build_img = {"images": ["us-central1-docker.pkg.dev/test-proj/repo/tenant-app:v1"]}
    assert is_build_related(build_img, "tenant-app") is True

    # 2. Related via substitutions
    build_sub = {"substitutions": {"_SERVICE_NAME": "tenant-app"}}
    assert is_build_related(build_sub, "tenant-app") is True

    # 3. Related via tags
    build_tag = {"tags": ["trigger-tenant-app"]}
    assert is_build_related(build_tag, "tenant-app") is True

    # 4. Unrelated build with explicit metadata for a different service
    build_unrelated = {
        "images": ["us-central1-docker.pkg.dev/test-proj/repo/other-svc:v1"],
        "substitutions": {"_SERVICE_NAME": "other-svc"},
        "tags": ["other-svc"],
    }
    assert is_build_related(build_unrelated, "tenant-app") is False

    # 5. Generic build without metadata (cannot rule out)
    build_generic: dict[str, Any] = {}
    assert is_build_related(build_generic, "tenant-app") is True

    # 6. No service hint provided
    assert is_build_related(build_unrelated, None) is True


def test_changes_specialist_prioritizes_related_builds_and_formats_human_readable_status() -> None:
    """Regression test for Phase 6R-Q4: ChangesSpecialist produces status=SUCCESS
    and prioritizes related builds over unrelated repository builds.
    """
    mock_client = MagicMock()

    now = datetime(2026, 10, 9, 6, 0, 0, tzinfo=timezone.utc)
    t_start = datetime(2026, 10, 9, 5, 0, 0, tzinfo=timezone.utc)

    # Empty audit logs and revisions
    mock_client.list_entries.return_value = []
    mock_client.list_revisions.return_value = []

    # Mock list_builds with 1 related build and 1 unrelated build
    b_rel = MagicMock()
    b_rel.id = "build-rel-001"
    b_rel.status = 3  # integer status 3 (SUCCESS)
    b_rel.finish_time = now
    b_rel.source_provenance.resolved_repo_source.commit_sha = "abcdef123456"
    b_rel.images = ["us-central1-docker.pkg.dev/test-proj/repo/test-app:v2"]
    b_rel.substitutions = {"_SERVICE_NAME": "test-app"}
    b_rel.tags = ["test-app"]
    b_rel.log_url = "https://storage.googleapis.com/logs/build-rel-001.txt"
    b_rel.results = None

    b_unrel = MagicMock()
    b_unrel.id = "build-unrel-999"
    b_unrel.status = 3
    b_unrel.finish_time = now
    b_unrel.source_provenance.resolved_repo_source.commit_sha = "987654321000"
    b_unrel.images = ["us-central1-docker.pkg.dev/test-proj/repo/unrelated-svc:v1"]
    b_unrel.substitutions = {"_SERVICE_NAME": "unrelated-svc"}
    b_unrel.tags = ["unrelated-svc"]
    b_unrel.log_url = "https://storage.googleapis.com/logs/build-unrel-999.txt"
    b_unrel.results = None

    mock_client.list_builds.return_value = [b_unrel, b_rel]

    specialist = ChangesSpecialist(project_id="test-proj", client=mock_client)
    incident = Incident(
        id="inc-build-test",
        source="servicenow",
        external_id="INC0099",
        external_key="INC0099",
        title="Build correlation test",
        description="Verify build status and correlation",
        severity="P2",
        service_hint="test-app",
        opened_at=now,
    )

    findings = specialist.run_investigation(incident=incident, start_time=t_start, end_time=now)

    build_evidence = [e for e in findings.evidence if e.kind == "build"]
    assert len(build_evidence) == 1

    excerpt = build_evidence[0].excerpt
    # Must format human-readable status=SUCCESS, NEVER status=3
    assert "status=SUCCESS" in excerpt
    assert "status=3" not in excerpt
    # Must be the related build
    assert "build-rel-001" in excerpt
    assert "abcdef1" in excerpt
    # Must exclude the unrelated build
    assert "build-unrel-999" not in excerpt


def test_changes_specialist_labels_unrelated_builds_when_no_related_builds_exist() -> None:
    """Regression test for Phase 6R-Q4: When only unrelated builds exist,
    ChangesSpecialist labels them clearly with [unrelated: ...].
    """
    mock_client = MagicMock()
    now = datetime(2026, 10, 9, 6, 0, 0, tzinfo=timezone.utc)
    t_start = datetime(2026, 10, 9, 5, 0, 0, tzinfo=timezone.utc)

    mock_client.list_entries.return_value = []
    mock_client.list_revisions.return_value = []

    b_unrel = MagicMock()
    b_unrel.id = "build-unrel-555"
    b_unrel.status = 4  # integer status 4 (FAILURE)
    b_unrel.finish_time = now
    b_unrel.source_provenance.resolved_repo_source.commit_sha = "112233445566"
    b_unrel.images = ["us-central1-docker.pkg.dev/test-proj/repo/worker-svc:v1"]
    b_unrel.substitutions = {"_SERVICE_NAME": "worker-svc"}
    b_unrel.tags = ["worker-svc"]
    b_unrel.log_url = "https://storage.googleapis.com/logs/build-unrel-555.txt"
    b_unrel.results = None

    mock_client.list_builds.return_value = [b_unrel]

    specialist = ChangesSpecialist(project_id="test-proj", client=mock_client)
    incident = Incident(
        id="inc-build-unrel",
        source="servicenow",
        external_id="INC0100",
        external_key="INC0100",
        title="Unrelated build labeling test",
        description="Verify unrelated build labeling",
        severity="P2",
        service_hint="tenant-app",
        opened_at=now,
    )

    findings = specialist.run_investigation(incident=incident, start_time=t_start, end_time=now)

    build_evidence = [e for e in findings.evidence if e.kind == "build"]
    assert len(build_evidence) == 1

    excerpt = build_evidence[0].excerpt
    # Must format human-readable status=FAILURE, NEVER status=4
    assert "status=FAILURE" in excerpt
    assert "status=4" not in excerpt
    # Must clearly label unrelated build
    assert "[unrelated:" in excerpt
    assert "worker-svc:v1" in excerpt or "worker-svc" in excerpt


def test_synthesizer_extract_recent_changes_normalizes_legacy_build_status() -> None:
    """Regression test for Phase 6R-Q4: Synthesizer converts any integer build status
    (e.g. status=3) to human-readable status=SUCCESS in recent changes.
    """
    from agent.app.synthesizer import SynthesizerAgent

    synth = SynthesizerAgent()
    now = datetime(2026, 10, 9, 6, 0, 0, tzinfo=timezone.utc)

    findings = {
        "changes": Findings(
            summary="Changes findings",
            evidence=[
                EvidenceItem(
                    kind="build",
                    excerpt="Build b-legacy status=3 (commit: abc1234)",
                    timestamp=now,
                ),
                EvidenceItem(
                    kind="build",
                    excerpt="Build b-modern status=SUCCESS (commit: def5678)",
                    timestamp=now,
                ),
            ],
            anomalies=[],
            queries_run=["list_cloud_builds"],
            confidence=0.8,
            errors=[],
        )
    }

    recent_changes = synth._extract_recent_changes(findings)
    assert len(recent_changes) == 2

    assert recent_changes[0].description == "Build b-legacy status=SUCCESS (commit: abc1234)"
    assert "status=3" not in recent_changes[0].description

    assert recent_changes[1].description == "Build b-modern status=SUCCESS (commit: def5678)"
