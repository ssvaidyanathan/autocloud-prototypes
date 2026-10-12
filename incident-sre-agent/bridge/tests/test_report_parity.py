"""Unit tests asserting final findings report section and content parity."""

import json
from datetime import datetime, timezone

from bridge.app.adapters.base import IncidentRef
from bridge.app.adapters.jira import JiraAdapter
from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig
from bridge.app.contracts.report import ProbableRootCause, RecentChangeItem, Report, TimelineItem
from bridge.app.templates.renderer import render_jira_adf, render_servicenow_html
from bridge.tests.fake_ticket_server import app as fake_server_app
from bridge.tests.fake_ticket_server import state as fake_state
from starlette.testclient import TestClient


def _make_full_report() -> Report:
    """Create a fully populated Report fixture containing all 8 sections."""
    return Report(
        summary="Severe degradation in shop-api checkout latency",
        impact="Users experience timeouts; checkout completion rate fell by 35%",
        probable_root_cause=ProbableRootCause(
            text="Database connection pool exhausted due to unclosed sessions in checkout handler",
            confidence=0.94,
        ),
        caveats=[
            (
                "Telemetry collection during initial 4 minutes was degraded in "
                "us-central1 due to Cloud Logging ingestion latency."
            ),
            "Trace sampling rate was restricted to 10% on internal endpoints.",
        ],
        recent_changes=[
            RecentChangeItem(
                timestamp=datetime(2026, 10, 10, 3, 30, 0, tzinfo=timezone.utc),
                description="Cloud Build deploy shop-api-v2 commit 7a8b9c",
                author="deployer@example.com",
            ),
            RecentChangeItem(
                timestamp=datetime(2026, 10, 10, 3, 45, 0, tzinfo=timezone.utc),
                description="Terraform update: increased min-instances to 3",
            ),
        ],
        recommended_actions=[
            (
                "Immediate Cloud Run revision rollback: gcloud run services "
                "update-traffic shop-api --to-revisions shop-api-00041=100"
            ),
            "Increase database connection pool maximum size to 100",
            "Verify unclosed session leak in checkout handler",
        ],
        timeline=[
            TimelineItem(
                timestamp=datetime(2026, 10, 10, 3, 30, 0, tzinfo=timezone.utc),
                description="Cloud Build trigger deployment completed",
            ),
            TimelineItem(
                timestamp=datetime(2026, 10, 10, 3, 40, 0, tzinfo=timezone.utc),
                description="Cloud Monitoring alert INC-9002 latency threshold breached",
            ),
        ],
    )


def test_full_report_parity_all_sections() -> None:
    """Validate that ServiceNow HTML and Jira ADF render all 8 sections with identical content."""
    report = _make_full_report()
    snow_html = render_servicenow_html(report)
    jira_adf = render_jira_adf(report)
    jira_json = json.dumps(jira_adf)

    # 1. Header & Subtitle Parity
    assert "Gemini Cloud Assist investigation" in snow_html
    assert "Gemini Cloud Assist investigation" in jira_json
    assert "Investigation findings" in snow_html
    assert "Investigation findings" in jira_json
    assert "Automated analysis complete." in snow_html
    assert "Automated analysis complete." in jira_json

    # 2. Executive Summary & Impact Parity
    assert f"<strong>Summary:</strong> {report.summary}" in snow_html
    assert "Summary: " in jira_json and report.summary in jira_json
    assert f"<strong>Impact:</strong> {report.impact}" in snow_html
    assert "Impact: " in jira_json and report.impact in jira_json

    # 3. Probable Root Cause Parity (confidence rounding and text)
    conf_title = "Probable Root Cause (Confidence: 94%)"
    assert conf_title in snow_html
    assert conf_title in jira_json
    assert (
        "Database connection pool exhausted due to unclosed sessions in checkout handler"
        in snow_html
    )
    assert (
        "Database connection pool exhausted due to unclosed sessions in checkout handler"
        in jira_json
    )
    # Jira warning panel check
    root_cause_panel = next(
        n
        for n in jira_adf["content"]
        if n.get("type") == "panel" and n.get("attrs", {}).get("panelType") == "warning"
    )
    assert conf_title in json.dumps(root_cause_panel)

    # 4. Investigation Caveats & Telemetry Limitations Parity
    caveats_title = "Investigation Caveats & Telemetry Limitations"
    assert "Investigation Caveats &amp; Telemetry Limitations" in snow_html
    assert caveats_title in jira_json
    for caveat in report.caveats:
        assert caveat in snow_html
        assert caveat in jira_json
    # Jira error panel check
    caveats_panel = next(
        n
        for n in jira_adf["content"]
        if n.get("type") == "panel" and n.get("attrs", {}).get("panelType") == "error"
    )
    assert caveats_title in json.dumps(caveats_panel)

    # 5. Recent Changes Parity
    changes_title = "Recent Changes"
    assert changes_title in snow_html
    assert changes_title in jira_json
    for change in report.recent_changes:
        assert change.description in snow_html
        assert change.description in jira_json
        assert change.timestamp.isoformat() in snow_html
        assert change.timestamp.isoformat() in jira_json

    # 6. Recommended Actions Parity
    actions_title = "Recommended Actions"
    assert actions_title in snow_html
    assert actions_title in jira_json
    for action in report.recommended_actions:
        assert action in snow_html
        assert action in jira_json

    # 7. Incident Timeline Highlights Parity
    timeline_title = "Incident Timeline Highlights"
    assert timeline_title in snow_html
    assert timeline_title in jira_json
    for item in report.timeline:
        assert item.description in snow_html
        assert item.description in jira_json
        assert item.timestamp.isoformat() in snow_html
        assert item.timestamp.isoformat() in jira_json

    # 8. Full Investigation Report Attached Callout Parity
    callout_title = "Full Investigation Report Attached"
    callout_body = (
        "For complete telemetry traces, metric graphs, logs, and raw evidence, "
        "refer to the attached standalone HTML report."
    )
    assert callout_title in snow_html
    assert callout_title in jira_json
    assert callout_body in snow_html
    assert callout_body in jira_json
    # Jira info panel check
    attached_panel = next(
        n
        for n in jira_adf["content"]
        if n.get("type") == "panel" and n.get("attrs", {}).get("panelType") == "info"
    )
    assert callout_title in json.dumps(attached_panel)


