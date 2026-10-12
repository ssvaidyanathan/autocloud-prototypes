"""Unit tests for Bridge main webhook endpoints payload validation."""

from typing import Generator
from unittest.mock import MagicMock

import pytest
from bridge.app.adapters.jira import JiraAdapter
from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig, get_settings
from bridge.app.core.tasks import TaskDispatcher
from bridge.app.main import app, set_task_dispatcher
from fastapi.testclient import TestClient


@pytest.fixture
def mock_dispatcher() -> MagicMock:
    dispatcher = MagicMock(spec=TaskDispatcher)
    set_task_dispatcher(dispatcher)
    return dispatcher


@pytest.fixture
def bridge_test_client(mock_dispatcher: MagicMock) -> Generator[TestClient, None, None]:
    test_config = BridgeConfig(
        app_env="local",
        gcp_project="test-project",
        jira_webhook_token="secret-jira-token",
        servicenow_webhook_token="secret-snow-token",
    )
    app.dependency_overrides[get_settings] = lambda: test_config
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_webhook_jira_empty_identifier_returns_400(
    bridge_test_client: TestClient,
    mock_dispatcher: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /webhook/jira with empty issue ID/key returns 400 without dispatch or ack."""
    mock_post_ack = MagicMock()
    monkeypatch.setattr(JiraAdapter, "post_ack", mock_post_ack)

    headers = {"X-Webhook-Token": "secret-jira-token"}
    payload = {"issue": {"id": "", "key": ""}}

    resp = bridge_test_client.post("/webhook/jira", json=payload, headers=headers)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Payload normalization failed: Missing required Jira incident identifier" in detail
    mock_dispatcher.dispatch.assert_not_called()
    mock_post_ack.assert_not_called()


def test_webhook_jira_missing_issue_returns_400(
    bridge_test_client: TestClient,
    mock_dispatcher: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /webhook/jira with missing issue object returns 400 without dispatch or ack."""
    mock_post_ack = MagicMock()
    monkeypatch.setattr(JiraAdapter, "post_ack", mock_post_ack)

    headers = {"X-Webhook-Token": "secret-jira-token"}
    payload = {"webhookEvent": "jira:issue_updated"}

    resp = bridge_test_client.post("/webhook/jira", json=payload, headers=headers)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Payload normalization failed: Missing required Jira incident identifier" in detail
    mock_dispatcher.dispatch.assert_not_called()
    mock_post_ack.assert_not_called()


def test_webhook_servicenow_empty_identifier_returns_400(
    bridge_test_client: TestClient,
    mock_dispatcher: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /webhook/servicenow with empty sys_id/number returns 400 without dispatch or ack."""
    mock_post_ack = MagicMock()
    monkeypatch.setattr(ServiceNowAdapter, "post_ack", mock_post_ack)

    headers = {"Authorization": "Bearer secret-snow-token"}
    payload = {"sys_id": "", "number": ""}

    resp = bridge_test_client.post("/webhook/servicenow", json=payload, headers=headers)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Payload normalization failed: Missing required ServiceNow incident identifier" in detail
    mock_dispatcher.dispatch.assert_not_called()
    mock_post_ack.assert_not_called()


def test_webhook_servicenow_empty_payload_returns_400(
    bridge_test_client: TestClient,
    mock_dispatcher: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /webhook/servicenow with empty dict returns 400 without dispatch or ack."""
    mock_post_ack = MagicMock()
    monkeypatch.setattr(ServiceNowAdapter, "post_ack", mock_post_ack)

    headers = {"Authorization": "Bearer secret-snow-token"}
    payload: dict[str, str] = {}

    resp = bridge_test_client.post("/webhook/servicenow", json=payload, headers=headers)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Payload normalization failed: Missing required ServiceNow incident identifier" in detail
    mock_dispatcher.dispatch.assert_not_called()
    mock_post_ack.assert_not_called()
