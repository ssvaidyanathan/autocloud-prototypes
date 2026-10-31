import json
from datetime import datetime, timezone
from typing import Any

import pytest
from bridge.app.adapters.jira import JiraAdapter
from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig, get_settings
from bridge.app.contracts.findings import EvidenceItem
from bridge.app.contracts.report import ProbableRootCause, Report, TimelineItem
from bridge.app.core.auth import create_mock_oidc_token
from bridge.app.core.tasks import TaskDispatcher
from bridge.app.main import app, set_task_dispatcher
from bridge.tests.fake_ticket_server import app as fake_server_app
from bridge.tests.fake_ticket_server import state as fake_state
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def setup_teardown_e2e(monkeypatch: pytest.MonkeyPatch) -> Any:
    # 1. Reset fake server state
    fake_state.reset()
    monkeypatch.setenv("APP_ENV", "local")

    # 2. Configure bridge settings for E2E testing
    test_config = BridgeConfig(
        app_env="local",
        gcp_project="test-project",
        gcp_region="us-central1",
        tasks_queue="investigations",
        tasks_location="us-central1",
        tasks_invoker_sa="tasks-invoker@test-project.iam.gserviceaccount.com",
        agent_service_url="https://sre-agent.example.com",
        agent_sa="agent-sa@test-project.iam.gserviceaccount.com",
        callback_url="https://bridge.example.com/callback/report",
        servicenow_instance_url="http://fake-ticket",
        servicenow_username="svc_sre_agent",
        servicenow_password="snow_password",
        servicenow_webhook_token="secret-snow-webhook-token",
        servicenow_in_progress_state="2",
        jira_base_url="http://fake-ticket",
        jira_email="sre-agent@example.com",
        jira_api_token="jira_api_token",
        jira_webhook_token="secret-jira-webhook-token",
        jira_in_progress_transition="In Progress",
    )

    app.dependency_overrides[get_settings] = lambda: test_config

    # Reset task dispatcher with clean in-memory state
    dispatcher = TaskDispatcher(config=test_config)
    set_task_dispatcher(dispatcher)

    # Route outbound adapter HTTP calls directly to the fake_ticket_server ASGI app
    fake_client = TestClient(
        fake_server_app,
        base_url="http://fake-ticket",
    )
    monkeypatch.setattr(ServiceNowAdapter, "_get_client", lambda self: fake_client)
    monkeypatch.setattr(JiraAdapter, "_get_client", lambda self: fake_client)

    yield

    app.dependency_overrides.clear()
    set_task_dispatcher(None)
    fake_client.close()


def _sample_report() -> Report:
    return Report(
        summary="Service shop-api experiencing 503 error spike due to missing DB password env var.",
        impact="Checkout flow failing for 40% of users.",
        timeline=[
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 0, 0, tzinfo=timezone.utc),
                description="Revision shop-api-v2 deployed.",
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 2, 0, tzinfo=timezone.utc),
                description="503 errors spike detected.",
            ),
        ],
        evidence_by_area={
            "logs": [
                EvidenceItem(
                    kind="error_log",
                    excerpt="KeyError: 'DB_PASSWORD' during database pool initialization",
                    timestamp=datetime(2026, 10, 9, 10, 2, 30, tzinfo=timezone.utc),
                    console_url="https://console.cloud.google.com/logs/query",
                )
            ]
        },
        probable_root_cause=ProbableRootCause(
            text="Revision shop-api-v2 omitted DB_PASSWORD environment variable.",
            confidence=0.95,
        ),
        recommended_actions=[
            "Roll back to revision shop-api-v1.",
            "Add DB_PASSWORD secret reference to deployment configuration.",
        ],
    )


