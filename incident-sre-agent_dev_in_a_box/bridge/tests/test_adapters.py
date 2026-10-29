import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from bridge.app.adapters import get_adapter
from bridge.app.adapters.base import IncidentRef, parse_utc_datetime
from bridge.app.adapters.generic import GenericAdapter
from bridge.app.adapters.jira import JiraAdapter
from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig
from bridge.app.contracts.findings import EvidenceItem
from bridge.app.contracts.report import ProbableRootCause, Report, TimelineItem
from bridge.app.templates.renderer import chunk_servicenow_work_note, render_servicenow_html
from bridge.tests.fake_ticket_server import app as fake_server_app
from bridge.tests.fake_ticket_server import state as fake_state
from fastapi import Request
from fastapi.testclient import TestClient


def _make_dummy_request(
    headers: dict[str, str],
    body: bytes = b"",
    method: str = "POST",
    path: str = "/webhook",
) -> Request:
    header_list = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": header_list,
    }

    async def receive() -> dict[str, bytes | str]:
        return {"type": "http.request", "body": body}

    return Request(scope, receive=receive)


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


# -----------------------------------------------------------------------------
# ServiceNow Adapter Tests
# -----------------------------------------------------------------------------


def test_servicenow_adapter_auth() -> None:
    config = BridgeConfig(servicenow_webhook_token="secret-snow-token")
    adapter = ServiceNowAdapter(config=config)

    # Valid Bearer token
    req_valid = _make_dummy_request({"Authorization": "Bearer secret-snow-token"})
    assert adapter.verify(req_valid, b"") is True

    # Invalid token
    req_invalid = _make_dummy_request({"Authorization": "Bearer wrong-token"})
    assert adapter.verify(req_invalid, b"") is False

    # Missing header
    req_missing = _make_dummy_request({})
    assert adapter.verify(req_missing, b"") is False


def test_servicenow_adapter_normalization() -> None:
    config = BridgeConfig(servicenow_instance_url="https://instance.service-now.com")
    adapter = ServiceNowAdapter(config=config)

    payload = {
        "sys_id": "abc123def456",
        "number": "INC0010001",
        "short_description": "shop-api 503 errors",
        "description": "Checkout endpoint throwing 503 errors",
        "priority": "1",
        "cmdb_ci": "shop-api",
        "caller_id": "Ops Lead",
        "category": "Cloud Service",
        "urgency": "1",
        "impact": "1",
        "opened_at": "2026-10-09 10:15:00",
    }

    incident = adapter.to_incident(payload)

    assert incident.id == "snow-abc123def456"
    assert incident.source == "servicenow"
    assert incident.external_id == "abc123def456"
    assert incident.external_key == "INC0010001"
    assert incident.severity == "P1"
    assert incident.service_hint == "shop-api"
    assert incident.reporter == "Ops Lead"
    assert incident.opened_at.tzinfo == timezone.utc
    assert incident.opened_at.year == 2026
    assert (
        incident.external_url
        == "https://instance.service-now.com/nav_to.do?uri=incident.do?sys_id=abc123def456"
    )


