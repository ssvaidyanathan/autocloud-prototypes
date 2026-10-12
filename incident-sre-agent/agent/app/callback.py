import asyncio
import json
import logging
import os
import sys
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional

import httpx

from .config import AgentConfig, get_settings
from .contracts.result import InvestigationResult

logger = logging.getLogger(__name__)


def _is_local_dev_env() -> bool:
    """Check if environment has explicit local/dev flag set."""
    for var in ("APP_ENV", "ENVIRONMENT"):
        val = os.environ.get(var, "").strip().lower()
        if val in ("local", "dev", "development", "test", "testing"):
            return True
    return False


def _default_token_provider(audience: str) -> str:
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

    if _is_local_dev_env():
        logger.warning(
            "Using fallback mock OIDC token for audience '%s'. "
            "This is allowed only because APP_ENV/ENVIRONMENT is set to local/dev.",
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


class CallbackClient:
    """Client delivering investigation results to the callback URL with OIDC and retries."""

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        token_provider: Optional[Callable[[str], str]] = None,
        max_retries: Optional[int] = None,
        backoff_factor: Optional[float] = None,
        http_client: Optional[httpx.Client] = None,
        async_http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        cfg = config or get_settings()
        self.config = cfg
        self.token_provider = token_provider or _default_token_provider
        self.max_retries = max_retries if max_retries is not None else cfg.callback_max_retries
        self.backoff_factor = (
            backoff_factor if backoff_factor is not None else cfg.callback_backoff_factor
        )
        self.http_client = http_client
        self.async_http_client = async_http_client

    def validate_url(self, callback_url: str) -> None:
        """Validate URL scheme and ensure hostname matches allowed hosts."""
        parsed = urllib.parse.urlparse(callback_url)
        hostname = (parsed.hostname or "").lower()

        if not hostname:
            raise ValueError(f"Invalid callback URL: missing hostname in '{callback_url}'")

        is_local = hostname in ("localhost", "127.0.0.1", "0.0.0.0")

        # Must use HTTPS unless localhost/test
        if not is_local and parsed.scheme != "https":
            raise ValueError(
                f"Invalid callback URL scheme '{parsed.scheme}': "
                "HTTPS is required for non-local endpoints"
            )

        # Hostname allowlist matching
        allowed_patterns = [h.lower().strip() for h in self.config.allowed_callback_hosts]
        matched = False

        for pattern in allowed_patterns:
            if pattern == "*":
                matched = True
                break
            if pattern.startswith("*."):
                suffix = pattern[1:]  # e.g. .run.app
                if hostname.endswith(suffix) and len(hostname) > len(suffix):
                    matched = True
                    break
            elif hostname == pattern:
                matched = True
                break
            elif is_local and pattern in ("localhost", "127.0.0.1"):
                matched = True
                break

        if not matched:
            raise ValueError(
                f"Security violation: callback host '{hostname}' is not in allowed hosts "
                f"{self.config.allowed_callback_hosts}"
            )

    def emit_structured_audit_log(self, result: InvestigationResult) -> Dict[str, Any]:
        """Emit structured log with jsonPayload.type = 'sre_report' for audit & recovery."""
        audit_payload = {
            "type": "sre_report",
            "investigation_id": result.investigation_id,
            "incident_id": result.incident_id,
            "incident_source": result.incident_source,
            "incident_external_id": result.incident_external_id,
            "incident_external_key": result.incident_external_key,
            "status": result.status,
            "result": result.model_dump(mode="json"),
        }
        structured_record = {
            "severity": "INFO",
            "message": f"SRE report audit log for incident {result.incident_external_key}",
            "jsonPayload": audit_payload,
        }

        # Print JSON string directly to stdout for Cloud Logging jsonPayload ingestion
        json_line = json.dumps(structured_record)
        sys.stdout.write(json_line + "\n")
        sys.stdout.flush()

        logger.info("Emitted structured SRE report audit log for %s", result.investigation_id)
        return structured_record

    def deliver(
        self,
        callback_url: str,
        result: InvestigationResult,
    ) -> httpx.Response:
        """Synchronously deliver investigation result with validation, OIDC, and 3x retries."""
        self.validate_url(callback_url)
        self.emit_structured_audit_log(result)

        token = self.token_provider(callback_url)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = result.model_dump_json()

        cb_timeout = httpx.Timeout(
            self.config.callback_read_timeout_seconds,
            connect=self.config.callback_connect_timeout_seconds,
        )
        client = self.http_client or httpx.Client(timeout=cb_timeout)
        close_client = self.http_client is None

        try:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = client.post(callback_url, content=payload, headers=headers)

                    # 4xx client errors fail immediately without retry
                    if 400 <= resp.status_code < 500:
                        resp.raise_for_status()

                    # 5xx server errors trigger retry
                    if resp.status_code >= 500:
                        resp.raise_for_status()

                    return resp

                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                    # If it's a 4xx error, do not retry
                    if (
                        isinstance(exc, httpx.HTTPStatusError)
                        and 400 <= exc.response.status_code < 500
                    ):
                        logger.error(
                            "Callback failed with non-retryable 4xx client error: %s",
                            exc.response.status_code,
                        )
                        raise

                    if attempt < self.max_retries:
                        sleep_time = self.backoff_factor * (2**attempt)
                        logger.warning(
                            "Callback attempt %d failed (%s). Retrying in %.2fs...",
                            attempt + 1,
                            exc,
                            sleep_time,
                        )
                        time.sleep(sleep_time)
                    else:
                        logger.error("Callback failed after %d retries: %s", self.max_retries, exc)
                        raise
            raise RuntimeError("Exhausted retries delivering callback")
        finally:
            if close_client:
                client.close()

    async def deliver_async(
        self,
        callback_url: str,
        result: InvestigationResult,
    ) -> httpx.Response:
        """Asynchronously deliver investigation result with validation, OIDC, and 3x retries."""
        self.validate_url(callback_url)
        self.emit_structured_audit_log(result)

        token = self.token_provider(callback_url)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = result.model_dump_json()

        cb_timeout = httpx.Timeout(
            self.config.callback_read_timeout_seconds,
            connect=self.config.callback_connect_timeout_seconds,
        )
        client = self.async_http_client or httpx.AsyncClient(timeout=cb_timeout)
        close_client = self.async_http_client is None

        try:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = await client.post(callback_url, content=payload, headers=headers)

                    if 400 <= resp.status_code < 500:
                        resp.raise_for_status()

                    if resp.status_code >= 500:
                        resp.raise_for_status()

                    return resp

                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                    if (
                        isinstance(exc, httpx.HTTPStatusError)
                        and 400 <= exc.response.status_code < 500
                    ):
                        logger.error(
                            "Callback failed with non-retryable 4xx client error: %s",
                            exc.response.status_code,
                        )
                        raise

                    if attempt < self.max_retries:
                        sleep_time = self.backoff_factor * (2**attempt)
                        logger.warning(
                            "Async callback attempt %d failed (%s). Retrying in %.2fs...",
                            attempt + 1,
                            exc,
                            sleep_time,
                        )
                        await asyncio.sleep(sleep_time)
                    else:
                        logger.error(
                            "Async callback failed after %d retries: %s", self.max_retries, exc
                        )
                        raise
            raise RuntimeError("Exhausted retries delivering callback")
        finally:
            if close_client:
                await client.aclose()
