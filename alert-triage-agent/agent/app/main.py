"""Alert Triage Agent FastAPI Application.

Implements:
- POST /health: service liveness check.
- POST /triage: raw alert ingestion dispatched from thin bridge into agent-owned grouping engine.
- POST /timer/wake: Cloud Tasks delayed timer wakeups evaluating settle, max-wait, and cooling deadlines.
- POST /investigate: diagnostic investigation execution and report compilation.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
import time
import traceback
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException

from .bridge_client import BridgeClient
from .config import AgentConfig, get_agent_settings
from .contracts.alert import AlertEvent, is_ata_alert
from .contracts.instructions import CreateTicketRequest, ResolveTicketRequest, WorkNoteRequest
from .contracts.report import InvestigationReport
from .contracts.timers import TimerWakeRequest, TimerWakeResponse
from .duplicate_checker import (
    DuplicateTargetMissingError,
    append_duplicate_alert_to_incident,
    check_for_duplicate,
)
from .grouping.late_alerts import TICKET_TITLE_MAX_LENGTH, refresh_ticket_for_late_alerts
from .grouping.manager import GroupingManager
from .grouping.models import COOLING_WINDOW_SECONDS, IncidentGroupStatus, OKFDocument
from .grouping.store import OKFStore, read_okf, update_okf_atomically
from .grouping.suppression import format_late_alert_work_note
from .grouping.stuck_investigation import (
    WATCHDOG_DEADLINE_TYPE,
    apply_degraded_predecessor_note,
    apply_recovery_note,
    investigation_started_epoch,
    is_stuck_investigation,
    mark_investigation_started,
    recover_stuck_investigation,
    schedule_watchdog,
)
from .recovery import recovery_note_for_group
from .specialists.origin_selection import alert_event_from_entry
from .specialists.synthesizer import (
    SynthesizerAgent,
    apply_correlation_note,
    apply_group_alerts,
    raise_file_priority,
)
from .timers.scheduler import schedule_delayed_timer
from .triage import (
    execute_multi_agent_investigation,
    generate_investigation_report,
    send_findings_to_bridge,
    write_preanalysis_lock,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Fail startup when a required setting is missing (TOOLS_URL when not local), so a
    misconfigured revision never takes traffic (Cloud Run keeps serving the previous one)."""
    settings = app.dependency_overrides.get(get_agent_settings, get_agent_settings)()
    missing = settings.missing_required_settings()
    if missing:
        message = f"{settings.name_prefix}-agent cannot start: required setting(s) not set: {', '.join(missing)}"
        logger.error(message)
        raise RuntimeError(message)
    yield


app = FastAPI(
    title="Alert Triage Agent",
    description="Agent performing pre-analysis locking, multi-agent triage, and diagnostic investigations",
    version="0.3.0",
    lifespan=lifespan,
)

# Virtual clock hook for deterministic replay testing
_clock_func: Optional[Callable[[], float]] = None


def set_clock_func(func: Optional[Callable[[], float]]) -> None:
    """Set custom clock function for timer/wake and triage timing."""
    global _clock_func
    _clock_func = func


def _get_current_time() -> float:
    """Return current timestamp using custom clock func if registered, else time.time()."""
    if _clock_func is not None:
        return _clock_func()
    return time.time()


@app.get("/health")
def health() -> dict[str, str]:
    """Standard health check endpoint."""
    return {"status": "ok"}


def _unpack_request(payload: dict[str, Any]) -> tuple[AlertEvent, Optional[str], Optional[str], Optional[list[Any]]]:
    """Unpack task dispatch payload supporting both bridge group schema and direct AlertEvent."""
    if "alert" in payload and isinstance(payload["alert"], dict):
        alert_data = payload["alert"]
        group_id = payload.get("group_id")
        filename = payload.get("filename")
        member_alerts = payload.get("member_alerts")
        alert = AlertEvent.model_validate(alert_data)
        return alert, group_id, filename, member_alerts
    elif "alert" in payload and hasattr(payload["alert"], "incident_id"):
        return payload["alert"], payload.get("group_id"), payload.get("filename"), payload.get("member_alerts")
    else:
        alert = AlertEvent.model_validate(payload)
        return alert, payload.get("group_id"), payload.get("filename"), payload.get("member_alerts")