def test_servicenow_adapter_ack_and_report_posting() -> None:
    fake_state.reset()
    client = TestClient(fake_server_app, base_url="http://fake-ticket")

    config = BridgeConfig(
        servicenow_instance_url="http://fake-ticket",
        servicenow_username="svc_sre_agent",
        servicenow_password="dummy_password",
        servicenow_in_progress_state="2",
    )
    adapter = ServiceNowAdapter(config=config, http_client=client)

    ref = IncidentRef(
        id="snow-12345",
        source="servicenow",
        external_id="12345",
        external_key="INC0012345",
    )

    # 1. Post Ack
    adapter.post_ack(ref)
    inc_record = fake_state.servicenow_incidents.get("12345")
    assert inc_record is not None
    assert inc_record["state"] == "2"
    assert any(
        "Gemini Cloud Assist investigation" in note
        and "Investigation started" in note
        and "[code]" in note
        for note in inc_record["work_notes"]
    )

    # 2. Post Report (strictly ONE work note, no chunking)
    report = _sample_report()
    adapter.post_report(ref, report)

    # Check work notes contain exactly 2 notes (1 ack + 1 consolidated report note)
    notes = inc_record["work_notes"]
    assert len(notes) == 2
    report_note = notes[1]
    assert report_note.startswith("[code]")
    assert report_note.endswith("[/code]")
    assert "Probable Root Cause" in report_note
    assert "Full Investigation Report Attached" in report_note
    assert len(report_note) < 12000

    # Check attachment was created
    assert len(fake_state.servicenow_attachments) == 1
    att = fake_state.servicenow_attachments[0]
    assert att["table_sys_id"] == "12345"
    assert "sre_investigation_report_INC0012345.html" in att["file_name"]
    assert "Probable Root Cause" in att["content"]

    # 3. Post Failure
    adapter.post_failure(ref, "Timeout during metrics collection")
    assert any("Investigation could not be completed" in note for note in inc_record["work_notes"])


def test_servicenow_report_escaping() -> None:
    # Ensure XSS injection in summary or impact is escaped strictly
    malicious_report = Report(
        summary="Injection <script>alert('xss')</script>",
        impact="<b>High impact</b><img src=x onerror=alert(1)>",
    )
    html = render_servicenow_html(malicious_report)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html
    assert "<b>" not in html
    assert "&lt;b&gt;" in html


def test_servicenow_single_work_note_no_chunking() -> None:
    """Ensure ServiceNow work note is wrapped as a single note without multi-part chunking."""
    # 1. Typical full report (~7,000 chars) is wrapped completely without truncation
    long_html = "<p>" + ("A" * 7000) + "</p>"
    chunks = chunk_servicenow_work_note(long_html)
    assert len(chunks) == 1
    assert chunks[0].startswith("[code]")
    assert chunks[0].endswith("[/code]")
    assert len(chunks[0]) == len(long_html) + 13
    assert len(chunks[0]) <= 12000
    assert "Part 1/" not in chunks[0]

    # 2. Oversized report triggers truncation guardrail at 12,000 chars with clean closure
    oversized_html = "<p>" + ("A" * 15000) + "</p>"
    trunc_chunks = chunk_servicenow_work_note(oversized_html)
    assert len(trunc_chunks) == 1
    assert len(trunc_chunks[0]) == 12000
    assert trunc_chunks[0].startswith("[code]")
    assert trunc_chunks[0].endswith("</div>[/code]")


def test_servicenow_adapter_full_report_capacity_and_truncation() -> None:
    """Validates ServiceNow adapter posts full report without truncation and clamps at 12,000."""
    fake_state.reset()
    client = TestClient(fake_server_app, base_url="http://fake-ticket")

    config = BridgeConfig(
        servicenow_instance_url="http://fake-ticket",
        servicenow_username="svc_sre_agent",
        servicenow_password="dummy_password",
        servicenow_chunk_size=12000,
    )
    adapter = ServiceNowAdapter(config=config, http_client=client)

    ref = IncidentRef(
        id="snow-98765",
        source="servicenow",
        external_id="98765",
        external_key="INC0098765",
    )

    # 1. Full report (~6,000-8,000 characters)
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
                    f"Timeline event {i}: diagnostic step verified service state and metrics."
                ),
            )
            for i in range(1, 11)
        ],
    )

    adapter.post_report(ref, full_report)
    inc_record = fake_state.servicenow_incidents["98765"]
    assert len(inc_record["work_notes"]) == 1
    posted_note = inc_record["work_notes"][0]
    assert posted_note.startswith("[code]")
    assert posted_note.endswith("[/code]")
    assert 6000 <= len(posted_note) <= 8000
    assert len(posted_note) < 12000
    assert "Probable Root Cause (Confidence: 96%)" in posted_note
    assert "Full Investigation Report Attached" in posted_note

    # 2. Oversized report exceeding 12,000 characters
    ref_trunc = IncidentRef(
        id="snow-trunc-987",
        source="servicenow",
        external_id="trunc-987",
        external_key="INC0098766",
    )
    oversized_report = Report(summary="Z" * 15000, impact="Outage")
    adapter.post_report(ref_trunc, oversized_report)

    inc_trunc = fake_state.servicenow_incidents["trunc-987"]
    assert len(inc_trunc["work_notes"]) == 1
    trunc_note = inc_trunc["work_notes"][0]
    assert len(trunc_note) == 12000
    assert trunc_note.startswith("[code]")
    assert trunc_note.endswith("</div>[/code]")