def test_report_parity_optional_sections_omitted() -> None:
    """Validate that when optional sections are empty or None, both adapters omit them."""
    minimal_report = Report(
        summary="Minimal incident",
        impact="Low impact",
        probable_root_cause=None,
        caveats=[],
        recent_changes=[],
        recommended_actions=[],
        timeline=[],
    )

    snow_html = render_servicenow_html(minimal_report)
    jira_adf = render_jira_adf(minimal_report)
    jira_json = json.dumps(jira_adf)

    # Required sections present
    assert "Gemini Cloud Assist investigation" in snow_html
    assert "Gemini Cloud Assist investigation" in jira_json
    assert "Investigation findings" in snow_html
    assert "Investigation findings" in jira_json
    assert f"<strong>Summary:</strong> {minimal_report.summary}" in snow_html
    assert "Summary: " in jira_json and "Minimal incident" in jira_json
    assert f"<strong>Impact:</strong> {minimal_report.impact}" in snow_html
    assert "Impact: " in jira_json and "Low impact" in jira_json
    assert "Full Investigation Report Attached" in snow_html
    assert "Full Investigation Report Attached" in jira_json

    # Optional sections omitted from both
    assert "Probable Root Cause" not in snow_html
    assert "Probable Root Cause" not in jira_json

    assert "Investigation Caveats" not in snow_html
    assert "Investigation Caveats" not in jira_json

    assert "Recent Changes" not in snow_html
    assert "Recent Changes" not in jira_json

    assert "Recommended Actions" not in snow_html
    assert "Recommended Actions" not in jira_json

    assert "Incident Timeline Highlights" not in snow_html
    assert "Incident Timeline Highlights" not in jira_json


def test_adapter_post_report_parity() -> None:
    """Validate that JiraAdapter and ServiceNowAdapter post equivalent reports."""
    fake_state.reset()
    client = TestClient(fake_server_app, base_url="http://fake-ticket")
    report = _make_full_report()

    # 1. Jira Adapter
    jira_config = BridgeConfig(
        jira_base_url="http://fake-ticket",
        jira_email="sre@example.com",
        jira_api_token="token",
        jira_in_progress_transition="In Progress",
    )
    jira_adapter = JiraAdapter(config=jira_config, http_client=client)
    jira_ref = IncidentRef(
        id="jira-PAR-9",
        source="jira",
        external_id="901",
        external_key="PAR-9",
    )

    jira_adapter.post_report(jira_ref, report)
    assert len(fake_state.jira_comments) == 1
    assert len(fake_state.jira_attachments) == 1
    assert "sre_investigation_PAR-9.html" in fake_state.jira_attachments[0]["filename"]

    # 2. ServiceNow Adapter
    snow_config = BridgeConfig(
        servicenow_instance_url="http://fake-ticket",
        servicenow_username="sre",
        servicenow_password="pwd",
    )
    snow_adapter = ServiceNowAdapter(config=snow_config, http_client=client)
    snow_ref = IncidentRef(
        id="snow-PAR-10",
        source="servicenow",
        external_id="902",
        external_key="INC00902",
    )

    snow_adapter.post_report(snow_ref, report)
    inc = fake_state.servicenow_incidents["902"]
    assert len(inc["work_notes"]) == 1
    assert len(fake_state.servicenow_attachments) == 1
    assert (
        "sre_investigation_report_INC00902.html"
        in fake_state.servicenow_attachments[0]["file_name"]
    )

    # Cross-adapter parity in posted note contents
    jira_posted_json = json.dumps(fake_state.jira_comments[0]["body"])
    snow_posted_html = inc["work_notes"][0]
    for section_text in [
        "Gemini Cloud Assist investigation",
        "Investigation findings",
        "Probable Root Cause (Confidence: 94%)",
        "Recent Changes",
        "Recommended Actions",
        "Incident Timeline Highlights",
        "Full Investigation Report Attached",
    ]:
        assert section_text in jira_posted_json
        assert section_text in snow_posted_html
