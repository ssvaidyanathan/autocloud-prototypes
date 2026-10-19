import logging
from typing import Any

import httpx
import pytest
from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig
from bridge.app.contracts.incident import Incident
from bridge.app.contracts.report import Report
from bridge.tests.fake_ticket_server import state as ticket_state


class TestServiceNowReverse:
    def test_create_incident_with_report(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)

        ref = adapter.create_incident_with_report(sample_incident, sample_report)

        assert ref.source == "servicenow"
        assert ref.external_key.startswith("INC001")
        assert ref.external_id.startswith("sys_")
        assert ref.external_url is not None
        assert ref.external_id in ref.external_url

        # Verify Table API ticket creation in fake server
        assert len(ticket_state.servicenow_incidents) == 1
        created_ticket = list(ticket_state.servicenow_incidents.values())[0]
        assert (
            "[CI/CD Failure] my-org/sample-app (Run Ruff Lint)"
            in created_ticket["short_description"]
        )
        assert created_ticket["category"] == "DevOps"
        assert created_ticket["urgency"] == "2"  # P2 -> 2
        assert created_ticket["impact"] == "2"
        assert created_ticket["correlation_display"] == "gha-deploy-investigator"
        assert created_ticket["correlation_id"] == "gdi:my-org/sample-app:1849201948:1"
        assert created_ticket["work_notes"].startswith("[code]")
        assert created_ticket["work_notes"].endswith("[/code]")
        assert "Run local linter" in created_ticket["work_notes"]
        assert "Recommended Remediation:" not in created_ticket["description"]
        assert "Unrelated Observations:" not in created_ticket["description"]
        assert "Repository: my-org/sample-app" in created_ticket["description"]
        assert "Branch: main" in created_ticket["description"]
        assert "Commit SHA: e4b2a8d3" in created_ticket["description"]
        assert "GitHub Actor: octocat" in created_ticket["description"]
        assert "Failing Stage: Run Ruff Lint" in created_ticket["description"]
        assert (
            "Workflow Run URL: https://github.com/my-org/sample-app/actions/runs/1849201948"
            in created_ticket["description"]
        )
        assert "Root Cause:" not in created_ticket["description"]
        assert sample_report.probable_root_cause.text not in created_ticket["description"]
        assert "Full diagnostic analysis" not in created_ticket["description"]
        assert "Work Notes" not in created_ticket["description"]

        # Verify Attachment API in fake server
        assert len(ticket_state.servicenow_attachments) == 1
        att = ticket_state.servicenow_attachments[0]
        assert att["table_name"] == "incident"
        assert att["table_sys_id"] == ref.external_id
        assert att["file_name"] == f"ci_cd_investigation_report_{ref.external_key}.html"
        assert "<!DOCTYPE html>" in att["content"]
        assert "Autocloud CI/CD Deployment Investigation Report" in att["content"]

    def test_create_incident_idempotent_query_existing(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)

        # 1. First call creates the incident with correlation tagging
        ref1 = adapter.create_incident_with_report(sample_incident, sample_report)
        assert len(ticket_state.servicenow_incidents) == 1
        assert len(ticket_state.servicenow_attachments) == 1

        # 2. Second call with same correlation_id queries and updates existing incident
        sample_report.summary = "Updated investigation summary with new evidence."
        ref2 = adapter.create_incident_with_report(sample_incident, sample_report)

        # Verify no duplicate incident was created
        assert len(ticket_state.servicenow_incidents) == 1
        assert ref2.external_id == ref1.external_id
        assert ref2.external_key == ref1.external_key

        # Verify work notes were updated on existing ticket
        ticket = ticket_state.servicenow_incidents[ref1.external_id]
        assert "Updated investigation summary with new evidence" in ticket["work_notes"][-1]

        # Verify attachment was uploaded to existing ticket
        assert len(ticket_state.servicenow_attachments) == 2
        assert ticket_state.servicenow_attachments[1]["table_sys_id"] == ref1.external_id

    def test_servicenow_timeout_configuration(self) -> None:
        cfg = BridgeConfig(
            adapter_timeout_seconds=60.0,
            servicenow_username="admin",
            servicenow_password="secret",
        )
        adapter = ServiceNowAdapter(config=cfg)
        client = adapter._get_client()
        assert client.timeout.read == 60.0
        assert client.timeout.connect == 10.0

    def test_create_incident_retries_transient_network_error(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)

        attempts = 0
        incident_query_attempts = 0
        original_get = fake_ticket_client.get

        def flaky_get(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal attempts, incident_query_attempts
            attempts += 1
            if "/table/incident" in str(url):
                incident_query_attempts += 1
                if incident_query_attempts == 1:
                    raise httpx.ConnectTimeout("Transient connection timeout")
            return original_get(url, *args, **kwargs)

        monkeypatch.setattr(fake_ticket_client, "get", flaky_get)
        monkeypatch.setattr("time.sleep", lambda s: None)

        ref = adapter.create_incident_with_report(sample_incident, sample_report)
        assert incident_query_attempts == 2
        assert ref.external_key.startswith("INC001")

    def test_create_incident_raises_after_exhausting_retries(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)

        def always_failing_get(*args: Any, **kwargs: Any) -> httpx.Response:
            raise httpx.NetworkError("Persistent network outage")

        monkeypatch.setattr(fake_ticket_client, "get", always_failing_get)
        monkeypatch.setattr("time.sleep", lambda s: None)

        with pytest.raises(httpx.NetworkError, match="Persistent network outage"):
            adapter.create_incident_with_report(sample_incident, sample_report)

    def test_create_incident_with_caller_id_and_assignment_group(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        cfg = bridge_settings.model_copy(
            update={
                "servicenow_caller": "admin",
                "servicenow_assignment_group": "Platform-SRE",
            }
        )
        adapter = ServiceNowAdapter(config=cfg, http_client=fake_ticket_client)

        ref = adapter.create_incident_with_report(sample_incident, sample_report)
        ticket = ticket_state.servicenow_incidents[ref.external_id]

        assert ticket["caller_id"] == "6816f79cc0a8016401c5a33be04be441"
        assert ticket["assignment_group"] == "Platform-SRE"
        assert ticket["state"] == "2"
        assert "GitHub Actor: octocat" in ticket["description"]
        assert "Commit SHA: e4b2a8d3" in ticket["description"]
        assert "Commit Author:" not in ticket["description"]

    def test_idempotent_retry_patches_caller_id_on_existing_incident(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        # Pre-seed existing incident without caller_id
        corr_id = f"gdi:{sample_incident.github_repo}:{sample_incident.github_run_id}:1"
        ticket_state.servicenow_incidents["sys_999999"] = {
            "sys_id": "sys_999999",
            "number": "INC0010074",
            "correlation_id": corr_id,
            "correlation_display": "gha-deploy-investigator",
            "work_notes": [],
        }

        cfg = bridge_settings.model_copy(
            update={
                "servicenow_caller": "admin",
                "servicenow_assignment_group": "Cloud-Operations",
            }
        )
        adapter = ServiceNowAdapter(config=cfg, http_client=fake_ticket_client)

        ref = adapter.create_incident_with_report(sample_incident, sample_report)
        assert ref.external_id == "sys_999999"
        assert ref.external_key == "INC0010074"

        # Verify existing incident was patched with caller_id, assignment_group,
        # short_description, and description
        patched = ticket_state.servicenow_incidents["sys_999999"]
        assert patched["caller_id"] == "6816f79cc0a8016401c5a33be04be441"
        assert patched["assignment_group"] == "Cloud-Operations"
        assert patched["state"] == "2"
        assert "my-org/sample-app" in patched["short_description"]
        assert "Root Cause:" not in patched["description"]
        assert sample_report.probable_root_cause.text not in patched["description"]
        assert "Recommended Remediation:" not in patched["description"]
        assert "Unrelated Observations:" not in patched["description"]
        assert "Full diagnostic analysis" not in patched["description"]
        assert "Work Notes" not in patched["description"]



    def test_state_transition_rejection_logs_error_and_raises(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        real_patch = fake_ticket_client.patch

        def reject_state_patch(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            json_body = kwargs.get("json", {})
            if "state" in json_body:
                req = httpx.Request("PATCH", str(url))
                return httpx.Response(
                    400,
                    request=req,
                    text='{"error": {"message": "State transition rejected: mandatory field"}}',
                )
            return real_patch(url, *args, **kwargs)

        fake_ticket_client.patch = reject_state_patch  # type: ignore[method-assign]
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)

        with caplog.at_level(logging.ERROR):
            with pytest.raises(httpx.HTTPStatusError):
                adapter.create_incident_with_report(sample_incident, sample_report)

        assert "ServiceNow rejected transition to state '2'" in caplog.text
        assert "State transition rejected: mandatory field" in caplog.text

    def test_description_builder_required_summary_fields(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)

        ref = adapter.create_incident_with_report(sample_incident, sample_report)
        ticket = ticket_state.servicenow_incidents[ref.external_id]
        desc = ticket["description"]

        # Required fields
        assert "Repository: my-org/sample-app" in desc
        assert "Branch: main" in desc
        assert "Commit SHA: e4b2a8d3" in desc
        assert sample_incident.github_head_sha not in desc  # full 40-char SHA must not be present
        assert "GitHub Actor: octocat" in desc
        assert "Failing Stage: Run Ruff Lint" in desc
        expected_run_url = (
            "Workflow Run URL: https://github.com/my-org/sample-app/actions/runs/1849201948"
        )
        assert expected_run_url in desc
        assert "Root Cause:" not in desc
        assert sample_report.probable_root_cause.text not in desc
        assert "Full diagnostic analysis" not in desc
        assert "Work Notes" not in desc

        # Optional fields omitted when absent
        sparse_incident = sample_incident.model_copy(
            update={
                "github_failing_stage": None,
                "external_url": None,
                "github_head_sha": None,
                "github_actor": None,
                "reporter": "backup-reporter",
            }
        )
        desc_sparse = adapter.build_description(sparse_incident, sample_report)
        assert "Failing Stage:" not in desc_sparse
        assert "Workflow Run URL:" not in desc_sparse
        assert "Commit SHA: N/A" in desc_sparse
        assert "GitHub Actor: backup-reporter" in desc_sparse

    def test_description_no_remediation_or_unrelated_observations(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)

        sample_report.causal_remediation = ["Revert commit e4b2a8d3 immediately"]
        sample_report.recommended_actions = ["Run pytest with verbose flags"]
        sample_report.unrelated_observations = ["Found deprecated library foo-bar in requirements"]

        ref = adapter.create_incident_with_report(sample_incident, sample_report)
        ticket = ticket_state.servicenow_incidents[ref.external_id]
        desc = ticket["description"]
        work_notes = ticket["work_notes"]

        # Neither section header nor items in description
        assert "Recommended Remediation:" not in desc
        assert "Revert commit e4b2a8d3 immediately" not in desc
        assert "Run pytest with verbose flags" not in desc
        assert "Unrelated Observations:" not in desc
        assert "Found deprecated library foo-bar in requirements" not in desc

        # Work notes must retain the full report including remediation and observations
        assert "Revert commit e4b2a8d3 immediately" in work_notes
        assert "Found deprecated library foo-bar in requirements" in work_notes

    def test_description_no_root_cause_findings_or_pointer(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)

        sample_report.probable_root_cause.text = "Severe container startup probe failure"
        sample_report.causal_remediation = ["Revert deploy commit"]
        sample_report.unrelated_observations = ["Deprecated runtime base image"]

        desc = adapter.build_description(sample_incident, sample_report)

        # Facts only: NO root cause, NO remediation, NO observations, NO pointer line
        assert "Root Cause:" not in desc
        assert "Severe container startup probe failure" not in desc
        assert "Revert deploy commit" not in desc
        assert "Deprecated runtime base image" not in desc
        assert "Recommended Remediation:" not in desc
        assert "Unrelated Observations:" not in desc
        assert "Work Notes" not in desc
        assert "attached HTML report" not in desc

        # Verify that description consists exclusively of facts lines
        lines = desc.splitlines()
        assert len(lines) == 6
        assert lines[0] == "Repository: my-org/sample-app"
        assert lines[1] == "Branch: main"
        assert lines[2] == "Commit SHA: e4b2a8d3"
        assert lines[3] == "GitHub Actor: octocat"
        assert lines[4] == "Failing Stage: Run Ruff Lint"
        assert (
            lines[5]
            == "Workflow Run URL: https://github.com/my-org/sample-app/actions/runs/1849201948"
        )

    def test_work_notes_payload_unchanged_full_report(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)

        ref = adapter.create_incident_with_report(sample_incident, sample_report)
        ticket = ticket_state.servicenow_incidents[ref.external_id]
        work_notes = ticket["work_notes"]

        # Work notes must remain valid HTML chunk wrapped in [code]...[/code]
        assert work_notes.startswith("[code]")
        assert work_notes.endswith("[/code]")
        assert "Autocloud CI/CD Deployment Investigation Report" in work_notes
        assert "Probable Root Cause" in work_notes
        assert "Local variable" in work_notes
        assert "unused_token" in work_notes
        assert "Run local linter" in work_notes