def test_e2e_servicenow_full_lifecycle() -> None:
    client = TestClient(app)

    # 1. Inbound ServiceNow Webhook
    snow_payload = {
        "sys_id": "998877aabbcc",
        "number": "INC0020002",
        "short_description": "shop-api checkout failing with 503s",
        "description": "Customers reported errors on checkout page",
        "priority": "1",
        "cmdb_ci": "shop-api",
        "caller_id": "Lead SRE",
        "opened_at": "2026-10-09 11:00:00",
    }

    headers = {"Authorization": "Bearer secret-snow-webhook-token"}
    resp = client.post("/webhook/servicenow", json=snow_payload, headers=headers)

    assert resp.status_code == 202
    resp_data = resp.json()
    assert resp_data["status"] == "accepted"
    assert resp_data["incident_id"] == "snow-998877aabbcc"
    assert resp_data["external_key"] == "INC0020002"
    task_id = resp_data["task_id"]
    assert task_id.startswith("inv-")

    # Verify initial ticket acknowledgement in fake ticket server
    snow_incident = fake_state.servicenow_incidents.get("998877aabbcc")
    assert snow_incident is not None
    assert snow_incident["state"] == "2"
    assert any(
        "Gemini Cloud Assist investigation" in note and "Investigation started" in note
        for note in snow_incident["work_notes"]
    )

    # 2. Simulate SRE Agent Callback POST /callback/report
    auth_token = create_mock_oidc_token(
        audience="https://bridge.example.com/callback/report",
        email="agent-sa@test-project.iam.gserviceaccount.com",
    )

    callback_payload = {
        "investigation_id": "inv-test-12345",
        "incident_id": "snow-998877aabbcc",
        "incident_source": "servicenow",
        "incident_external_id": "998877aabbcc",
        "incident_external_key": "INC0020002",
        "status": "completed",
        "report": _sample_report().model_dump(mode="json"),
    }

    callback_resp = client.post(
        "/callback/report",
        json=callback_payload,
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert callback_resp.status_code == 200
    assert callback_resp.json() == {"status": "ok"}

    # Verify report was posted to ServiceNow fake server (strictly ONE work note, no chunking)
    updated_incident = fake_state.servicenow_incidents["998877aabbcc"]
    assert len(updated_incident["work_notes"]) == 2
    report_note = updated_incident["work_notes"][1]
    assert report_note.startswith("[code]")
    assert report_note.endswith("[/code]")
    assert "Probable Root Cause" in report_note
    assert "Full Investigation Report Attached" in report_note
    assert len(report_note) < 12000

    # Verify HTML attachment was created
    assert len(fake_state.servicenow_attachments) == 1
    att = fake_state.servicenow_attachments[0]
    assert att["table_sys_id"] == "998877aabbcc"
    assert "sre_investigation_report_INC0020002.html" in att["file_name"]
    assert "Probable Root Cause" in att["content"]


def test_e2e_jira_full_lifecycle() -> None:
    client = TestClient(app)

    # 1. Inbound Jira Webhook
    jira_payload = {
        "issue": {
            "id": "20050",
            "key": "OPS-99",
            "fields": {
                "summary": "shop-api latency spike",
                "description": "P99 latency exceeding 4 seconds",
                "priority": {"name": "High"},
                "components": [{"name": "shop-api"}],
                "labels": ["latency", "alert"],
                "created": "2026-10-09T11:15:00.000+0000",
                "reporter": {"displayName": "Bob Engineer"},
            },
        }
    }

    headers = {"X-Webhook-Token": "secret-jira-webhook-token"}
    resp = client.post("/webhook/jira", json=jira_payload, headers=headers)

    assert resp.status_code == 202
    resp_data = resp.json()
    assert resp_data["status"] == "accepted"
    assert resp_data["incident_id"] == "jira-OPS-99"
    assert resp_data["external_key"] == "OPS-99"

    # Verify initial comment and transition in fake ticket server
    assert len(fake_state.jira_comments) == 1
    assert any("Investigation started" in json.dumps(c) for c in fake_state.jira_comments)
    assert len(fake_state.jira_transitions_history) == 1
    assert fake_state.jira_transitions_history[0]["transition_id"] == "21"

    # 2. Simulate SRE Agent Callback POST /callback/report
    auth_token = create_mock_oidc_token(
        audience="https://bridge.example.com/callback/report",
        email="agent-sa@test-project.iam.gserviceaccount.com",
    )

    callback_payload = {
        "investigation_id": "inv-test-67890",
        "incident_id": "jira-OPS-99",
        "incident_source": "jira",
        "incident_external_id": "20050",
        "incident_external_key": "OPS-99",
        "status": "completed",
        "report": _sample_report().model_dump(mode="json"),
    }

    callback_resp = client.post(
        "/callback/report",
        json=callback_payload,
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert callback_resp.status_code == 200

    # Verify ADF report comment was posted
    assert len(fake_state.jira_comments) == 2
    adf_comment = fake_state.jira_comments[1]["body"]
    assert adf_comment["type"] == "doc"
    assert "Probable Root Cause" in json.dumps(adf_comment)

    # Verify HTML attachment was created
    assert len(fake_state.jira_attachments) == 1
    assert fake_state.jira_attachments[0]["filename"] == "sre_investigation_OPS-99.html"
    assert "Probable Root Cause" in fake_state.jira_attachments[0]["content"]


def test_e2e_duplicate_webhook_deduplication() -> None:
    client = TestClient(app)

    snow_payload = {
        "sys_id": "dupe-112233",
        "number": "INC0030003",
        "short_description": "Dupe incident",
        "description": "First time arrival",
        "priority": "2",
        "opened_at": "2026-10-09 11:30:00",
    }
    headers = {"Authorization": "Bearer secret-snow-webhook-token"}

    # First webhook -> 202 Accepted
    resp1 = client.post("/webhook/servicenow", json=snow_payload, headers=headers)
    assert resp1.status_code == 202
    assert resp1.json()["status"] == "accepted"
    task_id1 = resp1.json()["task_id"]

    # Duplicate webhook replay -> 200 OK duplicate_ignored
    resp2 = client.post("/webhook/servicenow", json=snow_payload, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate_ignored"
    assert resp2.json()["task_id"] == task_id1

    # Verify ack was posted only once in fake ticket server
    inc_record = fake_state.servicenow_incidents.get("dupe-112233")
    assert inc_record is not None
    ack_notes = [n for n in inc_record["work_notes"] if "Gemini Cloud Assist investigation" in n]
    assert len(ack_notes) == 1


def test_e2e_investigation_failure_callback() -> None:
    client = TestClient(app)

    auth_token = create_mock_oidc_token(
        audience="https://bridge.example.com/callback/report",
        email="agent-sa@test-project.iam.gserviceaccount.com",
    )

    callback_payload = {
        "investigation_id": "inv-fail-123",
        "incident_id": "snow-fail-999",
        "incident_source": "servicenow",
        "incident_external_id": "fail-999",
        "incident_external_key": "INC0099999",
        "status": "failed",
        "error": "Investigation timeout after 600s: specialist deadlocked",
    }

    callback_resp = client.post(
        "/callback/report",
        json=callback_payload,
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert callback_resp.status_code == 200

    # Verify failure work note posted to fake ticket server
    inc_record = fake_state.servicenow_incidents.get("fail-999")
    assert inc_record is not None
    assert any("Investigation could not be completed" in n for n in inc_record["work_notes"])


def test_e2e_callback_url_auto_derivation() -> None:
    """When callback_url is empty, verify URL is auto-derived from request.base_url."""
    client = TestClient(app)

    # Configure bridge with empty callback_url
    no_callback_config = BridgeConfig(
        app_env="local",
        gcp_project="test-project",
        gcp_region="us-central1",
        tasks_queue="investigations",
        tasks_location="us-central1",
        tasks_invoker_sa="tasks-invoker@test-project.iam.gserviceaccount.com",
        agent_service_url="https://sre-agent.example.com",
        agent_sa="agent-sa@test-project.iam.gserviceaccount.com",
        callback_url="",  # Empty to trigger auto-derivation
        servicenow_instance_url="http://fake-ticket",
        servicenow_username="svc_sre_agent",
        servicenow_password="snow_password",
        servicenow_webhook_token="secret-snow-webhook-token",
        servicenow_in_progress_state="2",
    )
    app.dependency_overrides[get_settings] = lambda: no_callback_config

    dispatched_tasks: list[dict[str, Any]] = []

    class MockDispatcher(TaskDispatcher):
        def dispatch(self, incident: Any, callback_url: Any = None) -> Any:
            res = super().dispatch(incident, callback_url=callback_url)
            dispatched_tasks.append(res.payload)
            return res

    mock_dispatcher = MockDispatcher(config=no_callback_config)
    set_task_dispatcher(mock_dispatcher)

    snow_payload = {
        "sys_id": "cb-derive-1234",
        "number": "INC0098765",
        "short_description": "Auto callback url derivation test",
        "opened_at": "2026-10-09 11:00:00",
    }
    headers = {"Authorization": "Bearer secret-snow-webhook-token"}
    resp = client.post("/webhook/servicenow", json=snow_payload, headers=headers)
    assert resp.status_code == 202

    assert len(dispatched_tasks) == 1
    assert dispatched_tasks[0]["callback_url"] == "http://testserver/callback/report"


def test_e2e_servicenow_full_report_capacity_and_truncation_guardrail() -> None:
    """Verifies ServiceNow E2E lifecycle with full report and 12,000 guardrail."""
    client = TestClient(app)

    # 1. Inbound ServiceNow Webhook for full report test
    snow_payload = {
        "sys_id": "e2e-snow-full-1",
        "number": "INC0088001",
        "short_description": "Full capacity report test",
        "description": "Production outage requiring detailed analysis",
        "priority": "1",
        "cmdb_ci": "shop-api",
        "opened_at": "2026-10-09 12:00:00",
    }
    headers = {"Authorization": "Bearer secret-snow-webhook-token"}
    resp = client.post("/webhook/servicenow", json=snow_payload, headers=headers)
    assert resp.status_code == 202

    # Verify initial ticket acknowledgement
    inc_record = fake_state.servicenow_incidents["e2e-snow-full-1"]
    assert len(inc_record["work_notes"]) == 1
    assert "Gemini Cloud Assist investigation" in inc_record["work_notes"][0]

    # 2. Agent Callback with Full Report (~6,000-8,000 characters)
    full_report = Report(
        summary=(
            "Service shop-api experiencing elevated HTTP 503 error rates affecting checkout "
            "and payment processing flows across all production regions. Root cause identified "
            "as connection pool exhaustion triggered by missing DB_PASSWORD secret reference "
            "in revision shop-api-v2 deployment manifest. Impact is isolated to write operations "
            "while read traffic remains partially served."
        ),
        impact=(
            "Checkout flow failing for approximately 42% of customer requests in us-central1 "
            "and europe-west1. Estimated 1,200 transactions dropped over 18 minutes. "
            "Error rate elevated from baseline 0.02% to 41.8%."
        ),
        probable_root_cause=ProbableRootCause(
            text=(
                "Revision shop-api-v2 omitted the DB_PASSWORD environment variable secret "
                "reference in the Cloud Run service spec. Database connection pool initialization "
                "raised KeyError upon pod startup, preventing worker processes from handling "
                "incoming requests. The revision deployment passed initial syntactical validation "
                "but failed health checks immediately upon receiving live production traffic."
            ),
            confidence=0.96,
        ),
        caveats=[
            (
                "Telemetry collection during the initial 4 minutes of the incident window was "
                "degraded in ap-southeast-1 due to Cloud Logging ingestion latency."
            ),
            (
                "Database server-side connection logs could not be retrieved directly; "
                "analysis relies on client-side connection timeout stack traces."
            ),
            (
                "Provisional rollback to revision shop-api-v1 mitigated active errors; "
                "post-mitigation stabilization verification is ongoing."
            ),
        ],
        recommended_actions=[
            (
                "Roll back immediately to known stable revision shop-api-v1 via "
                "Cloud Run traffic migration."
            ),
            (
                "Verify the DB_PASSWORD secret reference exists and is properly populated "
                "in Google Cloud Secret Manager."
            ),
            (
                "Add pre-deployment configuration validator to CI/CD pipeline to ensure "
                "all required database credentials are bound before deployment."
            ),
            (
                "Review automated canary release criteria to catch container startup errors "
                "prior to routing 100% of production traffic."
            ),
            (
                "Update SRE operational runbook for shop-api with specific database "
                "connection pool failure recovery procedures."
            ),
        ],
        timeline=[
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 0, 0, tzinfo=timezone.utc),
                description=(
                    f"Timeline highlight {i}: diagnostic telemetry validated for shop-api."
                ),
            )
            for i in range(1, 11)
        ],
    )

    auth_token = create_mock_oidc_token(
        audience="https://bridge.example.com/callback/report",
        email="agent-sa@test-project.iam.gserviceaccount.com",
    )

    callback_payload = {
        "investigation_id": "inv-full-capacity-1",
        "incident_id": "snow-e2e-snow-full-1",
        "incident_source": "servicenow",
        "incident_external_id": "e2e-snow-full-1",
        "incident_external_key": "INC0088001",
        "status": "completed",
        "report": full_report.model_dump(mode="json"),
    }

    callback_resp = client.post(
        "/callback/report",
        json=callback_payload,
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert callback_resp.status_code == 200

    # Verify report note posted without truncation
    assert len(inc_record["work_notes"]) == 2
    report_note = inc_record["work_notes"][1]
    assert report_note.startswith("[code]")
    assert report_note.endswith("[/code]")
    assert 6000 <= len(report_note) <= 8000
    assert len(report_note) < 12000
    assert "Probable Root Cause (Confidence: 96%)" in report_note
    assert "Update SRE operational runbook for shop-api" in report_note
    assert "Full Investigation Report Attached" in report_note

    # Verify HTML attachment created
    assert len(fake_state.servicenow_attachments) == 1
    att = fake_state.servicenow_attachments[0]
    assert att["table_sys_id"] == "e2e-snow-full-1"
    assert "sre_investigation_report_INC0088001.html" in att["file_name"]

    # 3. Oversized report callback exceeding 12,000 characters
    snow_payload_trunc = {
        "sys_id": "e2e-snow-trunc-1",
        "number": "INC0088002",
        "short_description": "Oversized report test",
        "opened_at": "2026-10-09 12:30:00",
    }
    client.post("/webhook/servicenow", json=snow_payload_trunc, headers=headers)

    callback_payload_trunc = {
        "investigation_id": "inv-trunc-capacity-1",
        "incident_id": "snow-e2e-snow-trunc-1",
        "incident_source": "servicenow",
        "incident_external_id": "e2e-snow-trunc-1",
        "incident_external_key": "INC0088002",
        "status": "completed",
        "report": Report(summary="Q" * 15000, impact="W" * 500).model_dump(mode="json"),
    }
    cb_trunc_resp = client.post(
        "/callback/report",
        json=callback_payload_trunc,
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert cb_trunc_resp.status_code == 200

    inc_trunc_record = fake_state.servicenow_incidents["e2e-snow-trunc-1"]
    assert len(inc_trunc_record["work_notes"]) == 2
    trunc_note = inc_trunc_record["work_notes"][1]
    assert len(trunc_note) == 12000
    assert trunc_note.startswith("[code]")
    assert trunc_note.endswith("</div>[/code]")
