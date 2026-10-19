import hashlib
import hmac
import json
from datetime import timezone
from typing import Any

import pytest
from bridge.app.adapters import (
    GitHubAdapter,
    IncidentRef,
    ServiceNowAdapter,
    get_adapter,
)
from bridge.app.adapters.base import parse_utc_datetime
from bridge.app.config import BridgeConfig, get_settings
from bridge.app.contracts.incident import Incident
from bridge.app.contracts.report import Report
from bridge.app.contracts.result import InvestigationResult


class TestAdapters:
    def test_get_adapter_resolution(self, bridge_settings: BridgeConfig) -> None:
        github_adapter = get_adapter("github", bridge_settings)
        assert isinstance(github_adapter, GitHubAdapter)

        github_upper = get_adapter("GITHUB", bridge_settings)
        assert isinstance(github_upper, GitHubAdapter)

        sn_adapter = get_adapter("servicenow", bridge_settings)
        assert isinstance(sn_adapter, ServiceNowAdapter)

        with pytest.raises(ValueError, match="Unsupported adapter source"):
            get_adapter("unknown_platform", bridge_settings)

    def test_incident_ref_conversions(
        self,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        ref = IncidentRef.from_incident(sample_incident)
        assert ref.id == sample_incident.id
        assert ref.source == "github"
        assert ref.external_id == sample_incident.external_id
        assert ref.external_key == sample_incident.external_key

        res = InvestigationResult(
            investigation_id="inv-test-1",
            incident_id=sample_incident.id,
            incident_source=sample_incident.source,
            incident_external_id=sample_incident.external_id,
            incident_external_key=sample_incident.external_key,
            status="completed",
            report=sample_report,
            incident=sample_incident,
        )
        ref_from_res = IncidentRef.from_result(res)
        assert ref_from_res.id == sample_incident.id
        assert ref_from_res.source == sample_incident.source
        assert ref_from_res.external_id == sample_incident.external_id
        assert ref_from_res.external_key == sample_incident.external_key

    def test_parse_utc_datetime(self) -> None:
        dt1 = parse_utc_datetime("2026-10-16T21:00:00Z")
        assert dt1.tzinfo == timezone.utc
        assert dt1.year == 2026

        dt2 = parse_utc_datetime("2026-10-16 21:00:00")
        assert dt2.tzinfo == timezone.utc

        dt3 = parse_utc_datetime(1700000000)
        assert dt3.tzinfo == timezone.utc

        now = parse_utc_datetime(None)
        assert now.tzinfo == timezone.utc

    def test_github_webhook_ping_event(self, bridge_settings: BridgeConfig) -> None:
        """Verify that a signed GitHub ping event returns 200 pong."""
        from bridge.app.main import app
        from starlette.testclient import TestClient

        app.dependency_overrides[get_settings] = lambda: bridge_settings

        client = TestClient(app)
        payload = {"zen": "Keep it logically awesome.", "hook_id": 12345}
        body_bytes = json.dumps(payload).encode("utf-8")
        secret = bridge_settings.github_webhook_secret or ""
        valid_sig = "sha256=" + hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()

        # 1. Unauthenticated ping rejected
        unauth_resp = client.post(
            "/webhook/github",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": "sha256=invalidhexsig",
            },
        )
        assert unauth_resp.status_code == 401

        # 2. Authenticated ping returns 200 pong
        resp = client.post(
            "/webhook/github",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": valid_sig,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pong"
        assert "ping received successfully" in data["message"]

    def test_servicenow_adapter_renders_unrelated_observations_separately(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: Any,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        from bridge.tests.fake_ticket_server import state as ticket_state

        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)
        sample_report.causal_remediation = ["Update test assertion"]
        sample_report.unrelated_observations = ["Deprecation warning on helper library"]

        ref = adapter.create_incident_with_report(sample_incident, sample_report)
        incident_data = ticket_state.servicenow_incidents[ref.external_id]
        desc = incident_data["description"]
        work_notes = incident_data["work_notes"]

        # Description is facts only; remediation, observations, root cause and pointer are excluded
        assert "Recommended Remediation:" not in desc
        assert "Update test assertion" not in desc
        assert "Unrelated Observations:" not in desc
        assert "Deprecation warning on helper library" not in desc
        assert "Root Cause:" not in desc

        # Remediation and unrelated observations are rendered into work notes
        assert "Update test assertion" in work_notes
        assert "Deprecation warning on helper library" in work_notes

    def test_github_adapter_renders_unrelated_observations_separately(
        self,
        bridge_settings: BridgeConfig,
        fake_github_client: Any,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        from bridge.tests.fake_github_server import state as github_state

        adapter = GitHubAdapter(config=bridge_settings, http_client=fake_github_client)
        ref = IncidentRef(
            id="sn-sys_test_qf1",
            source="servicenow",
            external_id="sys_test_qf1",
            external_key="INC0099999",
        )
        sample_report.causal_remediation = ["Fix assertion in test"]
        sample_report.unrelated_observations = ["Unrelated runtime warning"]
        github_state.comments.clear()

        adapter.post_report_link(sample_incident, sample_report, ref)

        assert len(github_state.comments) == 1
        comment_body = github_state.comments[0]["body"]

        assert "<summary><strong>🛠️ Recommended Remediation</strong></summary>" in comment_body
        assert "- Fix assertion in test" in comment_body
        assert "<summary><strong>⚠️ Unrelated Observations</strong></summary>" in comment_body
        assert "- Unrelated runtime warning" in comment_body

        rem_block, obs_block = comment_body.split(
            "<summary><strong>⚠️ Unrelated Observations</strong></summary>"
        )
        assert "Unrelated runtime warning" not in rem_block
        assert "- Fix assertion in test" in rem_block
        assert "- Unrelated runtime warning" in obs_block

    def test_html_renderers_segregate_unrelated_observations(
        self,
        sample_report: Report,
    ) -> None:
        from bridge.app.templates.renderer import render_servicenow_html, render_standalone_html

        sample_report.causal_remediation = ["Primary causal action"]
        sample_report.unrelated_observations = ["Isolated side warning"]

        sn_html = render_servicenow_html(sample_report)
        assert "Recommended Actions" in sn_html
        assert "Primary causal action" in sn_html
        assert "Unrelated Observations" in sn_html
        assert "Isolated side warning" in sn_html

        rem_sn, obs_sn = sn_html.split("Unrelated Observations")
        assert "Isolated side warning" not in rem_sn
        assert "Primary causal action" in rem_sn
        assert "Isolated side warning" in obs_sn

        standalone_html = render_standalone_html(sample_report)
        assert "<h2>Recommended Remediation Actions</h2>" in standalone_html
        assert "Primary causal action" in standalone_html
        assert "<h2>Unrelated Observations</h2>" in standalone_html
        assert "Isolated side warning" in standalone_html

        rem_std, obs_std = standalone_html.split("<h2>Unrelated Observations</h2>")
        assert "Isolated side warning" not in rem_std
        assert "Primary causal action" in rem_std
        assert "Isolated side warning" in obs_std
