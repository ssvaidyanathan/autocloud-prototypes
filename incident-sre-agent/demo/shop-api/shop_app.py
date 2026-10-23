import asyncio
import json
import logging
import os
import random
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

ERROR_EVENT_TYPE = (
    "type.googleapis.com/google.devtools.clouderrorreporting.v1beta1.ReportedErrorEvent"
)
TRACEBACK_HEADER = "Traceback (most recent call last):"


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
        payload: Dict[str, Any] = {
            "severity": record.levelname,
            "message": message,
            "timestamp": timestamp,
            "serviceContext": {"service": self.service},
        }
        if record.levelno >= logging.ERROR:
            # Mark ERROR+ entries as Error Reporting events and expose any Python traceback
            # in stack_trace, since Error Reporting only parses a trace at the start of a field.
            payload["@type"] = ERROR_EVENT_TYPE
            trace_start = message.find(TRACEBACK_HEADER)
            if trace_start != -1:
                payload["stack_trace"] = message[trace_start:]
        return json.dumps(payload)


def setup_logger(name: str = "shop-api") -> logging.Logger:
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


logger = setup_logger("shop-api")

_FAULT_FALLBACK_PATTERN = re.compile(r'"[A-Z]+ /fault(?:/|[ ?]|$)|(?:^|\s)/fault(?:/|\?|\s|$)')


