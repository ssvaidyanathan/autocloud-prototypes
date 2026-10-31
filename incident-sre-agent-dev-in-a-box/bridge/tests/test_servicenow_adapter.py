"""Unit tests for ServiceNow adapter and work note rendering."""

from datetime import datetime, timedelta, timezone

import pytest
from bridge.app.adapters.servicenow import render_ack_note, render_work_note
from bridge.app.contracts.findings import EvidenceItem
from bridge.app.contracts.report import (
    ProbableRootCause,
    Report,
    TimelineItem,
)


def _sample_report_for_work_note() -> Report:
    """Build representative Report instance with timeline, root cause, and telemetry."""
    return Report(
        summary=(
            "Service shop-api experiencing HTTP 503 error spike due to missing DB password secret."
        ),
        impact="Checkout flow failing for 40% of users across all regions.",
        timeline=[
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 0, 0, tzinfo=timezone.utc),
                description="Revision shop-api-v2 deployed to production.",
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 2, 0, tzinfo=timezone.utc),
                description="Elevated 503 error rate alert triggered.",
            ),
        ],
        evidence_by_area={
            "logs": [
                EvidenceItem(
                    kind="error_log",
                    excerpt=(
                        "KeyError: 'DB_PASSWORD' during database connection pool initialization"
                    ),
                    timestamp=datetime(2026, 10, 9, 10, 2, 30, tzinfo=timezone.utc),
                    console_url="https://console.cloud.google.com/logs/query",
                )
            ]
        },
        probable_root_cause=ProbableRootCause(
            text=(
                "Revision shop-api-v2 omitted the DB_PASSWORD "
                "environment variable secret reference."
            ),
            confidence=0.95,
        ),
        recommended_actions=[
            "Roll back to revision shop-api-v1 immediately.",
            "Add DB_PASSWORD secret reference to deployment configuration before redeployment.",
        ],
    )


def test_render_work_note_success() -> None:
    """Validates that render_work_note produces executive summary < 12,000 chars."""
    report = _sample_report_for_work_note()
    html = render_work_note(report)

    assert isinstance(html, str)
    assert len(html) > 0
    assert len(html) < 12000

    # 1. Report summary and impact
    assert "<strong>Summary:</strong> Service shop-api experiencing HTTP 503 error spike" in html
    assert "<strong>Impact:</strong> Checkout flow failing for 40% of users" in html

    # 2. Probable root cause panel with confidence
    assert "Probable Root Cause" in html
    assert "Confidence: 95%" in html
    assert (
        "Revision shop-api-v2 omitted the DB_PASSWORD environment variable secret reference."
        in html
    )

    # 3. Incident timeline highlights table and entries
    assert "Incident Timeline Highlights" in html
    assert "<table" in html
    assert "Revision shop-api-v2 deployed to production." in html
    assert "Elevated 503 error rate alert triggered." in html
    assert "2026-10-09T10:00:00+00:00" in html
    assert "2026-10-09T10:02:00+00:00" in html

    # 4. Recommended actions
    assert "Recommended Actions" in html
    assert "Roll back to revision shop-api-v1 immediately." in html

    # 5. Explicit notice pointing to attached standalone HTML file for full traces/raw evidence
    assert "Full Investigation Report Attached" in html
    assert "attached standalone HTML report" in html


def test_render_work_note_and_standalone_with_caveats() -> None:
    """Validates that caveats are prominently rendered in ServiceNow and standalone reports."""
    from bridge.app.templates.renderer import render_standalone_html

    report = _sample_report_for_work_note()
    report.caveats = [
        (
            "Mitigation via revision shop-api-v3 is provisional: "
            "no post-deployment telemetry is available."
        ),
        "Zero error samples recorded during metrics window.",
    ]

    # ServiceNow work note
    snow_html = render_work_note(report)
    assert "Investigation Caveats &amp; Telemetry Limitations" in snow_html
    assert "Mitigation via revision shop-api-v3 is provisional" in snow_html
    assert "Zero error samples recorded during metrics window." in snow_html
    assert len(snow_html) < 12000

    # Standalone HTML report
    standalone_html = render_standalone_html(report)
    assert "Investigation Caveats &amp; Telemetry Limitations" in standalone_html
    assert "caveats-box" in standalone_html
    assert "Mitigation via revision shop-api-v3 is provisional" in standalone_html
    assert "Zero error samples recorded during metrics window." in standalone_html


