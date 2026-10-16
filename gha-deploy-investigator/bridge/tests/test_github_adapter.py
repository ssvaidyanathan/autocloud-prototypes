import hashlib
import hmac

import httpx
import pytest
from bridge.app.adapters.base import IncidentRef
from bridge.app.adapters.github import GitHubAdapter
from bridge.app.config import BridgeConfig
from bridge.app.contracts.incident import Incident
from bridge.app.contracts.report import CheckRunAnnotation, Report
from bridge.tests.fake_github_server import state as github_state
from starlette.requests import Request


class TestGitHubAdapter:
    def test_verify_valid_hmac(self, bridge_settings: BridgeConfig) -> None:
        adapter = GitHubAdapter(config=bridge_settings)
        payload = b'{"action": "completed"}'
        secret = bridge_settings.github_webhook_secret or ""
        valid_sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        scope = {
            "type": "http",
            "headers": [(b"x-hub-signature-256", valid_sig.encode())],
        }
        req = Request(scope)
        assert adapter.verify(req, payload) is True

    def test_verify_invalid_and_missing_hmac(self, bridge_settings: BridgeConfig) -> None:
        adapter = GitHubAdapter(config=bridge_settings)
        payload = b'{"action": "completed"}'

        # Invalid signature
        scope_bad = {
            "type": "http",
            "headers": [(b"x-hub-signature-256", b"sha256=invalidhex1234567890")],
        }
        assert adapter.verify(Request(scope_bad), payload) is False

        # Missing signature
        scope_empty = {
            "type": "http",
            "headers": [],
        }
        assert adapter.verify(Request(scope_empty), payload) is False

    def test_to_incident_parsing(self, bridge_settings: BridgeConfig) -> None:
        adapter = GitHubAdapter(config=bridge_settings)
        payload = {
            "action": "completed",
            "workflow_run": {
                "id": 1849201948,
                "name": "CI Pipeline",
                "conclusion": "failure",
                "head_sha": "e4b2a8d39c000",
                "head_branch": "main",
                "html_url": "https://github.com/my-org/sample-app/actions/runs/1849201948",
                "updated_at": "2026-10-16T21:00:00Z",
                "actor": {"login": "octocat"},
                "pull_requests": [{"number": 42}],
            },
            "repository": {
                "name": "sample-app",
                "full_name": "my-org/sample-app",
            },
        }

        incident = adapter.to_incident(payload)
        assert incident.source == "github"
        assert incident.external_id == "1849201948"
        assert incident.external_key == "my-org/sample-app#1849201948"
        assert incident.severity == "P2"  # main branch
        assert incident.github_repo == "my-org/sample-app"
        assert incident.github_pr_number == 42
        assert incident.reporter == "octocat"

    def test_to_incident_branch_severity_p3(self, bridge_settings: BridgeConfig) -> None:
        adapter = GitHubAdapter(config=bridge_settings)
        payload = {
            "action": "completed",
            "workflow_run": {
                "id": 999,
                "name": "CI",
                "conclusion": "failure",
                "head_sha": "abc123",
                "head_branch": "feat/my-feature",
                "updated_at": "2026-10-16T21:00:00Z",
            },
            "repository": {"name": "sample-app", "full_name": "org/sample-app"},
        }
        incident = adapter.to_incident(payload)
        assert incident.severity == "P3"

    def test_to_incident_rejects_non_failure(self, bridge_settings: BridgeConfig) -> None:
        adapter = GitHubAdapter(config=bridge_settings)
        success_payload = {
            "action": "completed",
            "workflow_run": {"id": 1, "conclusion": "success"},
            "repository": {"name": "sample-app"},
        }
        with pytest.raises(ValueError, match="Ignoring non-failure event"):
            adapter.to_incident(success_payload)

    def test_post_report_link(
        self,
        bridge_settings: BridgeConfig,
        fake_github_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        adapter = GitHubAdapter(config=bridge_settings, http_client=fake_github_client)
        ref = IncidentRef(
            id="sn-sys_123",
            source="servicenow",
            external_id="sys_123",
            external_key="INC0019482",
            external_url="https://example.service-now.com/nav_to.do?uri=incident.do?sys_id=sys_123",
        )

        adapter.post_report_link(sample_incident, sample_report, ref)

        assert len(github_state.check_runs) == 1
        check_run = github_state.check_runs[0]
        assert check_run["name"] == "Autocloud / ServiceNow Incident Link"
        assert check_run["head_sha"] == sample_incident.github_head_sha
        assert check_run["status"] == "completed"
        assert "INC0019482" in check_run["output"]["summary"]

        # Verify comment was posted
        assert len(github_state.comments) == 1
        comment = github_state.comments[0]
        assert "<details open>" in comment["body"]
        assert "INC0019482" in comment["body"]
        assert "Probable Root Cause" in comment["body"]

    def test_post_report_link_pr_comment(
        self,
        bridge_settings: BridgeConfig,
        fake_github_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        adapter = GitHubAdapter(config=bridge_settings, http_client=fake_github_client)
        ref = IncidentRef(
            id="sn-sys_456",
            source="servicenow",
            external_id="sys_456",
            external_key="INC0019483",
        )
        sample_incident.github_pr_number = 42
        github_state.comments.clear()

        adapter.post_report_link(sample_incident, sample_report, ref)

        assert len(github_state.comments) == 1
        comment = github_state.comments[0]
        assert comment.get("issue_number") == 42
        assert "INC0019483" in comment["body"]

    def test_post_report_link_annotation_path_sanitization(
        self,
        bridge_settings: BridgeConfig,
        fake_github_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        adapter = GitHubAdapter(config=bridge_settings, http_client=fake_github_client)
        ref = IncidentRef(
            id="sn-sys_789",
            source="servicenow",
            external_id="sys_789",
            external_key="INC0019484",
        )
        sample_report.annotations = [
            CheckRunAnnotation(
                path="./agent/app/main.py",
                start_line=10,
                end_line=10,
                annotation_level="failure",
                message="Syntax error",
            ),
            CheckRunAnnotation(
                path="/agent/app/models.py",
                start_line=20,
                end_line=20,
                annotation_level="failure",
                message="Type error",
            ),
        ]
        github_state.check_runs.clear()

        adapter.post_report_link(sample_incident, sample_report, ref)

        assert len(github_state.check_runs) == 1
        output = github_state.check_runs[0].get("output", {})
        annotations = output.get("annotations", [])
        assert len(annotations) == 2
        assert annotations[0]["path"] == "agent/app/main.py"
        assert annotations[1]["path"] == "agent/app/models.py"

