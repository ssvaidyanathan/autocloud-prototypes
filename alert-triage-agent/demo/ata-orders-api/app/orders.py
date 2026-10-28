"""Order management and downstream inventory calling with Google ID token auth."""

import time
import uuid
import random
import logging
from typing import Dict, Any, Optional
import httpx

from .config import settings

logger = logging.getLogger(__name__)

# In-memory orders store
ORDERS_STORE: Dict[str, Dict[str, Any]] = {}

# Fault state
FAULT_STATE = {
    "error_5xx": False,
    "error_rate": 0.0,
    "latency_delay_ms": 0,
}


def get_google_id_token(audience: str) -> Optional[str]:
    """Obtains a Google OIDC ID token for authenticating to another Cloud Run service."""
    # 1. Directly query metadata server if on Cloud Run
    try:
        url = f"http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience={audience}"
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(url, headers={"Metadata-Flavor": "Google"})
            if resp.status_code == 200:
                return resp.text.strip()
    except Exception:
        pass

    # 2. Fallback to google-auth id_token
    try:
        import google.auth
        import google.auth.transport.requests
        from google.oauth2 import id_token

        auth_req = google.auth.transport.requests.Request()
        token = id_token.fetch_id_token(auth_req, audience)
        return token
    except Exception as e:
        logger.error(f"Could not fetch Google ID token: {e}")
        return None


def check_and_apply_faults():
    """Applies active latency delay or raises 500 error."""
    if FAULT_STATE["latency_delay_ms"] > 0:
        time.sleep(FAULT_STATE["latency_delay_ms"] / 1000.0)

    if FAULT_STATE["error_5xx"]:
        if random.random() <= FAULT_STATE["error_rate"]:
            raise RuntimeError("Injected Orders API 5xx internal fault")


def create_order(item_id: str, quantity: int, customer_id: Optional[str] = None) -> Dict[str, Any]:
    """Creates an order by reserving inventory on ata-inventory-api."""
    check_and_apply_faults()

    target_url = f"{settings.inventory_service_url.rstrip('/')}/items/reserve"
    headers = {"Content-Type": "application/json"}

    # Attach Google ID token if communicating with Cloud Run
    token = get_google_id_token(settings.inventory_service_url)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                target_url,
                json={"item_id": item_id, "quantity": quantity},
                headers=headers,
            )
            if resp.status_code == 400:
                return {
                    "success": False,
                    "status_code": 400,
                    "error": resp.json().get("detail", "Stock reservation failed"),
                }
            elif resp.status_code >= 500:
                return {
                    "success": False,
                    "status_code": 502,
                    "error": f"Downstream inventory service error: {resp.text}",
                }
            resp.raise_for_status()
            reserve_data = resp.json()
    except httpx.HTTPError as e:
        logger.error(f"Failed to call inventory service at {target_url}: {e}")
        return {
            "success": False,
            "status_code": 502,
            "error": f"Failed to reach inventory service: {str(e)}",
        }

    order_id = f"ord-{uuid.uuid4().hex[:8]}"
    order = {
        "order_id": order_id,
        "item_id": item_id,
        "quantity": quantity,
        "customer_id": customer_id or "anonymous",
        "status": "confirmed",
        "reservation": reserve_data,
        "created_at": time.time(),
    }
    ORDERS_STORE[order_id] = order
    return {"success": True, "order": order}


def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    check_and_apply_faults()
    return ORDERS_STORE.get(order_id)


def set_latency_fault(delay_ms: int):
    FAULT_STATE["latency_delay_ms"] = delay_ms


def set_5xx_fault(rate: float):
    FAULT_STATE["error_5xx"] = True
    FAULT_STATE["error_rate"] = rate


def reset_faults():
    FAULT_STATE["error_5xx"] = False
    FAULT_STATE["error_rate"] = 0.0
    FAULT_STATE["latency_delay_ms"] = 0
    ORDERS_STORE.clear()
    return {"status": "reset", "faults_cleared": True}