# -----------------------------------------------------------------------------
# Jira Adapter Tests
# -----------------------------------------------------------------------------


def test_jira_adapter_auth() -> None:
    config = BridgeConfig(jira_webhook_token="secret-jira-token")
    adapter = JiraAdapter(config=config)

    # Valid token via X-Webhook-Token
    req_valid = _make_dummy_request({"X-Webhook-Token": "secret-jira-token"})
    assert adapter.verify(req_valid, b"") is True

    # Valid token via Authorization Bearer
    req_bearer = _make_dummy_request({"Authorization": "Bearer secret-jira-token"})
    assert adapter.verify(req_bearer, b"") is True

    # Invalid token
    req_invalid = _make_dummy_request({"X-Webhook-Token": "wrong-token"})
    assert adapter.verify(req_invalid, b"") is False


def test_jira_adapter_normalization() -> None:
    config = BridgeConfig(jira_base_url="https://jira.example.com")
    adapter = JiraAdapter(config=config)

    payload = {
        "issue": {
            "id": "10050",
            "key": "OPS-42",
            "fields": {
                "summary": "shop-api latency spike",
                "description": "Latency jumped to 5 seconds",
                "priority": {"name": "Highest"},
                "components": [{"name": "shop-api"}],
                "labels": ["prod", "latency"],
                "created": "2026-10-09T10:30:00.000+0000",
                "reporter": {"displayName": "Alice Engineer"},
            },
        }
    }

    incident = adapter.to_incident(payload)

    assert incident.id == "jira-OPS-42"
    assert incident.source == "jira"
    assert incident.external_id == "10050"
    assert incident.external_key == "OPS-42"
    assert incident.severity == "P1"
    assert incident.service_hint == "shop-api"
    assert incident.reporter == "Alice Engineer"
    assert incident.opened_at.tzinfo == timezone.utc
    assert incident.external_url == "https://jira.example.com/browse/OPS-42"


def test_jira_adapter_ack_and_report_posting() -> None:
    fake_state.reset()
    client = TestClient(fake_server_app, base_url="http://fake-ticket")

    config = BridgeConfig(
        jira_base_url="http://fake-ticket",
        jira_email="sre-agent@example.com",
        jira_api_token="dummy_token",
        jira_in_progress_transition="In Progress",
    )
    adapter = JiraAdapter(config=config, http_client=client)

    ref = IncidentRef(
        id="jira-OPS-42",
        source="jira",
        external_id="10050",
        external_key="OPS-42",
    )

    # 1. Post Ack
    adapter.post_ack(ref)
    assert len(fake_state.jira_comments) == 1
    assert any("Investigation started" in json.dumps(c) for c in fake_state.jira_comments)
    assert len(fake_state.jira_transitions_history) == 1
    assert fake_state.jira_transitions_history[0]["transition_id"] == "21"

    # 2. Post Report
    report = _sample_report()
    adapter.post_report(ref, report)

    # Check ADF comment
    assert len(fake_state.jira_comments) == 2
    adf_comment = fake_state.jira_comments[1]["body"]
    assert adf_comment["type"] == "doc"
    assert "Probable Root Cause" in json.dumps(adf_comment)

    # Check Attachment
    assert len(fake_state.jira_attachments) == 1
    assert fake_state.jira_attachments[0]["filename"] == "sre_investigation_OPS-42.html"
    assert "Probable Root Cause" in fake_state.jira_attachments[0]["content"]

    # 3. Post Failure
    adapter.post_failure(ref, "Missing Cloud Monitoring permissions")
    assert len(fake_state.jira_comments) == 3
    assert "Missing Cloud Monitoring permissions" in json.dumps(fake_state.jira_comments[2])


