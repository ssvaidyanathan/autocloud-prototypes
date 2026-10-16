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
    title="GitHub Actions Deployment Investigator Bridge",
    description="Bridge connecting GitHub Actions webhooks and ServiceNow via Cloud Tasks",
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
def health(settings: BridgeConfig = Depends(get_settings)) -> dict[str, str]:
    """Health check probe."""
    return {"status": "healthy", "service": settings.service_name}


async def _process_webhook(
    source: str,
    request: Request,
    response: Response,
    settings: BridgeConfig,
    dispatcher: TaskDispatcher,
) -> dict[str, Any]:
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
    except ValueError as exc:
        err_msg = str(exc)
        if "ignoring non-failure" in err_msg.lower():
            logger.info("Ignoring non-failure event: %s", exc)
            response.status_code = status.HTTP_200_OK
            return {"status": "ignored", "reason": str(exc)}
        logger.error("Failed to normalize payload to Incident contract: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=f"Payload normalization failed: {exc}",
        ) from exc
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

    # 4. Post initial ack if supported
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


@app.post("/webhook/github")
async def handle_github_webhook(
    request: Request,
    response: Response,
    settings: BridgeConfig = Depends(get_settings),
    dispatcher: TaskDispatcher = Depends(get_task_dispatcher),
) -> dict[str, Any]:
    """Ingest GitHub Actions workflow failure webhook."""
    return await _process_webhook("github", request, response, settings, dispatcher)


@app.post("/webhook/{source}")
async def handle_generic_source_webhook(
    source: str,
    request: Request,
    response: Response,
    settings: BridgeConfig = Depends(get_settings),
    dispatcher: TaskDispatcher = Depends(get_task_dispatcher),
) -> dict[str, Any]:
    """Generic endpoint dispatching to appropriate adapter by source path parameter."""
    return await _process_webhook(source, request, response, settings, dispatcher)


@app.post("/callback/report")
async def handle_callback_report(
    request: Request,
    settings: BridgeConfig = Depends(get_settings),
    dedup: CallbackDedupRegistry = Depends(get_dedup_registry),
) -> dict[str, Any]:
    """Secure endpoint receiving investigation results from Agent with OIDC token auth."""
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

    # 3. Handle reverse flow: when incident comes from GitHub and investigation completed
    if result.incident_source == "github" and result.status == "completed":
        if result.report is None or result.incident is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing report or incident in completed result",
            )

        servicenow_adapter = get_adapter("servicenow", settings)
        sn_ref = servicenow_adapter.create_incident_with_report(result.incident, result.report)
        logger.info(
            "Successfully opened ServiceNow incident %s for GitHub failure", sn_ref.external_key
        )

        if settings.enable_github_writeback:
            try:
                github_adapter = get_adapter("github", settings)
                github_adapter.post_report_link(result.incident, result.report, sn_ref)
            except Exception as exc:
                logger.warning("Failed to post report link to GitHub Check Run: %s", exc)

        return {
            "status": "ok",
            "servicenow_ticket": sn_ref.external_key,
            "incident_id": result.incident_id,
        }

    # Standard forward flow for ServiceNow or other sources
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

    return {"status": "ok"}
