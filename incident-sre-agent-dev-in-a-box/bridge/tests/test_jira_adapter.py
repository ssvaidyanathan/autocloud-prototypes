"""Unit tests for Jira adapter identifier validation."""

from unittest.mock import MagicMock

import pytest
from bridge.app.adapters.jira import JiraAdapter
from bridge.app.config import BridgeConfig

_ERR_MSG = "Missing required Jira incident identifier: 'issue.id' or 'issue.key' must be non-empty"


def test_jira_to_incident_missing_issue_raises_value_error() -> None:
    """Payload with missing issue object raises ValueError."""
    config = BridgeConfig(jira_base_url="https://jira.example.com")
    adapter = JiraAdapter(config=config)

    with pytest.raises(ValueError, match=_ERR_MSG):
        adapter.to_incident({})

    with pytest.raises(ValueError, match=_ERR_MSG):
        adapter.to_incident({"webhookEvent": "jira:issue_updated"})

    with pytest.raises(ValueError, match=_ERR_MSG):
        adapter.to_incident({"issue": None})


def test_jira_to_incident_empty_identifiers_raises_value_error() -> None:
    """Payload with empty id and key raises ValueError."""
    config = BridgeConfig(jira_base_url="https://jira.example.com")
    adapter = JiraAdapter(config=config)

    # Empty strings
    with pytest.raises(ValueError, match=_ERR_MSG):
        adapter.to_incident({"issue": {"id": "", "key": ""}})

    # Whitespace strings
    with pytest.raises(ValueError, match=_ERR_MSG):
        adapter.to_incident({"issue": {"id": "   ", "key": " \t\n "}})

    # Empty issue dict
    with pytest.raises(ValueError, match=_ERR_MSG):
        adapter.to_incident({"issue": {}})


def test_jira_to_incident_valid_identifiers_success() -> None:
    """Payload with valid id or key succeeds in creating Incident contract."""
    config = BridgeConfig(jira_base_url="https://jira.example.com")
    adapter = JiraAdapter(config=config)

    # Only key present
    inc_key_only = adapter.to_incident({"issue": {"key": "OPS-101"}})
    assert inc_key_only.id == "jira-OPS-101"
    assert inc_key_only.external_key == "OPS-101"
    assert inc_key_only.external_id == "OPS-101"
    assert inc_key_only.external_url == "https://jira.example.com/browse/OPS-101"

    # Only id present
    inc_id_only = adapter.to_incident({"issue": {"id": "10020"}})
    assert inc_id_only.id == "jira-10020"
    assert inc_id_only.external_key == "10020"
    assert inc_id_only.external_id == "10020"
    assert inc_id_only.external_url == "https://jira.example.com/browse/10020"

    # Both id and key present with whitespace that should be stripped
    inc_both = adapter.to_incident(
        {
            "issue": {
                "id": " 10050 ",
                "key": " OPS-42 ",
                "fields": {
                    "summary": "Latency spike",
                },
            }
        }
    )
    assert inc_both.id == "jira-OPS-42"
    assert inc_both.external_id == "10050"
    assert inc_both.external_key == "OPS-42"
    assert inc_both.title == "Latency spike"


def test_jira_create_issue_empty_description_adf() -> None:
    """Empty or whitespace-only description converts to empty paragraph without text node in ADF."""
    from unittest.mock import MagicMock

    config = BridgeConfig(
        jira_base_url="https://jira.example.com",
        jira_email="test@example.com",
        jira_api_token="token",
    )
    adapter = JiraAdapter(config=config)

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"key": "OPS-200", "id": "200"}
    mock_client.post.return_value = mock_resp
    adapter._get_client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]
    mock_client.__enter__.return_value = mock_client

    # Test empty string description
    adapter.create_issue(
        project_key="OPS",
        summary="Empty desc test",
        description="",
    )
    call_args = mock_client.post.call_args
    posted_desc = call_args[1]["json"]["fields"]["description"]
    assert posted_desc == {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": []}],
    }

    # Test whitespace-only description
    adapter.create_issue(
        project_key="OPS",
        summary="Whitespace desc test",
        description="   \n \t  ",
    )
    call_args = mock_client.post.call_args
    posted_desc = call_args[1]["json"]["fields"]["description"]
    assert posted_desc == {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": []}],
    }


def test_jira_issue_property_key_normalization() -> None:
    """get_issue_property normalizes jira- prefix before querying Jira."""
    from unittest.mock import MagicMock

    config = BridgeConfig(
        jira_base_url="https://jira.example.com",
        jira_email="test@example.com",
        jira_api_token="token",
    )
    adapter = JiraAdapter(config=config)

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"value": {"callback_url": "https://callback.example.internal"}}
    mock_client.get.return_value = mock_resp
    adapter._get_client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]
    mock_client.__enter__.return_value = mock_client

    # With jira- prefix
    val = adapter.get_issue_property("jira-OPS-101", "hitl_metadata")
    assert val == {"callback_url": "https://callback.example.internal"}
    mock_client.get.assert_called_with(
        "https://jira.example.com/rest/api/3/issue/OPS-101/properties/hitl_metadata"
    )

    # Without prefix
    val = adapter.get_issue_property("OPS-101", "hitl_metadata")
    assert val == {"callback_url": "https://callback.example.internal"}
    mock_client.get.assert_called_with(
        "https://jira.example.com/rest/api/3/issue/OPS-101/properties/hitl_metadata"
    )


