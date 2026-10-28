"""FastAPI Application for ata-orders-api."""

from typing import Optional
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .config import settings
from .middleware import AdminLogSuppressionMiddleware, install_admin_log_filter
from .orders import (
    create_order,
    get_order,
    set_latency_fault,
    set_5xx_fault,
    reset_faults,
)

# Install log suppression filter immediately upon import
install_admin_log_filter()

app = FastAPI(
    title="ATA Orders API",
    description="Demo microservice for orders processing calling inventory API via Google ID token",
    version="1.0.0",
)

# Suppress /admin/* access logs
app.add_middleware(AdminLogSuppressionMiddleware)


class CreateOrderRequest(BaseModel):
    item_id: str = Field(..., description="Inventory item ID to order")
    quantity: int = Field(1, ge=1, description="Quantity to purchase")
    customer_id: Optional[str] = Field(None, description="Customer identifier")


class LatencyFaultRequest(BaseModel):
    delay_ms: int = Field(6500, ge=0, description="Injected delay in milliseconds")


class FiveXxFaultRequest(BaseModel):
    rate: float = Field(0.85, ge=0.0, le=1.0, description="Error rate between 0.0 and 1.0")


@app.get("/health")
def health_check():
    """Standard health check returning status ok."""
    return {"status": "ok"}


@app.post("/orders")
def post_order(req: CreateOrderRequest):
    """Creates an order by calling ata-inventory-api."""
    try:
        res = create_order(req.item_id, req.quantity, req.customer_id)
        if not res.get("success"):
            status_code = res.get("status_code", status.HTTP_400_BAD_REQUEST)
            raise HTTPException(status_code=status_code, detail=res.get("error"))
        return res.get("order")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Order processing error: {str(e)}",
        )


@app.get("/orders/{order_id}")
def retrieve_order(order_id: str):
    """Retrieves order status by order ID."""
    try:
        order = get_order(order_id)
        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Order {order_id} not found",
            )
        return order
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving order: {str(e)}",
        )


# ==========================================
# Admin / Fault Injection Endpoints (Suppressed)
# ==========================================

@app.post("/admin/fault/latency")
def admin_fault_latency(req: Optional[LatencyFaultRequest] = None):
    """Fault injection introducing artificial delay."""
    delay = req.delay_ms if req else 6500
    set_latency_fault(delay)
    return {"status": "latency_fault_configured", "delay_ms": delay}


@app.post("/admin/fault/5xx")
def admin_fault_5xx(req: Optional[FiveXxFaultRequest] = None):
    """Fault injection returning 500 Internal Server Error."""
    rate = req.rate if req else 0.85
    set_5xx_fault(rate)
    return {"status": "5xx_fault_configured", "rate": rate}


@app.post("/admin/reset")
@app.post("/admin/fault/reset")
def admin_reset():
    """Clears all configured faults and resets orders."""
    return reset_faults()


@app.post("/admin/fault/inject")
def admin_fault_inject(req: dict):
    """Generic fault injection endpoint for orders API."""
    fault_type = req.get("type", "500")
    if str(fault_type) in ("500", "5xx"):
        rate = float(req.get("rate", 1.0))
        set_5xx_fault(rate)
        return {"status": "5xx_fault_configured", "rate": rate}
    elif str(fault_type) == "latency":
        delay = int(req.get("delay_ms", 6500))
        set_latency_fault(delay)
        return {"status": "latency_fault_configured", "delay_ms": delay}
    return {"status": "unknown_fault_type", "type": fault_type}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=settings.port)