def test_render_ack_note() -> None:
    """Validates that render_ack_note produces standardized HTML with [code] tags."""
    ack_note = render_ack_note()

    assert ack_note.startswith("[code]")
    assert ack_note.endswith("[/code]")

    # Verify required content and hierarchy
    assert "<h1" in ack_note and "Gemini Cloud Assist investigation</h1>" in ack_note
    assert "<h2" in ack_note and "Investigation started</h2>" in ack_note
    assert "<em>Automated analysis is running now.</em>" in ack_note
    assert "<h3" in ack_note and "What happens next</h3>" in ack_note

    # Verify bullet items
    assert "Gemini Cloud Assist is analysing this incident against the project" in ack_note
    assert (
        "The findings are posted to this ticket automatically, as a second work note." in ack_note
    )
    assert (
        "This usually takes 3 to 6 minutes. No action is needed from you in the meantime."
        in ack_note
    )


def test_servicenow_to_incident_realtime_window_anchoring() -> None:
    """Validates naive local timezone detection and real-time anchoring for ServiceNow."""
    from bridge.app.adapters.servicenow import ServiceNowAdapter
    from bridge.app.config import BridgeConfig

    adapter = ServiceNowAdapter(config=BridgeConfig(servicenow_instance_url="http://fake-ticket"))

    # Case 1: opened_at > 60m in the past (e.g. 5 hours ago without timezone offset)
    old_time = datetime.now(timezone.utc) - timedelta(hours=5)
    payload_old = {
        "sys_id": "snow-time-1",
        "number": "INC001122",
        "short_description": "Time offset test",
        "opened_at": old_time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    now_before = datetime.now(timezone.utc)
    inc_old = adapter.to_incident(payload_old)
    now_after = datetime.now(timezone.utc)

    # Must be anchored to now_utc
    assert now_before <= inc_old.opened_at <= now_after

    # Case 2: opened_at is recent (e.g. 10m ago)
    recent_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    payload_recent = {
        "sys_id": "snow-time-2",
        "number": "INC001123",
        "short_description": "Recent time test",
        "opened_at": recent_time.isoformat(),
    }
    inc_recent = adapter.to_incident(payload_recent)
    assert abs((inc_recent.opened_at - recent_time).total_seconds()) < 2


def _full_report_for_work_note() -> Report:
    """Build representative Report instance (~6,000-8,000 chars) with detailed sections."""
    return Report(
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
                    "Cloud Build triggered for commit sha 4f98a12 containing revision shop-api-v2."
                ),
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 2, 15, tzinfo=timezone.utc),
                description=(
                    "Revision shop-api-v2 deployment completed and 100% traffic shifted "
                    "to new revision."
                ),
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 2, 45, tzinfo=timezone.utc),
                description=(
                    "First KeyError: DB_PASSWORD recorded in Cloud Logging from worker "
                    "container startup."
                ),
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 3, 30, tzinfo=timezone.utc),
                description=(
                    "Cloud Monitoring alert INC-8821 triggered for HTTP 503 error rate "
                    "exceeding 5% threshold."
                ),
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 4, 10, tzinfo=timezone.utc),
                description=(
                    "ServiceNow incident INC0012345 created automatically via webhook integration."
                ),
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 5, 0, tzinfo=timezone.utc),
                description=(
                    "Gemini Cloud Assist SRE Agent dispatched investigation task via Cloud Tasks."
                ),
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 6, 20, tzinfo=timezone.utc),
                description=(
                    "Log Specialist identified repeated container crash loop and KeyError "
                    "stack traces in service shop-api."
                ),
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 7, 50, tzinfo=timezone.utc),
                description=(
                    "Metric Specialist confirmed 503 spike correlated exactly with "
                    "revision shop-api-v2 activation timestamp."
                ),
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 9, 10, tzinfo=timezone.utc),
                description=(
                    "Automated triage synthesized probable root cause with 96% confidence "
                    "and drafted remediation actions."
                ),
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 9, 10, 10, 0, tzinfo=timezone.utc),
                description=(
                    "Investigation findings compiled and posted to ServiceNow incident "
                    "with full attachment."
                ),
            ),
        ],
    )


