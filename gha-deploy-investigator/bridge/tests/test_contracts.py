from datetime import datetime, timezone

import pytest
from bridge.app.contracts import (
    CheckRunAnnotation,
    Findings,
    Incident,
    InvestigationRequest,
    InvestigationResult,
    LogFinding,
    Report,
)
from pydantic import ValidationError


class TestContracts:
    def test_incident_contract_full_github_fields(self) -> None:
        incident = Incident(
            id="gha-12345",
            source="github",
            external_id="12345",
            external_key="owner/repo#12345",
            external_url="https://github.com/owner/repo/actions/runs/12345",
            title="[CI/CD Failure] repo: CI",
            description="Run failed on main",
            severity="P2",
            opened_at=datetime(2026, 10, 16, 20, 0, 0),  # naive -> converted to UTC
            github_repo="owner/repo",
            github_run_id=12345,
            github_head_sha="abcdef123456",
            github_branch="main",
            github_pr_number=99,
            github_failing_stage="lint",
        )
        assert incident.opened_at.tzinfo == timezone.utc
        assert incident.github_repo == "owner/repo"
        assert incident.github_run_id == 12345
        assert incident.github_branch == "main"
        assert incident.github_pr_number == 99
        assert incident.github_failing_stage == "lint"

    def test_incident_contract_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Incident(
                id="gha-1",
                source="github",
                external_id="1",
                external_key="k",
                title="t",
                description="d",
                severity="P3",
                opened_at=datetime.now(timezone.utc),
                unrecognized_field="illegal",  # type: ignore[call-arg]
            )

    def test_investigation_request_utc_validation(self, sample_incident: Incident) -> None:
        req = InvestigationRequest(
            incident=sample_incident,
            callback_url="http://localhost:8080/callback/report",
            requested_at=datetime(2026, 10, 16, 21, 0, 0),  # naive
        )
        assert req.requested_at.tzinfo == timezone.utc

    def test_investigation_result_pairwise_validation(
        self,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        # Completed without report should fail
        with pytest.raises(ValidationError):
            InvestigationResult(
                incident_id=sample_incident.id,
                incident_source="github",
                status="completed",
                report=None,
            )

        # Failed without error should fail
        with pytest.raises(ValidationError):
            InvestigationResult(
                incident_id=sample_incident.id,
                incident_source="github",
                status="failed",
                error="",
            )

        # Completed with report succeeds
        res_ok = InvestigationResult(
            incident_id=sample_incident.id,
            incident_source="github",
            status="completed",
            report=sample_report,
            incident=sample_incident,
        )
        assert res_ok.status == "completed"
        assert res_ok.incident_external_id == sample_incident.external_id
        assert res_ok.skills_used == ["github-actions-logs-analysis"]

    def test_report_annotations_and_findings(self) -> None:
        annotation = CheckRunAnnotation(
            path="app/main.py",
            start_line=42,
            end_line=42,
            annotation_level="failure",
            message="Local variable unused",
            title="Linter Error",
        )
        assert annotation.path == "app/main.py"
        assert annotation.annotation_level == "failure"

        log_finding = LogFinding(
            stage="lint",
            step_name="Run Ruff",
            line_number=42,
            file_path="app/main.py",
            message="Unused variable",
        )
        assert log_finding.stage == "lint"

        findings = Findings(
            summary="Found 1 lint error",
            confidence=0.95,
        )
        assert findings.confidence == 0.95
