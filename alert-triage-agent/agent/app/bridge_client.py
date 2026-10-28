"""Agent client for calling ata-bridge instruction endpoints."""

import logging
import time
from typing import Any, Optional, Union
import httpx

from .config import AgentConfig, get_agent_settings
from .contracts.instructions import (
    ActionStatusResponse,
    CreateTicketRequest,
    ResolveTicketRequest,
    TicketResponse,
    UpdateReportRequest,
    UpdateTicketFieldsRequest,
    WorkNoteRequest,
)

logger = logging.getLogger(__name__)


_default_http_client: Optional[Any] = None


def set_default_http_client(client: Optional[Any]) -> None:
    """Set global default HTTP client for BridgeClient (e.g. TestClient in tests)."""
    global _default_http_client
    _default_http_client = client


def get_default_http_client() -> Optional[Any]:
    """Get global default HTTP client for BridgeClient."""
    return _default_http_client


def fetch_id_token(audience: str) -> str:
    """Google-signed ID token of the runtime service account for ``audience``.

    On Cloud Run the token comes from the metadata server. Raises on failure; callers decide
    whether a missing token is fatal. Used for ata-bridge and ata-tools calls.
    """
    import google.auth.transport.requests
    import google.oauth2.id_token

    req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(req, audience.rstrip("/"))


def _get_auth_headers(bridge_url: str, config: AgentConfig) -> dict[str, str]:
    """Generate authorization headers for bridge instruction calls."""
    if config.is_local:
        return {"Authorization": "Bearer valid-agent-token"}

    try:
        token = fetch_id_token(bridge_url)
        return {"Authorization": f"Bearer {token}"}
    except Exception as exc:
        logger.warning("Failed to fetch Google ID token for bridge instruction: %s", exc)
        return {}


def _execute_instruction_request(
    method: str,
    bridge_url: str,
    endpoint_path: str,
    payload: Optional[dict[str, Any]] = None,
    config: Optional[AgentConfig] = None,
    http_client: Optional[httpx.Client] = None,
    max_retries: int = 3,
    backoff_factor: float = 0.5,
) -> dict[str, Any]:
    """Execute an HTTP instruction request with transient retry logic for 5xx and network errors."""
    cfg = config or get_agent_settings()
    endpoint = f"{bridge_url.rstrip('/')}/{endpoint_path.lstrip('/')}"
    headers = {
        "Content-Type": "application/json",
        **_get_auth_headers(bridge_url, cfg),
    }

    last_exc: Optional[Exception] = None
    client_candidate = http_client or _default_http_client
    own_client = httpx.Client(timeout=30.0) if client_candidate is None else None
    client_to_use = client_candidate if client_candidate is not None else own_client

    try:
        for attempt in range(max_retries + 1):
            try:
                is_test_client = "TestClient" in type(client_to_use).__name__
                if method.upper() == "GET":
                    if is_test_client:
                        resp = client_to_use.get(endpoint, headers=headers)
                    else:
                        resp = client_to_use.get(endpoint, headers=headers, timeout=30.0)
                else:
                    if is_test_client:
                        resp = client_to_use.post(endpoint, json=payload, headers=headers)
                    else:
                        resp = client_to_use.post(endpoint, json=payload, headers=headers, timeout=30.0)

                if resp.status_code >= 500:
                    if attempt < max_retries:
                        sleep_sec = backoff_factor * (2 ** attempt)
                        logger.warning(
                            "Bridge instruction 5xx error (status=%s, attempt=%s/%s). Retrying in %ss...",
                            resp.status_code,
                            attempt + 1,
                            max_retries,
                            sleep_sec,
                        )
                        time.sleep(sleep_sec)
                        continue
                    resp.raise_for_status()

                # For 4xx errors, raise immediately without retry
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, dict) else {"data": data}

            except (httpx.TimeoutException, httpx.NetworkError) as net_err:
                last_exc = net_err
                if attempt < max_retries:
                    sleep_sec = backoff_factor * (2 ** attempt)
                    logger.warning(
                        "Bridge instruction network error (%s, attempt=%s/%s). Retrying in %ss...",
                        net_err,
                        attempt + 1,
                        max_retries,
                        sleep_sec,
                    )
                    time.sleep(sleep_sec)
                    continue
                raise
            except httpx.HTTPStatusError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    sleep_sec = backoff_factor * (2 ** attempt)
                    logger.warning(
                        "Bridge instruction error (%s, attempt=%s/%s). Retrying in %ss...",
                        exc,
                        attempt + 1,
                        max_retries,
                        sleep_sec,
                    )
                    time.sleep(sleep_sec)
                    continue
                raise

    finally:
        if own_client is not None:
            own_client.close()

    if last_exc:
        raise last_exc
    raise RuntimeError(f"Instruction request to {endpoint} failed after retries")