def test_render_full_report_capacity_without_truncation() -> None:
    """Validates full report (~6,000-8,000 chars) renders completely without truncation."""
    report = _full_report_for_work_note()
    html = render_work_note(report)

    assert 6000 <= len(html) <= 8000
    assert len(html) < 12000

    # Verify all sections are intact and rendered
    assert "Gemini Cloud Assist investigation" in html
    assert "Investigation findings" in html
    assert "Probable Root Cause (Confidence: 96%)" in html
    assert "Investigation Caveats &amp; Telemetry Limitations" in html
    assert "Recommended Actions" in html
    assert "Incident Timeline Highlights" in html
    assert "Full Investigation Report Attached" in html

    # Verify all 5 recommended actions are rendered
    assert "Roll back immediately to known stable revision shop-api-v1" in html
    assert "Verify the DB_PASSWORD secret reference exists" in html
    assert "Add pre-deployment configuration validator to CI/CD pipeline" in html
    assert "Review automated canary release criteria" in html
    assert "Update SRE operational runbook for shop-api" in html

    # Verify all 10 timeline entries are rendered without slicing truncation
    assert "Cloud Build triggered for commit sha 4f98a12" in html
    assert "Revision shop-api-v2 deployment completed" in html
    assert "First KeyError: DB_PASSWORD recorded in Cloud Logging" in html
    assert "Cloud Monitoring alert INC-8821 triggered" in html
    assert "ServiceNow incident INC0012345 created automatically" in html
    assert "Gemini Cloud Assist SRE Agent dispatched investigation task" in html
    assert "Log Specialist identified repeated container crash loop" in html
    assert "Metric Specialist confirmed 503 spike correlated" in html
    assert "Automated triage synthesized probable root cause" in html
    assert "Investigation findings compiled and posted to ServiceNow incident" in html

    # Verify ends cleanly with closing div tag without truncation
    assert html.rstrip().endswith("</div>")


def test_servicenow_adapter_full_report_posted_without_truncation() -> None:
    """Validates ServiceNowAdapter posts full report (~6,000-8,000 chars) completely."""
    from bridge.app.adapters.base import IncidentRef
    from bridge.app.adapters.servicenow import ServiceNowAdapter
    from bridge.app.config import BridgeConfig
    from bridge.tests.fake_ticket_server import app as fake_server_app
    from bridge.tests.fake_ticket_server import state as fake_state
    from fastapi.testclient import TestClient

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
        id="snow-full-1",
        source="servicenow",
        external_id="full-1",
        external_key="INC0099001",
    )

    report = _full_report_for_work_note()
    adapter.post_report(ref, report)

    inc_record = fake_state.servicenow_incidents["full-1"]
    assert len(inc_record["work_notes"]) == 1
    work_note = inc_record["work_notes"][0]

    assert work_note.startswith("[code]")
    assert work_note.endswith("[/code]")
    assert 6000 <= len(work_note) <= 8000
    assert len(work_note) < 12000

    # Assert complete content
    assert "Probable Root Cause (Confidence: 96%)" in work_note
    assert "Update SRE operational runbook for shop-api" in work_note
    assert "Investigation findings compiled and posted to ServiceNow incident" in work_note
    assert "Full Investigation Report Attached" in work_note


def test_servicenow_truncation_guardrail_at_12000_chars() -> None:
    """Validates ServiceNowAdapter truncates at 12,000 chars with clean </div>[/code] tag."""
    from bridge.app.adapters.base import IncidentRef
    from bridge.app.adapters.servicenow import ServiceNowAdapter
    from bridge.app.config import BridgeConfig
    from bridge.tests.fake_ticket_server import app as fake_server_app
    from bridge.tests.fake_ticket_server import state as fake_state
    from fastapi.testclient import TestClient

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
        id="snow-trunc-1",
        source="servicenow",
        external_id="trunc-1",
        external_key="INC0099002",
    )

    # Oversized report exceeding 12,000 chars
    oversized_report = Report(
        summary="A" * 15000,
        impact="B" * 1000,
    )
    adapter.post_report(ref, oversized_report)

    inc_record = fake_state.servicenow_incidents["trunc-1"]
    assert len(inc_record["work_notes"]) == 1
    work_note = inc_record["work_notes"][0]

    assert len(work_note) == 12000
    assert work_note.startswith("[code]")
    assert work_note.endswith("</div>[/code]")


