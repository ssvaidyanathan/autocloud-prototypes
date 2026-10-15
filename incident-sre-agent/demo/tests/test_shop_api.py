import logging
import os
import time
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from shop_app import (
    FaultAccessLogFilter,
    _get_inventory_auth_headers,
    install_fault_access_log_filter,
)


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


def test_uvicorn_access_log_filter_drops_fault_endpoints_and_passes_checkout() -> None:
    """Verify FaultAccessLogFilter drops uvicorn.access records for /fault/inject,
    /fault/reset, and /fault/status, while allowing /checkout and normal traffic through.
    """
    flt = FaultAccessLogFilter()

    # 1. Test individual records via filter() directly with standard uvicorn args format
    # (client_addr, method, path, http_version, status_code)
    def make_record(
        method: str,
        path: str,
        status_code: int = 200,
        formatted: str | None = None,
    ) -> logging.LogRecord:
        msg = '%s - "%s %s HTTP/%s" %d' if formatted is None else formatted
        args: tuple[object, ...] = (
            ("127.0.0.1:12345", method, path, "1.1", status_code) if formatted is None else ()
        )
        return logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=args,
            exc_info=None,
        )

    # Fault endpoints must be dropped (filter returns False)
    assert flt.filter(make_record("POST", "/fault/inject")) is False
    assert flt.filter(make_record("POST", "/fault/reset")) is False
    assert flt.filter(make_record("GET", "/fault/status")) is False
    assert flt.filter(make_record("GET", "/fault")) is False
    assert flt.filter(make_record("GET", "/fault/")) is False
    assert flt.filter(make_record("GET", "/fault/x")) is False
    assert flt.filter(make_record("GET", "/fault?query=1")) is False

    # Normal application endpoints must pass through (filter returns True)
    assert flt.filter(make_record("POST", "/checkout")) is True
    assert flt.filter(make_record("GET", "/catalog")) is True
    assert flt.filter(make_record("GET", "/health")) is True
    assert flt.filter(make_record("GET", "/readyz")) is True
    assert flt.filter(make_record("GET", "/faultyroute")) is True

    # 2. Test fallback path with records without args (pre-formatted strings or raw messages)
    def make_args_free_record(msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )

    # Fallback pre-formatted access lines (Cloud Logging stdout textPayload format)
    assert (
        flt.filter(
            make_args_free_record(
                'INFO:     169.254.169.126:22774 - "GET /fault/status HTTP/1.1" 200 OK'
            )
        )
        is False
    )
    assert (
        flt.filter(
            make_args_free_record(
                'INFO:     127.0.0.1:44248 - "POST /fault/inject HTTP/1.1" 200 OK'
            )
        )
        is False
    )
    assert (
        flt.filter(
            make_args_free_record('INFO:     127.0.0.1:44248 - "POST /fault/reset HTTP/1.1" 200 OK')
        )
        is False
    )
    assert (
        flt.filter(
            make_args_free_record('INFO:     127.0.0.1:44248 - "GET /fault HTTP/1.1" 200 OK')
        )
        is False
    )
    assert (
        flt.filter(
            make_args_free_record('INFO:     127.0.0.1:44248 - "GET /fault/ HTTP/1.1" 200 OK')
        )
        is False
    )
    assert (
        flt.filter(
            make_args_free_record('INFO:     127.0.0.1:44248 - "GET /fault/x HTTP/1.1" 200 OK')
        )
        is False
    )
    assert (
        flt.filter(
            make_args_free_record(
                'INFO:     127.0.0.1:44248 - "GET /fault?query=1 HTTP/1.1" 200 OK'
            )
        )
        is False
    )

    # Bare path fallback messages without args
    assert flt.filter(make_args_free_record("/fault/status")) is False
    assert flt.filter(make_args_free_record("/fault")) is False
    assert flt.filter(make_args_free_record("/fault/")) is False
    assert flt.filter(make_args_free_record("/fault/x")) is False
    assert flt.filter(make_args_free_record("/fault?query=1")) is False

    # Confirm zero false drops on fallback path
    assert (
        flt.filter(
            make_args_free_record(
                'INFO:     169.254.169.126:22774 - "POST /checkout HTTP/1.1" 200 OK'
            )
        )
        is True
    )
    assert (
        flt.filter(
            make_args_free_record(
                'INFO:     169.254.169.126:22774 - "GET /catalog HTTP/1.1" 200 OK'
            )
        )
        is True
    )
    assert (
        flt.filter(
            make_args_free_record(
                'INFO:     169.254.169.126:22774 - "GET /faultyroute HTTP/1.1" 200 OK'
            )
        )
        is True
    )
    assert flt.filter(make_args_free_record("/checkout")) is True
    assert flt.filter(make_args_free_record("/catalog")) is True
    assert flt.filter(make_args_free_record("/faultyroute")) is True

    # 3. Test integrated logger behavior on uvicorn.access
    access_logger = logging.getLogger("uvicorn.access")
    install_fault_access_log_filter()

    captured: list[logging.LogRecord] = []

    class RecordCollector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    collector = RecordCollector()
    access_logger.addHandler(collector)
    old_level = access_logger.level
    access_logger.setLevel(logging.INFO)

    try:
        # Simulate uvicorn logging access records for fault endpoints
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:12345",
            "POST",
            "/fault/inject",
            "1.1",
            200,
        )
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:12345",
            "POST",
            "/fault/reset",
            "1.1",
            200,
        )
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "169.254.169.126:22774",
            "GET",
            "/fault/status",
            "1.1",
            200,
        )

        # Simulate uvicorn logging access record for checkout
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:12345",
            "POST",
            "/checkout",
            "1.1",
            200,
        )

        # Assert only /checkout was emitted to handlers
        assert len(captured) == 1
        assert captured[0].args[2] == "/checkout"  # type: ignore[index]
    finally:
        access_logger.removeHandler(collector)
        access_logger.setLevel(old_level)
