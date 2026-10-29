from datetime import datetime, timezone

import pytest
from agent.app.contracts import (
    EvidenceItem,
    Findings,
    Incident,
    InvestigationRequest,
    InvestigationResult,
    ProbableRootCause,
    RecentChangeItem,
    Report,
    SpecialistStatus,
    TimelineItem,
)
from pydantic import ValidationError


def create_sample_incident() -> Incident:
    return Incident(
        id="inc-1001",
        source="servicenow",
        external_id="sys_12345",
        external_key="INC0010001",
        external_url="https://example.service-now.com/nav_to.do?uri=incident.do?sys_id=sys_12345",
        title="High error rate on shop-api",
        description="Customers getting HTTP 500 errors during checkout",
        severity="P1",
        service_hint="shop-api",
        labels=["outage", "checkout"],
        opened_at=datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc),
        reporter="sre-oncall@example.com",
    )


def create_sample_report() -> Report:
    evidence = EvidenceItem(
        kind="log",
        excerpt="Exception in /checkout: connection timeout",
        timestamp=datetime(2026, 10, 9, 2, 5, 0, tzinfo=timezone.utc),
        console_url="https://console.cloud.google.com/logs/query",
    )
    timeline = TimelineItem(
        timestamp=datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc),
        description="First 500 error logged",
    )
    prc = ProbableRootCause(
        text="Database pool exhausted after bad configuration deployment",
        confidence=0.92,
    )
    change = RecentChangeItem(
        timestamp=datetime(2026, 10, 9, 1, 55, 0, tzinfo=timezone.utc),
        description="Cloud Run revision deployed with reduced DB pool size",
        author="developer@example.com",
        console_url="https://console.cloud.google.com/run/detail",
    )
    specialist = SpecialistStatus(
        specialist="logs",
        status="completed",
        error=None,
    )
    return Report(
        summary="Service degradation due to DB connection starvation",
        impact="Checkout endpoint failing with 500 for ~40% of requests",
        timeline=[timeline],
        evidence_by_area={"logs": [evidence]},
        probable_root_cause=prc,
        recent_changes=[change],
        recommended_actions=["Roll back Cloud Run revision", "Increase DB pool size"],
        specialists_status=[specialist],
        skills_used=["cloud-logging-queries"],
        queries_appendix={"logs": ["resource.type=cloud_run_revision severity>=ERROR"]},
        metadata={"region": "us-central1"},
    )


class TestIncidentContract:
    def test_valid_incident(self) -> None:
        incident = create_sample_incident()
        assert incident.id == "inc-1001"
        assert incident.severity == "P1"
        assert incident.source == "servicenow"
        assert len(incident.labels) == 2

    def test_default_labels(self) -> None:
        incident = Incident(
            id="inc-1002",
            source="jira",
            external_id="10001",
            external_key="OPS-42",
            title="Latency spike",
            description="p99 > 3s",
            severity="P2",
            opened_at=datetime.now(timezone.utc),
        )
        assert incident.labels == []
        assert incident.external_url is None
        assert incident.service_hint is None
        assert incident.reporter is None

    def test_invalid_severity(self) -> None:
        with pytest.raises(ValidationError):
            Incident(
                id="inc-1003",
                source="jira",
                external_id="10002",
                external_key="OPS-43",
                title="Bad severity",
                description="desc",
                severity="P5",  # type: ignore[arg-type]
                opened_at=datetime.now(timezone.utc),
            )

    def test_invalid_source(self) -> None:
        with pytest.raises(ValidationError):
            Incident(
                id="inc-1004",
                source="pagerduty",  # type: ignore[arg-type]
                external_id="10003",
                external_key="OPS-44",
                title="Bad source",
                description="desc",
                severity="P3",
                opened_at=datetime.now(timezone.utc),
            )

    def test_missing_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            Incident(  # type: ignore[call-arg]
                id="inc-1005",
                source="generic",
            )

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Incident(
                id="inc-1006",
                source="generic",
                external_id="gen-1",
                external_key="GEN-1",
                title="Extra field test",
                description="desc",
                severity="P4",
                opened_at=datetime.now(timezone.utc),
                unrecognized_field="forbidden",  # type: ignore[call-arg]
            )

    def test_json_roundtrip(self) -> None:
        incident = create_sample_incident()
        json_str = incident.model_dump_json()
        restored = Incident.model_validate_json(json_str)
        assert restored == incident

    def test_schema_generation(self) -> None:
        schema = Incident.model_json_schema()
        assert schema["title"] == "Incident"
        assert "severity" in schema["properties"]


class TestRequestContract:
    def test_valid_request(self) -> None:
        incident = create_sample_incident()
        now = datetime.now(timezone.utc)
        req = InvestigationRequest(
            incident=incident,
            callback_url="https://bridge.example.com/callback",
            requested_at=now,
        )
        assert req.incident.id == incident.id
        assert req.callback_url == "https://bridge.example.com/callback"
        assert req.requested_at == now

    def test_json_roundtrip(self) -> None:
        incident = create_sample_incident()
        req = InvestigationRequest(
            incident=incident,
            callback_url="https://bridge.example.com/callback",
            requested_at=datetime.now(timezone.utc),
        )
        restored = InvestigationRequest.model_validate_json(req.model_dump_json())
        assert restored == req