def test_servicenow_adapter_service_hint_fallback_inference() -> None:
    """Validates fallback service inference when cmdb_ci is absent."""
    from bridge.app.adapters.servicenow import ServiceNowAdapter

    adapter = ServiceNowAdapter()

    # 1. Fallback to shop-api from short_description when cmdb_ci is missing
    payload1 = {
        "sys_id": "fallback-1",
        "number": "INC0010056",
        "short_description": "shop-api: Scenario 3 (Bad Release) missing PAYMENT_GATEWAY_KEY",
        "description": "Readiness probe failed on /readyz",
        "opened_at": "2026-10-10 02:08:16",
    }
    inc1 = adapter.to_incident(payload1)
    assert inc1.service_hint == "shop-api"

    # 2. Fallback to inventory-svc from description when cmdb_ci is None
    payload2 = {
        "sys_id": "fallback-2",
        "number": "INC0010057",
        "short_description": "Database timeout during reservation",
        "description": "Failures occurring in inventory-svc reservation handler",
        "cmdb_ci": None,
        "opened_at": "2026-10-10 02:08:16",
    }
    inc2 = adapter.to_incident(payload2)
    assert inc2.service_hint == "inventory-svc"

    # 3. Regex matching arbitrary service name ending in -api or -svc
    payload3 = {
        "sys_id": "fallback-3",
        "number": "INC0010058",
        "short_description": "payment-svc 500 error spike",
        "description": "Gateway unreachable",
        "cmdb_ci": "",
        "opened_at": "2026-10-10 02:08:16",
    }
    inc3 = adapter.to_incident(payload3)
    assert inc3.service_hint == "payment-svc"

    # 4. Explicit cmdb_ci takes precedence
    payload4 = {
        "sys_id": "fallback-4",
        "number": "INC0010059",
        "short_description": "shop-api 500 error spike",
        "description": "Checkout failure",
        "cmdb_ci": "custom-ci-service",
        "opened_at": "2026-10-10 02:08:16",
    }
    inc4 = adapter.to_incident(payload4)
    assert inc4.service_hint == "custom-ci-service"


def test_servicenow_to_incident_empty_identifiers_raises_value_error() -> None:
    """Payload with empty sys_id and empty number raises ValueError."""
    from bridge.app.adapters.servicenow import ServiceNowAdapter

    adapter = ServiceNowAdapter()
    err_msg = (
        "Missing required ServiceNow incident identifier: 'sys_id' or 'number' must be non-empty"
    )

    # Empty dict
    with pytest.raises(ValueError, match=err_msg):
        adapter.to_incident({})

    # Empty strings
    with pytest.raises(ValueError, match=err_msg):
        adapter.to_incident({"sys_id": "", "number": ""})

    # Whitespace strings
    with pytest.raises(ValueError, match=err_msg):
        adapter.to_incident({"sys_id": "   ", "number": " \t\n "})

    # Nested under result with empty identifiers
    with pytest.raises(ValueError, match=err_msg):
        adapter.to_incident({"result": {"sys_id": "", "number": ""}})

    with pytest.raises(ValueError, match=err_msg):
        adapter.to_incident({"result": {}})


def test_servicenow_to_incident_valid_identifiers_success() -> None:
    """Payload with valid sys_id or number succeeds."""
    from bridge.app.adapters.servicenow import ServiceNowAdapter

    adapter = ServiceNowAdapter()

    # sys_id only
    inc1 = adapter.to_incident({"sys_id": "sys_abc123"})
    assert inc1.id == "snow-sys_abc123"
    assert inc1.external_id == "sys_abc123"
    assert inc1.external_key == "sys_abc123"

    # number only
    inc2 = adapter.to_incident({"number": "INC0012345"})
    assert inc2.id == "snow-INC0012345"
    assert inc2.external_id == "INC0012345"
    assert inc2.external_key == "INC0012345"

    # both sys_id and number with whitespace that should be stripped
    inc3 = adapter.to_incident({"sys_id": "  sys_999  ", "number": "  INC00999  "})
    assert inc3.id == "snow-sys_999"
    assert inc3.external_id == "sys_999"
    assert inc3.external_key == "INC00999"

    # nested under result
    inc4 = adapter.to_incident({"result": {"sys_id": "sys_res_1", "number": "INC00RES1"}})
    assert inc4.id == "snow-sys_res_1"
    assert inc4.external_id == "sys_res_1"
    assert inc4.external_key == "INC00RES1"
