import json
import logging
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from .adapters import IncidentRef, get_adapter
from .adapters.jira import JiraAdapter
from .config import BridgeConfig, get_settings
from .contracts.result import InvestigationResult
from .core.auth import fetch_oidc_token, verify_oidc_token
from .core.dedup import CallbackDedupRegistry, get_dedup_registry
from .core.metadata import TicketMetadataRegistry, get_ticket_registry
from .core.tasks import TaskDispatcher
from .templates.ticket_template import render_ticket_description

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Incident Bridge",
    description="Bridge connecting ServiceNow, Jira, and webhooks via Cloud Tasks",
    version="0.1.0",
)

# Global dispatcher instance (can be overridden in tests)
_dispatcher: Optional[TaskDispatcher] = None


class JiraIssueCreateRequest(BaseModel):
    """Payload for creating a Jira incident issue via the bridge."""

    summary: str
    description: str = ""
    project_key: str = "AMJTEST"
    issue_type: str = "Task"
    priority: str = "P2"
    labels: list[str] = Field(default_factory=lambda: ["hitl"])
    callback_url: Optional[str] = None
    slack_thread: Optional[str] = None
    alert_started_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def handle_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "project" in data and "project_key" not in data:
                data["project_key"] = data["project"]
        return data


class AddCommentRequest(BaseModel):
    """Payload for adding a comment to an existing Jira issue."""

    body: str
    properties: Optional[dict[str, Any]] = None


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
@app.get("/healthz")
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


@app.post("/api/jira/issue")
async def create_jira_issue_endpoint(
    payload: JiraIssueCreateRequest,
    request: Request,
    settings: BridgeConfig = Depends(get_settings),
    ticket_cache: TicketMetadataRegistry = Depends(get_ticket_registry),
) -> dict[str, Any]:
    """Create a Jira incident issue with OIDC auth, idempotency check, and metadata caching."""
    # 1. Guarded by verify_oidc_token
    allowed_sa: list[str] = [
        e for e in [settings.hitl_caller_sa, settings.agent_sa] if e is not None
    ]
    verify_oidc_token(
        request,
        settings,
        allowed_emails=allowed_sa,
    )

    try:
        adapter = get_adapter("jira", settings)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not isinstance(adapter, JiraAdapter):
        raise HTTPException(status_code=500, detail="Jira adapter is not configured")

    # 2. Idempotency Check: search Jira Cloud: project = {project_key} AND labels = "{alert_label}"
    alert_label = None
    for lbl in payload.labels:
        if lbl.startswith("alert-"):
            alert_label = lbl
            break
    if not alert_label:
        for lbl in payload.labels:
            if lbl.lower() != "hitl":
                alert_label = lbl
                break

    base_url = settings.jira_base_url or ""
    if alert_label:
        jql = f'project = "{payload.project_key}" AND labels = "{alert_label}"'
        try:
            existing_issues = adapter.search_issues(jql, max_results=1)
            if existing_issues:
                existing = existing_issues[0]
                existing_key = str(existing.get("key"))
                issue_url = (
                    f"{base_url.rstrip('/')}/browse/{existing_key}"
                    if base_url
                    else f"https://jira.internal/browse/{existing_key}"
                )
                ticket_cache.set(
                    issue_key=existing_key,
                    summary=payload.summary,
                    status="already_exists",
                    callback_url=payload.callback_url,
                    slack_thread=payload.slack_thread,
                    alert_started_at=payload.alert_started_at,
                )
                return {
                    "status": "already_exists",
                    "issue_key": existing_key,
                    "issue_url": issue_url,
                }
        except Exception as exc:
            logger.warning("Idempotency check search failed: %s", exc)

    # 3. Use or render ticket description
    description = payload.description
    if not description or not description.strip():
        description = render_ticket_description(
            {
                "title": payload.summary,
                "summary": payload.summary,
            },
            template_path=settings.ticket_template_path,
        )

    # 4. Create issue in Jira Cloud REST API v3
    try:
        res_data = adapter.create_issue(
            project_key=payload.project_key,
            summary=payload.summary,
            description=description,
            issue_type=payload.issue_type,
            priority=payload.priority,
            labels=payload.labels,
        )
    except Exception as exc:
        logger.error("Failed to create Jira issue: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to create Jira issue: {exc}") from exc

    created_key = str(res_data.get("key"))
    issue_url = (
        f"{base_url.rstrip('/')}/browse/{created_key}"
        if base_url
        else f"https://jira.internal/browse/{created_key}"
    )

    # 5. Cache metadata
    ticket_cache.set(
        issue_key=created_key,
        summary=payload.summary,
        status="created",
        callback_url=payload.callback_url,
        slack_thread=payload.slack_thread,
        alert_started_at=payload.alert_started_at,
    )

    # Optionally persist as Jira property
    try:
        adapter.set_issue_property(
            created_key,
            "hitl_metadata",
            {
                "callback_url": payload.callback_url,
                "slack_thread": payload.slack_thread,
                "alert_started_at": payload.alert_started_at,
            },
        )
    except Exception as exc:
        logger.debug("Failed to set Jira property on %s: %s", created_key, exc)

    return {
        "status": "created",
        "issue_key": created_key,
        "issue_url": issue_url,
    }


