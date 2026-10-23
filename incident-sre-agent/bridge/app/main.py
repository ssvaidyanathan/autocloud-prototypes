import json
import logging
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status

from .adapters import IncidentRef, get_adapter
from .config import BridgeConfig, get_settings
from .contracts.result import InvestigationResult
from .core.auth import verify_oidc_token
from .core.dedup import CallbackDedupRegistry, get_dedup_registry
from .core.tasks import TaskDispatcher

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Incident Bridge",
    description="Bridge connecting ServiceNow, Jira, and webhooks via Cloud Tasks",
    version="0.1.0",
)

# Global dispatcher instance (can be overridden in tests)
_dispatcher: Optional[TaskDispatcher] = None


def get_task_dispatcher(
    settings: BridgeConfig = Depends(get_settings),
) -> TaskDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = TaskDispatcher(config=settings)
    return _dispatcher


def set_task_dispatcher(dispatcher: Optional[TaskDispatcher]) -> None:
    """Helper to set or reset the TaskDispatcher instance (e.g. for testing)."""
    global _dispatcher
    _dispatcher = dispatcher


@app.get("/health")
def health() -> dict[str, str]:
    """Health check probe."""
    return {"status": "healthy", "service": get_settings().service_name}


@app.post("/webhook/{source}")
async def handle_webhook(
    source: str,
    request: Request,
    response: Response,
    settings: BridgeConfig = Depends(get_settings),
    dispatcher: TaskDispatcher = Depends(get_task_dispatcher),
) -> dict[str, Any]:
    """Ingest incident webhook from ticketing system, deduplicate, and enqueue investigation."""
    try:
        adapter = get_adapter(source, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raw_body = await request.body()

    # 1. Verify webhook signature or token
    if not adapter.verify(request, raw_body):
        logger.warning("Inbound webhook verification failed for source '%s'", source)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook authentication failed",
        )

    # 2. Parse payload and normalize to Incident contract
    try:
        json_payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc

    try:
        incident = adapter.to_incident(json_payload)
    except Exception as exc:
        logger.error("Failed to normalize payload to Incident contract: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=f"Payload normalization failed: {exc}",
        ) from exc

    # 3. Enqueue investigation in Cloud Tasks with deterministic deduplication
    effective_callback = (
        settings.callback_url or f"{str(request.base_url).rstrip('/')}/callback/report"
    )
    dispatch_res = dispatcher.dispatch(incident, callback_url=effective_callback)

    if dispatch_res.is_duplicate:
        logger.info(
            "Duplicate incident webhook received for %s (%s). Dropping.",
            incident.external_key,
            dispatch_res.task_id,
        )
        response.status_code = status.HTTP_200_OK
        return {
            "status": "duplicate_ignored",
            "task_id": dispatch_res.task_id,
            "incident_id": incident.id,
            "external_key": incident.external_key,
        }

    # 4. Post ticket acknowledgement (failures logged, non-fatal to webhook response)
    try:
        ref = IncidentRef.from_incident(incident)
        adapter.post_ack(ref)
    except Exception as exc:
        logger.warning(
            "Non-fatal error: failed to post initial acknowledgement to %s: %s",
            incident.external_key,
            exc,
        )

    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "status": "accepted",
        "task_id": dispatch_res.task_id,
        "incident_id": incident.id,
        "external_key": incident.external_key,
    }


@app.post("/callback/report")
async def handle_callback_report(
    request: Request,
    settings: BridgeConfig = Depends(get_settings),
    dedup: CallbackDedupRegistry = Depends(get_dedup_registry),
) -> dict[str, str]:
    """Secure endpoint receiving investigation results from SRE Agent with OIDC token auth."""
    # 1. Verify caller OIDC authentication
    verify_oidc_token(request, settings)

    # 2. Parse and validate InvestigationResult contract
    try:
        data = await request.json()
        result = InvestigationResult.model_validate(data)
    except Exception as exc:
        logger.error("Invalid InvestigationResult callback payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid InvestigationResult payload: {exc}",
        ) from exc

    # Check for duplicate callback report
    if not dedup.check_and_mark(result.incident_id, result.status):
        logger.info(
            "Duplicate callback report received for %s (%s). Dropping.",
            result.incident_id,
            result.status,
        )
        return {
            "status": "duplicate_ignored",
            "incident_id": result.incident_id,
        }

    # 3. Look up adapter and deliver report or failure. If delivery fails, release the
    # dedup key so the agent's retry re-attempts it instead of being dropped as a duplicate.
    try:
        try:
            adapter = get_adapter(result.incident_source, settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        ref = IncidentRef.from_result(result)

        if result.status == "completed":
            if result.report is None:
                raise HTTPException(status_code=400, detail="Missing report for completed result")
            adapter.post_report(ref, result.report)
        elif result.status == "failed":
            if not result.error:
                raise HTTPException(status_code=400, detail="Missing error for failed result")
            adapter.post_failure(ref, result.error)
    except Exception:
        dedup.release(result.incident_id, result.status)
        raise

    return {"status": "ok"}


if __name__ == "__main__":
    import sys

    import uvicorn

    config = get_settings()
    target = "app.main:app" if "bridge" not in sys.modules else "bridge.app.main:app"
    uvicorn.run(target, host="0.0.0.0", port=config.port)
