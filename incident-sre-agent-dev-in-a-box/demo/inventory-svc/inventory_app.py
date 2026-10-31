import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field


class JsonLogFormatter(logging.Formatter):
    """Structured JSON log formatter for Cloud Logging and observability."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            message = f"{message}\n{record.exc_text}" if message else record.exc_text
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"

        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload = {
            "severity": record.levelname,
            "message": message,
            "timestamp": timestamp,
            "serviceContext": {"service": self.service},
        }
        return json.dumps(payload)


def setup_logger(name: str = "inventory-svc") -> logging.Logger:
    """Configure and return a structured JSON logger."""
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    if not any(
        isinstance(h, logging.StreamHandler)
        and isinstance(getattr(h, "formatter", None), JsonLogFormatter)
        for h in log.handlers
    ):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonLogFormatter(service=name))
        log.addHandler(handler)
    return log


logger = setup_logger("inventory-svc")


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


class RestockResponse(BaseModel):
    status: str = "ok"
    items: List[InventoryItem]
    restocked_items: List[InventoryItem] = Field(default_factory=list)


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


def replenish_stock(amount: Optional[int] = None) -> Dict[str, int]:
    """Replenish stock for items that have fallen below initial stock levels."""
    global _inventory
    step = amount if amount is not None else int(os.getenv("RESTOCK_AMOUNT", "10"))
    replenished: Dict[str, int] = {}
    for item_id, initial in INITIAL_INVENTORY.items():
        if item_id in _inventory:
            current = _inventory[item_id]["stock"]
            max_stock = initial["stock"]
            if current < max_stock:
                new_stock = min(max_stock, current + step)
                _inventory[item_id]["stock"] = new_stock
                replenished[item_id] = new_stock
                logger.info(
                    "Replenished stock for %s: %d -> %d",
                    item_id,
                    current,
                    new_stock,
                )
        else:
            _inventory[item_id] = dict(initial)
            replenished[item_id] = initial["stock"]
    return replenished


async def restock_loop() -> None:
    """Periodic background task that adds stock to items to prevent depletion."""
    while True:
        try:
            interval = float(os.getenv("RESTOCK_INTERVAL_SECONDS", "30.0"))
            await asyncio.sleep(interval)
            replenish_stock()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Error during background restock cycle: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan context manager for background tasks."""
    logger.info("Starting background restock loop")
    task = asyncio.create_task(restock_loop())
    try:
        yield
    finally:
        logger.info("Stopping background restock loop")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Inventory Service",
    description="Internal inventory reservation and stock service for shop-api",
    version="0.1.0",
    lifespan=lifespan,
)


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
    logger.info(
        "Reserved %d unit(s) of %s (remaining: %d)",
        request.quantity,
        request.item_id,
        item["stock"],
    )

    return ReserveResponse(
        reservation_id=reservation_id,
        item_id=request.item_id,
        quantity=request.quantity,
        status="reserved",
        remaining_stock=item["stock"],
    )


@app.post("/inventory/restock", status_code=status.HTTP_200_OK, response_model=RestockResponse)
def restock_inventory(
    authorization: Optional[str] = Header(default=None),
    x_simulate_auth_failure: Optional[str] = Header(default=None),
) -> RestockResponse:
    """Immediately replenish and reset all inventory items to initial stock levels."""
    _check_auth(authorization, x_simulate_auth_failure)
    reset_inventory()
    items_list = [InventoryItem(**item) for item in _inventory.values()]
    logger.info("Inventory restocked: %d items reset to initial levels", len(items_list))
    return RestockResponse(
        status="ok",
        items=items_list,
        restocked_items=items_list,
    )
