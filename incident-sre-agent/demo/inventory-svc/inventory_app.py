import os
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

app = FastAPI(
    title="Inventory Service",
    description="Internal inventory reservation and stock service for shop-api",
    version="0.1.0",
)


class InventoryItem(BaseModel):
    item_id: str
    name: str
    stock: int
    price: float
    category: str


class ReserveRequest(BaseModel):
    item_id: str
    quantity: int = Field(default=1, gt=0)


class ReserveResponse(BaseModel):
    reservation_id: str
    item_id: str
    quantity: int
    status: str
    remaining_stock: int


# Default inventory catalog in memory
INITIAL_INVENTORY: Dict[str, Dict[str, Any]] = {
    "item-1": {
        "item_id": "item-1",
        "name": "Mechanical Keyboard",
        "stock": 45,
        "price": 129.99,
        "category": "peripherals",
    },
    "item-2": {
        "item_id": "item-2",
        "name": "Ergonomic Mouse",
        "stock": 120,
        "price": 59.99,
        "category": "peripherals",
    },
    "item-3": {
        "item_id": "item-3",
        "name": "USB-C 4K Monitor",
        "stock": 18,
        "price": 349.99,
        "category": "displays",
    },
    "item-4": {
        "item_id": "item-4",
        "name": "Noise Cancelling Headphones",
        "stock": 35,
        "price": 199.99,
        "category": "audio",
    },
    "item-5": {
        "item_id": "item-5",
        "name": "Webcam 1080p",
        "stock": 60,
        "price": 79.99,
        "category": "video",
    },
}

_inventory: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in INITIAL_INVENTORY.items()}


def reset_inventory() -> None:
    """Reset inventory state to initial values (useful for tests)."""
    global _inventory
    _inventory = {k: dict(v) for k, v in INITIAL_INVENTORY.items()}


def _check_auth(
    authorization: Optional[str] = None,
    simulate_failure: Optional[str] = None,
) -> None:
    """Validate Cloud Run IAM invoker authentication if configured."""
    require_auth = os.getenv("REQUIRE_AUTH", "false").lower() in ("true", "1", "yes")
    force_simulate = (
        os.getenv("SIMULATE_AUTH_FAILURE", "false").lower() in ("true", "1", "yes")
        or simulate_failure == "true"
    )

    if force_simulate:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Cloud Run IAM invoker check failed (simulated auth failure)",
        )

    if require_auth:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: missing or invalid authorization header",
            )
        token = authorization.split(" ", 1)[1].strip()
        if not token or token == "invalid":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: invalid IAM token",
            )


@app.get("/health")
def health() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "inventory-svc"}


@app.get("/inventory")
def list_inventory(
    authorization: Optional[str] = Header(default=None),
    x_simulate_auth_failure: Optional[str] = Header(default=None),
) -> List[InventoryItem]:
    """List all available items in inventory."""
    _check_auth(authorization, x_simulate_auth_failure)
    return [InventoryItem(**item) for item in _inventory.values()]


@app.get("/inventory/{item_id}")
def get_inventory(
    item_id: str,
    authorization: Optional[str] = Header(default=None),
    x_simulate_auth_failure: Optional[str] = Header(default=None),
) -> InventoryItem:
    """Retrieve inventory details for a specific item."""
    _check_auth(authorization, x_simulate_auth_failure)
    item = _inventory.get(item_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item {item_id} not found",
        )
    return InventoryItem(**item)


@app.post("/inventory/reserve", status_code=status.HTTP_200_OK)
def reserve_inventory(
    request: ReserveRequest,
    authorization: Optional[str] = Header(default=None),
    x_simulate_auth_failure: Optional[str] = Header(default=None),
) -> ReserveResponse:
    """Reserve inventory for an item purchase."""
    _check_auth(authorization, x_simulate_auth_failure)

    item = _inventory.get(request.item_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item {request.item_id} not found",
        )

    current_stock = item["stock"]
    if current_stock < request.quantity:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Insufficient stock for item {request.item_id}. "
                f"Requested: {request.quantity}, Available: {current_stock}"
            ),
        )

    item["stock"] -= request.quantity
    reservation_id = f"res-{uuid4().hex[:8]}"

    return ReserveResponse(
        reservation_id=reservation_id,
        item_id=request.item_id,
        quantity=request.quantity,
        status="reserved",
        remaining_stock=item["stock"],
    )
