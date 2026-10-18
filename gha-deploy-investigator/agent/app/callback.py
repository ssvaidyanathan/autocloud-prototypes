import logging
import os
import urllib.parse
from typing import Callable, Optional

import httpx

from .config import AgentConfig, get_settings
from .contracts.result import InvestigationResult

logger = logging.getLogger(__name__)


def _is_local_dev_env(config: Optional[AgentConfig] = None) -> bool:
    """Check if environment has explicit local/dev flag set."""
    if config and getattr(config, "is_local", False):
        return True
    for var in ("APP_ENV", "ENVIRONMENT"):
        val = os.environ.get(var, "").strip().lower()
        if val in ("local", "dev", "development", "test", "testing"):
            return True
    return False


def _default_token_provider(audience: str, config: Optional[AgentConfig] = None) -> str:
    """Fetch OIDC ID token from Google Cloud metadata server with local fallback."""
    url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        f"service-accounts/default/identity?audience={urllib.parse.quote(audience, safe='')}"
    )
    headers = {"Metadata-Flavor": "Google"}
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.text.strip()
            logger.debug(
                "Google Cloud metadata server token request returned status %d: %s",
                resp.status_code,
                resp.text,
            )
    except Exception as exc:
        logger.debug("Google Cloud metadata server token request failed: %s", exc)

    if _is_local_dev_env(config):
        logger.debug(
            "Using fallback mock OIDC token for audience '%s' in local/dev environment.",
            audience,
        )
        return "mock-oidc-token-for-testing"

    logger.error(
        "Failed to obtain OIDC token from Google Cloud metadata server for audience '%s'. "
        "Production environment does not allow fallback mock token.",
        audience,
    )
    raise RuntimeError(
        f"Failed to fetch OIDC ID token for audience '{audience}' from metadata server"
    )


def send_callback_report(
    result: InvestigationResult,
    callback_url: str,
    client: Optional[httpx.Client] = None,
    config: Optional[AgentConfig] = None,
    token_provider: Optional[Callable[[str], str]] = None,
) -> bool:
    """Send completed InvestigationResult payload to Bridge callback endpoint."""
    cfg = config or get_settings()
    payload = result.model_dump(mode="json")

    provider = token_provider or (lambda aud: _default_token_provider(aud, cfg))
    token = provider(callback_url)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    owns_client = client is None
    c = client or httpx.Client(timeout=httpx.Timeout(75.0, connect=10.0))
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