def _raising_frame(exc: BaseException) -> str:
    """Return ``file:line in function`` of the innermost frame that raised ``exc``.

    The location carries no exception text, so it is safe to log.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "unknown location"
    frame = frames[-1]
    return f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"


def _fallback_report(
    alert: AlertEvent,
    group_id: str,
    filename: str,
    settings: AgentConfig,
    exc: Exception,
    caller: str,
) -> InvestigationReport:
    """Build the single-pass fallback report after a failed multi-agent investigation.

    The report is marked degraded so the ticket shows it is not the synthesized report. Only
    the exception type and the raising frame's location are logged, and only the type is
    recorded; the exception text can quote incident file content.
    """
    exc_type = type(exc).__name__
    logger.error(
        "Multi-agent investigation failed on %s for group %s: %s at %s; using the fallback report.",
        caller,
        group_id,
        exc_type,
        _raising_frame(exc),
    )
    return generate_investigation_report(
        alert,
        group_id,
        filename,
        settings,
        degraded_reason=f"multi-agent investigation failed ({exc_type}), single-pass fallback report",
    )


@app.post("/triage")
async def triage_alert(
    payload: dict[str, Any] = Body(...),
    settings: AgentConfig = Depends(get_agent_settings),
) -> dict[str, Any]:
    """Handle triage trigger dispatched from thin bridge into agent-owned grouping engine."""
    alert, bridge_group_id, bridge_filename, member_alerts = _unpack_request(payload)
    logger.info(
        "Received triage task for incident %s on %s (bridge_group_id=%s)",
        alert.incident_id,
        alert.resource_name,
        bridge_group_id,
    )

    # Defensive scoping check: ignore non-ata alerts
    if not is_ata_alert(alert):
        logger.warning(
            "Defensively dropping non-ata alert %s (policy: %s, resource: %s) in triage pipeline",
            alert.incident_id,
            alert.policy_name,
            alert.resource_name,
        )
        return {
            "status": "ignored",
            "reason": "non-ata alert",
            "incident_id": alert.incident_id,
        }

    now = _get_current_time()

    # Multi-grep duplicate alert & stale lock check for identical policy and resource
    store_path = str(settings.get_effective_store_path())
    if alert.state and alert.state.lower() != "closed":
        dup_res = check_for_duplicate(
            policy_name=alert.policy_name,
            resource_name=alert.resource_name,
            store_path=store_path,
        )
        appended = False
        if dup_res.is_duplicate and dup_res.active_file:
            logger.info(
                "Alert %s is an active duplicate of %s; appending alert and refreshing timestamp.",
                alert.incident_id,
                dup_res.active_file,
            )
            # Storage failures propagate (HTTP 5xx) so Cloud Tasks redelivers; the append is idempotent.
            try:
                append_duplicate_alert_to_incident(
                    file_path=dup_res.active_file,
                    alert=alert,
                    config=settings,
                )
                appended = True
            except DuplicateTargetMissingError as exc:
                # Nothing in the agent deletes groups, so FUSE matching a missing object is an anomaly.
                logger.error(
                    "Duplicate target %s (group %s, file %s) vanished before append; "
                    "routing alert %s through grouping.",
                    exc.target,
                    exc.group_id,
                    exc.file_path,
                    alert.incident_id,
                )
        if appended:
            act_path = Path(dup_res.active_file)
            clean_grp = act_path.stem.replace("incident-", "")
            return {
                "status": "duplicate",
                "is_duplicate": True,
                "incident_id": alert.incident_id,
                "group_id": clean_grp,
                "filename": act_path.name,
                "existing_file": dup_res.active_file,
                "lock_file": dup_res.active_file,
                "detail": dup_res.status,
                "action": "duplicate_suppressed",
            }

    manager = GroupingManager(config=settings)
    res = manager.ingest_alert(
        alert=alert,
        current_time=now,
        bridge_group_id=bridge_group_id,
        bridge_filename=bridge_filename,
    )

    lock_file = settings.get_effective_store_path() / res.filename

    if res.is_duplicate:
        return {
            "status": "duplicate",
            "is_duplicate": True,
            "incident_id": alert.incident_id,
            "group_id": res.group_id,
            "filename": res.filename,
            "lock_file": str(lock_file),
            "existing_file": str(lock_file),
            "detail": "duplicate_alert",
            "action": res.action,
        }

    if res.status == "closed":
        return {
            "status": "closed",
            "is_duplicate": False,
            "incident_id": alert.incident_id,
            "group_id": res.group_id,
            "filename": res.filename,
            "lock_file": str(lock_file),
            "action": res.action,
        }

    return {
        "status": "triaged",
        "group_id": res.group_id,
        "filename": res.filename,
        "lock_file": str(lock_file),
        "existing_file": str(lock_file),
        "action": res.action,
        "is_duplicate": False,
        "correlation": res.correlation or {
            "decision": "attach" if "attach" in res.action else "new_group",
            "best_score": 100.0 if "attach" in res.action else 0.0,
            "confidence": 0.95,
        },
        "settle_deadline": res.new_settle_deadline,
    }


@app.post("/timer/wake", response_model=TimerWakeResponse)
async def timer_wake(
    payload: TimerWakeRequest,
    settings: AgentConfig = Depends(get_agent_settings),
) -> TimerWakeResponse:
    """Handle Cloud Tasks delayed timer wakeups evaluating settle, max-wait, or cooling deadlines."""
    now = _get_current_time()
    store = OKFStore(config=settings)
    doc = store.read(payload.group_id)

    if doc is None:
        logger.warning("Timer wake received for non-existent group %s", payload.group_id)
        return TimerWakeResponse(
            status="ignored",
            group_id=payload.group_id,
            reason="group_not_found",
        )

    deadline_type = payload.deadline_type.lower()
    tolerance_seconds = 2.0

    # 1. Settle deadline evaluation
    if deadline_type == "settle":
        # Group must still be in open/investigating state, or cooling_down without ticket
        curr_status = str(doc.status).lower()
        if curr_status not in ("open", "investigating", "mitigation-in-progress"):
            if not (curr_status == IncidentGroupStatus.COOLING_DOWN.value and doc.servicenow_sys_id is None):
                logger.info(
                    "Settle wake ignored: group %s status is %s (not open)",
                    doc.group_id,
                    doc.status,
                )
                return TimerWakeResponse(
                    status="ignored",
                    group_id=doc.group_id,
                    reason=f"group_status_{doc.status}",
                )

        settle_epoch = doc.settle_deadline_epoch()
        if settle_epoch is not None and now < (settle_epoch - tolerance_seconds):
            logger.info(
                "JSON_DECISION: stale_settle_wake_ignored (now=%s < settle_deadline=%s)",
                now,
                doc.settle_deadline,
            )
            return TimerWakeResponse(
                status="ignored",
                group_id=doc.group_id,
                reason="stale_wake",
            )

        # Valid settle wake: transition to investigating, synthesize, and create ticket
        logger.info(
            "Valid settle wake for group %s (now=%s >= settle=%s); initiating ticket minting",
            doc.group_id,
            now,
            doc.settle_deadline,
        )
        investigating_doc = _start_investigation(store, doc.group_id, now, settings)
        if investigating_doc is None:
            return _group_file_gone(doc.group_id, f"{deadline_type} wake")
        doc = investigating_doc

        primary_alert = _extract_primary_alert(doc)
        filename = f"incident-{doc.group_id}.md"

        try:
            report = await execute_multi_agent_investigation(
                primary_alert, doc.group_id, filename, settings, group_alerts=list(doc.alerts or [])
            )
        except Exception as exc:
            report = _fallback_report(
                _origin_alert(doc, primary_alert, settings), doc.group_id, filename, settings, exc, "settle wake"
            )

        report.group_id = doc.group_id
        report.filename = filename
        _apply_current_group_alerts(report, store, doc)

        bridge_client = BridgeClient(config=settings)
        ticket_resp = bridge_client.create_ticket(
            bridge_url=settings.bridge_url,
            request=CreateTicketRequest(group_id=doc.group_id, report=report),
        )

        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        cooling_scheduled = False
        cooling_dt: Optional[datetime] = None

        def _mark_ticketed(d: OKFDocument) -> OKFDocument:
            nonlocal cooling_scheduled, cooling_dt
            d.servicenow_sys_id = ticket_resp.sys_id
            d.servicenow_number = ticket_resp.number
            d.servicenow_url = ticket_resp.url
            _record_ticket_title(d, report)
            d.origin = report.origin
            d.blast_radius = report.blast_radius
            d.updated_at = now_iso
            if d.all_alerts_closed():
                d.status = IncidentGroupStatus.COOLING_DOWN.value
                cooling_epoch = now + COOLING_WINDOW_SECONDS
                cooling_dt = datetime.fromtimestamp(cooling_epoch, tz=timezone.utc)
                d.cooling_deadline = cooling_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                cooling_scheduled = True
            else:
                d.status = IncidentGroupStatus.TICKETED.value
            return d

        updated_doc = store.update_atomically(doc.group_id, _mark_ticketed, create_if_missing=False)
        if updated_doc is None:
            logger.warning(
                "Ticket %s was created for group %s, but its incident file was archived or deleted "
                "meanwhile; the ticket fields are not recorded and no cooling timer is scheduled",
                ticket_resp.number,
                doc.group_id,
            )
            return TimerWakeResponse(status="executed", group_id=doc.group_id, reason="group_file_gone")
        _note_alerts_missed_by_ticket(report, updated_doc, bridge_client, settings, store=store)
        if cooling_scheduled and cooling_dt is not None:
            try:
                schedule_delayed_timer(
                    project_id=settings.project_id,
                    location=settings.location,
                    queue_name=settings.timer_queue_name,
                    target_url=settings.agent_url,
                    group_id=updated_doc.group_id,
                    deadline_type="cooling",
                    deadline_dt=cooling_dt,
                    service_account_email=settings.service_account_email,
                )
            except Exception as exc:
                logger.error(
                    "Failed to schedule cooling timer for group %s: %s",
                    updated_doc.group_id,
                    exc,
                    exc_info=True,
                )
        return TimerWakeResponse(status="executed", group_id=doc.group_id)

    # 2. Max-wait deadline evaluation
    elif deadline_type == "max_wait":
        curr_status = str(doc.status).lower()
        if curr_status not in ("open", "mitigation-in-progress"):
            return TimerWakeResponse(
                status="ignored",
                group_id=doc.group_id,
                reason=f"group_status_{doc.status}",
            )

        max_wait_epoch = doc.max_wait_deadline_epoch()
        if max_wait_epoch is not None and now < (max_wait_epoch - tolerance_seconds):
            return TimerWakeResponse(
                status="ignored",
                group_id=doc.group_id,
                reason="max_wait_not_reached",
            )

        logger.info(
            "Max-wait deadline reached for group %s (now=%s >= max_wait=%s); forcing ticket minting",
            doc.group_id,
            now,
            doc.max_wait_deadline,
        )
        investigating_doc = _start_investigation(store, doc.group_id, now, settings)
        if investigating_doc is None:
            return _group_file_gone(doc.group_id, f"{deadline_type} wake")
        doc = investigating_doc

        primary_alert = _extract_primary_alert(doc)
        filename = f"incident-{doc.group_id}.md"

        try:
            report = await execute_multi_agent_investigation(
                primary_alert, doc.group_id, filename, settings, group_alerts=list(doc.alerts or [])
            )
        except Exception as exc:
            report = _fallback_report(
                _origin_alert(doc, primary_alert, settings), doc.group_id, filename, settings, exc, "max_wait wake"
            )

        report.group_id = doc.group_id
        report.filename = filename
        _apply_current_group_alerts(report, store, doc)

        bridge_client = BridgeClient(config=settings)
        ticket_resp = bridge_client.create_ticket(
            bridge_url=settings.bridge_url,
            request=CreateTicketRequest(group_id=doc.group_id, report=report),
        )

        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cooling_scheduled = False
        cooling_dt: Optional[datetime] = None

        def _mark_ticketed_mw(d: OKFDocument) -> OKFDocument:
            nonlocal cooling_scheduled, cooling_dt
            d.servicenow_sys_id = ticket_resp.sys_id
            d.servicenow_number = ticket_resp.number
            d.servicenow_url = ticket_resp.url
            _record_ticket_title(d, report)
            d.origin = report.origin
            d.blast_radius = report.blast_radius
            d.updated_at = now_iso
            if d.all_alerts_closed():
                d.status = IncidentGroupStatus.COOLING_DOWN.value
                cooling_epoch = now + COOLING_WINDOW_SECONDS
                cooling_dt = datetime.fromtimestamp(cooling_epoch, tz=timezone.utc)
                d.cooling_deadline = cooling_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                cooling_scheduled = True
            else:
                d.status = IncidentGroupStatus.TICKETED.value
            return d

        updated_doc = store.update_atomically(doc.group_id, _mark_ticketed_mw, create_if_missing=False)
        if updated_doc is None:
            logger.warning(
                "Ticket %s was created for group %s on max_wait, but its incident file was archived or "
                "deleted meanwhile; the ticket fields are not recorded and no cooling timer is scheduled",
                ticket_resp.number,
                doc.group_id,
            )
            return TimerWakeResponse(status="executed", group_id=doc.group_id, reason="group_file_gone")
        _note_alerts_missed_by_ticket(report, updated_doc, bridge_client, settings, store=store)
        if cooling_scheduled and cooling_dt is not None:
            try:
                schedule_delayed_timer(
                    project_id=settings.project_id,
                    location=settings.location,
                    queue_name=settings.timer_queue_name,
                    target_url=settings.agent_url,
                    group_id=updated_doc.group_id,
                    deadline_type="cooling",
                    deadline_dt=cooling_dt,
                    service_account_email=settings.service_account_email,
                )
            except Exception as exc:
                logger.error(
                    "Failed to schedule cooling timer for group %s on max_wait: %s",
                    updated_doc.group_id,
                    exc,
                    exc_info=True,
                )
        return TimerWakeResponse(status="executed", group_id=doc.group_id)

    # 3. Cooling-down deadline evaluation
    elif deadline_type == "cooling":
        curr_status = str(doc.status).lower()
        if curr_status != IncidentGroupStatus.COOLING_DOWN.value:
            logger.info(
                "Cooling wake ignored: group %s status is %s (not cooling_down)",
                doc.group_id,
                doc.status,
            )
            return TimerWakeResponse(
                status="ignored",
                group_id=doc.group_id,
                reason=f"group_status_{doc.status}",
            )

        cooling_epoch = doc.cooling_deadline_epoch()
        if cooling_epoch is not None and now < (cooling_epoch - tolerance_seconds):
            return TimerWakeResponse(
                status="ignored",
                group_id=doc.group_id,
                reason="cooling_not_reached",
            )

        # Resolution check: must have ticket or explicit suppression reason
        if doc.servicenow_sys_id is None and not (doc.suppressed and doc.suppression_reason):
            logger.error(
                "Rejecting resolution of unticketed group %s without explicit suppression reason",
                doc.group_id,
            )
            now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            def _mark_degraded(d: OKFDocument) -> OKFDocument:
                d.status = IncidentGroupStatus.DEGRADED.value
                d.degraded = True
                d.degraded_reason = "Attempted resolution without ticket or suppression reason"
                d.updated_at = now_iso
                return d

            store.update_atomically(doc.group_id, _mark_degraded, create_if_missing=False)
            return TimerWakeResponse(
                status="rejected",
                group_id=doc.group_id,
                reason="unresolved_unticketed",
            )

        if doc.servicenow_sys_id and not settings.auto_resolve_on_recovery:
            return _mark_recovered_without_resolving(store, doc, now, settings)

        # Valid cooling wake: call bridge resolve and archive OKF
        logger.info(
            "JSON_DECISION: auto_resolve_on_recovery=%s; resolving group %s (servicenow_sys_id=%s)",
            settings.auto_resolve_on_recovery,
            doc.group_id,
            doc.servicenow_sys_id,
        )
        logger.info(
            "Cooling deadline expired for group %s; resolving and archiving incident",
            doc.group_id,
        )
        bridge_client = BridgeClient(config=settings)
        if doc.servicenow_sys_id:
            try:
                bridge_client.resolve_ticket(
                    bridge_url=settings.bridge_url,
                    request=ResolveTicketRequest(
                        sys_id=doc.servicenow_sys_id,
                        group_id=doc.group_id,
                        close_notes="Incident resolved. All monitoring conditions recovered and cool-down expired.",
                    ),
                )
            except Exception as exc:
                logger.warning("Could not resolve ticket on bridge during cooling wake: %s", exc)

        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def _mark_resolved(d: OKFDocument) -> OKFDocument:
            d.status = IncidentGroupStatus.RESOLVED.value
            d.updated_at = now_iso
            return d

        if store.update_atomically(doc.group_id, _mark_resolved, create_if_missing=False) is None:
            logger.warning(
                "Group %s was resolved on the bridge, but its incident file was archived or deleted "
                "meanwhile; status resolved is not recorded and the file is not recreated",
                doc.group_id,
            )
        return TimerWakeResponse(status="executed", group_id=doc.group_id)

    # 4. Stuck-investigation watchdog (grouping/stuck_investigation.py)
    elif deadline_type == WATCHDOG_DEADLINE_TYPE:
        return _investigation_watchdog(store, doc, now, settings, payload.scheduled_epoch)

    return TimerWakeResponse(
        status="ignored",
        group_id=doc.group_id,
        reason=f"unsupported_deadline_type_{payload.deadline_type}",
    )


def _start_investigation(
    store: OKFStore, group_id: str, now: float, settings: AgentConfig
) -> Optional[OKFDocument]:
    """Move the group to investigating and stamp this wake attempt; schedule the watchdog on first entry.

    Returns None if the group file is gone. A watchdog scheduling failure is logged at ERROR
    and does not block ticketing; the next alert ingest still detects a stuck group.
    """
    first_entry = False

    def _mutate(d: OKFDocument) -> OKFDocument:
        nonlocal first_entry
        first_entry = mark_investigation_started(d, now)
        return d

    updated = store.update_atomically(group_id, _mutate, create_if_missing=False)
    if updated is not None and first_entry:
        try:
            schedule_watchdog(settings, group_id, now + settings.investigation_stale_seconds)
        except Exception as exc:
            logger.error(
                "Failed to schedule the investigation watchdog for group %s: %s; if this investigation "
                "never tickets, only the next alert ingest detects it",
                group_id,
                exc,
                exc_info=True,
            )
    return updated


def _investigation_watchdog(
    store: OKFStore, doc: OKFDocument, now: float, settings: AgentConfig, scheduled_epoch: int
) -> TimerWakeResponse:
    """Recover a group stuck in investigating, or re-arm the watchdog while its investigation is active.

    Re-arming failures propagate (5xx) so Cloud Tasks retries this watchdog wake. The re-armed
    wake is always later than this one: Cloud Tasks refuses a task name (which encodes the
    epoch) reused soon after deletion, and the scheduler treats that as already scheduled.
    """
    curr_status = str(doc.status).lower()
    if curr_status != IncidentGroupStatus.INVESTIGATING.value or doc.servicenow_sys_id:
        return TimerWakeResponse(status="ignored", group_id=doc.group_id, reason=f"group_status_{doc.status}")

    stale = settings.investigation_stale_seconds
    if not is_stuck_investigation(doc, now, stale):
        started = investigation_started_epoch(doc)
        next_epoch = max((started if started is not None else now) + stale, float(scheduled_epoch) + 1.0)
        schedule_watchdog(settings, doc.group_id, next_epoch)
        return TimerWakeResponse(status="ignored", group_id=doc.group_id, reason="investigation_active")

    result = recover_stuck_investigation(store, doc.group_id, now, settings, trigger="investigation watchdog")
    if result.doc is None:
        return _group_file_gone(doc.group_id, "investigation_watchdog wake")
    return TimerWakeResponse(status="executed", group_id=doc.group_id, reason=f"stuck_investigation_{result.outcome}")


def _group_file_gone(group_id: str, context: str) -> TimerWakeResponse:
    """The group's incident file vanished (archived/deleted) between the wake's read and its update."""
    logger.warning(
        "%s ignored: incident file for group %s was archived or deleted since it was read; not recreating it",
        context.capitalize(),
        group_id,
    )
    return TimerWakeResponse(status="ignored", group_id=group_id, reason="group_not_found")


def _mark_recovered_without_resolving(
    store: OKFStore,
    doc: OKFDocument,
    now: float,
    settings: AgentConfig,
) -> TimerWakeResponse:
    """Cool-down expired with AUTO_RESOLVE_ON_RECOVERY=false: leave the ticket In Progress.

    Monitoring recovery is not resolution, so instead of calling ticket/resolve this posts
    one work note asking a human to verify the fix and moves the group to the terminal
    RECOVERED state (excluded from grouping exactly like RESOLVED, so new alerts open a
    fresh group).

    Idempotency: the group is claimed (cooling_down -> recovered) before the note is posted,
    so a retried or duplicate cooling wake finds it no longer cooling_down and is ignored.
    If posting the note fails the claim is released and the error re-raised, so the timer
    queue retries the wake rather than the note being silently dropped.
    """
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cooling_epoch = doc.cooling_deadline_epoch()
    if cooling_epoch is not None:
        recovered_epoch = cooling_epoch - COOLING_WINDOW_SECONDS
    else:
        logger.warning(
            "Group %s has no cooling_deadline; reporting recovery time as the wake time %s",
            doc.group_id,
            now_iso,
        )
        recovered_epoch = now
    recovered_iso = datetime.fromtimestamp(recovered_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    claimed = False

    def _claim(d: OKFDocument) -> OKFDocument:
        nonlocal claimed
        claimed = str(d.status).lower() == IncidentGroupStatus.COOLING_DOWN.value
        if claimed:
            d.status = IncidentGroupStatus.RECOVERED.value
            d.recovered_at = recovered_iso
            d.updated_at = now_iso
        return d

    claimed_doc = store.update_atomically(doc.group_id, _claim, create_if_missing=False)
    if claimed_doc is None:
        return _group_file_gone(doc.group_id, "cooling wake")
    if not claimed:
        logger.info(
            "Cooling wake ignored: group %s status changed to %s before recovery was recorded",
            doc.group_id,
            claimed_doc.status,
        )
        return TimerWakeResponse(
            status="ignored",
            group_id=doc.group_id,
            reason=f"group_status_{claimed_doc.status}",
        )

    note = recovery_note_for_group(
        list(claimed_doc.alerts or []), recovered_epoch, now, int(COOLING_WINDOW_SECONDS), settings
    )
    logger.info(
        "JSON_DECISION: auto_resolve_on_recovery=false; not resolving ServiceNow ticket %s for group %s; "
        "posting recovery work note and marking group recovered",
        claimed_doc.servicenow_sys_id,
        doc.group_id,
    )

    try:
        BridgeClient(config=settings).append_work_note(
            bridge_url=settings.bridge_url,
            request=WorkNoteRequest(
                sys_id=claimed_doc.servicenow_sys_id,
                work_note=note,
                group_id=doc.group_id,
            ),
        )
    except Exception as note_exc:
        logger.exception(
            "Failed posting recovery work note for group %s; releasing claim so the cooling wake is retried",
            doc.group_id,
        )

        def _release(d: OKFDocument) -> OKFDocument:
            if str(d.status).lower() == IncidentGroupStatus.RECOVERED.value and d.recovery_note_posted_at is None:
                d.status = IncidentGroupStatus.COOLING_DOWN.value
                d.recovered_at = None
                d.updated_at = now_iso
            return d

        try:
            store.update_atomically(doc.group_id, _release, create_if_missing=False)
        except Exception as release_exc:
            # Both storage steps failed: the group stays 'recovered' with no note, and every
            # retried wake will be ignored. Make that visible rather than silent.
            logger.error(
                "Recovery note NOT posted and claim NOT released for group %s (servicenow_sys_id=%s): "
                "%s: %s; group is stuck in 'recovered' without a note, manual work note required",
                doc.group_id,
                claimed_doc.servicenow_sys_id,
                type(release_exc).__name__,
                release_exc,
                exc_info=True,
            )
            raise note_exc from release_exc
        raise

    def _mark_note_posted(d: OKFDocument) -> OKFDocument:
        d.recovery_note_posted_at = now_iso
        d.updated_at = now_iso
        return d

    # The reset may archive the file once it reads 'recovered' plus recovery_note_posted_at;
    # if it was archived or deleted anyway (e.g. reset --no-wait), do not recreate it.
    if store.update_atomically(doc.group_id, _mark_note_posted, create_if_missing=False) is None:
        logger.warning(
            "Recovery note posted for group %s, but its incident file was archived or deleted before "
            "recovery_note_posted_at could be recorded; the file is not recreated",
            doc.group_id,
        )
    return TimerWakeResponse(status="executed", group_id=doc.group_id, reason="recovered_not_resolved")


def _apply_current_group_alerts(report: InvestigationReport, store: OKFStore, doc: OKFDocument) -> None:
    """Merge the incident group's alerts, as stored now, into the report before ticketing.

    The investigation (and the fallback report) is built from the primary alert only. The
    group is re-read here, at ticket time, so alerts attached while the investigation ran
    are counted too; the read-at-wake ``doc`` is the fallback if the re-read fails.
    """
    current = doc
    try:
        current = store.read(doc.group_id) or doc
    except Exception as exc:
        logger.warning(
            "Could not re-read group %s before ticketing (%s); using the alerts read at wake",
            doc.group_id,
            type(exc).__name__,
        )
    apply_group_alerts(report, list(current.alerts or []))
    apply_correlation_note(report, current.correlation)
    apply_recovery_note(report, getattr(current, "investigation_recoveries", None))
    apply_degraded_predecessor_note(report, getattr(current, "degraded_predecessors", None))


def _record_ticket_title(d: OKFDocument, report: InvestigationReport) -> None:
    """Store the title/priority the bridge built for the new ticket (late-alert updates start from it)."""
    d.ticket_short_description = report.build_short_description(max_length=TICKET_TITLE_MAX_LENGTH)
    d.ticket_priority = report.priority
    # The file's priority follows the ticket's: alerts merged at ticket time can raise it.
    # Only raised, never lowered automatically.
    raise_file_priority(d, report.priority, "ticket priority")
    d.ticket_title_problem = report.resolve_title_problem()
    d.ticket_origin = report.origin


def _note_alerts_missed_by_ticket(
    report: InvestigationReport,
    doc: OKFDocument,
    bridge_client: BridgeClient,
    settings: AgentConfig,
    store: Optional[OKFStore] = None,
) -> None:
    """Append a late-alert work note for each group alert the created ticket does not list.

    An alert that attaches after the ticket-time re-read but before ``servicenow_sys_id`` is
    stored finds the group without a ticket, so GroupingManager sends no work note for it.
    ``doc`` is the group as stored after the ticket fields were written; every alert it holds
    that is absent from ``report.alerts`` is reported here.
    """
    if not doc.servicenow_sys_id:
        return
    reported = {
        a.get("incident_id") for a in (report.alerts or []) if isinstance(a, dict) and a.get("incident_id")
    }
    missed: list[str] = []
    for entry in doc.alerts or []:
        inc_id = entry.get("incident_id") if isinstance(entry, dict) else None
        if not inc_id or inc_id in reported:
            continue
        missed.append(inc_id)
        logger.warning(
            "Alert %s attached to group %s while ticket %s was being created; appending a late-alert work note",
            inc_id,
            doc.group_id,
            doc.servicenow_number or doc.servicenow_sys_id,
        )
        note = format_late_alert_work_note(
            _alert_event_from_entry(entry, doc),
            doc.blast_radius,
            project_id=settings.project_id or doc.project,
        )
        try:
            bridge_client.append_work_note(
                bridge_url=settings.bridge_url,
                request=WorkNoteRequest(sys_id=doc.servicenow_sys_id, work_note=note, group_id=doc.group_id),
            )
        except Exception as exc:
            logger.error(
                "Failed appending late-alert work note for alert %s to group %s: %s",
                inc_id,
                doc.group_id,
                type(exc).__name__,
            )
    if missed:
        # The ticket title was built without these alerts: update count/priority (work-noted).
        refresh_ticket_for_late_alerts(doc, missed, bridge_client, settings, store or OKFStore(config=settings))


def _group_fallback_alert(doc: OKFDocument) -> AlertEvent:
    """AlertEvent built from the group document alone (used when an entry lacks a field)."""
    return AlertEvent(
        incident_id="0.unknown",
        policy_name=doc.policy_name or "unknown",
        resource_name=doc.resource_name or "unknown",
        state="open",
        started_at=doc.created_at,
    )


def _alert_event_from_entry(entry: dict[str, Any], doc: OKFDocument) -> AlertEvent:
    """Build an AlertEvent from one OKF ``alerts`` entry (group fields fill the gaps)."""
    return alert_event_from_entry(entry, _group_fallback_alert(doc))


def _origin_alert(doc: OKFDocument, primary_alert: AlertEvent, settings: Optional[AgentConfig] = None) -> AlertEvent:
    """Alert on the group's topology-selected origin (the first alert when only one service alerts)."""
    selection = SynthesizerAgent._select_group_origin(
        primary_alert,
        list(doc.alerts or []),
        onset_tolerance_seconds=settings.origin_onset_tolerance_seconds if settings is not None else None,
    )
    if selection is None or not selection.is_multi_service:
        return primary_alert
    return alert_event_from_entry(selection.primary_alert, primary_alert)


