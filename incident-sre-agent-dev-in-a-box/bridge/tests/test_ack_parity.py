"""Unit tests asserting single-sourced acknowledgment and failure note parity."""

import json

from bridge.app.adapters.base import IncidentRef
from bridge.app.adapters.jira import JiraAdapter
from bridge.app.adapters.servicenow import ServiceNowAdapter, render_ack_note, render_failure_note
from bridge.app.config import BridgeConfig
from bridge.app.templates.ack_content import (
    ACK_STATUS_ITALIC,
    ACK_SUBTITLE,
    FAILURE_MESSAGE_TEMPLATE,
    FAILURE_SUBTITLE,
    INVESTIGATION_TITLE,
    WHAT_HAPPENS_NEXT_TITLE,
    get_failure_message,
    get_next_steps,
)
from bridge.app.templates.renderer import (
    render_jira_ack_adf,
    render_jira_failure_adf,
    render_servicenow_ack_html,
    render_servicenow_failure_html,
)
from bridge.tests.fake_ticket_server import app as fake_server_app
from bridge.tests.fake_ticket_server import state as fake_state
from starlette.testclient import TestClient


def test_ack_content_constants_and_helpers() -> None:
    """Validate that shared constants and helper functions produce required text."""
    assert INVESTIGATION_TITLE == "Gemini Cloud Assist investigation"
    assert ACK_SUBTITLE == "Investigation started"
    assert ACK_STATUS_ITALIC == "Automated analysis is running now."
    assert WHAT_HAPPENS_NEXT_TITLE == "What happens next"
    assert FAILURE_SUBTITLE == "Investigation failed"

    snow_steps = get_next_steps("work note")
    assert len(snow_steps) == 3
    assert "Gemini Cloud Assist is analysing this incident" in snow_steps[0]
    assert "as a second work note." in snow_steps[1]
    assert "This usually takes 3 to 6 minutes." in snow_steps[2]

    jira_steps = get_next_steps("comment")
    assert len(jira_steps) == 3
    assert "Gemini Cloud Assist is analysing this incident" in jira_steps[0]
    assert "as a second comment." in jira_steps[1]
    assert "This usually takes 3 to 6 minutes." in jira_steps[2]

    err = "Cloud Monitoring timeout"
    expected_msg = f"Investigation could not be completed. Error: {err}"
    assert get_failure_message(err) == expected_msg
    assert FAILURE_MESSAGE_TEMPLATE.format(error=err) == expected_msg


def test_acknowledgement_text_and_element_parity() -> None:
    """Validate ServiceNow HTML and Jira ADF render identical text elements from shared content."""
    snow_html = render_servicenow_ack_html()
    jira_adf = render_jira_ack_adf()
    jira_json = json.dumps(jira_adf)

    # 1. Title Parity
    assert INVESTIGATION_TITLE in snow_html
    assert INVESTIGATION_TITLE in jira_json

    # 2. Subtitle Parity
    assert ACK_SUBTITLE in snow_html
    assert ACK_SUBTITLE in jira_json

    # 3. Status Italic Parity
    assert ACK_STATUS_ITALIC in snow_html
    assert ACK_STATUS_ITALIC in jira_json

    # 4. Next Steps Title Parity
    assert WHAT_HAPPENS_NEXT_TITLE in snow_html
    assert WHAT_HAPPENS_NEXT_TITLE in jira_json

    # 5. Next Steps Content Parity
    # Step 1: common prefix
    assert "Gemini Cloud Assist is analysing this incident" in snow_html
    assert "Gemini Cloud Assist is analysing this incident" in jira_json

    # Step 2: noun-adapted second entry
    assert "as a second work note." in snow_html
    assert "as a second comment." in jira_json

    # Step 3: timing note
    assert "This usually takes 3 to 6 minutes." in snow_html
    assert "This usually takes 3 to 6 minutes." in jira_json

    # Structural verification of Jira ADF
    assert jira_adf["type"] == "doc"
    assert jira_adf["version"] == 1
    content_types = [node["type"] for node in jira_adf["content"]]
    assert content_types == ["heading", "heading", "paragraph", "heading", "bulletList"]
    assert jira_adf["content"][0]["attrs"]["level"] == 2
    assert jira_adf["content"][1]["attrs"]["level"] == 3
    assert jira_adf["content"][2]["content"][0]["marks"] == [{"type": "em"}]
    assert jira_adf["content"][3]["attrs"]["level"] == 3
    assert len(jira_adf["content"][4]["content"]) == 3


def test_failure_note_text_and_element_parity() -> None:
    """Validate ServiceNow HTML and Jira ADF render identical failure elements."""
    err_text = "Missing Cloud Monitoring permissions"
    snow_html = render_servicenow_failure_html(err_text)
    jira_adf = render_jira_failure_adf(err_text)
    jira_json = json.dumps(jira_adf)

    # 1. Title Parity
    assert INVESTIGATION_TITLE in snow_html
    assert INVESTIGATION_TITLE in jira_json

    # 2. Subtitle Parity
    assert FAILURE_SUBTITLE in snow_html
    assert FAILURE_SUBTITLE in jira_json

    # 3. Formatted error message parity
    expected_msg = get_failure_message(err_text)
    assert expected_msg in snow_html
    assert expected_msg in jira_json

    # 4. Jira ADF panel type check
    assert jira_adf["type"] == "doc"
    panel_node = next(n for n in jira_adf["content"] if n["type"] == "panel")
    assert panel_node["attrs"]["panelType"] == "error"


def test_adapters_post_shared_ack_and_failure_notes() -> None:
    """Validate that JiraAdapter and ServiceNowAdapter post shared ack and failure notes."""
    fake_state.reset()
    client = TestClient(fake_server_app, base_url="http://fake-ticket")

    # 1. Jira Adapter
    jira_config = BridgeConfig(
        jira_base_url="http://fake-ticket",
        jira_email="sre@example.com",
        jira_api_token="token",
        jira_in_progress_transition="In Progress",
    )
    jira_adapter = JiraAdapter(config=jira_config, http_client=client)
    jira_ref = IncidentRef(
        id="jira-PAR-1",
        source="jira",
        external_id="101",
        external_key="PAR-1",
    )

    jira_adapter.post_ack(jira_ref)
    assert len(fake_state.jira_comments) == 1
    posted_jira_ack = fake_state.jira_comments[0]["body"]
    assert posted_jira_ack == render_jira_ack_adf()

    jira_adapter.post_failure(jira_ref, "Simulated Jira failure")
    assert len(fake_state.jira_comments) == 2
    posted_jira_fail = fake_state.jira_comments[1]["body"]
    assert posted_jira_fail == render_jira_failure_adf("Simulated Jira failure")

    # 2. ServiceNow Adapter
    snow_config = BridgeConfig(
        servicenow_instance_url="http://fake-ticket",
        servicenow_username="sre",
        servicenow_password="pwd",
    )
    snow_adapter = ServiceNowAdapter(config=snow_config, http_client=client)
    snow_ref = IncidentRef(
        id="snow-PAR-2",
        source="servicenow",
        external_id="202",
        external_key="INC00202",
    )

    snow_adapter.post_ack(snow_ref)
    inc = fake_state.servicenow_incidents["202"]
    assert inc["work_notes"][-1] == render_ack_note()

    snow_adapter.post_failure(snow_ref, "Simulated SN failure")
    assert inc["work_notes"][-1] == render_failure_note("Simulated SN failure")