class FaultAccessLogFilter(logging.Filter):
    """Filter out uvicorn access logs for fault-control endpoints (/fault/*).

    Autonomous SRE investigation agents inspect stdout / access logs for anomalies.
    Dropping uvicorn access logs for /fault/* prevents agents from discovering synthetic
    test-harness fault injection / reset calls on stdout while preserving all genuine
    traffic access logs (/catalog, /checkout, /readyz, etc.).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Standard uvicorn access log record.args format:
        # (client_addr, method, full_path, http_version, status_code)
        args = record.args
        if isinstance(args, (tuple, list)) and len(args) >= 3:
            path = args[2]
            if isinstance(path, str) and (
                path == "/fault" or path.startswith("/fault/") or path.startswith("/fault?")
            ):
                return False

        # Fallback check on formatted message or unformatted msg (records without args)
        try:
            msg = record.getMessage()
            if _FAULT_FALLBACK_PATTERN.search(msg):
                return False
        except Exception:
            pass

        return True


def install_fault_access_log_filter() -> None:
    """Install FaultAccessLogFilter on the uvicorn.access logger and its handlers."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, FaultAccessLogFilter) for f in access_logger.filters):
        access_logger.addFilter(FaultAccessLogFilter())
    for handler in access_logger.handlers:
        if not any(isinstance(f, FaultAccessLogFilter) for f in handler.filters):
            handler.addFilter(FaultAccessLogFilter())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan context manager for startup/shutdown tasks."""
    install_fault_access_log_filter()
    yield


app = FastAPI(
    title="Shop API",
    description="Online storefront API service for browsing catalog and processing checkout",
    version="0.1.0",
    lifespan=lifespan,
)

# Also install at import time for early logger setup
install_fault_access_log_filter()


class CatalogItem(BaseModel):
    item_id: str
    name: str
    stock: int
    price: float
    category: str


class CheckoutRequest(BaseModel):
    item_id: str
    quantity: int = Field(default=1, gt=0)
    customer_id: Optional[str] = "cust-guest"


class CheckoutResponse(BaseModel):
    order_id: str
    item_id: str
    quantity: int
    total_price: float
    status: str
    reservation_id: str


class FaultInjectRequest(BaseModel):
    scenario: int = Field(ge=1, le=5, description="Scenario ID (1 to 5)")
    error_rate: Optional[float] = Field(default=0.40, ge=0.0, le=1.0)
    latency_seconds: Optional[float] = Field(default=None, ge=0.0)


class FaultStatusResponse(BaseModel):
    active_scenarios: List[int]
    error_spike: bool
    error_rate: float
    latency_spike: bool
    latency_min: float
    latency_max: float
    missing_env: bool
    memory_leak: bool
    memory_leak_mb: float
    dependency_failure: bool


class FaultState:
    """Manages active fault injection flags and buffers."""

    def __init__(self) -> None:
        self.active_scenarios: List[int] = []
        self.error_spike: bool = False
        self.error_rate: float = 0.40
        self.latency_spike: bool = False
        self.latency_min: float = 3.0
        self.latency_max: float = 8.0
        self.missing_env: bool = False
        self.memory_leak: bool = False
        self.dependency_failure: bool = False
        self.leak_buffer: List[bytes] = []

    def reset(self) -> None:
        self.active_scenarios.clear()
        self.error_spike = False
        self.error_rate = 0.40
        self.latency_spike = False
        self.latency_min = 3.0
        self.latency_max = 8.0
        self.missing_env = False
        self.memory_leak = False
        self.dependency_failure = False
        self.leak_buffer.clear()

    @property
    def memory_leak_mb(self) -> float:
        total_bytes = sum(len(chunk) for chunk in self.leak_buffer)
        return round(total_bytes / (1024 * 1024), 2)


fault_state = FaultState()


def get_inventory_url() -> str:
    return os.getenv("INVENTORY_SERVICE_URL", "http://localhost:8081").rstrip("/")


def get_payment_key() -> Optional[str]:
    # In scenario 3, we simulate missing PAYMENT_GATEWAY_KEY
    if fault_state.missing_env:
        return None
    return os.getenv("PAYMENT_GATEWAY_KEY", "demo-live-payment-key-xyz")


async def _apply_fault_delays() -> None:
    """Apply latency delay if scenario 2 is active."""
    if fault_state.latency_spike:
        delay = random.uniform(fault_state.latency_min, fault_state.latency_max)
        await asyncio.sleep(delay)


def _apply_memory_leak() -> None:
    """Simulate memory leak if scenario 4 is active."""
    if fault_state.memory_leak:
        # Allocate 10MB chunk per request into the unreleased buffer
        chunk = b"X" * (10 * 1024 * 1024)
        fault_state.leak_buffer.append(chunk)


def _get_inventory_auth_headers(inventory_url: Optional[str] = None) -> Dict[str, str]:
    """Retrieve auth headers for calling inventory-svc."""
    if fault_state.dependency_failure:
        # In dependency failure scenario, omit token to trigger authentic 403 Forbidden
        # on Cloud Run, and provide simulation header for local mock testing
        return {"X-Simulate-Auth-Failure": "true"}

    token = os.getenv("INVENTORY_SERVICE_TOKEN")
    if not token:
        target_url = inventory_url or get_inventory_url()
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import id_token

            token = str(id_token.fetch_id_token(Request(), audience=target_url))  # type: ignore[no-untyped-call]
        except Exception as exc:
            if os.getenv("APP_ENV", "").lower() in ("local", "test", "dev") or os.getenv(
                "PYTEST_CURRENT_TEST"
            ):
                token = "valid-invoker-token"
            else:
                logger.warning("Failed to obtain ID token for inventory-svc: %s", exc)
                return {}
    return {"Authorization": f"Bearer {token}"}


# Fallback catalog in case inventory service is unreachable in dev/test
FALLBACK_CATALOG: Dict[str, Dict[str, Any]] = {
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
}


@app.get("/health")
def health() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "shop-api"}


@app.get("/readyz")
def readyz() -> Dict[str, str]:
    """Readiness probe verifying critical environment configuration."""
    payment_key = get_payment_key()
    if not payment_key or payment_key.strip() == "":
        logger.error("Missing required environment variable: PAYMENT_GATEWAY_KEY")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service Unavailable: Missing required environment variable PAYMENT_GATEWAY_KEY",
        )
    return {"status": "ready", "service": "shop-api"}


@app.get("/catalog")
async def get_catalog() -> List[CatalogItem]:
    """List catalog products with real-time stock from inventory-svc."""
    await _apply_fault_delays()
    _apply_memory_leak()

    inventory_url = get_inventory_url()
    headers = _get_inventory_auth_headers(inventory_url)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{inventory_url}/inventory", headers=headers)
            if resp.status_code == 403:
                logger.error("Downstream dependency inventory-svc returned HTTP 403 Forbidden")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        "Downstream dependency inventory-svc returned HTTP 403 Forbidden "
                        "(IAM permission revoked)"
                    ),
                )
            if resp.status_code != 200:
                logger.warning("inventory-svc returned HTTP %d: %s", resp.status_code, resp.text)
                return [CatalogItem(**item) for item in FALLBACK_CATALOG.values()]

            items = resp.json()
            return [CatalogItem(**item) for item in items]
    except httpx.RequestError as exc:
        logger.warning("Could not reach inventory-svc at %s: %s", inventory_url, exc)
        return [CatalogItem(**item) for item in FALLBACK_CATALOG.values()]


@app.get("/catalog/{item_id}")
async def get_catalog_item(item_id: str) -> CatalogItem:
    """Retrieve catalog product details with stock from inventory-svc."""
    await _apply_fault_delays()
    _apply_memory_leak()

    inventory_url = get_inventory_url()
    headers = _get_inventory_auth_headers(inventory_url)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{inventory_url}/inventory/{item_id}", headers=headers)
            if resp.status_code == 403:
                logger.error("Downstream dependency inventory-svc returned HTTP 403 Forbidden")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Downstream dependency inventory-svc returned HTTP 403 Forbidden",
                )
            if resp.status_code == 404:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Item {item_id} not found",
                )
            if resp.status_code == 200:
                return CatalogItem(**resp.json())
    except httpx.RequestError as exc:
        logger.warning("Could not reach inventory-svc at %s: %s", inventory_url, exc)

    fallback = FALLBACK_CATALOG.get(item_id)
    if not fallback:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item {item_id} not found",
        )
    return CatalogItem(**fallback)


@app.post("/checkout", status_code=status.HTTP_200_OK)
async def checkout(request: CheckoutRequest) -> CheckoutResponse:
    """Process customer purchase order and reserve inventory."""
    await _apply_fault_delays()
    _apply_memory_leak()

    # Scenario 3: Bad release / missing env key check
    payment_key = get_payment_key()
    if not payment_key or payment_key.strip() == "":
        logger.error("Cannot process checkout; missing PAYMENT_GATEWAY_KEY")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment gateway misconfigured: missing PAYMENT_GATEWAY_KEY",
        )

    # Scenario 1: Error spike injection (~40% failure rate)
    if fault_state.error_spike:
        if random.random() < fault_state.error_rate:
            err_msg = (
                "DatabaseConnectionError: Connection pool exhausted at shop-db:5432 "
                "(active: 100, max: 100). Failed to acquire lock for checkout transaction."
            )
            fake_stack = (
                "Traceback (most recent call last):\n"
                '  File "/app/shop_app.py", line 210, in checkout\n'
                "    db_conn = await pool.acquire(timeout=5.0)\n"
                "asyncpg.exceptions.TooManyConnectionsError: connection limit reached"
            )
            logger.error("%s\n%s", err_msg, fake_stack)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Internal Server Error: {err_msg}",
            )

    inventory_url = get_inventory_url()
    headers = _get_inventory_auth_headers(inventory_url)
    reservation_id = f"res-{uuid4().hex[:8]}"
    item_price = 49.99

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{inventory_url}/inventory/reserve",
                json={"item_id": request.item_id, "quantity": request.quantity},
                headers=headers,
            )
            if resp.status_code == 403:
                logger.error("Downstream dependency inventory-svc returned HTTP 403 Forbidden")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        "Failed to reserve inventory: downstream service returned 403 Forbidden "
                        "(IAM permission revoked)"
                    ),
                )
            if resp.status_code == 404:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Item {request.item_id} not found",
                )
            if resp.status_code == 409:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=resp.json().get("detail", "Out of stock"),
                )
            if resp.status_code == 200:
                res_data = resp.json()
                reservation_id = res_data.get("reservation_id", reservation_id)
                # Fetch price if available
                item_info = FALLBACK_CATALOG.get(request.item_id)
                if item_info:
                    item_price = float(item_info["price"])
    except httpx.RequestError as exc:
        logger.warning("Inventory service unreachable; falling back to local reservation: %s", exc)
        item_info = FALLBACK_CATALOG.get(request.item_id)
        if item_info:
            item_price = float(item_info["price"])

    order_id = f"ord-{uuid4().hex[:8]}"
    total_price = round(item_price * request.quantity, 2)
    logger.info("Checkout successful: order_id=%s, total=$%.2f", order_id, total_price)

    return CheckoutResponse(
        order_id=order_id,
        item_id=request.item_id,
        quantity=request.quantity,
        total_price=total_price,
        status="confirmed",
        reservation_id=reservation_id,
    )


# Fault injection control endpoints
@app.post("/fault/inject")
def inject_fault(req: FaultInjectRequest) -> Dict[str, Any]:
    """Inject one of the 5 predefined failure scenarios."""
    scenario = req.scenario
    if scenario not in fault_state.active_scenarios:
        fault_state.active_scenarios.append(scenario)

    details = ""
    if scenario == 1:
        fault_state.error_spike = True
        fault_state.error_rate = req.error_rate if req.error_rate is not None else 0.40
        details = (
            f"Scenario 1: Checkout error spike enabled (rate={fault_state.error_rate * 100:.0f}%)"
        )
    elif scenario == 2:
        fault_state.latency_spike = True
        if req.latency_seconds is not None:
            fault_state.latency_min = req.latency_seconds
            fault_state.latency_max = req.latency_seconds
        else:
            fault_state.latency_min = 3.0
            fault_state.latency_max = 8.0
        details = (
            f"Scenario 2: Artificial latency enabled "
            f"({fault_state.latency_min}s-{fault_state.latency_max}s)"
        )
    elif scenario == 3:
        fault_state.missing_env = True
        details = (
            "Scenario 3: Bad release enabled (PAYMENT_GATEWAY_KEY missing, /readyz returns 503)"
        )
    elif scenario == 4:
        fault_state.memory_leak = True
        details = "Scenario 4: Memory leak enabled (allocates 10MB per request towards OOM)"
    elif scenario == 5:
        fault_state.dependency_failure = True
        details = (
            "Scenario 5: Dependency failure enabled (inventory-svc IAM token dropped/forbidden)"
        )

    return {
        "status": "fault_injected",
        "scenario": scenario,
        "details": details,
        "active_scenarios": fault_state.active_scenarios,
    }


@app.post("/fault/reset")
def reset_faults() -> Dict[str, Any]:
    """Reset all injected faults to normal operation."""
    fault_state.reset()
    return {
        "status": "reset",
        "active_scenarios": [],
        "message": "Baseline restored successfully",
    }


@app.get("/fault/status")
def get_fault_status() -> FaultStatusResponse:
    """Query current status of fault injection."""
    return FaultStatusResponse(
        active_scenarios=list(fault_state.active_scenarios),
        error_spike=fault_state.error_spike,
        error_rate=fault_state.error_rate,
        latency_spike=fault_state.latency_spike,
        latency_min=fault_state.latency_min,
        latency_max=fault_state.latency_max,
        missing_env=fault_state.missing_env,
        memory_leak=fault_state.memory_leak,
        memory_leak_mb=fault_state.memory_leak_mb,
        dependency_failure=fault_state.dependency_failure,
    )
