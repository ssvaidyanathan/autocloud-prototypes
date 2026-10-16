import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from bridge.app.adapters.github import GitHubAdapter
from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig, get_settings
from bridge.app.contracts.report import Report
from bridge.app.contracts.result import InvestigationResult
from bridge.app.core.dedup import get_dedup_registry
from bridge.app.main import app
from bridge.tests.fake_github_server import state as github_state
from bridge.tests.fake_ticket_server import state as ticket_state
from starlette.testclient import TestClient


class TestE2EReverseFlow:
    def test_e2e_github_webhook_to_servicenow_and_checkrun(
        self,
        bridge_settings: BridgeConfig,
        fake_github_client: httpx.Client,
        fake_ticket_client: httpx.Client,
        sample_report: Report,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        get_dedup_registry().clear()
        app.dependency_overrides[get_settings] = lambda: bridge_settings

        # Wire adapters to talk directly to fake in-memory servers
        github_adapter = GitHubAdapter(config=bridge_settings, http_client=fake_github_client)
        servicenow_adapter = ServiceNowAdapter(
            config=bridge_settings, http_client=fake_ticket_client
        )

        def mock_get_adapter(source: str, config: Any = None) -> Any:
            if source.lower() == "github":
                return github_adapter
            elif source.lower() == "servicenow":
                return servicenow_adapter
            raise ValueError(source)

        monkeypatch.setattr("bridge.app.main.get_adapter", mock_get_adapter)

        client = TestClient(app)

        # 1. Step 1: Ingest GitHub workflow failure webhook
        webhook_payload = {
            "action": "completed",
            "workflow_run": {
                "id": 1849201948,
                "name": "Sample App CI/CD Pipeline",
                "conclusion": "failure",
                "head_sha": "e4b2a8d39c1234567890abcdef1234567890abcd",
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
        body_bytes = json.dumps(webhook_payload).encode("utf-8")
        secret = bridge_settings.github_webhook_secret or ""
        valid_sig = "sha256=" + hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()

        response = client.post(
            "/webhook/github",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": valid_sig,
            },
        )
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["incident_id"] == "gha-1849201948"

        # 2. Duplicate webhook test (idempotency)
        dup_response = client.post(
            "/webhook/github",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": valid_sig,
            },
        )
        assert dup_response.status_code == 200
        assert dup_response.json()["status"] == "duplicate_ignored"

        # 3. Step 2: Agent completes investigation and calls back to /callback/report
        incident = github_adapter.to_incident(webhook_payload)
        incident.github_failing_stage = "Run Ruff Lint"

        result = InvestigationResult(
            investigation_id="inv-e2e-slice-001",
            incident_id=incident.id,
            incident_source=incident.source,
            incident_external_id=incident.external_id,
            incident_external_key=incident.external_key,
            status="completed",
            report=sample_report,
            incident=incident,
        )

        callback_response = client.post(
            "/callback/report",
            json=result.model_dump(mode="json"),
            headers={"Authorization": "Bearer mock-oidc-token-for-testing"},
        )
        assert callback_response.status_code == 200
        cb_data = callback_response.json()
        assert cb_data["status"] == "ok"
        assert "INC001" in cb_data["servicenow_ticket"]

        # 4. Step 3: Verify reverse ServiceNow Incident was created
        assert len(ticket_state.servicenow_incidents) == 1
        created_sn_ticket = list(ticket_state.servicenow_incidents.values())[0]
        assert (
            "[CI/CD Failure] my-org/sample-app (Run Ruff Lint)"
            in created_sn_ticket["short_description"]
        )
        assert created_sn_ticket["category"] == "DevOps"
        assert len(ticket_state.servicenow_attachments) == 1

        # 5. Step 4: Verify developer-facing GitHub Check Run was posted
        assert len(github_state.check_runs) == 1
        check_run = github_state.check_runs[0]
        assert check_run["name"] == "Autocloud / ServiceNow Incident Link"
        assert check_run["head_sha"] == "e4b2a8d39c1234567890abcdef1234567890abcd"
        assert cb_data["servicenow_ticket"] in check_run["output"]["summary"]

        # 6. Step 5: Duplicate callback report is ignored
        dup_cb_response = client.post(
            "/callback/report",
            json=result.model_dump(mode="json"),
            headers={"Authorization": "Bearer mock-oidc-token-for-testing"},
        )
        assert dup_cb_response.status_code == 200
        assert dup_cb_response.json()["status"] == "duplicate_ignored"

    def test_non_failure_webhook_ignored_with_http_200(
        self,
        bridge_settings: BridgeConfig,
    ) -> None:
        client = TestClient(app)
        payload = {
            "action": "completed",
            "workflow_run": {
                "id": 12345,
                "name": "CI",
                "conclusion": "success",
            },
            "repository": {"name": "sample-app"},
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        secret = bridge_settings.github_webhook_secret or ""
        valid_sig = "sha256=" + hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()

        resp = client.post(
            "/webhook/github",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": valid_sig,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ignored"
        assert "Ignoring non-failure" in data["reason"]