class BridgeClient:
    """Client for calling ata-bridge instruction endpoints."""

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        http_client: Optional[httpx.Client] = None,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
    ) -> None:
        self.config = config
        self.http_client = http_client or _default_http_client
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    def create_ticket(
        self,
        bridge_url: str,
        request: CreateTicketRequest,
        config: Optional[AgentConfig] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> TicketResponse:
        """Call POST /instructions/ticket/create to mint a ServiceNow incident."""
        cfg = config or self.config
        h_client = http_client or self.http_client
        data = _execute_instruction_request(
            method="POST",
            bridge_url=bridge_url,
            endpoint_path="/instructions/ticket/create",
            payload=request.model_dump(mode="json"),
            config=cfg,
            http_client=h_client,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        )
        return TicketResponse.model_validate(data)

    def append_work_note(
        self,
        bridge_url: str,
        request: WorkNoteRequest,
        config: Optional[AgentConfig] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> ActionStatusResponse:
        """Call POST /instructions/ticket/work-note to append a work note or journal entry."""
        cfg = config or self.config
        h_client = http_client or self.http_client
        data = _execute_instruction_request(
            method="POST",
            bridge_url=bridge_url,
            endpoint_path="/instructions/ticket/work-note",
            payload=request.model_dump(mode="json"),
            config=cfg,
            http_client=h_client,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        )
        return ActionStatusResponse.model_validate(data)

    def update_report(
        self,
        bridge_url: str,
        request: UpdateReportRequest,
        config: Optional[AgentConfig] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> TicketResponse:
        """Call POST /instructions/ticket/update-report to refresh ticket description and diagnostic report."""
        cfg = config or self.config
        h_client = http_client or self.http_client
        data = _execute_instruction_request(
            method="POST",
            bridge_url=bridge_url,
            endpoint_path="/instructions/ticket/update-report",
            payload=request.model_dump(mode="json"),
            config=cfg,
            http_client=h_client,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        )
        return TicketResponse.model_validate(data)

    def update_ticket_fields(
        self,
        bridge_url: str,
        request: UpdateTicketFieldsRequest,
        config: Optional[AgentConfig] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> ActionStatusResponse:
        """Call POST /instructions/ticket/update-fields to update the ticket title / raise its priority."""
        cfg = config or self.config
        h_client = http_client or self.http_client
        data = _execute_instruction_request(
            method="POST",
            bridge_url=bridge_url,
            endpoint_path="/instructions/ticket/update-fields",
            payload=request.model_dump(mode="json"),
            config=cfg,
            http_client=h_client,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        )
        return ActionStatusResponse.model_validate(data)

    def resolve_ticket(
        self,
        bridge_url: str,
        request: ResolveTicketRequest,
        config: Optional[AgentConfig] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> ActionStatusResponse:
        """Call POST /instructions/ticket/resolve to resolve an incident."""
        cfg = config or self.config
        h_client = http_client or self.http_client
        data = _execute_instruction_request(
            method="POST",
            bridge_url=bridge_url,
            endpoint_path="/instructions/ticket/resolve",
            payload=request.model_dump(mode="json"),
            config=cfg,
            http_client=h_client,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        )
        return ActionStatusResponse.model_validate(data)

    def query_ticket(
        self,
        bridge_url: str,
        group_id: str,
        config: Optional[AgentConfig] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> TicketResponse:
        """Call GET /instructions/ticket/query/{group_id} to query an existing ticket."""
        cfg = config or self.config
        h_client = http_client or self.http_client
        data = _execute_instruction_request(
            method="GET",
            bridge_url=bridge_url,
            endpoint_path=f"/instructions/ticket/query/{group_id}",
            payload=None,
            config=cfg,
            http_client=h_client,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        )
        return TicketResponse.model_validate(data)


# Module-level convenience functions
def create_ticket(
    bridge_url: str,
    request: CreateTicketRequest,
    config: Optional[AgentConfig] = None,
    http_client: Optional[httpx.Client] = None,
    max_retries: int = 3,
    backoff_factor: float = 0.5,
) -> TicketResponse:
    client = BridgeClient(config=config, http_client=http_client, max_retries=max_retries, backoff_factor=backoff_factor)
    return client.create_ticket(bridge_url=bridge_url, request=request)


def append_work_note(
    bridge_url: str,
    request: WorkNoteRequest,
    config: Optional[AgentConfig] = None,
    http_client: Optional[httpx.Client] = None,
    max_retries: int = 3,
    backoff_factor: float = 0.5,
) -> ActionStatusResponse:
    client = BridgeClient(config=config, http_client=http_client, max_retries=max_retries, backoff_factor=backoff_factor)
    return client.append_work_note(bridge_url=bridge_url, request=request)


def update_report(
    bridge_url: str,
    request: UpdateReportRequest,
    config: Optional[AgentConfig] = None,
    http_client: Optional[httpx.Client] = None,
    max_retries: int = 3,
    backoff_factor: float = 0.5,
) -> TicketResponse:
    client = BridgeClient(config=config, http_client=http_client, max_retries=max_retries, backoff_factor=backoff_factor)
    return client.update_report(bridge_url=bridge_url, request=request)


def resolve_ticket(
    bridge_url: str,
    request: ResolveTicketRequest,
    config: Optional[AgentConfig] = None,
    http_client: Optional[httpx.Client] = None,
    max_retries: int = 3,
    backoff_factor: float = 0.5,
) -> ActionStatusResponse:
    client = BridgeClient(config=config, http_client=http_client, max_retries=max_retries, backoff_factor=backoff_factor)
    return client.resolve_ticket(bridge_url=bridge_url, request=request)


def query_ticket(
    bridge_url: str,
    group_id: str,
    config: Optional[AgentConfig] = None,
    http_client: Optional[httpx.Client] = None,
    max_retries: int = 3,
    backoff_factor: float = 0.5,
) -> TicketResponse:
    client = BridgeClient(config=config, http_client=http_client, max_retries=max_retries, backoff_factor=backoff_factor)
    return client.query_ticket(bridge_url=bridge_url, group_id=group_id)
