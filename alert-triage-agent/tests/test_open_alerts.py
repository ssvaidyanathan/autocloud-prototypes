"""Unit tests for list_open_alerts querying and schema parsing."""

from typing import Any, Dict
from unittest.mock import MagicMock
import pytest

from agent.app.contracts.alert import AlertEvent
from tools.app.server import list_open_alerts


class MockResponse:
    def __init__(self, json_data: Dict[str, Any], status_code: int = 200):
        self._json_data = json_data
        self.status_code = status_code

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")


class MockHttpClient:
    def __init__(self, response_data: Dict[str, Any]):
        self.response_data = response_data
        self.last_url: str = ""
        self.last_params: Dict[str, Any] = {}

    def get(self, url: str, params: Dict[str, Any] = None, **kwargs) -> MockResponse:
        self.last_url = url
        self.last_params = params or {}
        return MockResponse(self.response_data)


def test_list_open_alerts_query_and_pagination():
    """Assert list_open_alerts queries Cloud Monitoring API with filter=state='OPEN' and handles pagination."""
    mock_payload = {
        "alerts": [
            {
                "name": "projects/cloud-assist-fde-2/alerts/0.inc12345",
                "state": "OPEN",
                "openTime": "2026-10-22T21:15:00Z",
                "resource": {
                    "type": "cloud_run_revision",
                    "labels": {"service_name": "ata-inventory-api"},
                },
                "policy": {
                    "name": "projects/cloud-assist-fde-2/alertPolicies/pol1",
                    "displayName": "ata-backend-5xx-error-rate",
                },
            }
        ],
        "nextPageToken": "token-page-2-xyz",
    }
    client = MockHttpClient(mock_payload)

    result = list_open_alerts(
        page_size=25,
        page_token="token-page-1-abc",
        project_id="cloud-assist-fde-2",
        http_client=client,
    )

    # 1. Assert requested URL and query parameters
    assert client.last_url == "https://monitoring.googleapis.com/v3/projects/cloud-assist-fde-2/alerts"
    assert client.last_params["filter"] == 'state="OPEN"'
    assert client.last_params["pageSize"] == 25
    assert client.last_params["pageToken"] == "token-page-1-abc"

    # 2. Assert pagination token propagation
    assert result["next_page_token"] == "token-page-2-xyz"
    assert result["total_count"] == 1


def test_list_open_alerts_schema_parsing_into_alert_event():
    """Assert alert items are parsed into AlertEvent schema."""
    mock_payload = {
        "alerts": [
            {
                "name": "projects/cloud-assist-fde-2/alerts/0.run500",
                "state": "OPEN",
                "openTime": "2026-10-22T21:14:30Z",
                "resource": {
                    "type": "cloud_run_revision",
                    "labels": {"service_name": "ata-inventory-api"},
                },
                "policy": {
                    "name": "projects/cloud-assist-fde-2/alertPolicies/p1",
                    "displayName": "ata-backend-5xx-error-rate",
                },
            },
            {
                "name": "projects/cloud-assist-fde-2/alerts/0.apigee502",
                "state": "OPEN",
                "openTime": "2026-10-22T21:16:00Z",
                "resource": {
                    "type": "apigee_proxy",
                    "labels": {"proxy_name": "ata-orders-v1"},
                },
                "policy": {
                    "name": "projects/cloud-assist-fde-2/alertPolicies/p2",
                    "displayName": "ata-apigee-5xx-spike",
                },
            },
        ]
    }
    client = MockHttpClient(mock_payload)

    result = list_open_alerts(project_id="cloud-assist-fde-2", http_client=client)

    assert result["total_count"] == 2
    alerts = result["alerts"]
    assert len(alerts) == 2

    # Verify each alert conforms to AlertEvent
    first = AlertEvent(**alerts[0])
    assert first.incident_id == "0.run500"
    assert first.policy_name == "projects/cloud-assist-fde-2/alertPolicies/p1"
    assert first.resource_name == "ata-inventory-api"
    assert first.state == "open"
    assert first.started_at == "2026-10-22T21:14:30Z"

    second = AlertEvent(**alerts[1])
    assert second.incident_id == "0.apigee502"
    assert second.policy_name == "projects/cloud-assist-fde-2/alertPolicies/p2"
    assert second.resource_name == "ata-orders-v1"
    assert second.state == "open"


def test_list_open_alerts_empty():
    """Assert empty API response yields 0 alerts and None token."""
    client = MockHttpClient({"alerts": []})
    result = list_open_alerts(project_id="cloud-assist-fde-2", http_client=client)

    assert result["total_count"] == 0
    assert result["alerts"] == []
    assert result["next_page_token"] is None
