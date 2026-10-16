import logging
from typing import Optional

import httpx

from .config import AgentConfig
from .contracts.result import InvestigationResult

logger = logging.getLogger(__name__)


def send_callback_report(
    result: InvestigationResult,
    callback_url: str,
    client: Optional[httpx.Client] = None,
    config: Optional[AgentConfig] = None,
) -> bool:
    """Send completed InvestigationResult payload to Bridge callback endpoint."""
    payload = result.model_dump(mode="json")

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer mock-oidc-token-for-testing",
    }

    owns_client = client is None
    c = client or httpx.Client(timeout=30.0)
    try:
        resp = c.post(callback_url, json=payload, headers=headers)
        resp.raise_for_status()
        logger.info(
            "Successfully delivered callback report for incident %s to %s",
            result.incident_id,
            callback_url,
        )
        return True
    except Exception as exc:
        logger.error("Failed to deliver callback report to %s: %s", callback_url, exc)
        raise
    finally:
        if owns_client:
            c.close()


__all__ = [
    "send_callback_report",
]
