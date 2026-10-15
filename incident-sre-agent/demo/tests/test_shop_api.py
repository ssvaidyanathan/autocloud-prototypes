import logging
import os
import time
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from shop_app import _get_inventory_auth_headers


def test_shop_health(shop_client: TestClient) -> None:
    resp = shop_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "shop-api"}


def test_shop_readyz_normal(shop_client: TestClient) -> None:
    resp = shop_client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "service": "shop-api"}


def test_shop_catalog_browsing(shop_client: TestClient) -> None:
    resp = shop_client.get("/catalog")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 4
    item_ids = [item["item_id"] for item in items]
    assert "item-1" in item_ids


def test_shop_catalog_single_item(shop_client: TestClient) -> None:
    resp = shop_client.get("/catalog/item-1")
    assert resp.status_code == 200
    assert resp.json()["item_id"] == "item-1"

    resp_404 = shop_client.get("/catalog/nonexistent-item-99")
    assert resp_404.status_code == 404


def test_shop_checkout_success(shop_client: TestClient) -> None:
    resp = shop_client.post(
        "/checkout",
        json={"item_id": "item-1", "quantity": 1, "customer_id": "cust-test"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["item_id"] == "item-1"
    assert data["quantity"] == 1
    assert data["status"] == "confirmed"
    assert data["order_id"].startswith("ord-")


def test_fault_scenario_1_error_spike(shop_client: TestClient) -> None:
    # Inject Scenario 1 with 100% error rate for deterministic testing
    inject_resp = shop_client.post(
        "/fault/inject",
        json={"scenario": 1, "error_rate": 1.0},
    )
    assert inject_resp.status_code == 200
    assert inject_resp.json()["scenario"] == 1

    # Verify checkout fails with 500
    checkout_resp = shop_client.post(
        "/checkout",
        json={"item_id": "item-1", "quantity": 1},
    )
    assert checkout_resp.status_code == 500
    assert "DatabaseConnectionError" in checkout_resp.json()["detail"]

    # Verify reset restores 200
    reset_resp = shop_client.post("/fault/reset")
    assert reset_resp.status_code == 200

    checkout_restored = shop_client.post(
        "/checkout",
        json={"item_id": "item-1", "quantity": 1},
    )
    assert checkout_restored.status_code == 200


def test_fault_scenario_2_latency(shop_client: TestClient) -> None:
    # Inject 0.05s latency for fast test execution
    inject_resp = shop_client.post(
        "/fault/inject",
        json={"scenario": 2, "latency_seconds": 0.05},
    )
    assert inject_resp.status_code == 200

    t0 = time.perf_counter()
    resp = shop_client.get("/catalog")
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 200
    assert elapsed >= 0.04

    # Reset
    shop_client.post("/fault/reset")
    status_resp = shop_client.get("/fault/status")
    assert status_resp.json()["latency_spike"] is False


def test_fault_scenario_3_bad_release(shop_client: TestClient) -> None:
    # Inject Scenario 3
    inject_resp = shop_client.post(
        "/fault/inject",
        json={"scenario": 3},
    )
    assert inject_resp.status_code == 200

    # /readyz must fail with 503
    ready_resp = shop_client.get("/readyz")
    assert ready_resp.status_code == 503
    assert "PAYMENT_GATEWAY_KEY" in ready_resp.json()["detail"]

    # /checkout must fail with 503
    checkout_resp = shop_client.post(
        "/checkout",
        json={"item_id": "item-1", "quantity": 1},
    )
    assert checkout_resp.status_code == 503

    # Reset
    shop_client.post("/fault/reset")
    assert shop_client.get("/readyz").status_code == 200


def test_fault_scenario_4_memory_leak(shop_client: TestClient) -> None:
    # Inject Scenario 4
    shop_client.post("/fault/inject", json={"scenario": 4})

    status_before = shop_client.get("/fault/status").json()
    assert status_before["memory_leak_mb"] == 0.0

    # Issue multiple requests
    shop_client.get("/catalog")
    shop_client.get("/catalog/item-1")

    status_after = shop_client.get("/fault/status").json()
    assert status_after["memory_leak_mb"] >= 20.0

    # Reset clears the memory buffer
    shop_client.post("/fault/reset")
    status_reset = shop_client.get("/fault/status").json()
    assert status_reset["memory_leak_mb"] == 0.0
    assert status_reset["memory_leak"] is False


def test_fault_scenario_5_dependency_failure(shop_client: TestClient) -> None:
    # Inject Scenario 5
    shop_client.post("/fault/inject", json={"scenario": 5})

    # Mock response from inventory service returning 403 Forbidden
    with mock.patch("httpx.AsyncClient.post") as mock_post:
        mock_response = mock.MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"
        mock_post.return_value = mock_response

        checkout_resp = shop_client.post(
            "/checkout",
            json={"item_id": "item-1", "quantity": 1},
        )
        assert checkout_resp.status_code == 502
        assert "403 Forbidden" in checkout_resp.json()["detail"]

    # Reset
    shop_client.post("/fault/reset")
    status_resp = shop_client.get("/fault/status")
    assert status_resp.json()["dependency_failure"] is False


def test_fault_status_all_fields(shop_client: TestClient) -> None:
    shop_client.post("/fault/inject", json={"scenario": 1, "error_rate": 0.5})
    shop_client.post("/fault/inject", json={"scenario": 4})

    resp = shop_client.get("/fault/status")
    assert resp.status_code == 200
    data = resp.json()
    assert 1 in data["active_scenarios"]
    assert 4 in data["active_scenarios"]
    assert data["error_spike"] is True
    assert data["error_rate"] == 0.5
    assert data["memory_leak"] is True


def test_get_inventory_auth_headers_fallback_local_and_production(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 1. Local/test environments fallback to dummy valid-invoker-token when token fetch fails
    with mock.patch(
        "google.oauth2.id_token.fetch_id_token",
        side_effect=RuntimeError("Metadata service unavailable"),
    ):
        # Default pytest run has PYTEST_CURRENT_TEST set
        headers = _get_inventory_auth_headers()
        assert headers == {"Authorization": "Bearer valid-invoker-token"}

        for env_val in ("local", "test", "dev"):
            with mock.patch.dict(os.environ, {"APP_ENV": env_val}, clear=False):
                headers = _get_inventory_auth_headers()
                assert headers == {"Authorization": "Bearer valid-invoker-token"}

    # 2. Production environment: empty headers + warning when token fetch fails
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTEST_CURRENT_TEST", "INVENTORY_SERVICE_TOKEN")
    }
    clean_env["APP_ENV"] = "production"

    with mock.patch.dict(os.environ, clean_env, clear=True):
        with mock.patch(
            "google.oauth2.id_token.fetch_id_token",
            side_effect=RuntimeError("Metadata service unavailable"),
        ):
            with caplog.at_level(logging.WARNING, logger="shop-api"):
                caplog.clear()
                headers = _get_inventory_auth_headers()
                assert headers == {}
                assert any(
                    record.levelname == "WARNING"
                    and "Failed to obtain ID token for inventory-svc: Metadata service unavailable"
                    in record.message
                    for record in caplog.records
                )

    # 3. Explicit INVENTORY_SERVICE_TOKEN takes precedence
    with mock.patch.dict(os.environ, {"INVENTORY_SERVICE_TOKEN": "custom-token"}):
        headers = _get_inventory_auth_headers()
        assert headers == {"Authorization": "Bearer custom-token"}


def test_fault_control_endpoints_emit_zero_app_logs(
    shop_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify fault injection and reset endpoints emit zero application log records.

    Administrative toggles must remain silent so autonomous SRE agents do not
    detect artificial fault injection in application telemetry.
    """
    with caplog.at_level(logging.DEBUG, logger="shop-api"):
        caplog.clear()

        # 1. Fault injection endpoint
        inject_resp = shop_client.post("/fault/inject", json={"scenario": 1, "error_rate": 0.4})
        assert inject_resp.status_code == 200
        assert len(caplog.records) == 0

        # 2. Fault status query endpoint
        status_resp = shop_client.get("/fault/status")
        assert status_resp.status_code == 200
        assert len(caplog.records) == 0

        # 3. Fault reset endpoint
        reset_resp = shop_client.post("/fault/reset")
        assert reset_resp.status_code == 200
        assert len(caplog.records) == 0
