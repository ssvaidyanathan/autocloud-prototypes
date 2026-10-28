"""Unit tests for demo applications (ata-inventory-api and ata-orders-api)."""

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from demo.ata_inventory_api.app.main import app as inventory_app
from demo.ata_orders_api.app.main import app as orders_app
from demo.ata_inventory_api.app.db import reset_connections_and_faults


@pytest.fixture(autouse=True)
def cleanup():
    from demo.ata_inventory_api.app.config import settings as inv_settings
    inv_settings.mock_db = True
    reset_connections_and_faults()
    yield
    reset_connections_and_faults()


# ==========================================
# Inventory API Tests
# ==========================================

def test_inventory_health():
    client = TestClient(inventory_app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_inventory_list_items():
    client = TestClient(inventory_app)
    resp = client.get("/items")
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)
    assert len(items) >= 5
    item_ids = [item["id"] for item in items]
    assert "item-101" in item_ids


def test_inventory_reserve_success():
    client = TestClient(inventory_app)
    resp = client.post("/items/reserve", json={"item_id": "item-101", "quantity": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["item_id"] == "item-101"
    assert data["remaining_stock"] == 48


def test_inventory_reserve_insufficient_stock():
    client = TestClient(inventory_app)
    resp = client.post("/items/reserve", json={"item_id": "item-104", "quantity": 9999})
    assert resp.status_code == 400
    assert "Insufficient stock" in resp.json()["detail"]


def test_inventory_reserve_invalid_item():
    client = TestClient(inventory_app)
    resp = client.post("/items/reserve", json={"item_id": "nonexistent-item", "quantity": 1})
    assert resp.status_code == 400


def test_inventory_admin_fault_injection():
    client = TestClient(inventory_app)
    # Inject 500 fault
    resp = client.post("/admin/fault/inject", json={"type": "500", "rate": 1.0})
    assert resp.status_code == 200

    # Next call should fail with 500
    resp_fail = client.get("/items")
    assert resp_fail.status_code == 500

    # Reset
    resp_reset = client.post("/admin/reset")
    assert resp_reset.status_code == 200

    # Now items should succeed again
    resp_ok = client.get("/items")
    assert resp_ok.status_code == 200


def test_inventory_connection_pool_configuration():
    """Verify SQLAlchemy QueuePool configuration matches Stage 2 requirements."""
    from demo.ata_inventory_api.app.db import engine
    from sqlalchemy.pool import QueuePool

    assert isinstance(engine.pool, QueuePool)
    assert engine.pool.size() == 5
    assert engine.pool._max_overflow == 2
    assert engine.pool._timeout == 30
    assert engine.pool._recycle == 1800


def test_inventory_engine_connect_live_mode():
    """Verify list_items and reserve_item_stock check out pooled connections via with engine.connect()."""
    from demo.ata_inventory_api.app.config import settings as inv_settings
    from demo.ata_inventory_api.app.db import list_items, reserve_item_stock, engine
    from unittest.mock import MagicMock

    inv_settings.mock_db = False

    mock_conn = MagicMock()
    mock_result_list = MagicMock()
    mock_result_list.fetchall.return_value = [
        ("item-pooled-1", "Pooled Item 1", 20, 5000)
    ]
    mock_result_reserve = MagicMock()
    mock_result_reserve.fetchone.return_value = (20,)

    mock_conn.exec_driver_sql.side_effect = [mock_result_list, mock_result_reserve, MagicMock()]
    mock_conn.begin.return_value.__enter__.return_value = mock_conn

    with patch.object(engine, "connect") as mock_connect:
        mock_connect.return_value.__enter__.return_value = mock_conn

        # Test list_items via engine.connect()
        items = list_items()
        assert len(items) == 1
        assert items[0]["id"] == "item-pooled-1"
        assert items[0]["name"] == "Pooled Item 1"

        # Test reserve_item_stock via engine.connect()
        reserve_res = reserve_item_stock("item-pooled-1", 5)
        assert reserve_res["success"] is True
        assert reserve_res["remaining_stock"] == 15
        assert mock_connect.call_count == 2


# ==========================================
# Orders API Tests
# ==========================================

def test_orders_health():
    client = TestClient(orders_app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_orders_create_and_retrieve():
    client = TestClient(orders_app)

    # Mock downstream call to inventory API
    mock_reserve_response = {
        "success": True,
        "item_id": "item-101",
        "remaining_stock": 49,
    }

    with patch("demo.ata_orders_api.app.orders.httpx.Client") as mock_client_cls:
        mock_instance = mock_client_cls.return_value.__enter__.return_value
        mock_instance.post.return_value.status_code = 200
        mock_instance.post.return_value.json.return_value = mock_reserve_response
        mock_instance.post.return_value.raise_for_status.return_value = None

        resp = client.post(
            "/orders",
            json={"item_id": "item-101", "quantity": 1, "customer_id": "cust-test-01"},
        )
        assert resp.status_code == 200
        order = resp.json()
        assert order["status"] == "confirmed"
        assert order["item_id"] == "item-101"
        order_id = order["order_id"]

        # Retrieve order
        get_resp = client.get(f"/orders/{order_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["order_id"] == order_id


def test_orders_retrieve_nonexistent():
    client = TestClient(orders_app)
    resp = client.get("/orders/ord-nonexistent")
    assert resp.status_code == 404


def test_orders_admin_fault_injection():
    client = TestClient(orders_app)

    # Inject 5xx fault
    resp = client.post("/admin/fault/5xx", json={"rate": 1.0})
    assert resp.status_code == 200

    # Creating order should now fail with 500
    resp_fail = client.post("/orders", json={"item_id": "item-101", "quantity": 1})
    assert resp_fail.status_code == 500

    # Reset
    resp_reset = client.post("/admin/reset")
    assert resp_reset.status_code == 200
