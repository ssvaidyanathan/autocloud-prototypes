import json
import logging
from typing import Any, Callable, Optional, cast

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status

from .adapters import GitHubAdapter, IncidentRef, ServiceNowAdapter, get_adapter
from .config import BridgeConfig, get_settings
from .contracts.report import PullRequestRef
from .contracts.result import InvestigationResult
from .core.auth import verify_oidc_token
from .core.dedup import CallbackDedupRegistry, get_dedup_registry
from .core.tasks import TaskDispatcher
from .fixer.ci_listener import CIFeedbackListener
from .fixer.interface import FixPRWriterBase, is_autofix_branch
from .fixer.selection import select_fix_proposal
from .fixer.writer import FixPRWriter

# Ensure INFO-level decision logs (e.g. auto-fix outcomes) reach Cloud Run / Cloud Logging.
# No-op when the hosting process has already configured root handlers.
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# The package is `app` under uvicorn (`uvicorn app.main:app`) and `bridge.app` under pytest.
for _pkg in ("bridge.app", "app"):
    logging.getLogger(_pkg).setLevel(logging.INFO)
    logging.getLogger(f"{_pkg}.fixer").setLevel(logging.INFO)

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


# Global CI feedback listener for auto-fix verification runs (overridable in tests)
_ci_listener: Optional[CIFeedbackListener] = None


def get_ci_feedback_listener() -> CIFeedbackListener:
    global _ci_listener
    if _ci_listener is None:
        _ci_listener = CIFeedbackListener()
    return _ci_listener


def set_ci_feedback_listener(listener: Optional[CIFeedbackListener]) -> None:
    """Helper to set or reset the CIFeedbackListener instance (e.g. for testing)."""
    global _ci_listener
    _ci_listener = listener


# Global auto-fix PR writer (overridable in tests)
_fix_pr_writer: Optional[FixPRWriterBase] = None


def get_fix_pr_writer(settings: BridgeConfig) -> FixPRWriterBase:
    global _fix_pr_writer
    if _fix_pr_writer is None:
        _fix_pr_writer = FixPRWriter(config=settings)
    return _fix_pr_writer


def set_fix_pr_writer(writer: Optional[FixPRWriterBase]) -> None:
    """Helper to set or reset the FixPRWriter instance (e.g. for testing)."""
    global _fix_pr_writer
    _fix_pr_writer = writer


def _bind_fetch_file(
    writer: FixPRWriterBase, result: InvestigationResult
) -> Callable[[str], Optional[str]]:
    """Bind a read-only file fetcher to the failing commit of the investigated run."""
    incident = result.incident
    repo_full = (incident.github_repo if incident else None) or ""
    owner, _, repo = repo_full.partition("/")
    ref = incident.github_head_sha if incident else None

    def fetch_file(path: str) -> Optional[str]:
        if not owner or not repo or not ref:
            return None
        return writer.fetch_file_content(owner, repo, path, ref)

    return fetch_file


# Deterministic fallback fixers, keyed by the strategy they emit (for decision logging).
_DETERMINISTIC_SOURCES: dict[str, str] = {
    "deterministic_linter": "deterministic_s1_lint",
    "config_tuning": "deterministic_s4_config",
}