class TestFindingsContract:
    def test_valid_findings(self) -> None:
        evidence = EvidenceItem(
            kind="metric",
            excerpt="Error rate 42% (threshold: 5%)",
            timestamp=datetime.now(timezone.utc),
            console_url="https://console.cloud.google.com/monitoring",
        )
        findings = Findings(
            summary="Metric anomaly detected",
            evidence=[evidence],
            anomalies=["High 5xx error rate"],
            queries_run=["metric.type=run.googleapis.com/request_count"],
            confidence=0.88,
            errors=[],
        )
        assert findings.confidence == 0.88
        assert len(findings.evidence) == 1
        assert len(findings.anomalies) == 1

    def test_confidence_range(self) -> None:
        with pytest.raises(ValidationError):
            Findings(
                summary="Invalid confidence",
                confidence=1.5,
            )
        with pytest.raises(ValidationError):
            Findings(
                summary="Invalid confidence",
                confidence=-0.1,
            )

    def test_defaults(self) -> None:
        findings = Findings(
            summary="Empty findings",
            confidence=0.5,
        )
        assert findings.evidence == []
        assert findings.anomalies == []
        assert findings.queries_run == []
        assert findings.errors == []

    def test_json_roundtrip(self) -> None:
        evidence = EvidenceItem(
            kind="log",
            excerpt="Critical error",
            timestamp=datetime.now(timezone.utc),
        )
        findings = Findings(
            summary="Log errors detected",
            evidence=[evidence],
            confidence=0.75,
        )
        restored = Findings.model_validate_json(findings.model_dump_json())
        assert restored == findings


class TestReportContract:
    def test_valid_report(self) -> None:
        report = create_sample_report()
        assert report.summary == "Service degradation due to DB connection starvation"
        assert report.probable_root_cause is not None
        assert report.probable_root_cause.confidence == 0.92
        assert len(report.timeline) == 1
        assert len(report.specialists_status) == 1

    def test_specialist_status_literal(self) -> None:
        with pytest.raises(ValidationError):
            SpecialistStatus(
                specialist="logs",
                status="in_progress",  # type: ignore[arg-type]
            )

    def test_probable_root_cause_confidence_range(self) -> None:
        with pytest.raises(ValidationError):
            ProbableRootCause(
                text="Some cause",
                confidence=1.2,
            )

    def test_optional_fields_none(self) -> None:
        report = Report(
            summary="Basic report",
            impact="Minimal",
        )
        assert report.probable_root_cause is None
        assert report.timeline == []
        assert report.evidence_by_area == {}
        assert report.recent_changes == []
        assert report.recommended_actions == []
        assert report.specialists_status == []
        assert report.skills_used == []
        assert report.queries_appendix == {}
        assert report.metadata == {}

    def test_json_roundtrip(self) -> None:
        report = create_sample_report()
        restored = Report.model_validate_json(report.model_dump_json())
        assert restored == report


class TestResultContract:
    def test_completed_result(self) -> None:
        report = create_sample_report()
        result = InvestigationResult(
            investigation_id="inv-1234",
            incident_id="inc-1001",
            incident_source="servicenow",
            incident_external_id="sys_12345",
            incident_external_key="INC0010001",
            status="completed",
            report=report,
            error=None,
        )
        assert result.status == "completed"
        assert result.report is not None
        assert result.error is None

    def test_failed_result(self) -> None:
        result = InvestigationResult(
            investigation_id="inv-1235",
            incident_id="inc-1002",
            incident_source="jira",
            incident_external_id="10002",
            incident_external_key="OPS-43",
            status="failed",
            report=None,
            error="Investigation timed out after 600s",
        )
        assert result.status == "failed"
        assert result.report is None
        assert result.error == "Investigation timed out after 600s"

    def test_invalid_status(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationResult(
                investigation_id="inv-1236",
                incident_id="inc-1003",
                incident_source="generic",
                incident_external_id="gen-1",
                incident_external_key="GEN-1",
                status="pending",  # type: ignore[arg-type]
            )

    def test_json_roundtrip(self) -> None:
        report = create_sample_report()
        result = InvestigationResult(
            investigation_id="inv-1237",
            incident_id="inc-1001",
            incident_source="servicenow",
            incident_external_id="sys_12345",
            incident_external_key="INC0010001",
            status="completed",
            report=report,
        )
        restored = InvestigationResult.model_validate_json(result.model_dump_json())
        assert restored == result

    def test_completed_result_requires_report(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InvestigationResult(
                investigation_id="inv-fail-1",
                incident_id="inc-1001",
                incident_source="servicenow",
                incident_external_id="sys_12345",
                incident_external_key="INC0010001",
                status="completed",
                report=None,
            )
        assert "report must not be None when status is 'completed'" in str(exc_info.value)

    def test_failed_result_requires_non_empty_error(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InvestigationResult(
                investigation_id="inv-fail-2",
                incident_id="inc-1001",
                incident_source="servicenow",
                incident_external_id="sys_12345",
                incident_external_key="INC0010001",
                status="failed",
                error=None,
            )
        assert "error must not be None and non-empty when status is 'failed'" in str(exc_info.value)

        with pytest.raises(ValidationError) as exc_info2:
            InvestigationResult(
                investigation_id="inv-fail-3",
                incident_id="inc-1001",
                incident_source="servicenow",
                incident_external_id="sys_12345",
                incident_external_key="INC0010001",
                status="failed",
                error="   ",
            )
        assert "error must not be None and non-empty when status is 'failed'" in str(
            exc_info2.value
        )

    def test_naive_datetimes_normalized_to_utc(self) -> None:
        naive_dt = datetime(2026, 10, 9, 2, 0, 0)
        incident = Incident(
            id="inc-utc-1",
            source="generic",
            external_id="gen-utc",
            external_key="GEN-UTC",
            title="UTC test",
            description="Testing UTC normalization",
            severity="P3",
            opened_at=naive_dt,
        )
        assert incident.opened_at.tzinfo == timezone.utc
        assert incident.opened_at.hour == 2

        req = InvestigationRequest(
            incident=incident,
            callback_url="https://app.run.app/callback",
            requested_at=naive_dt,
        )
        assert req.requested_at.tzinfo == timezone.utc
