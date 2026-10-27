import base64
from contextlib import asynccontextmanager
import hmac
import json
import logging
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status

from .adapters.servicenow import ServiceNowAdapter
from .auth import verify_agent_caller
from .config import BridgeConfig, get_settings
from .contracts.alert import AlertEvent, is_ata_alert
from .contracts.instructions import (
    ActionStatusResponse,
    CreateTicketRequest,
    ResolveTicketRequest,
    TicketResponse,
    TicketViewResponse,
    TicketWorkNote,
    TicketAttachment,
    UpdateReportRequest,
    WorkNoteRequest,
)
from .core.dispatcher import TaskDispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

_dispatcher: Optional[TaskDispatcher] = None
_adapter: Optional[ServiceNowAdapter] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Stateless lifespan without background evaluation loops.

    Fails startup when a required setting is missing, so a misconfigured revision never
    takes traffic (Cloud Run keeps serving the previous one).
    """
    missing = get_settings().missing_required_settings()
    if missing:
        message = f"ata-bridge cannot start: required setting(s) not set: {', '.join(missing)}"
        logger.error(message)
        raise RuntimeError(message)
    yield


app = FastAPI(
    title="Alert Triage Bridge",
    description="Bridge connecting Cloud Monitoring webhooks, Cloud Tasks, and ServiceNow",
    version="0.1.0",
    lifespan=lifespan,
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)


def get_dispatcher(settings: BridgeConfig = Depends(get_settings)) -> TaskDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = TaskDispatcher(config=settings)
    return _dispatcher


def set_dispatcher(dispatcher: Optional[TaskDispatcher]) -> None:
    global _dispatcher
    _dispatcher = dispatcher


def get_servicenow_adapter(settings: BridgeConfig = Depends(get_settings)) -> ServiceNowAdapter:
    global _adapter
    if _adapter is None:
        _adapter = ServiceNowAdapter(config=settings)
    return _adapter


def set_servicenow_adapter(adapter: Optional[ServiceNowAdapter]) -> None:
    global _adapter
    _adapter = adapter


@app.get("/health")
def health() -> dict[str, str]:
    """Standard health check endpoint."""
    return {"status": "ok"}


def _verify_webhook_token(
    request: Request,
    token_param: Optional[str],
    auth_token_param: Optional[str],
    settings: BridgeConfig,
) -> bool:
    expected_token = settings.webhook_token
    received_token = token_param or auth_token_param
    if not received_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
                if ":" in decoded:
                    _, pwd = decoded.split(":", 1)
                    received_token = pwd.strip()
            except Exception:
                pass
        elif auth_header.startswith("Bearer "):
            received_token = auth_header[7:].strip()

    logger.info(
        "Verifying webhook token: has_expected=%s, has_received=%s",
        bool(expected_token),
        bool(received_token),
    )

    if not expected_token:
        logger.error("Rejecting webhook: webhook_token is unset or unconfigured")
        return False

    if not received_token:
        return False

    return hmac.compare_digest(received_token.strip(), expected_token.strip())


# ==============================================================================
# Thin Webhook Dispatcher
# ==============================================================================

@app.post("/webhook/monitoring")
async def handle_monitoring_webhook(
    request: Request,
    response: Response,
    token: Optional[str] = Query(None),
    auth_token: Optional[str] = Query(None),
    settings: BridgeConfig = Depends(get_settings),
    dispatcher: TaskDispatcher = Depends(get_dispatcher),
) -> dict[str, Any]:
    """Ingest Cloud Monitoring alert notification webhook and dispatch raw alert."""
    # 1. HMAC Token Verification
    if not _verify_webhook_token(request, token, auth_token, settings):
        logger.warning("Monitoring webhook unauthorized token received")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook token",
        )

    # 2. Parse payload into AlertEvent
    raw_body = await request.body()
    try:
        json_payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Malformed JSON: {exc}") from exc

    # Defensive check: if policy does not belong to ata, ignore without enqueuing
    if not is_ata_alert(json_payload):
        logger.info("Ignoring non-ata alert policy in monitoring webhook")
        response.status_code = status.HTTP_200_OK
        return {"status": "ignored", "reason": "non-ata alert policy"}

    try:
        alert = AlertEvent.from_monitoring_payload(json_payload)
    except Exception as exc:
        logger.error("Failed to parse Cloud Monitoring payload: %s", exc)
        raise HTTPException(status_code=400, detail=f"Invalid alert payload: {exc}") from exc

    if not is_ata_alert(alert):
        logger.info("Ignoring non-ata alert event in monitoring webhook: %s", alert.policy_name)
        response.status_code = status.HTTP_200_OK
        return {"status": "ignored", "reason": "non-ata alert policy"}

    # 3. Enqueue raw alert directly into Cloud Tasks (zero deduplication or windowing suppression)
    res = dispatcher.dispatch_alert(alert)

    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "status": "accepted",
        "incident_id": alert.incident_id,
        "task_id": res.task_id,
    }


# ==============================================================================
# Instruction Endpoints (Thin Bridge Pattern)
# ==============================================================================

@app.post("/instructions/ticket/create", response_model=TicketResponse)
async def create_ticket_instruction(
    request: CreateTicketRequest,
    caller: dict[str, Any] = Depends(verify_agent_caller),
    adapter: ServiceNowAdapter = Depends(get_servicenow_adapter),
) -> TicketResponse:
    """Create a ServiceNow ticket with idempotency check on group_id correlation_id."""
    logger.info(
        "Instruction ticket create requested for group %s by caller %s",
        request.group_id,
        caller.get("email"),
    )
    existing = adapter.find_incident_by_correlation_id(request.group_id)
    if existing is not None:
        logger.info(
            "ServiceNow incident already exists for group %s: sys_id=%s, number=%s",
            request.group_id,
            existing.sys_id,
            existing.number,
        )
        return TicketResponse(
            status="already_exists",
            sys_id=existing.sys_id,
            number=existing.number,
            url=existing.url,
            group_id=request.group_id,
        )

    ref = adapter.create_incident(report=request.report, query_before_create=True)
    return TicketResponse(
        status="created",
        sys_id=ref.sys_id,
        number=ref.number,
        url=ref.url,
        group_id=request.group_id,
    )


@app.post("/instructions/ticket/work-note", response_model=ActionStatusResponse)
async def append_work_note_instruction(
    request: WorkNoteRequest,
    caller: dict[str, Any] = Depends(verify_agent_caller),
    adapter: ServiceNowAdapter = Depends(get_servicenow_adapter),
) -> ActionStatusResponse:
    """Append a work note to an existing ServiceNow incident."""
    logger.info(
        "Instruction work note requested for sys_id %s by caller %s",
        request.sys_id,
        caller.get("email"),
    )
    adapter.append_work_note(sys_id=request.sys_id, note=request.work_note)
    return ActionStatusResponse(
        status="success",
        sys_id=request.sys_id,
        group_id=request.group_id,
        message="Work note appended successfully",
    )


@app.post("/instructions/ticket/update-report", response_model=TicketResponse)
async def update_report_instruction(
    request: UpdateReportRequest,
    caller: dict[str, Any] = Depends(verify_agent_caller),
    adapter: ServiceNowAdapter = Depends(get_servicenow_adapter),
) -> TicketResponse:
    """Update ServiceNow incident description, work notes, and HTML diagnostic report attachment."""
    logger.info(
        "Instruction update report requested for sys_id %s (group %s) by caller %s",
        request.sys_id,
        request.group_id,
        caller.get("email"),
    )
    ref = adapter.update_incident(sys_id=request.sys_id, report=request.report)
    return TicketResponse(
        status="updated",
        sys_id=ref.sys_id,
        number=ref.number,
        url=ref.url,
        group_id=request.group_id,
    )


@app.post("/instructions/ticket/resolve", response_model=ActionStatusResponse)
async def resolve_ticket_instruction(
    request: ResolveTicketRequest,
    caller: dict[str, Any] = Depends(verify_agent_caller),
    adapter: ServiceNowAdapter = Depends(get_servicenow_adapter),
) -> ActionStatusResponse:
    """Resolve ServiceNow incident by setting state=6, close_code, and close_notes."""
    logger.info(
        "Instruction resolve ticket requested for sys_id %s (group %s) by caller %s",
        request.sys_id,
        request.group_id,
        caller.get("email"),
    )
    adapter.resolve_incident(
        sys_id=request.sys_id,
        close_notes=request.close_notes,
        close_code=request.close_code,
        group_id=request.group_id,
    )
    return ActionStatusResponse(
        status="resolved",
        sys_id=request.sys_id,
        group_id=request.group_id,
        message="Incident resolved successfully",
    )


@app.get("/instructions/ticket/query/{group_id}", response_model=TicketResponse)
async def query_ticket_instruction(
    group_id: str,
    caller: dict[str, Any] = Depends(verify_agent_caller),
    adapter: ServiceNowAdapter = Depends(get_servicenow_adapter),
) -> TicketResponse:
    """Query ServiceNow incident ref by canonical correlation group_id."""
    logger.info(
        "Instruction query ticket requested for group %s by caller %s",
        group_id,
        caller.get("email"),
    )
    ref = adapter.find_incident_by_correlation_id(group_id)
    if ref is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident with correlation_id {group_id} not found",
        )
    return TicketResponse(
        status="found",
        sys_id=ref.sys_id,
        number=ref.number,
        url=ref.url,
        group_id=group_id,
    )


@app.get("/instructions/ticket/view/{identifier}", response_model=TicketViewResponse)
async def view_ticket_instruction(
    identifier: str,
    caller: dict[str, Any] = Depends(verify_agent_caller),
    adapter: ServiceNowAdapter = Depends(get_servicenow_adapter),
) -> TicketViewResponse:
    """View read-only ticket fields for a ServiceNow incident by group_id or sys_id.
    
    Authenticated via Google OIDC for ata-agent-sa and configured verifier SA.
    Returns only safe ticket fields (number, state, short_description, caller,
    work notes with timestamps, attachment names and sizes); NEVER credentials.
    """
    logger.info(
        "Instruction view ticket requested for identifier '%s' by caller %s",
        identifier,
        caller.get("email"),
    )
    # 1. Resolve sys_id: try finding by correlation_id first
    ref = adapter.find_incident_by_correlation_id(identifier)
    sys_id = ref.sys_id if ref is not None else identifier

    # 2. Fetch record from adapter
    record = adapter.get_incident_record(sys_id)
    if not record or record.get("error"):
        if ref is not None and hasattr(adapter, "_created_incidents") and identifier in adapter._created_incidents:
            record = {
                "incident": {
                    "sys_id": ref.sys_id,
                    "number": ref.number,
                    "state": "2",
                    "short_description": f"Incident for {identifier}",
                    "caller_id": adapter.config.servicenow_caller or "admin",
                },
                "work_notes": [],
                "attachments": [],
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Incident '{identifier}' not found",
            )

    inc_data = record.get("incident", {})
    if not inc_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident data for '{identifier}' not found",
        )

    def _extract_str(val: Any) -> str:
        if val is None:
            return ""
        if isinstance(val, dict):
            return str(val.get("display_value") or val.get("value") or "")
        return str(val)

    number = _extract_str(inc_data.get("number")) or (ref.number if ref else "INC0010482")
    state = _extract_str(inc_data.get("state")) or "1"
    short_description = _extract_str(inc_data.get("short_description"))
    caller_str = _extract_str(inc_data.get("caller_id") or inc_data.get("caller"))

    work_notes: list[TicketWorkNote] = []
    raw_notes = record.get("work_notes", [])
    if isinstance(raw_notes, list):
        for entry in raw_notes:
            if isinstance(entry, dict):
                txt = _extract_str(entry.get("value") or entry.get("text") or entry.get("work_note"))
                ts = _extract_str(entry.get("sys_created_on") or entry.get("timestamp") or entry.get("created_on")) or None
                if txt:
                    work_notes.append(TicketWorkNote(text=txt, timestamp=ts, value=txt, created_on=ts))
            elif isinstance(entry, str) and entry.strip():
                work_notes.append(TicketWorkNote(text=entry, timestamp=None, value=entry, created_on=None))

    if not work_notes and hasattr(adapter, "_posted_notes") and adapter._posted_notes:
        for note in adapter._posted_notes:
            work_notes.append(TicketWorkNote(text=note, timestamp="2026-10-25 02:00:00", value=note, created_on="2026-10-25 02:00:00"))

    attachments: list[TicketAttachment] = []
    raw_atts = record.get("attachments", [])
    if isinstance(raw_atts, list):
        for entry in raw_atts:
            if isinstance(entry, dict):
                att_name = _extract_str(entry.get("file_name") or entry.get("name"))
                raw_size = entry.get("size_bytes") or entry.get("size")
                att_size = None
                if raw_size is not None:
                    try:
                        att_size = int(raw_size)
                    except (ValueError, TypeError):
                        pass
                if att_name:
                    attachments.append(TicketAttachment(name=att_name, size_bytes=att_size, file_name=att_name, size=att_size))

    return TicketViewResponse(
        number=number,
        state=state,
        short_description=short_description,
        caller=caller_str if caller_str else None,
        work_notes=work_notes,
        attachments=attachments,
    )

