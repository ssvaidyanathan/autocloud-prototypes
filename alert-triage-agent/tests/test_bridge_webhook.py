import pytest
from fastapi.testclient import TestClient

from bridge.app.config import BridgeConfig, reset_settings
from bridge.app.contracts.alert import AlertEvent
from bridge.app.core.dispatcher import TaskDispatcher
from bridge.app.main import app, set_dispatcher


@pytest.fixture
def test_config():
    config = BridgeConfig(
        project_id="test-project",
        name_prefix="ata",
        region="us-west1",
        webhook_token="secret-webhook-token",
        is_local=True,
    )
    reset_settings(config)
    yield config
    reset_settings(None)


@pytest.fixture
def client(test_config):
    dispatcher = TaskDispatcher(config=test_config)
    set_dispatcher(dispatcher)
    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_webhook_valid_token_parses_payload(client, test_config):
    payload = {
        "version": "1.2",
        "incident": {
            "incident_id": "0.12345678",
            "resource_id": "projects/cloud-assist-fde-2/locations/us-west1/services/ata-inventory-api",
            "resource_name": "ata-inventory-api",
            "resource_type": "cloud_run_revision",
            "resource": {
                "type": "cloud_run_revision",
                "labels": {
                    "service_name": "ata-inventory-api",
                    "location": "us-west1",
                    "project_id": "cloud-assist-fde-2",
                },
            },
            "state": "open",
            "started_at": 1729631700,
            "policy_name": "ata-backend-5xx-error-rate",
            "condition_name": "Cloud Run 5xx error rate high",
            "summary": "Cloud Run 5xx rate exceeded threshold",
            "metric": {
                "type": "run.googleapis.com/request_count",
            },
        },
    }

    # 1. Valid token in query param: thin webhook accepts raw alert with 202
    resp = client.post("/webhook/monitoring?token=secret-webhook-token", json=payload)
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["incident_id"] == "0.12345678"
    assert "ata-alert-" in data["task_id"]

    # 2. Subsequent alert delivery (zero bridge alert deduplication or windowing suppression)
    dup_resp = client.post("/webhook/monitoring?token=secret-webhook-token", json=payload)
    assert dup_resp.status_code == 202
    dup_data = dup_resp.json()
    assert dup_data["status"] == "accepted"
    assert dup_data["incident_id"] == "0.12345678"


def test_webhook_drops_non_ata_alert(client):
    """Assert non-ata alert policies are ignored with HTTP 200 and not dispatched."""
    non_ata_payload = {
        "version": "1.2",
        "incident": {
            "incident_id": "0.non_ata_999",
            "resource_name": "default-vpc-network",
            "resource_type": "gce_network",
            "state": "open",
            "policy_name": "general-system-health-check",
            "summary": "Unrelated health check failure",
        },
    }
    resp = client.post("/webhook/monitoring?token=secret-webhook-token", json=non_ata_payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_webhook_invalid_token_returns_401(client):
    payload = {"incident": {"incident_id": "0.999", "policy_name": "test", "resource_name": "test"}}
    resp = client.post("/webhook/monitoring?token=wrong-token", json=payload)
    assert resp.status_code == 401
    assert "Invalid or missing webhook token" in resp.json()["detail"]


def test_webhook_missing_token_returns_401(client):
    payload = {"incident": {"incident_id": "0.999", "policy_name": "test", "resource_name": "test"}}
    resp = client.post("/webhook/monitoring", json=payload)
    assert resp.status_code == 401


def test_alert_event_parsing():
    raw = {
        "incident": {
            "incident_id": "inc-42",
            "policy_name": "projects/cloud-assist-fde-2/alertPolicies/123",
            "resource": {
                "type": "cloud_run_revision",
                "labels": {"service_name": "ata-orders-api"},
            },
            "started_at": "2026-10-22T21:15:00Z",
            "state": "OPEN",
        }
    }
    event = AlertEvent.from_monitoring_payload(raw)
    assert event.incident_id == "inc-42"
    assert event.resource_name == "ata-orders-api"
    assert event.policy_slug == "123"
    assert event.state == "open"
    assert event.started_at == "2026-10-22T21:15:00Z"


def test_all_routes_reject_unauthenticated_except_health_and_webhook(client):
    """Default-deny: assert all routes except /health and /webhook/monitoring reject unauthenticated requests with 401/403."""
    exempt_routes = {"/health", "/webhook/monitoring"}
    tested_paths = set()

    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or path in exempt_routes:
            continue
        methods = getattr(route, "methods", set())
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            resp = client.request(method, path)
            assert resp.status_code in (401, 403), (
                f"Route {method} {path} returned HTTP {resp.status_code}, expected 401 or 403"
            )
            tested_paths.add(path)

    # Verify that instruction endpoints were discovered and tested
    assert "/instructions/ticket/create" in tested_paths
    assert "/instructions/ticket/work-note" in tested_paths
    assert "/instructions/ticket/update-report" in tested_paths
    assert "/instructions/ticket/resolve" in tested_paths
    assert "/instructions/ticket/query/{group_id}" in tested_paths


def test_deleted_endpoints_return_404(client):
    """Assert that previously exposed debug/legacy endpoints now return 404 Not Found."""
    assert client.get("/servicenow/incident/any-sys-id").status_code == 404
    assert client.get("/servicenow/attachment/any-sys-id").status_code == 404
    assert client.get("/windowing/groups").status_code == 404
    assert client.post("/windowing/check-deadlines").status_code == 404
    assert client.post("/investigate/findings").status_code == 404
    assert client.post("/callback").status_code == 404
    assert client.post("/windowing/reset").status_code == 404
    assert client.post("/reset").status_code == 404
    assert client.post("/windowing/groups/any-group/archive").status_code == 404
    assert client.post("/windowing/groups/any-group/resolve").status_code == 404