# -----------------------------------------------------------------------------
# Generic Adapter Tests
# -----------------------------------------------------------------------------


def test_generic_adapter_auth_and_flow() -> None:
    secret_key = "test-hmac-secret-key"
    config = BridgeConfig(generic_hmac_key=secret_key)
    adapter = GenericAdapter(config=config)

    body = b'{"id": "GEN-99", "title": "Test outage", "severity": "P2"}'
    valid_sig = hmac.new(secret_key.encode(), body, hashlib.sha256).hexdigest()

    # Valid signature
    req_valid = _make_dummy_request({"X-Hub-Signature-256": f"sha256={valid_sig}"}, body=body)
    assert adapter.verify(req_valid, body) is True

    # Invalid signature
    req_invalid = _make_dummy_request({"X-Hub-Signature-256": "sha256=invalid"}, body=body)
    assert adapter.verify(req_invalid, body) is False

    # Normalization
    incident = adapter.to_incident(json.loads(body.decode()))
    assert incident.source == "generic"
    assert incident.external_key == "GEN-99"
    assert incident.severity == "P2"
    assert incident.opened_at.tzinfo == timezone.utc

    # Delivery tracking
    ref = IncidentRef.from_incident(incident)
    adapter.post_ack(ref)
    assert len(adapter.state.acks) == 1

    report = _sample_report()
    adapter.post_report(ref, report)
    assert len(adapter.state.reports) == 1

    adapter.post_failure(ref, "Generic error")
    assert len(adapter.state.failures) == 1


def test_get_adapter_factory() -> None:
    config = BridgeConfig()
    assert isinstance(get_adapter("servicenow", config), ServiceNowAdapter)
    assert isinstance(get_adapter("jira", config), JiraAdapter)
    assert isinstance(get_adapter("generic", config), GenericAdapter)

    with pytest.raises(ValueError, match="Unsupported adapter source"):
        get_adapter("pagerduty", config)


def test_parse_utc_datetime_variations() -> None:
    # None or empty
    now_utc = parse_utc_datetime(None)
    assert now_utc.tzinfo == timezone.utc

    # Naive datetime
    naive = datetime(2026, 10, 9, 12, 0, 0)
    aware = parse_utc_datetime(naive)
    assert aware.tzinfo == timezone.utc

    # ISO with Z
    iso_z = parse_utc_datetime("2026-10-09T14:30:00Z")
    assert iso_z.tzinfo == timezone.utc
    assert iso_z.hour == 14

    # Jira timezone format with +0000
    jira_ts = parse_utc_datetime("2026-10-09T14:30:00.000+0000")
    assert jira_ts.tzinfo == timezone.utc

    # ServiceNow format YYYY-MM-DD HH:MM:SS
    snow_ts = parse_utc_datetime("2026-10-09 08:15:30")
    assert snow_ts.tzinfo == timezone.utc
    assert snow_ts.hour == 8


def test_health_endpoint() -> None:
    from bridge.app.main import app

    client = TestClient(app)

    # Verify GET /health
    res_health = client.get("/health")
    assert res_health.status_code == 200
    data_health = res_health.json()
    assert data_health["status"] == "healthy"
    assert data_health["service"] == "incident-bridge"
