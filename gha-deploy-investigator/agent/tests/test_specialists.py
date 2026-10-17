from datetime import datetime, timezone

import pytest
from agent.app.contracts.incident import Incident
from agent.app.specialists.iac_config import IaCConfigSpecialist
from agent.app.specialists.pipeline_logs import PipelineLogsSpecialist
from agent.app.specialists.runtime_health import RuntimeHealthSpecialist
from agent.app.tools.runtime_tools import (
    add_mock_log,
    reset_runtime_state,
    set_mock_revision,
    set_mock_service_revisions,
)


def _make_incident(failing_stage: str = "pipeline") -> Incident:
    return Incident(
        id="gha-test-123",
        source="github",
        external_id="123",
        external_key="my-org/sample-app#123",
        external_url="https://github.com/my-org/sample-app/actions/runs/123",
        title="[CI/CD Failure] sample-app: CI",
        description="Run 123 failed",
        severity="P2",
        service_hint="sample-app",
        opened_at=datetime.now(timezone.utc),
        github_repo="my-org/sample-app",
        github_run_id=123,
        github_head_sha="e4b2a8d39c01",
        github_branch="main",
        github_failing_stage=failing_stage,
    )


class TestSpecialists:
    def test_pipeline_logs_specialist_lint_failure(self) -> None:
        specialist = PipelineLogsSpecialist()
        incident = _make_incident("lint")
        job = {"id": 1, "name": "lint-job"}
        step = {"name": "Run Ruff Linter", "conclusion": "failure"}
        log_text = (
            "2026-10-16T21:00:10Z app/main.py:42:5: F841 "
            "Local variable `secret_key` is assigned to but never used\n"
            "2026-10-16T21:00:11Z Found 1 error.\n"
            "Process completed with exit code 1."
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "lint"
        assert res.confidence >= 0.90
        assert "F841" in res.probable_cause
        assert "app/main.py" in res.probable_cause
        assert len(res.evidence_items) >= 1
        assert len(res.annotations) == 1
        assert res.annotations[0].path == "app/main.py"
        assert res.annotations[0].start_line == 42
        assert any("ruff check --fix" in act for act in res.remediation_actions)
        assert res.skill_first is True
        assert "lint-troubleshooting" in res.skills_used
        assert any("get_commit_diff" in q for q in res.queries_run)

    def test_pipeline_logs_specialist_structured_linter_parsing_mixed_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        specialist = PipelineLogsSpecialist()
        incident = _make_incident("lint")
        job = {"id": 10, "name": "lint"}
        step = {"name": "Run Ruff Lint", "conclusion": "failure"}

        log_text = (
            "src/service.py:4:1: B006 [*] Mutable argument defaults not allowed\n"
            "src/service.py:6:1: E501 Line too long (88 > 79 characters)\n"
            "src/service.py:7:1: E501 Line too long (88 > 79 characters)\n"
            "src/service.py:8:1: E501 Line too long (88 > 79 characters)\n"
            "src/service.py:15:10: W291 Trailing whitespace\n"
            "Found 5 errors.\n"
        )

        monkeypatch.setattr(
            "agent.app.specialists.pipeline_logs.get_commit_diff",
            lambda **kwargs: {"files": [{"filename": "src/service.py"}]},
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "lint"
        assert res.confidence >= 0.90
        assert "Linter detected 5 errors in 'src/service.py'" in res.probable_cause
        assert "3x E501" in res.probable_cause
        assert "1x W291" in res.probable_cause
        assert "1x B006" in res.probable_cause
        assert (
            "Confirmed: all 5 errors are in 'src/service.py' modified in this commit."
            in res.summary
        )
        assert "potentially in other files" not in res.summary
        assert "potentially in other files" not in res.probable_cause
        assert len(res.annotations) == 5
        assert any(e.kind == "linter_breakdown" for e in res.evidence_items)
        assert res.skill_first is True
        assert "lint-troubleshooting" in res.skills_used
        assert any("get_commit_diff" in q for q in res.queries_run)

    def test_pipeline_logs_specialist_multiline_ruff_with_timestamps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        specialist = PipelineLogsSpecialist()
        incident = _make_incident("lint")
        job = {"id": 11, "name": "lint"}
        step = {"name": "Run Ruff Lint", "conclusion": "failure"}

        log_text = (
            "2026-10-17T10:09:06.5738347Z B006 [*] Do not use mutable default arguments\n"
            "2026-10-17T10:09:06.5738627Z  --> src/service.py:1:8\n"
            "2026-10-17T10:09:06.5740168Z W291 Trailing whitespace\n"
            "2026-10-17T10:09:06.5740393Z  --> src/service.py:5:36\n"
            "2026-10-17T10:09:06.5742056Z E501 Line too long\n"
            "2026-10-17T10:09:06.5742254Z  --> src/service.py:7:1\n"
            "2026-10-17T10:09:06.5743000Z E501 Line too long\n"
            "2026-10-17T10:09:06.5743200Z  --> src/service.py:8:1\n"
            "2026-10-17T10:09:06.5763858Z Found 4 errors.\n"
        )

        monkeypatch.setattr(
            "agent.app.specialists.pipeline_logs.get_commit_diff",
            lambda **kwargs: {"files": [{"filename": "src/service.py"}]},
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "lint"
        assert res.confidence >= 0.90
        assert "Linter detected 4 errors in 'src/service.py'" in res.probable_cause
        assert "2x E501" in res.probable_cause
        assert "1x W291" in res.probable_cause
        assert "1x B006" in res.probable_cause
        assert "mutable default arguments" in res.probable_cause
        assert (
            "Confirmed: all 4 errors are in 'src/service.py' modified in this commit."
            in res.summary
        )
        assert len(res.annotations) == 4
        b006_ann = next(a for a in res.annotations if a.title == "Linter Violation: B006")
        assert b006_ann.path == "src/service.py"
        assert b006_ann.start_line == 1
        assert "mutable default arguments" in b006_ann.message

        w291_ann = next(a for a in res.annotations if a.title == "Linter Violation: W291")
        assert w291_ann.path == "src/service.py"
        assert w291_ann.start_line == 5

        e501_anns = [a for a in res.annotations if a.title == "Linter Violation: E501"]
        assert len(e501_anns) == 2
        assert e501_anns[0].start_line == 7

        assert any(e.kind == "linter_breakdown" for e in res.evidence_items)
        assert res.skill_first is True
        assert "lint-troubleshooting" in res.skills_used

    def test_pipeline_logs_specialist_pytest_failure(self) -> None:
        specialist = PipelineLogsSpecialist()
        incident = _make_incident("test")
        job = {"id": 2, "name": "unit-tests"}
        step = {"name": "Run Pytest", "conclusion": "failure"}
        log_text = (
            "============================= test session starts ==============================\n"
            "tests/test_auth.py:28: in test_token_expiration\n"
            ">       assert response.status_code == 200\n"
            "E       AssertionError: assert 401 == 200\n"
            "FAILED tests/test_auth.py::test_token_expiration - AssertionError: assert 401 == 200\n"
            "============================== 1 failed in 0.45s ==============================="
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "test"
        assert res.confidence >= 0.85
        assert "test_token_expiration" in res.probable_cause
        assert "AssertionError" in res.probable_cause or "401 == 200" in res.probable_cause
        assert len(res.evidence_items) >= 1
        assert any("pytest" in act for act in res.remediation_actions)
        assert res.skill_first is True
        assert "test-failure-triage" in res.skills_used
        assert any("get_commit_diff" in q for q in res.queries_run)

    def test_pipeline_logs_specialist_build_failure(self) -> None:
        specialist = PipelineLogsSpecialist()
        incident = _make_incident("build")
        job = {"id": 3, "name": "build-container"}
        step = {"name": "Docker Build & Push", "conclusion": "failure"}
        log_text = (
            "#7 [4/7] COPY requirements.txt .\n"
            "#7 ERROR: failed to solve: failed to compute cache key: "
            "COPY failed: file not found in build context or excluded by .dockerignore: "
            "stat requirements.txt: file does not exist\n"
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "build"
        assert res.confidence >= 0.85
        assert "COPY failed" in res.probable_cause or ".dockerignore" in res.probable_cause
        assert any(
            ".dockerignore" in act or "Dockerfile" in act for act in res.remediation_actions
        )
        assert res.skill_first is True
        assert "container-build-troubleshooting" in res.skills_used
        assert any("get_commit_diff" in q for q in res.queries_run)

    def test_runtime_health_specialist_oom_exit_137(self) -> None:
        reset_runtime_state()
        specialist = RuntimeHealthSpecialist()
        incident = _make_incident("deploy")
        job = {"id": 4, "name": "deploy-cloudrun"}
        step = {"name": "Deploy to Cloud Run", "conclusion": "failure"}
        log_text = (
            "Deploying container to Cloud Run service [sample-app] in region [us-central1]...\n"
            "Deploying revision [sample-app-00002-oom]...\n"
            "ERROR: (gcloud.run.deploy) Container failed to start and listen on $PORT.\n"
        )

        # Configure mock telemetry indicating exit code 137 OOM
        set_mock_revision(
            "sample-app-00002-oom",
            {
                "service_name": "sample-app",
                "revision_name": "sample-app-00002-oom",
                "region": "us-central1",
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "False",
                        "reason": "ContainerTerminated",
                        "message": "Container terminated due to memory limit (exit code 137)",
                    }
                ],
                "container_statuses": [
                    {
                        "name": "sample-app",
                        "restart_count": 3,
                        "last_exit_code": 137,
                    }
                ],
            },
        )

        add_mock_log(
            {
                "text_payload": "Allocating heap space for background processor...",
                "severity": "INFO",
            }
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "deploy"
        assert res.confidence >= 0.90
        assert (
            "137" in res.probable_cause
            or "OOM" in res.probable_cause
            or "memory" in res.probable_cause.lower()
        )
        assert any("--memory 1Gi" in act for act in res.remediation_actions)
        assert len(res.evidence_items) >= 2
        assert res.skill_first is True
        assert "cloudrun-deploy-troubleshooting" in res.skills_used
        assert any("describe_service" in q for q in res.queries_run)
        assert any("describe_cloud_run_revision" in q for q in res.queries_run)
        assert any("get_commit_diff" in q for q in res.queries_run)
        assert any(e.kind == "cloud_run_container_log" for e in res.evidence_items)
        assert any("Allocating heap space" in e.excerpt for e in res.evidence_items)

    def test_runtime_health_specialist_cloud_logging_correlation(self) -> None:
        reset_runtime_state()
        specialist = RuntimeHealthSpecialist()
        incident = _make_incident("deploy")
        job = {"id": 5, "name": "deploy-cloudrun"}
        step = {"name": "Deploy to Cloud Run", "conclusion": "failure"}
        log_text = (
            "Deploying container to Cloud Run service [sample-app] in region [us-central1]...\n"
            "Deploying revision [sample-app-00003-crash]...\n"
        )

        # Set mock revision without OOM, but mock logs with startup crash
        set_mock_revision(
            "sample-app-00003-crash",
            {
                "service_name": "sample-app",
                "revision_name": "sample-app-00003-crash",
                "conditions": [],
                "container_statuses": [{"last_exit_code": 1}],
            },
        )
        add_mock_log(
            {
                "text_payload": "ModuleNotFoundError: No module named 'cryptography'",
                "severity": "ERROR",
            }
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "deploy"
        assert "ModuleNotFoundError" in res.probable_cause
        assert any(e.kind == "cloud_logging" for e in res.evidence_items)
        assert res.skill_first is True
        assert "cloudrun-deploy-troubleshooting" in res.skills_used
        assert any("describe_service" in q for q in res.queries_run)
        assert any("query_cloud_logging" in q for q in res.queries_run)

    def test_iac_config_specialist_terraform_error(self) -> None:
        specialist = IaCConfigSpecialist()
        incident = _make_incident("iac_config")
        job = {"id": 6, "name": "terraform-apply"}
        step = {"name": "Terraform Apply", "conclusion": "failure"}
        log_text = (
            "Error: Unsupported attribute\n"
            '  on main.tf line 45, in resource "google_cloud_run_service" "app":\n'
            'This object has no argument, nested block, or exported attribute '
            'named "invalid_field".\n'
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "iac_config"
        assert "main.tf:45" in res.probable_cause or "main.tf" in res.probable_cause
        assert any("terraform validate" in act for act in res.remediation_actions)
        assert res.skill_first is True
        assert "cloudrun-deploy-troubleshooting" in res.skills_used
        assert any("get_commit_diff" in q for q in res.queries_run)

    def test_runtime_health_specialist_permission_denial_error(self) -> None:
        reset_runtime_state()
        specialist = RuntimeHealthSpecialist()
        incident = _make_incident("deploy")
        job = {"id": 7, "name": "deploy-cloudrun"}
        step = {"name": "Deploy to Cloud Run", "conclusion": "failure"}
        log_text = (
            "Deploying container to Cloud Run service [sample-app] in region [us-central1]...\n"
            "ERROR: (gcloud.run.deploy) PERMISSION_DENIED: The caller does not have permission\n"
            "ERROR: Access denied. Service account requires roles/run.admin on the "
            "target resource [projects/demo-proj/services/sample-app].\n"
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "deploy"
        assert res.confidence >= 0.70
        assert "-00001-deploy" not in res.probable_cause
        assert "sample-app" in res.summary
        assert "cloudrun-deploy-troubleshooting" in res.skills_used
        assert res.skill_first is True
        assert any("fetch_job_logs" in q for q in res.queries_run)
        assert any("describe_service" in q for q in res.queries_run)
        # Live status contextualized with caveat that revision was not created
        assert any(
            "No revision existed at failure time" in e.excerpt
            for e in res.evidence_items
            if e.kind == "cloud_run_service"
        )
        assert not any(
            "Active revision from previous deployment" in e.excerpt
            for e in res.evidence_items
        )
        # Verbatim error line excerpt in evidence
        assert any("PERMISSION_DENIED" in e.excerpt for e in res.evidence_items)
        assert any(
            "permission" in a.lower() or "configuration" in a.lower()
            for a in res.remediation_actions
        )

    def test_runtime_health_specialist_no_hallucinated_revision(self) -> None:
        reset_runtime_state()
        specialist = RuntimeHealthSpecialist()
        incident = _make_incident("deploy")
        job = {"id": 8, "name": "deploy-cloudrun"}
        step = {"name": "Deploy to Cloud Run", "conclusion": "failure"}
        log_text = (
            "Deploying container to Cloud Run service [sample-app] in region [us-central1]...\n"
            "Deployment failed before creating revision.\n"
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "deploy"
        assert "-00001-deploy" not in res.probable_cause
        assert "-00001-deploy" not in res.summary
        assert res.skill_first is True
        assert "cloudrun-deploy-troubleshooting" in res.skills_used
        assert any("fetch_job_logs" in q for q in res.queries_run)
        assert any("describe_service" in q for q in res.queries_run)
        # Must not produce remediation command asking to describe non-existent revision
        assert not any("revisions describe" in a for a in res.remediation_actions)
        assert any("services describe" in a for a in res.remediation_actions)

    def test_service_pattern_matches_gcloud_run_deploy(self) -> None:
        from agent.app.specialists.runtime_health import SERVICE_PATTERN

        match1 = SERVICE_PATTERN.search("gcloud run deploy my-cool-service --image gcr.io/app:v1")
        assert match1 is not None
        assert match1.group("srv1") == "my-cool-service"

        match2 = SERVICE_PATTERN.search(
            "Deploying container to Cloud Run service [my-cool-service]"
        )
        assert match2 is not None
        assert match2.group("srv1") == "my-cool-service"

    def test_runtime_health_case_a_no_revision_existed_at_failure_time(self) -> None:
        """Case A: initial deploy failure where no revision existed at failure time.

        Revisions list is either empty or revisions were created strictly after the failure window.
        Must accurately state that no revision existed at failure time.
        """
        reset_runtime_state()
        specialist = RuntimeHealthSpecialist()
        incident = _make_incident("deploy")
        # Failure occurred at 05:01:00 UTC
        incident.opened_at = datetime(2026, 10, 17, 5, 1, 0, tzinfo=timezone.utc)
        job = {"id": 101, "name": "deploy-cloudrun"}
        step = {"name": "Deploy to Cloud Run", "conclusion": "failure"}
        log_text = (
            "Deploying container to Cloud Run service [sample-app] in region [us-central1]...\n"
            "ERROR: (gcloud.run.deploy) PERMISSION_DENIED: Access denied.\n"
        )

        # Revisions were created later (e.g. 05:26:00 UTC)
        set_mock_service_revisions(
            "sample-app",
            [
                {
                    "revision_name": "sample-app-00001-cwl",
                    "create_time": "2026-10-17T05:26:00Z",
                }
            ],
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "deploy"
        # Must dynamically compute that no revision existed at failure time
        assert any(
            "No revision existed at failure time" in e.excerpt
            for e in res.evidence_items
            if e.kind == "cloud_run_service"
        )
        assert not any(
            "Active revision at failure time" in e.excerpt
            for e in res.evidence_items
        )
        assert not any(
            "Active revision from previous deployment" in e.excerpt
            for e in res.evidence_items
        )

    def test_runtime_health_case_b_prior_revision_existed_at_failure_time(self) -> None:
        """Case B: subsequent update deploy failure where a prior revision existed at failure time.

        Revisions list contains a revision created before the failure window.
        Must dynamically name that active revision at failure time.
        """
        reset_runtime_state()
        specialist = RuntimeHealthSpecialist()
        incident = _make_incident("deploy")
        # Failure occurred at 06:00:00 UTC
        incident.opened_at = datetime(2026, 10, 17, 6, 0, 0, tzinfo=timezone.utc)
        job = {"id": 102, "name": "deploy-cloudrun"}
        step = {"name": "Deploy to Cloud Run", "conclusion": "failure"}
        log_text = (
            "Deploying container to Cloud Run service [sample-app] in region [us-central1]...\n"
            "ERROR: (gcloud.run.deploy) PERMISSION_DENIED: Access denied.\n"
        )

        # Prior revision existed at 05:26:00 UTC (before failure at 06:00:00 UTC)
        set_mock_service_revisions(
            "sample-app",
            [
                {
                    "revision_name": "sample-app-00001-cwl",
                    "create_time": "2026-10-17T05:26:00Z",
                }
            ],
        )

        res = specialist.diagnose(incident, job, step, log_text)
        assert res.stage == "deploy"
        # Must dynamically identify the active prior revision by name
        assert any(
            "Active revision at failure time was sample-app-00001-cwl" in e.excerpt
            for e in res.evidence_items
            if e.kind == "cloud_run_service"
        )
        assert not any(
            "No revision existed at failure time" in e.excerpt
            for e in res.evidence_items
        )
        assert not any(
            "Active revision from previous deployment" in e.excerpt
            for e in res.evidence_items
        )