def _maybe_open_fix_pr(
    result: InvestigationResult, settings: BridgeConfig
) -> Optional[PullRequestRef]:
    """Open an auto-fix PR for a completed GitHub investigation, if enabled and proposed.

    The agent's proposal is preferred; otherwise the deterministic lint (S1) and memory
    config (S4) fixers are tried. Never raises: auto-fix is best-effort and must not block
    ticket creation.
    """
    incident_id = result.incident_id
    enabled = settings.autofix_enabled is True
    logger.info("autofix.decision incident=%s stage=enabled enabled=%s", incident_id, enabled)
    if not enabled:
        logger.info("autofix.skipped incident=%s reason=disabled", incident_id)
        return None
    if result.report is None or result.incident is None:
        logger.info(
            "autofix.skipped incident=%s reason=missing_report_or_incident "
            "has_report=%s has_incident=%s",
            incident_id,
            result.report is not None,
            result.incident is not None,
        )
        return None
    try:
        writer = get_fix_pr_writer(settings)
        proposal = select_fix_proposal(
            result.incident, result.report, _bind_fetch_file(writer, result)
        )
        if proposal is None:
            logger.info("autofix.selection incident=%s source=none", incident_id)
            logger.info("autofix.skipped incident=%s reason=no_proposal", incident_id)
            return None
        is_agent_proposal = result.report.fix_proposal is proposal
        if is_agent_proposal:
            source = "agent"
        else:
            source = _DETERMINISTIC_SOURCES.get(proposal.strategy, "deterministic")
        logger.info(
            "autofix.selection incident=%s source=%s strategy=%s patches=%d files=%s",
            incident_id,
            source,
            proposal.strategy,
            len(proposal.patches),
            ",".join(p.path for p in proposal.patches),
        )
        if not is_agent_proposal:
            result.report.fix_proposal = proposal
        pr_ref = writer.execute_fix_proposal(result.incident, result.report, proposal)
        if pr_ref is None:
            logger.info(
                "autofix.writer incident=%s outcome=no_pr strategy=%s",
                incident_id,
                proposal.strategy,
            )
        else:
            logger.info(
                "autofix.writer incident=%s outcome=pr_opened pr_number=%d pr_url=%s branch=%s",
                incident_id,
                pr_ref.pr_number,
                pr_ref.pr_url,
                pr_ref.branch_name,
            )
        return pr_ref
    except Exception as exc:
        logger.error("autofix.writer incident=%s outcome=error error=%r", incident_id, exc)
        return None


@app.get("/health")
@app.get("/healthz")
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

    # Handle GitHub webhook ping event
    if request.headers.get("X-GitHub-Event", "").lower() == "ping":
        logger.info("Received GitHub webhook ping event")
        response.status_code = status.HTTP_200_OK
        return {
            "status": "pong",
            "message": "GitHub webhook ping received successfully",
        }

    # 2. Parse payload and normalize to Incident contract
    try:
        json_payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc

    # Loop prevention: CI runs on auto-fix branches are verification feedback only.
    # They must never enqueue a new investigation or trigger another fix attempt.
    workflow_run = json_payload.get("workflow_run") if isinstance(json_payload, dict) else None
    if isinstance(workflow_run, dict):
        head_branch = workflow_run.get("head_branch")
        if is_autofix_branch(head_branch):
            conclusion = workflow_run.get("conclusion")
            try:
                outcome = get_ci_feedback_listener().handle_autofix_workflow_run(
                    json_payload, settings
                )
                outcome_name = outcome.outcome
            except Exception as exc:
                logger.error("Auto-fix CI feedback processing failed for %s: %s", head_branch, exc)
                outcome_name = "error"
            response.status_code = status.HTTP_200_OK
            return {
                "status": "autofix_verification_processed",
                "branch": head_branch,
                "conclusion": conclusion,
                "outcome": outcome_name,
            }

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

        # Auto-fix runs first so the PR link can be included in the ticket and Check Run.
        pr_ref = _maybe_open_fix_pr(result, settings)
        if pr_ref is not None:
            result.pull_request = pr_ref

        servicenow_adapter = cast(ServiceNowAdapter, get_adapter("servicenow", settings))
        sn_ref = servicenow_adapter.create_incident_with_report(
            result.incident, result.report, pull_request=pr_ref
        )
        logger.info(
            "Successfully opened ServiceNow incident %s for GitHub failure", sn_ref.external_key
        )

        if pr_ref is not None:
            try:
                servicenow_adapter.post_fix_pr_note(sn_ref, pr_ref)
            except Exception as exc:
                logger.warning("Failed to post fix PR work note to ServiceNow: %s", exc)
        elif settings.autofix_enabled:
            proposal = result.report.fix_proposal
            try:
                servicenow_adapter.post_fix_pr_handoff_note(
                    sn_ref, proposal.rationale if proposal is not None else None
                )
            except Exception as exc:
                logger.warning("Failed to post auto-fix hand-off work note to ServiceNow: %s", exc)

        if settings.enable_github_writeback:
            try:
                github_adapter = cast(GitHubAdapter, get_adapter("github", settings))
                github_adapter.post_report_link(
                    result.incident, result.report, sn_ref, pull_request=pr_ref
                )
            except Exception as exc:
                logger.warning("Failed to post report link to GitHub Check Run: %s", exc)

        response_body: dict[str, Any] = {
            "status": "ok",
            "servicenow_ticket": sn_ref.external_key,
            "incident_id": result.incident_id,
        }
        if pr_ref is not None:
            response_body["fix_pr_url"] = pr_ref.pr_url
        return response_body

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
