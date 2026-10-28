"""Unit tests verifying log suppression on /admin/* fault injection endpoints."""

import logging
import pytest
from fastapi.testclient import TestClient

# Import from demo inventory API
import sys
import os
sys.path.insert(0, os.path.abspath("demo/ata-inventory-api"))
from demo.ata_inventory_api.app.main import app as inventory_app
from demo.ata_inventory_api.app.middleware import SuppressAdminLogsFilter

sys.path.insert(0, os.path.abspath("demo/ata-orders-api"))
from demo.ata_orders_api.app.main import app as orders_app


def test_suppress_admin_logs_filter_direct():
    log_filter = SuppressAdminLogsFilter()

    # Normal log record should be kept
    normal_record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="server.py",
        lineno=10,
        msg='127.0.0.1 - "GET /items HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert log_filter.filter(normal_record) is True

    # Admin log record should be dropped
    admin_record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="server.py",
        lineno=10,
        msg='127.0.0.1 - "POST /admin/leak-connections HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert log_filter.filter(admin_record) is False

    # Admin in args should also be dropped
    admin_arg_record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="server.py",
        lineno=10,
        msg='Handling request: %s',
        args=("/admin/fault/latency",),
        exc_info=None,
    )
    assert log_filter.filter(admin_arg_record) is False


def test_inventory_admin_endpoints_suppressed(caplog):
    caplog.handler.addFilter(SuppressAdminLogsFilter())
    client = TestClient(inventory_app)
    caplog.set_level(logging.DEBUG)

    # 1. Normal request - should succeed
    resp = client.get("/health")
    assert resp.status_code == 200

    # 2. Call admin endpoints
    resp_leak = client.post("/admin/leak-connections", json={"count": 2})
    assert resp_leak.status_code == 200

    resp_reset = client.post("/admin/reset")
    assert resp_reset.status_code == 200

    resp_inject = client.post("/admin/fault/inject", json={"type": "latency", "delay_ms": 10})
    assert resp_inject.status_code == 200

    # Assert that NO log entry captured contains "/admin"
    for record in caplog.records:
        assert "/admin" not in record.getMessage()


def test_orders_admin_endpoints_suppressed(caplog):
    caplog.handler.addFilter(SuppressAdminLogsFilter())
    client = TestClient(orders_app)
    caplog.set_level(logging.DEBUG)

    # Call admin endpoints
    resp_lat = client.post("/admin/fault/latency", json={"delay_ms": 10})
    assert resp_lat.status_code == 200

    resp_5xx = client.post("/admin/fault/5xx", json={"rate": 0.5})
    assert resp_5xx.status_code == 200

    resp_rst = client.post("/admin/reset")
    assert resp_rst.status_code == 200

    # Assert no logs contain /admin
    for record in caplog.records:
        assert "/admin" not in record.getMessage()
