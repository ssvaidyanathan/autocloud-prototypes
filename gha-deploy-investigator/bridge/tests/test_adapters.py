import hashlib
import hmac
import json
from datetime import timezone

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
