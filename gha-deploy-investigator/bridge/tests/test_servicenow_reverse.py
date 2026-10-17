import httpx
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
