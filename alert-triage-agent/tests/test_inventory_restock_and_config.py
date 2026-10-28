"""ata-inventory-api: POST /admin/restock and the ATA_INVENTORY_*-only configuration.

Restock (QA r3 R3-5): demo stock ran out across scenario runs (item-101..103 at 0), so
POST /orders returned HTTP 400. reset-scenarios.sh restocks to a configured baseline via
POST /admin/restock: absolute values (idempotent), all or nothing, unknown ids rejected.

Config: the legacy DB_USER / MOCK_DB aliases are gone (only ATA_INVENTORY_* is read), and
the Cloud SQL settings have no built-in project values.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from demo.ata_inventory_api.app import db as inv_db
from demo.ata_inventory_api.app.config import Settings, settings as inv_settings
from demo.ata_inventory_api.app.main import app as inventory_app


@pytest.fixture(autouse=True)
def mock_store():
    inv_settings.mock_db = True
    inv_db.reset_connections_and_faults()
    yield
    inv_settings.mock_db = True
    inv_db.reset_connections_and_faults()


def _client():
    return TestClient(inventory_app)


# --- POST /admin/restock (in-memory catalog) -------------------------------------------


def test_restock_sets_absolute_stock_and_returns_it():
    client = _client()
    client.post("/items/reserve", json={"item_id": "item-101", "quantity": 7})

    resp = client.post("/admin/restock", json={"items": {"item-101": 1000, "item-105": 900}})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status": "restocked",
        "items": [{"id": "item-101", "stock": 1000}, {"id": "item-105", "stock": 900}],
    }
    stock = {i["id"]: i["stock"] for i in client.get("/items").json()}
    assert stock["item-101"] == 1000 and stock["item-105"] == 900
    assert stock["item-102"] == 120  # not listed: unchanged


def test_restock_is_idempotent():
    client = _client()
    body = {"items": {"item-101": 1000}}
    first = client.post("/admin/restock", json=body).json()
    second = client.post("/admin/restock", json=body).json()
    assert first == second


def test_restock_unknown_item_changes_nothing_and_names_it():
    client = _client()
    resp = client.post("/admin/restock", json={"items": {"item-101": 999, "item-999": 5}})

    assert resp.status_code == 400
    assert resp.json()["detail"] == {"error": "unknown items", "missing": ["item-999"]}
    stock = {i["id"]: i["stock"] for i in client.get("/items").json()}
    assert stock["item-101"] == 50


@pytest.mark.parametrize("body", [{"items": {}}, {}, {"items": {"item-101": "lots"}}])
def test_restock_rejects_invalid_body(body):
    assert _client().post("/admin/restock", json=body).status_code == 422


def test_restock_rejects_negative_stock():
    resp = _client().post("/admin/restock", json={"items": {"item-101": -1}})
    assert resp.status_code == 400
    assert resp.json()["detail"] == {"error": "negative stock", "items": ["item-101"]}


def test_restock_get_probe_is_harmless():
    client = _client()
    resp = client.get("/admin/restock")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "endpoint": "/admin/restock"}
    stock = {i["id"]: i["stock"] for i in client.get("/items").json()}
    assert stock["item-101"] == 50


def test_restock_ignores_injected_faults():
    """Reset restocks after the fault reset, but the admin action must not be faulted."""
    client = _client()
    client.post("/admin/fault/inject", json={"type": "500", "rate": 1.0})
    assert client.post("/admin/restock", json={"items": {"item-101": 10}}).status_code == 200


# --- restock_items against the database path ---------------------------------------------


def _fake_engine(rowcounts, rows):
    conn = MagicMock()
    results = []
    for rc in rowcounts:
        r = MagicMock()
        r.rowcount = rc
        results.append(r)
    select = MagicMock()
    select.fetchall.return_value = rows
    conn.exec_driver_sql.side_effect = results + [select]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    return engine, conn


def test_restock_db_updates_in_one_transaction_and_reads_back():
    inv_settings.mock_db = False
    engine, conn = _fake_engine([1, 1], [("item-101", 1000), ("item-102", 120), ("item-105", 900)])
    with patch.object(inv_db, "engine", engine):
        items = inv_db.restock_items({"item-105": 900, "item-101": 1000})

    assert items == [{"id": "item-101", "stock": 1000}, {"id": "item-105", "stock": 900}]
    conn.begin.assert_called_once()
    updates = [c.args for c in conn.exec_driver_sql.call_args_list[:2]]
    assert all(sql.startswith("UPDATE inventory_items SET stock = %s") for sql, _ in updates)
    assert [params for _, params in updates] == [(1000, "item-101"), (900, "item-105")]


def test_restock_db_unknown_item_raises_inside_the_transaction():
    """Raising inside conn.begin() rolls back: nothing is restocked."""
    inv_settings.mock_db = False
    engine, conn = _fake_engine([1, 0], [])
    begin_cm = conn.begin.return_value
    with patch.object(inv_db, "engine", engine):
        with pytest.raises(inv_db.UnknownItemsError) as exc:
            inv_db.restock_items({"item-101": 1000, "item-999": 1})

    assert exc.value.missing == ["item-999"]
    exit_args = begin_cm.__exit__.call_args.args
    assert exit_args[0] is inv_db.UnknownItemsError  # the transaction saw the exception


def test_restock_db_error_is_a_500_not_a_fallback():
    inv_settings.mock_db = False
    engine = MagicMock()
    engine.connect.side_effect = RuntimeError("connection refused")
    with patch.object(inv_db, "engine", engine):
        resp = _client().post("/admin/restock", json={"items": {"item-101": 1000}})
    assert resp.status_code == 500
    assert "connection refused" in resp.json()["detail"]


# --- configuration: ATA_INVENTORY_* only ----------------------------------------------------


def test_legacy_aliases_are_not_read(monkeypatch):
    for var in ("ATA_INVENTORY_DB_USER", "ATA_INVENTORY_MOCK_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DB_USER", "legacy@proj.iam")
    monkeypatch.setenv("MOCK_DB", "true")

    s = Settings()

    assert s.effective_db_user == ""
    assert s.effective_mock_db is False


def test_prefixed_settings_are_read(monkeypatch):
    monkeypatch.setenv("ATA_INVENTORY_DB_USER", "ata-inventory-sa@proj.iam")
    monkeypatch.setenv("ATA_INVENTORY_MOCK_DB", "true")
    monkeypatch.setenv("ATA_INVENTORY_INSTANCE_CONNECTION_NAME", "proj:us-west1:ata-inventory-db")

    s = Settings()

    assert s.effective_db_user == "ata-inventory-sa@proj.iam"
    assert s.effective_mock_db is True
    s.require_db_settings()


def test_invalid_mock_db_value_fails_instead_of_meaning_false(monkeypatch):
    monkeypatch.setenv("ATA_INVENTORY_MOCK_DB", "maybe")
    with pytest.raises(Exception):
        Settings()


def test_no_built_in_project_values(monkeypatch):
    for var in ("ATA_INVENTORY_DB_USER", "ATA_INVENTORY_INSTANCE_CONNECTION_NAME"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.db_user == "" and s.instance_connection_name == ""
    with pytest.raises(RuntimeError) as exc:
        s.require_db_settings()
    assert "ATA_INVENTORY_INSTANCE_CONNECTION_NAME" in str(exc.value)
    assert "ATA_INVENTORY_DB_USER" in str(exc.value)


def test_db_connection_without_settings_fails_loudly():
    inv_settings.mock_db = False
    saved = (inv_settings.db_user, inv_settings.instance_connection_name)
    inv_settings.db_user, inv_settings.instance_connection_name = "", ""
    try:
        with pytest.raises(RuntimeError, match="ATA_INVENTORY_DB_USER"):
            inv_db.get_db_connection()
    finally:
        inv_settings.db_user, inv_settings.instance_connection_name = saved