@app.post("/api/jira/issue/{key}/comment", status_code=status.HTTP_201_CREATED)
async def add_jira_comment_endpoint(
    key: str,
    payload: AddCommentRequest,
    request: Request,
    settings: BridgeConfig = Depends(get_settings),
) -> dict[str, Any]:
    """Append a comment to issue {key} with Google Cloud OIDC Bearer token authentication."""
    # 1. Protected with Google Cloud OIDC Bearer token authentication
    # (authorized caller: hitl-chatbot-sa or agent_sa)
    allowed_sa: list[str] = [
        e for e in [settings.hitl_caller_sa, settings.agent_sa] if e is not None
    ]
    verify_oidc_token(
        request,
        settings,
        allowed_emails=allowed_sa,
    )

    clean_key = key.strip()
    if not clean_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Issue key must not be empty",
        )
    if not payload.body or not payload.body.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Comment body must not be empty",
        )

    try:
        adapter = get_adapter("jira", settings)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not isinstance(adapter, JiraAdapter):
        raise HTTPException(status_code=500, detail="Jira adapter is not configured")

    try:
        comment_res = adapter.add_comment(
            issue_key=clean_key,
            body=payload.body,
            properties=payload.properties,
        )
        return {
            "status": "created",
            "issue_key": clean_key,
            "comment_id": comment_res.get("id"),
            "comment": comment_res,
        }
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Jira issue not found: {clean_key}",
            ) from exc
        if exc.response.status_code == 400:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Bad request to Jira API: {exc.response.text}",
            ) from exc
        logger.error("Upstream Jira failure when adding comment to %s: %s", clean_key, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream Jira failure: {exc}",
        ) from exc
    except Exception as exc:
        logger.error("Error adding comment to Jira %s: %s", clean_key, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream Jira failure: {exc}",
        ) from exc


@app.get("/api/investigations/{external_key}")
async def get_investigation_endpoint(
    external_key: str,
    settings: BridgeConfig = Depends(get_settings),
    ticket_cache: TicketMetadataRegistry = Depends(get_ticket_registry),
) -> dict[str, Any]:
    """Retrieve investigation status and summary for a given ticket key."""
    # 1. Check in-memory ticket metadata cache
    meta = ticket_cache.get(external_key)
    if meta:
        return {
            "external_key": meta.issue_key,
            "status": meta.status,
            "summary": meta.summary,
        }

    # 2. Check Jira directly if configured
    try:
        adapter = get_adapter("jira", settings)
        if isinstance(adapter, JiraAdapter):
            clean_key = (
                external_key[5:] if external_key.lower().startswith("jira-") else external_key
            )
            issue = adapter.get_issue(clean_key)
            if issue:
                fields = issue.get("fields", {})
                status_obj = fields.get("status", {})
                status_name = (
                    status_obj.get("name", "Unknown")
                    if isinstance(status_obj, dict)
                    else str(status_obj)
                )
                summary = str(fields.get("summary") or "")
                return {
                    "external_key": clean_key,
                    "status": status_name,
                    "summary": summary,
                }
    except Exception as exc:
        logger.debug("Jira fallback lookup failed for %s: %s", external_key, exc)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Investigation not found for key: {external_key}",
    )


