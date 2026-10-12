import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Ensure demo packages are discoverable on sys.path
BASE_DIR = Path(__file__).resolve().parents[2]
INVENTORY_SVC_DIR = BASE_DIR / "demo" / "inventory-svc"
SHOP_API_DIR = BASE_DIR / "demo" / "shop-api"
LOADGEN_DIR = BASE_DIR / "demo" / "loadgen"
SCRIPTS_DIR = BASE_DIR / "demo" / "scripts"

for p in [str(INVENTORY_SVC_DIR), str(SHOP_API_DIR), str(LOADGEN_DIR), str(SCRIPTS_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from inventory_app import app as inventory_app  # noqa: E402
from inventory_app import reset_inventory  # noqa: E402
from shop_app import app as shop_app  # noqa: E402
from shop_app import fault_state  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset service in-memory state and fault injection before and after every test."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(
        "google.oauth2.id_token.fetch_id_token",
        lambda *args, **kwargs: "valid-invoker-token",
    )
    reset_inventory()
    fault_state.reset()
    yield
    reset_inventory()
    fault_state.reset()


@pytest.fixture
def inventory_client() -> Iterator[TestClient]:
    with TestClient(inventory_app) as client:
        yield client


@pytest.fixture
def shop_client() -> Iterator[TestClient]:
    with TestClient(shop_app) as client:
        yield client
