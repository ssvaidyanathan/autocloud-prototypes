import os
from unittest import mock

import pytest
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


def test_inventory_restock_endpoint(inventory_client: TestClient) -> None:
    # Deplete stock
    reserve_resp = inventory_client.post(
        "/inventory/reserve",
        json={"item_id": "item-1", "quantity": 10},
    )
    assert reserve_resp.status_code == 200
    assert reserve_resp.json()["remaining_stock"] == 35

    # Trigger restock endpoint
    resp = inventory_client.post("/inventory/restock")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "items" in data
    assert len(data["items"]) >= 5
    restocked_item1 = next(item for item in data["items"] if item["item_id"] == "item-1")
    assert restocked_item1["stock"] == 45

    # Check GET /inventory/item-1 confirms replenishment
    item_resp = inventory_client.get("/inventory/item-1")
    assert item_resp.status_code == 200
    assert item_resp.json()["stock"] == 45


def test_inventory_restock_endpoint_auth(inventory_client: TestClient) -> None:
    with mock.patch.dict(os.environ, {"REQUIRE_AUTH": "true"}):
        # Missing auth header -> 403
        resp_unauth = inventory_client.post("/inventory/restock")
        assert resp_unauth.status_code == 403

        # Invalid auth header -> 403
        resp_invalid = inventory_client.post(
            "/inventory/restock", headers={"Authorization": "Bearer invalid"}
        )
        assert resp_invalid.status_code == 403

        # Valid auth header -> 200
        resp_valid = inventory_client.post(
            "/inventory/restock", headers={"Authorization": "Bearer valid-token-123"}
        )
        assert resp_valid.status_code == 200


def test_inventory_restock_simulate_auth_failure_header(inventory_client: TestClient) -> None:
    resp = inventory_client.post(
        "/inventory/restock",
        headers={"X-Simulate-Auth-Failure": "true"},
    )
    assert resp.status_code == 403
    assert "Forbidden" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_inventory_background_restock_replenish() -> None:
    from inventory_app import INITIAL_INVENTORY, _inventory, replenish_stock

    # Deplete stock manually
    _inventory["item-1"]["stock"] = 25
    _inventory["item-2"]["stock"] = 100

    # Replenish with specific amount (e.g., 10)
    replenished = replenish_stock(amount=10)
    assert replenished["item-1"] == 35
    assert _inventory["item-1"]["stock"] == 35
    assert replenished["item-2"] == 110
    assert _inventory["item-2"]["stock"] == 110

    # Replenish with amount that exceeds initial capacity
    replenished2 = replenish_stock(amount=50)
    assert replenished2["item-1"] == INITIAL_INVENTORY["item-1"]["stock"]  # 45
    assert replenished2["item-2"] == INITIAL_INVENTORY["item-2"]["stock"]  # 120

    # Already at initial stock -> returns empty dict
    replenished3 = replenish_stock(amount=10)
    assert replenished3 == {}


@pytest.mark.asyncio
async def test_inventory_background_restock_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from inventory_app import _inventory, restock_loop

    monkeypatch.setenv("RESTOCK_INTERVAL_SECONDS", "0.02")
    monkeypatch.setenv("RESTOCK_AMOUNT", "15")

    _inventory["item-1"]["stock"] = 10

    loop_task = asyncio.create_task(restock_loop())
    await asyncio.sleep(0.06)
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    assert _inventory["item-1"]["stock"] > 10
