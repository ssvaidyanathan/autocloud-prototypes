"""Tests for alert resolution, MCP context, Jira drafting, and Jira creation tools."""

from __future__ import annotations

import pytest

from app.tools.alert import get_latest_incident, resolve_alert
from app.tools.jira_create import create_jira_issue
from app.tools.jira_draft import draft_jira_ticket
from app.tools.mcp_client import get_recent_deployments, lookup_alert_context


def test_resolve_alert_fuzzy_matching() -> None:
    # Query with alias
    match = resolve_alert("ip-health-snow")
    assert match.policy_id == "15010275944346199662"
    assert "snow-ip-healthcheck-3" in match.display_name
    assert match.match_confidence >= 0.5

    # Direct policy ID match
    match_exact = resolve_alert("15010275944346199662")
    assert match_exact.policy_id == "15010275944346199662"
    assert match_exact.match_confidence == 1.0


def test_resolve_alert_fuzzy_matching_mocked_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify fuzzy matching resolves against mocked Monitoring API responses."""
    mock_policies = [
        {
            "name": "projects/test-proj/alertPolicies/999911112222",
            "displayName": "Database High CPU Utilization",
            "userLabels": {"env": "prod", "tier": "db"},
        },
        {
            "name": "projects/test-proj/alertPolicies/15010275944346199662",
            "displayName": "Alert Policy for snow-ip-healthcheck-3",
            "userLabels": {"service": "snow", "check": "health"},
            "conditions": [
                {
                    "displayName": "Uptime check failed",
                    "conditionThreshold": {
                        "filter": 'metric.label.check_id="snow-ip-healthcheck-3"',
                    },
                }
            ],
        },
    ]

    monkeypatch.setattr("app.tools.alert.fetch_alert_policies", lambda pid: mock_policies)

    # Fuzzy match token query "snow-ip"
    match1 = resolve_alert("snow-ip", project_id="mock-project")
    assert match1.policy_id == "15010275944346199662"
    assert "snow-ip-healthcheck-3" in match1.display_name

    # Fuzzy match token query "ip-health-snow"
    match2 = resolve_alert("ip-health-snow", project_id="mock-project")
    assert match2.policy_id == "15010275944346199662"

    # Direct policy ID match
    match3 = resolve_alert("15010275944346199662", project_id="mock-project")
    assert match3.policy_id == "15010275944346199662"
    assert match3.match_confidence == 1.0


def test_get_latest_incident() -> None:
    incident = get_latest_incident("15010275944346199662")
    assert incident.incident_id == "0.test-inc-12345"
    assert incident.policy_id == "15010275944346199662"
    assert incident.state == "OPEN"
    assert incident.check_id == "snow-ip-healthcheck-3"


def test_get_latest_incident_sorted_by_opentime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_latest_incident sorts alerts by openTime descending and selects the newest."""
    mock_alerts = [
        {
            "name": "projects/test-proj/alerts/0.oldest_violation",
            "state": "CLOSED",
            "openTime": "2026-10-30T01:00:00Z",
            "policy": {"displayName": "Alert Policy for snow-ip-healthcheck-3"},
        },
        {
            "name": "projects/test-proj/alerts/0.latest_violation",
            "state": "OPEN",
            "openTime": "2026-10-30T05:30:00Z",
            "policy": {"displayName": "Alert Policy for snow-ip-healthcheck-3"},
            "metric": {
                "labels": {
                    "check_id": "snow-ip-healthcheck-3",
                    "checked_resource_id": "10.1.2.3",
                }
            },
            "resource": {"labels": {"host": "10.1.2.3"}},
        },
        {
            "name": "projects/test-proj/alerts/0.middle_violation",
            "state": "CLOSED",
            "openTime": "2026-10-30T03:15:00Z",
            "policy": {"displayName": "Alert Policy for snow-ip-healthcheck-3"},
        },
    ]

    monkeypatch.setattr("app.tools.alert.fetch_policy_alerts", lambda pid, pol_id: mock_alerts)

    incident = get_latest_incident("15010275944346199662", project_id="mock-project")
    assert incident.incident_id == "0.latest_violation"
    assert incident.started_at == "2026-10-30T05:30:00Z"
    assert incident.state == "OPEN"
    assert incident.target_endpoint == "http://10.1.2.3/"
    assert incident.check_id == "snow-ip-healthcheck-3"


def test_get_recent_deployments() -> None:
    resp = get_recent_deployments(workload="test-workload", cluster="test-cluster", limit=2)
    assert len(resp.deployments) <= 2
    assert resp.total_count >= 1

    first = resp.deployments[0]
    assert first.revision == "89"
    assert first.tool == "helm"
    assert first.pipeline_id is None
    assert resp.missing_pipeline_notice is not None
    assert (
        "CI/CD pipeline ID and Git commit metadata were not reported"
        in resp.missing_pipeline_notice
    )


