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



    def test_create_incident_custom_configured_state_in_insert_payload(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        """Assert create payload carries custom configured state
        and no separate state PATCH occurs.
        """
        cfg = bridge_settings.model_copy(
            update={"servicenow_incident_state_after_report": "3"}
        )
        adapter = ServiceNowAdapter(config=cfg, http_client=fake_ticket_client)
        ref = adapter.create_incident_with_report(sample_incident, sample_report)

        mutating_requests = [
            req
            for req in ticket_state.recorded_requests
            if req.method in ("POST", "PATCH") and req.path != "/test/reset"
        ]
        assert len(mutating_requests) == 3
        # First call carries the state directly
        assert mutating_requests[0].body.get("state") == "3"

        # No separate state PATCH anywhere
        state_patches = [
            req
            for req in mutating_requests
            if req.method == "PATCH" and "state" in req.body
        ]
        assert len(state_patches) == 0
        assert ref.external_key.startswith("INC001")
        assert len(ticket_state.servicenow_incidents) == 1
        assert len(ticket_state.servicenow_attachments) == 1

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

    def test_lifecycle_call_order_create_state_worknotes_attachment(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        """Verify strict ServiceNow lifecycle call order on new incident creation:
        1. create incident directly in target state (facts-only description, NO work notes yet)
        2. post findings as work notes (PATCH work_notes)
        3. upload standalone dossier HTML attachment (POST attachment)

        Assert that create payload carries state directly, and no separate state PATCH occurs.
        """
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)
        ref = adapter.create_incident_with_report(sample_incident, sample_report)

        # Filter mutating HTTP calls
        mutating_requests = [
            req
            for req in ticket_state.recorded_requests
            if req.method in ("POST", "PATCH") and req.path != "/test/reset"
        ]

        assert len(mutating_requests) == 3

        # 1. First call: POST create incident
        req1 = mutating_requests[0]
        assert req1.method == "POST"
        assert req1.path == "/api/now/table/incident"
        assert "short_description" in req1.body
        assert "description" in req1.body
        assert "work_notes" not in req1.body  # Must NOT contain work notes yet!
        assert req1.body.get("state") == "2"  # Must carry state directly in create payload

        # Verify NO separate state PATCH exists anywhere
        state_patches = [
            req
            for req in mutating_requests
            if req.method == "PATCH" and "state" in req.body
        ]
        assert len(state_patches) == 0

        # 2. Second call: PATCH work notes
        req2 = mutating_requests[1]
        assert req2.method == "PATCH"
        assert req2.path == f"/api/now/table/incident/{ref.external_id}"
        assert "work_notes" in req2.body
        assert req2.body["work_notes"].startswith("[code]")

        # 3. Third call: POST upload attachment
        req3 = mutating_requests[2]
        assert req3.method == "POST"
        assert req3.path == "/api/now/attachment/file"
        assert req3.query_params.get("table_sys_id") == ref.external_id
        assert req3.query_params.get("table_name") == "incident"
        expected_file = f"ci_cd_investigation_report_{ref.external_key}.html"
        assert req3.query_params.get("file_name") == expected_file

    def test_lifecycle_call_order_idempotent_existing_incident(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        """Verify strict ServiceNow lifecycle call order on idempotent existing incident path:
        1. patch metadata and state (facts-only description, NO work notes)
        2. post findings as work notes (PATCH work_notes)
        3. upload standalone dossier HTML attachment (POST attachment)
        """
        # Pre-seed existing incident in state="1" (New)
        corr_id = f"gdi:{sample_incident.github_repo}:{sample_incident.github_run_id}:1"
        ticket_state.servicenow_incidents["sys_existing_001"] = {
            "sys_id": "sys_existing_001",
            "number": "INC0010999",
            "correlation_id": corr_id,
            "correlation_display": "gha-deploy-investigator",
            "state": "1",
            "work_notes": [],
        }

        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)
        ref = adapter.create_incident_with_report(sample_incident, sample_report)
        assert ref.external_id == "sys_existing_001"

        mutating_requests = [
            req
            for req in ticket_state.recorded_requests
            if req.method in ("POST", "PATCH") and req.path != "/test/reset"
        ]

        assert len(mutating_requests) == 3

        # 1. Update metadata and state together before work notes
        req1 = mutating_requests[0]
        assert req1.method == "PATCH"
        assert req1.path == "/api/now/table/incident/sys_existing_001"
        assert "work_notes" not in req1.body
        assert req1.body.get("state") == "2"

        # 2. Work notes
        req2 = mutating_requests[1]
        assert req2.method == "PATCH"
        assert req2.path == "/api/now/table/incident/sys_existing_001"
        assert "work_notes" in req2.body

        # 3. Attachment upload
        req3 = mutating_requests[2]
        assert req3.method == "POST"
        assert req3.path == "/api/now/attachment/file"

    def test_lifecycle_existing_incident_already_in_target_state(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        """Verify that when an existing incident is already in target state,
        no state update is sent in the first PATCH.
        """
        corr_id = f"gdi:{sample_incident.github_repo}:{sample_incident.github_run_id}:1"
        ticket_state.servicenow_incidents["sys_existing_002"] = {
            "sys_id": "sys_existing_002",
            "number": "INC0010998",
            "correlation_id": corr_id,
            "correlation_display": "gha-deploy-investigator",
            "state": "2",
            "work_notes": [],
        }

        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)
        ref = adapter.create_incident_with_report(sample_incident, sample_report)
        assert ref.external_id == "sys_existing_002"

        mutating_requests = [
            req
            for req in ticket_state.recorded_requests
            if req.method in ("POST", "PATCH") and req.path != "/test/reset"
        ]

        assert len(mutating_requests) == 3
        req1 = mutating_requests[0]
        assert "state" not in req1.body

    def test_attachment_contains_sections_absent_from_work_notes(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        """Verify attachment dossier contains the elaborate diagnostic sections
        that are deliberately absent from the concise work notes.
        """
        from bridge.app.contracts.findings import EvidenceItem
        from bridge.app.contracts.report import CheckRunAnnotation, SpecialistStatus
        from bridge.app.templates.renderer import render_servicenow_html, render_standalone_html

        sample_report.impact = "Deployment pipeline completely blocked on main branch"
        sample_report.caveats = [
            "Logs truncated after 5MB limit",
            "Secondary container metrics unavailable",
        ]
        sample_report.evidence_by_area = {
            "ci_pipeline_logs": [
                EvidenceItem(
                    kind="runner_log",
                    excerpt="Step 'Run Ruff Lint' failed with code 1:\nF841 unused variable token",
                    console_url=(
                        "https://github.com/my-org/sample-app/actions/runs/1849201948/job/987654"
                    ),
                )
            ],
            "cloud_run_runtime": [
                EvidenceItem(
                    kind="container_event",
                    excerpt="Revision service-00002 failed to start: health check timeout",
                )
            ],
        }
        sample_report.specialists_status = [
            SpecialistStatus(specialist="ci_pipeline_logs", status="completed"),
            SpecialistStatus(specialist="cloud_run_runtime", status="completed"),
            SpecialistStatus(
                specialist="iac_terraform",
                status="skipped",
                error="No Terraform files changed",
            ),
        ]
        sample_report.annotations = [
            CheckRunAnnotation(
                path="app/main.py",
                start_line=42,
                end_line=42,
                annotation_level="failure",
                message="Local variable `unused_token` is assigned to but never used",
                title="Ruff F841",
            )
        ]
        sample_report.queries_appendix = {
            "ci_pipeline_logs": [
                "fetch_job_logs(run_id=1849201948)",
                "get_commit_diff(head_sha=e4b2a8d3)",
            ],
        }

        work_notes = render_servicenow_html(sample_report)
        dossier = render_standalone_html(
            sample_report,
            incident=sample_incident,
            incident_ref=Incident(
                id="sn-sys_123",
                source="servicenow",
                external_id="sys_123",
                external_key="INC0019482",
                title="CI Failure",
                description="desc",
                severity="P2",
                opened_at=sample_incident.opened_at,
            ),
            config=bridge_settings,
        )

        # Work notes is concise summary; sections below are ABSENT from work notes
        assert "Specialist Investigation Coverage" not in work_notes
        assert "Check Run Annotations" not in work_notes
        assert "Telemetry &amp; Specialist Evidence" not in work_notes
        assert "Investigation Caveats &amp; Telemetry Limitations" not in work_notes
        assert "Queries Run Appendix" not in work_notes
        assert "Quick Reference Links" not in work_notes
        assert "fetch_job_logs(run_id=1849201948)" not in work_notes

        # Attachment DOSSIER is elaborate and standalone; all sections MUST be present
        assert "<h2>Specialist Investigation Coverage</h2>" in dossier
        assert "<h2>Check Run Annotations</h2>" in dossier
        assert "<h2>Telemetry &amp; Specialist Evidence</h2>" in dossier
        assert "Investigation Caveats &amp; Telemetry Limitations" in dossier
        assert "<h2>Queries Run Appendix</h2>" in dossier
        assert "<h2>Quick Reference Links</h2>" in dossier
        assert "fetch_job_logs(run_id=1849201948)" in dossier
        assert "Logs truncated after 5MB limit" in dossier
        assert "Revision service-00002 failed to start" in dossier
        assert "app/main.py" in dossier
        assert "Ruff F841" in dossier
        assert "ci_pipeline_logs" in dossier
        assert "Completed" in dossier
        assert "Skipped" in dossier
        assert "No Terraform files changed" in dossier
        assert "https://github.com/my-org/sample-app/actions/runs/1849201948" in dossier

    def test_attachment_dossier_html_escaping(
        self,
        bridge_settings: BridgeConfig,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        """Verify dynamic content is strictly escaped against XSS in dossier HTML."""
        from bridge.app.contracts.findings import EvidenceItem
        from bridge.app.contracts.report import CheckRunAnnotation
        from bridge.app.templates.renderer import render_standalone_html

        sample_report.summary = "Test <script>alert('xss')</script> summary"
        sample_report.caveats = ["<img src=x onerror=alert(1)>"]
        sample_report.evidence_by_area = {
            "security": [
                EvidenceItem(kind="<script>", excerpt="<iframe src='malicious.site'></iframe>")
            ]
        }
        sample_report.annotations = [
            CheckRunAnnotation(
                path="<svg onload=alert(2)>",
                start_line=1,
                end_line=1,
                annotation_level="failure",
                message="<b>Dangerous</b>",
            )
        ]

        dossier = render_standalone_html(
            sample_report,
            incident=sample_incident,
            config=bridge_settings,
        )

        assert "<script>alert('xss')</script>" not in dossier
        xss_escaped = (
            "&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;" in dossier
            or "&lt;script&gt;alert('xss')&lt;/script&gt;" in dossier
        )
        assert xss_escaped
        assert "<img src=x onerror=alert(1)>" not in dossier
        assert "<iframe src='malicious.site'></iframe>" not in dossier
        assert "<svg onload=alert(2)>" not in dossier
        assert "<b>Dangerous</b>" not in dossier
        assert "&lt;b&gt;Dangerous&lt;/b&gt;" in dossier

    def test_attachment_dossier_structure_and_snapshot(
        self,
        bridge_settings: BridgeConfig,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        """Verify structural elements and styling in diagnostic dossier."""
        from bridge.app.templates.renderer import render_standalone_html

        dossier = render_standalone_html(
            sample_report,
            incident=sample_incident,
            config=bridge_settings,
        )

        assert dossier.startswith("<!DOCTYPE html>")
        assert '<html lang="en">' in dossier
        assert "<style>" in dossier
        assert "</style>" in dossier
        assert '<div class="container">' in dossier
        assert '<div class="meta-grid">' in dossier
        assert "Repository" in dossier
        assert "Branch" in dossier
        assert "Commit SHA" in dossier
        assert "Failing Stage" in dossier
        assert "<h2>Executive Summary</h2>" in dossier
        assert "<h2>Recommended Remediation Actions</h2>" in dossier
        assert '<div class="footer">' in dossier
        assert "Autocloud CI/CD Deployment Investigator v0.1.0" in dossier

    def test_create_incident_missing_sys_id_in_response_raises_value_error(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify clear ValueError raised if ServiceNow response lacks sys_id."""
        real_post = fake_ticket_client.post

        def post_without_sys_id(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            resp = real_post(url, *args, **kwargs)
            if "/api/now/table/incident" in str(url):
                data = resp.json()
                data["result"].pop("sys_id", None)
                return httpx.Response(201, json=data, request=resp.request)
            return resp

        monkeypatch.setattr(fake_ticket_client, "post", post_without_sys_id)
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)

        with pytest.raises(ValueError, match="missing 'sys_id'"):
            adapter.create_incident_with_report(sample_incident, sample_report)

    def test_create_incident_missing_number_in_response_raises_value_error(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify clear ValueError raised if ServiceNow response lacks number."""
        real_post = fake_ticket_client.post

        def post_without_number(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            resp = real_post(url, *args, **kwargs)
            if "/api/now/table/incident" in str(url):
                data = resp.json()
                data["result"].pop("number", None)
                return httpx.Response(201, json=data, request=resp.request)
            return resp

        monkeypatch.setattr(fake_ticket_client, "post", post_without_number)
        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)

        with pytest.raises(ValueError, match="missing 'number'"):
            adapter.create_incident_with_report(sample_incident, sample_report)

    def test_existing_incident_missing_sys_id_raises_value_error(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        """Verify clear ValueError raised if existing incident lacks sys_id."""
        corr_id = f"gdi:{sample_incident.github_repo}:{sample_incident.github_run_id}:1"
        ticket_state.servicenow_incidents["corrupted_001"] = {
            "sys_id": "",
            "number": "INC0019999",
            "correlation_id": corr_id,
            "correlation_display": "gha-deploy-investigator",
        }

        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)
        with pytest.raises(ValueError, match="missing 'sys_id'"):
            adapter.create_incident_with_report(sample_incident, sample_report)

    def test_existing_incident_missing_number_raises_value_error(
        self,
        bridge_settings: BridgeConfig,
        fake_ticket_client: httpx.Client,
        sample_incident: Incident,
        sample_report: Report,
    ) -> None:
        """Verify clear ValueError raised if existing incident lacks number."""
        corr_id = f"gdi:{sample_incident.github_repo}:{sample_incident.github_run_id}:1"
        ticket_state.servicenow_incidents["corrupted_002"] = {
            "sys_id": "sys_corrupted_002",
            "number": "",
            "correlation_id": corr_id,
            "correlation_display": "gha-deploy-investigator",
        }

        adapter = ServiceNowAdapter(config=bridge_settings, http_client=fake_ticket_client)
        with pytest.raises(ValueError, match="missing 'number'"):
            adapter.create_incident_with_report(sample_incident, sample_report)