@app.post("/callback/report")
async def handle_callback_report(
    request: Request,
    settings: BridgeConfig = Depends(get_settings),
    dedup: CallbackDedupRegistry = Depends(get_dedup_registry),
    ticket_cache: TicketMetadataRegistry = Depends(get_ticket_registry),
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

    # 3. Look up adapter and deliver report or failure
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

    # 4. RCA Forwarding & metadata cache update
    ticket_key = result.incident_external_key
    cached_meta = ticket_cache.get(ticket_key)
    cb_url = cached_meta.callback_url if cached_meta else None
    slack_thread = cached_meta.slack_thread if cached_meta else None

    if not cb_url and result.incident_source == "jira":
        try:
            if isinstance(adapter, JiraAdapter):
                prop_data = adapter.get_issue_property(ticket_key, "hitl_metadata")
                if prop_data and isinstance(prop_data, dict):
                    cb_url = prop_data.get("callback_url")
                    slack_thread = prop_data.get("slack_thread")
        except Exception as exc:
            logger.debug("Failed to retrieve Jira issue property for %s: %s", ticket_key, exc)

    ticket_cache.update_status(
        issue_key=ticket_key,
        status=result.status,
        summary=result.report.summary if result.report else (result.error or ""),
        report=result.report.model_dump(mode="json") if result.report else None,
    )

    if cb_url:
        try:
            logger.info(
                "Forwarding RCA report to %s for ticket %s (slack_thread=%s)",
                cb_url,
                ticket_key,
                slack_thread,
            )
            rca_payload = {
                "ticket_key": ticket_key,
                "thread_ref": slack_thread,
                "report": result.report.model_dump(mode="json") if result.report else {},
            }
            token = fetch_oidc_token(cb_url, config=settings)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(cb_url, json=rca_payload, headers=headers)
                if resp.is_error:
                    logger.warning(
                        "RCA forward to %s returned %d: %s",
                        cb_url,
                        resp.status_code,
                        resp.text,
                    )
                else:
                    logger.info(
                        "Successfully forwarded RCA report to %s (HTTP %d)",
                        cb_url,
                        resp.status_code,
                    )
        except Exception as exc:
            logger.error("Failed to forward RCA report to %s: %s", cb_url, exc)

    # 5. Update Jira issue description Root Causes section
    if result.incident_source == "jira" and isinstance(adapter, JiraAdapter) and result.report:
        rc_text = ""
        if result.report.probable_root_cause and result.report.probable_root_cause.text:
            rc_text = result.report.probable_root_cause.text
        elif result.report.summary:
            rc_text = result.report.summary

        if rc_text:
            try:
                updated = adapter.update_issue_root_causes(ticket_key, rc_text)
                if updated:
                    logger.info(
                        "Successfully updated Root Causes in Jira issue %s description", ticket_key
                    )
                else:
                    logger.info(
                        "Root Causes section could not be updated in Jira issue %s description",
                        ticket_key,
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to update Root Causes in Jira issue %s description: %s", ticket_key, exc
                )

    return {"status": "ok"}


if __name__ == "__main__":
    import sys

    import uvicorn

    config = get_settings()
    target = "app.main:app" if "bridge" not in sys.modules else "bridge.app.main:app"
    uvicorn.run(target, host="0.0.0.0", port=config.port)