def test_lookup_alert_context() -> None:
    resp = lookup_alert_context(target_ip="10.0.0.1", check_id="snow-ip-healthcheck-3")
    assert resp.found is True
    assert resp.context is not None
    assert resp.context["target_ip"] == "10.0.0.1"


def test_draft_jira_ticket_generates_signed_token() -> None:
    draft = draft_jira_ticket(policy_id="15010275944346199662", priority="P2")
    assert draft.project_key == "AMJTEST"
    assert draft.issue_type == "Task"
    assert draft.priority == "P2"
    assert draft.incident_id == "0.test-inc-12345"
    assert "hitl" in draft.labels
    assert f"alert-{draft.incident_id}" in draft.labels
    assert len(draft.signed_token) > 20


def test_create_jira_issue_with_valid_token() -> None:
    draft = draft_jira_ticket(policy_id="15010275944346199662")
    result = create_jira_issue(signed_token=draft.signed_token)

    assert result.status == "created"
    assert result.issue_key.startswith("AMJTEST-")
    assert result.incident_id == "0.test-inc-12345"


def test_create_jira_issue_with_invalid_token_fails() -> None:
    with pytest.raises(ValueError, match="Approval token validation failed"):
        create_jira_issue(signed_token="invalid.forged.token")


def test_create_jira_issue_single_use_and_idempotency() -> None:
    draft = draft_jira_ticket(policy_id="15010275944346199662")
    res1 = create_jira_issue(signed_token=draft.signed_token)
    assert res1.status == "created"

    # Re-using the exact same token triggers single-use replay check
    with pytest.raises(ValueError, match="single-use replay detected"):
        create_jira_issue(signed_token=draft.signed_token)

    # Creating a new token for the same incident triggers Jira idempotency
    draft2 = draft_jira_ticket(policy_id="15010275944346199662")
    res2 = create_jira_issue(signed_token=draft2.signed_token)
    assert res2.status == "already_exists"
    assert res2.issue_key == res1.issue_key


def test_create_jira_issue_bridge_already_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockResponse:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {
                "status": "already_exists",
                "issue_key": "AMJTEST-22",
                "issue_url": "https://google-autocloud-team.atlassian.net/browse/AMJTEST-22",
            }

    class MockClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> MockClient:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def post(self, *args: object, **kwargs: object) -> MockResponse:
            return MockResponse()

    monkeypatch.setattr("httpx.Client", MockClient)
    draft = draft_jira_ticket(policy_id="15010275944346199662")
    result = create_jira_issue(signed_token=draft.signed_token)
    assert result.status == "already_exists"
    assert result.issue_key == "AMJTEST-22"
    assert result.issue_url == "https://google-autocloud-team.atlassian.net/browse/AMJTEST-22"


def test_draft_jira_ticket_template_formatting() -> None:
    draft = draft_jira_ticket(
        policy_id="15010275944346199662",
        session_id="C12345:1699999.00",
        requester_id="U12345",
    )
    assert draft.author == "<@U12345>"
    assert draft.slack_thread == "C12345:1699999.00"

    desc = draft.description
    assert "**Date:**" in desc
    assert "**Authors:** <@U12345>" in desc
    assert "**Status:** In progress" in desc
    assert "**Statuspage:**" in desc
    assert "**Summary:**" in desc
    assert "**Impact / Observations" in desc
    assert "**Impacted Customers" in desc
    assert "**Root Causes:**" in desc
    assert "**Trigger:**" in desc
    assert "**Detection:**" in desc
    assert "**Initial Response:**" in desc
    assert "**Resolution:**" in desc
    assert "**Action Items / Jira Tickets:**" in desc
    assert "**Timeline:**" in desc
    assert "**Lessons Learned and Incident Feedback:**" in desc
    assert "**Supporting information**" in desc
    assert draft.alert_started_at is not None
    assert f"- Started At: {draft.alert_started_at}" in desc


def test_create_jira_issue_payload_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_payload = {}

    class MockResponse:
        status_code = 201

        def json(self) -> dict[str, str]:
            return {
                "status": "created",
                "issue_key": "AMJTEST-99",
                "issue_url": "https://google-autocloud-team.atlassian.net/browse/AMJTEST-99",
            }

    class MockClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> MockClient:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def post(self, url: str, *args: object, **kwargs: object) -> MockResponse:
            nonlocal captured_payload
            captured_payload = kwargs.get("json", {})
            return MockResponse()

    monkeypatch.setattr("httpx.Client", MockClient)
    draft = draft_jira_ticket(
        policy_id="15010275944346199662",
        session_id="C999:1234.56",
        requester_id="U999",
    )
    result = create_jira_issue(
        signed_token=draft.signed_token,
        approver_id="U999",
        session_id="C999:1234.56",
    )
    assert result.status == "created"
    assert captured_payload.get("slack_thread") == "C999:1234.56"
    assert captured_payload.get("callback_url", "").endswith("/internal/rca-callback")
    assert captured_payload.get("alert_started_at") == draft.alert_started_at
    assert any(lbl.startswith("alert-open-") for lbl in captured_payload.get("labels", []))
