"""Unit tests for Jira adapter identifier validation."""

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