def test_markdown_to_adf_full_elements() -> None:
    """Validate markdown_to_adf converts headings, bullet lists, tables, bold, and links."""
    from bridge.app.templates.jira_adf import markdown_to_adf

    md = (
        "# Heading 1\n\n"
        "## Heading 2 with **bold**\n\n"
        "- Bullet item 1\n"
        "- Bullet item with [link](https://example.com)\n\n"
        "| Header 1 | Header 2 |\n"
        "| --- | --- |\n"
        "| Cell 1 | **Bold Cell** |\n\n"
        "Standard paragraph with *italic* text."
    )
    adf = markdown_to_adf(md)
    assert adf["version"] == 1
    assert adf["type"] == "doc"

    content = adf["content"]
    types = [b["type"] for b in content]
    assert types == ["heading", "heading", "bulletList", "table", "paragraph"]

    # Heading 1
    assert content[0]["attrs"]["level"] == 1
    assert content[0]["content"][0]["text"] == "Heading 1"

    # Heading 2 bold
    assert content[1]["attrs"]["level"] == 2
    assert any(c.get("marks") and c["marks"][0]["type"] == "strong" for c in content[1]["content"])

    # Bullet list with link
    bl = content[2]["content"]
    assert len(bl) == 2
    item2_content = bl[1]["content"][0]["content"]
    link_node = next(c for c in item2_content if c.get("marks") and c["marks"][0]["type"] == "link")
    assert link_node["marks"][0]["attrs"]["href"] == "https://example.com"

    # Table
    table_rows = content[3]["content"]
    assert len(table_rows) == 2
    assert table_rows[0]["content"][0]["type"] == "tableHeader"
    assert table_rows[1]["content"][0]["type"] == "tableCell"


def test_jira_update_issue_root_causes() -> None:
    """Validate JiraAdapter.update_issue_root_causes updates existing Root Causes in description."""
    from unittest.mock import MagicMock

    config = BridgeConfig(
        jira_base_url="https://jira.example.com",
        jira_email="test@example.com",
        jira_api_token="token",
    )
    adapter = JiraAdapter(config=config)

    initial_desc = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 1},
                "content": [{"type": "text", "text": "Incident Title"}],
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Root Causes:", "marks": [{"type": "strong"}]}
                ],
            },
            {"type": "paragraph", "content": [{"type": "text", "text": "- todo"}]},
        ],
    }

    mock_client = MagicMock()
    # Mock get_issue
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {
        "key": "OPS-300",
        "fields": {"description": initial_desc},
    }
    mock_client.get.return_value = mock_get_resp

    # Mock update_issue (PUT)
    mock_put_resp = MagicMock()
    mock_put_resp.status_code = 204
    mock_put_resp.text = ""
    mock_client.put.return_value = mock_put_resp

    adapter._get_client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]
    mock_client.__enter__.return_value = mock_client

    updated = adapter.update_issue_root_causes(
        "OPS-300",
        "Memory leak in v1.2.0 cache prewarm buffer caused OOM kill.",
    )
    assert updated is True
    mock_client.put.assert_called_once()
    put_call_args = mock_client.put.call_args
    assert "https://jira.example.com/rest/api/3/issue/OPS-300" in put_call_args[0][0]
    updated_desc = put_call_args[1]["json"]["fields"]["description"]
    rc_node = updated_desc["content"][2]
    assert "Memory leak in v1.2.0" in rc_node["content"][0]["text"]


def test_jira_adapter_to_incident_alert_started_at_from_property() -> None:
    config = BridgeConfig(jira_base_url="https://jira.example.com")
    adapter = JiraAdapter(config=config)
    adapter.get_issue_property = MagicMock(  # type: ignore[method-assign]
        return_value={"alert_started_at": "2026-10-30T20:02:58Z"}
    )
    payload = {
        "issue": {
            "key": "OPS-400",
            "fields": {
                "summary": "Alert violation on snow-ip",
                "description": "Some description",
                "created": "2026-10-30T20:21:59Z",
            },
        }
    }
    incident = adapter.to_incident(payload)
    assert incident.opened_at.isoformat() == "2026-10-30T20:02:58+00:00"
    # Also verify alias from_issue
    assert adapter.from_issue(payload).opened_at == incident.opened_at


def test_jira_adapter_to_incident_alert_started_at_from_description_regex() -> None:
    config = BridgeConfig(jira_base_url="https://jira.example.com")
    adapter = JiraAdapter(config=config)
    adapter.get_issue_property = MagicMock(return_value=None)  # type: ignore[method-assign]

    payload = {
        "issue": {
            "key": "OPS-401",
            "fields": {
                "summary": "Alert violation",
                "description": (
                    "# **Incident**\n\n"
                    "**Detection:**\n"
                    "- Started At: 2026-10-30T20:02:58Z\n"
                    "- Cloud Monitoring alert violation inc-123 detected.\n"
                ),
                "created": "2026-10-30T20:21:59Z",
            },
        }
    }
    incident = adapter.to_incident(payload)
    assert incident.opened_at.isoformat() == "2026-10-30T20:02:58+00:00"


def test_jira_adapter_to_incident_fallback_to_created() -> None:
    config = BridgeConfig(jira_base_url="https://jira.example.com")
    adapter = JiraAdapter(config=config)
    adapter.get_issue_property = MagicMock(return_value=None)  # type: ignore[method-assign]

    payload = {
        "issue": {
            "key": "OPS-402",
            "fields": {
                "summary": "Alert violation without timestamp in body",
                "description": "Generic description",
                "created": "2026-10-30T20:21:59Z",
            },
        }
    }
    incident = adapter.to_incident(payload)
    assert incident.opened_at.isoformat() == "2026-10-30T20:21:59+00:00"


def test_jira_adapter_empty_heading_guard() -> None:
    from bridge.app.templates.jira_adf import markdown_to_adf

    adf = markdown_to_adf("##\n\nSome text")
    h_node = adf["content"][0]
    assert h_node["type"] == "heading"
    assert h_node["attrs"]["level"] == 2
    assert h_node["content"][0]["text"] == "Incident Section"
