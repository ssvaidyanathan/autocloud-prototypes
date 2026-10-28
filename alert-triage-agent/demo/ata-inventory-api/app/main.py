"""FastAPI Application for ata-inventory-api."""

from typing import Dict, Optional
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .config import settings
from .middleware import AdminLogSuppressionMiddleware, install_admin_log_filter
from .db import (
    list_items,
    reserve_item_stock,
    restock_items,
    UnknownItemsError,
    leak_connections,
    reset_connections_and_faults,
    set_fault,
)

# Install log suppression filter immediately upon import
install_admin_log_filter()

app = FastAPI(
    title="ATA Inventory API",
    description="Demo microservice for inventory stock management with Cloud SQL IAM auth",
    version="1.0.0",
)

# Add custom ASGI middleware to suppress /admin/* access logs
app.add_middleware(AdminLogSuppressionMiddleware)


class ReserveRequest(BaseModel):
    item_id: str = Field(..., description="Item ID to reserve")
    quantity: int = Field(1, ge=1, description="Quantity of stock to reserve")


class LeakRequest(BaseModel):
    count: int = Field(10, ge=1, le=100, description="Number of connections to leak")


class FaultInjectRequest(BaseModel):
    type: str = Field("500", description="Fault type: '500', '5xx', or 'latency'")
    rate: float = Field(1.0, ge=0.0, le=1.0, description="Error rate between 0.0 and 1.0")
    delay_ms: int = Field(0, ge=0, description="Delay in milliseconds for latency fault")


@app.get("/health")
def health_check():
    """Standard health check returning status ok."""
    return {"status": "ok"}


@app.get("/items")
def get_items():
    """List inventory items with stock levels."""
    try:
        return list_items()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database query error: {str(e)}"
        )


@app.post("/items/reserve")
def reserve_item(req: ReserveRequest):
    """Reserve stock for an order."""
    try:
        result = reserve_item_stock(req.item_id, req.quantity)
        if not result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result.get("error", "Stock reservation failed"),
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stock reservation internal error: {str(e)}"
        )


class RestockRequest(BaseModel):
    items: Dict[str, int] = Field(
        ..., min_length=1,
        description="Item id -> absolute stock to set (>= 0). Every id must exist.",
    )


class LockTableRequest(BaseModel):
    duration_seconds: int = Field(180, ge=1, le=600, description="Duration in seconds to hold table lock")


# ==========================================
# Admin / Fault Injection Endpoints (Suppressed)
# ==========================================

@app.get("/admin/leak-connections")
def admin_leak_connections_probe():
    """Health probe for leak-connections endpoint."""
    return {"status": "ok", "endpoint": "/admin/leak-connections"}


@app.post("/admin/leak-connections")
def admin_leak_connections(req: Optional[LeakRequest] = None):
    """Fault injection endpoint leaking DB connections without closing them."""
    count = req.count if req else 10
    return leak_connections(count)


@app.get("/admin/reset")
@app.get("/admin/fault/reset")
def admin_reset_probe():
    """Health probe for reset endpoint."""
    return {"status": "ok", "endpoint": "/admin/reset"}


@app.post("/admin/reset")
@app.post("/admin/fault/reset")
def admin_reset():
    """Reset connection pools, close locked/leaked connections, and clear faults."""
    return reset_connections_and_faults()


@app.get("/admin/restock")
def admin_restock_probe():
    """Health probe for the restock endpoint."""
    return {"status": "ok", "endpoint": "/admin/restock"}


@app.post("/admin/restock")
def admin_restock(req: RestockRequest):
    """Set demo item stock to absolute values (idempotent; used by reset-scenarios.sh).

    400 names unknown item ids (nothing is changed); 500 on a database error.
    """
    negative = sorted(item_id for item_id, stock in req.items.items() if stock < 0)
    if negative:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "negative stock", "items": negative},
        )
    try:
        items = restock_items(req.items)
    except UnknownItemsError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown items", "missing": e.missing},
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Restock error: {str(e)}",
        )
    return {"status": "restocked", "items": items}


@app.get("/admin/lock-table")
def admin_lock_table_probe():
    """Health probe for lock-table endpoint."""
    return {"status": "ok", "endpoint": "/admin/lock-table"}


@app.post("/admin/lock-table")
def admin_lock_table(
    duration_seconds: Optional[int] = None,
    req: Optional[LockTableRequest] = None,
):
    """Fault injection endpoint holding row/table locks (Scenario S7)."""
    from .db import hold_table_lock
    hold_duration = 180
    if duration_seconds is not None:
        hold_duration = duration_seconds
    elif req is not None and req.duration_seconds is not None:
        hold_duration = req.duration_seconds
    return hold_table_lock(duration_seconds=hold_duration)


@app.get("/admin/fault/inject")
def admin_fault_inject_probe():
    """Health probe for admin fault inject endpoint."""
    return {"status": "ok", "endpoint": "/admin/fault/inject"}


@app.post("/admin/fault/inject")
def admin_fault_inject(req: FaultInjectRequest):
    """Fault injection endpoint for scenario S1 (500) and S2 (latency)."""
    set_fault(req.type, rate=req.rate, delay_ms=req.delay_ms)
    return {
        "status": "fault_configured",
        "type": req.type,
        "rate": req.rate,
        "delay_ms": req.delay_ms,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=settings.port)
