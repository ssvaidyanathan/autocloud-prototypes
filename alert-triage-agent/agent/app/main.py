"""Alert Triage Agent FastAPI Application.

Implements:
- POST /health: service liveness check.
- POST /triage: raw alert ingestion dispatched from thin bridge into agent-owned grouping engine.
- POST /timer/wake: Cloud Tasks delayed timer wakeups evaluating settle, max-wait, and cooling deadlines.
- POST /investigate: diagnostic investigation execution and report compilation.
"""

from datetime import datetime, timezone
import logging
from pathlib import Path
import time
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException

from .bridge_client import BridgeClient
from .config import AgentConfig, get_agent_settings
from .contracts.alert import AlertEvent, is_ata_alert
from .contracts.instructions import CreateTicketRequest, ResolveTicketRequest
from .contracts.timers import TimerWakeRequest, TimerWakeResponse
from .duplicate_checker import append_duplicate_alert_to_incident, check_for_duplicate
from .grouping.manager import GroupingManager
from .grouping.models import COOLING_WINDOW_SECONDS, IncidentGroupStatus, OKFDocument
from .grouping.store import OKFStore, read_okf, update_okf_atomically
from .timers.scheduler import schedule_delayed_timer
from .triage import (
    execute_multi_agent_investigation,
    generate_investigation_report,
    send_findings_to_bridge,
    write_preanalysis_lock,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Alert Triage Agent",
    description="Agent performing pre-analysis locking, multi-agent triage, and diagnostic investigations",
    version="0.3.0",
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
        if dup_res.is_duplicate and dup_res.active_file:
            logger.info(
                "Alert %s is an active duplicate of %s; appending alert and refreshing timestamp.",
                alert.incident_id,
                dup_res.active_file,
            )
            append_duplicate_alert_to_incident(
                file_path=dup_res.active_file,
                alert=alert,
                config=settings,
            )
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
        doc = store.update_atomically(
            doc.group_id,
            lambda d: setattr(d, "status", IncidentGroupStatus.INVESTIGATING.value) or d,
        )

        primary_alert = _extract_primary_alert(doc)
        filename = f"incident-{doc.group_id}.md"

        try:
            report = await execute_multi_agent_investigation(primary_alert, doc.group_id, filename, settings)
        except Exception as exc:
            logger.warning("Multi-agent investigation fallback on timer wake: %s", exc)
            report = generate_investigation_report(primary_alert, doc.group_id, filename, settings)

        report.group_id = doc.group_id
        report.filename = filename

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

        updated_doc = store.update_atomically(doc.group_id, _mark_ticketed)
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
        doc = store.update_atomically(
            doc.group_id,
            lambda d: setattr(d, "status", IncidentGroupStatus.INVESTIGATING.value) or d,
        )

        primary_alert = _extract_primary_alert(doc)
        filename = f"incident-{doc.group_id}.md"

        try:
            report = await execute_multi_agent_investigation(primary_alert, doc.group_id, filename, settings)
        except Exception as exc:
            logger.warning("Multi-agent investigation fallback on max_wait wake: %s", exc)
            report = generate_investigation_report(primary_alert, doc.group_id, filename, settings)

        report.group_id = doc.group_id
        report.filename = filename

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

        updated_doc = store.update_atomically(doc.group_id, _mark_ticketed_mw)
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

            store.update_atomically(doc.group_id, _mark_degraded)
            return TimerWakeResponse(
                status="rejected",
                group_id=doc.group_id,
                reason="unresolved_unticketed",
            )

        # Valid cooling wake: call bridge resolve and archive OKF
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

        store.update_atomically(doc.group_id, _mark_resolved)
        return TimerWakeResponse(status="executed", group_id=doc.group_id)

    return TimerWakeResponse(
        status="ignored",
        group_id=doc.group_id,
        reason=f"unsupported_deadline_type_{payload.deadline_type}",
    )


def _extract_primary_alert(doc: OKFDocument) -> AlertEvent:
    """Extract primary alert from OKFDocument for investigation synthesizers."""
    if doc.alerts:
        first_a = doc.alerts[0]
        return AlertEvent(
            incident_id=first_a.get("incident_id", "0.unknown"),
            policy_name=first_a.get("policy_name", doc.policy_name or "unknown"),
            resource_name=first_a.get("resource_name", doc.resource_name or "unknown"),
            state=first_a.get("state", "open"),
            started_at=first_a.get("started_at", doc.created_at),
        )
    return AlertEvent(
        incident_id="0.unknown",
        policy_name=doc.policy_name or "unknown",
        resource_name=doc.resource_name or "unknown",
        state="open",
        started_at=doc.created_at,
    )


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
        logger.warning("Multi-agent investigation fallback: %s", exc)
        report = generate_investigation_report(alert, group_id, filename, settings)

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