def _extract_primary_alert(doc: OKFDocument) -> AlertEvent:
    """Extract primary alert from OKFDocument for investigation synthesizers."""
    if doc.alerts:
        return _alert_event_from_entry(doc.alerts[0], doc)
    return _group_fallback_alert(doc)


@app.post("/investigate")
async def investigate_alert(
    payload: dict[str, Any] = Body(...),
    settings: AgentConfig = Depends(get_agent_settings),
) -> dict[str, Any]:
    """Standalone investigation endpoint invoked at settle or max-wait deadline."""
    alert, bridge_group_id, bridge_filename, member_alerts = _unpack_request(payload)
    if not is_ata_alert(alert):
        logger.warning("Defensively dropping non-ata alert %s in investigate endpoint", alert.incident_id)
        return {
            "status": "ignored",
            "reason": "non-ata alert",
            "incident_id": alert.incident_id,
        }

    file_path, group_id, filename = write_preanalysis_lock(
        alert,
        settings,
        group_id=bridge_group_id,
        filename=bridge_filename,
        member_alerts=member_alerts,
    )
    try:
        report = await execute_multi_agent_investigation(alert, group_id, filename, settings)
    except Exception as exc:
        report = _fallback_report(alert, group_id, filename, settings, exc, "/investigate")

    callback_res: dict[str, Any] = {}
    try:
        callback_res = send_findings_to_bridge(report, settings.bridge_url, config=settings)
        logger.info("Bridge callback succeeded for group %s: %s", group_id, callback_res)
    except Exception as exc:
        logger.warning("Bridge callback non-blocking error: %s", exc)

    return {
        "status": "investigated",
        "group_id": group_id,
        "lock_file": str(file_path),
        "report": report.model_dump(mode="json"),
        "callback": callback_res,
    }
