import os
from unittest import mock

from fastapi.testclient import TestClient


def test_inventory_health(inventory_client: TestClient) -> None:
    resp = inventory_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "inventory-svc"}


def test_inventory_list_items(inventory_client: TestClient) -> None:
    resp = inventory_client.get("/inventory")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 4
    item_ids = [item["item_id"] for item in items]
    assert "item-1" in item_ids
    assert "item-2" in item_ids


def test_inventory_get_single_item(inventory_client: TestClient) -> None:
    resp = inventory_client.get("/inventory/item-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["item_id"] == "item-1"
    assert data["stock"] == 45
    assert data["price"] == 129.99

    # Nonexistent item
    resp_404 = inventory_client.get("/inventory/nonexistent-item")
    assert resp_404.status_code == 404


def test_inventory_reserve_success(inventory_client: TestClient) -> None:
    initial = inventory_client.get("/inventory/item-1").json()["stock"]
    resp = inventory_client.post(
        "/inventory/reserve",
        json={"item_id": "item-1", "quantity": 2},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "reserved"
    assert data["quantity"] == 2
    assert data["remaining_stock"] == initial - 2
    assert data["reservation_id"].startswith("res-")


def test_inventory_reserve_insufficient_stock(inventory_client: TestClient) -> None:
    resp = inventory_client.post(
        "/inventory/reserve",
        json={"item_id": "item-3", "quantity": 9999},
    )
    assert resp.status_code == 409
    assert "Insufficient stock" in resp.json()["detail"]


def test_inventory_auth_enforcement(inventory_client: TestClient) -> None:
    with mock.patch.dict(os.environ, {"REQUIRE_AUTH": "true"}):
        # Missing auth header -> 403
        resp_unauth = inventory_client.get("/inventory")
        assert resp_unauth.status_code == 403
        assert "Forbidden" in resp_unauth.json()["detail"]

        # Invalid auth header -> 403
        resp_invalid = inventory_client.get(
            "/inventory", headers={"Authorization": "Bearer invalid"}
        )
        assert resp_invalid.status_code == 403

        # Valid auth header -> 200
        resp_valid = inventory_client.get(
            "/inventory", headers={"Authorization": "Bearer valid-token-123"}
        )
        assert resp_valid.status_code == 200


def test_inventory_simulate_auth_failure_header(inventory_client: TestClient) -> None:
    resp = inventory_client.get(
        "/inventory",
        headers={"X-Simulate-Auth-Failure": "true"},
    )
    assert resp.status_code == 403
    assert "Forbidden" in resp.json()["detail"]
