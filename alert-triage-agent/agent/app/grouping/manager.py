"""Agent Incident Grouping and Suppression Manager.

Owns all alert grouping and suppression (bridge does zero suppression):
- Duplicate detection across active OKF documents.
- Alert correlation using CorrelationEngine.
- Cooling-down reactivation to ticketed with late alert work note.
- Ticketed group late alert work note appending.
- Open group settle deadline extension with Cloud Tasks delayed timer rescheduling.
- New incident group creation with settle and max-wait timers.
- Closed alert lifecycle handling and cool-down timer scheduling.
- Safe upstream DAG origin promotion.
"""

from datetime import datetime, timezone
import json
import logging
import re
import threading
import time
from typing import Any, Optional
from pydantic import BaseModel, Field

from ..bridge_client import BridgeClient
from ..config import AgentConfig, get_agent_settings
from ..contracts.alert import AlertEvent, slugify
from ..contracts.instructions import WorkNoteRequest
from ..correlation.engine import (
    CandidateGroup,
    CorrelationDecision,
    CorrelationEngine,
    candidate_from_okf,
)
from ..timers.scheduler import TimerScheduleError, schedule_delayed_timer
from .models import COOLING_WINDOW_SECONDS, IncidentGroupStatus, OKFDocument
from .store import OKFStore
from .suppression import (
    check_duplicate_alert,
    format_late_alert_work_note,
    safe_promote_upstream_origin,
)

logger = logging.getLogger(__name__)


_global_ingest_lock = threading.RLock()


class GroupingResult(BaseModel):
    """Result returned by GroupingManager.ingest_alert."""
    status: str  # "triaged", "duplicate", "closed"
    is_duplicate: bool = False
    group_id: str
    filename: str
    action: str  # "duplicate_suppressed", "closed_updated", "reactivate_cooling", "attached_ticketed", "attached_open", "new_group"
    doc: Optional[OKFDocument] = None
    correlation: Optional[dict[str, Any]] = None
    note_appended: bool = False
    new_settle_deadline: Optional[str] = None


class GroupingManager:
    """Manager orchestrating agent-owned incident grouping, suppression, and timers."""

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        store: Optional[OKFStore] = None,
        bridge_client: Optional[BridgeClient] = None,
        correlation_engine: Optional[CorrelationEngine] = None,
    ) -> None:
        self.config = config or get_agent_settings()
        self.store = store or OKFStore(config=self.config)
        self.bridge_client = bridge_client or BridgeClient(config=self.config)
        self.correlation_engine = correlation_engine or CorrelationEngine(
            incident_store_path=str(self.config.get_effective_store_path()),
            model_name=self.config.model_name,
            project_id=self.config.project_id,
        )

    def _emit_decision_log(
        self,
        event: str,
        alert: AlertEvent,
        action: str,
        group_id: Optional[str],
        status: Optional[str] = None,
        settle_deadline: Optional[str] = None,
    ) -> None:
        """Emit structured JSON log line for grouping decisions."""
        entry = {
            "event": event,
            "incident_id": alert.incident_id,
            "policy_name": alert.policy_name,
            "resource_name": alert.resource_name,
            "state": alert.state.lower() if alert.state else "unknown",
            "action": action,
            "group_id": group_id,
            "group_status": status,
            "settle_deadline": settle_deadline,
        }
        logger.info("JSON_DECISION: %s", json.dumps(entry))

    def _mark_group_degraded(self, group_id: str, reason: str) -> OKFDocument:
        """Mark incident group as degraded in OKF with failure reason."""
        def _mutate(doc: OKFDocument) -> OKFDocument:
            doc.status = IncidentGroupStatus.DEGRADED.value
            doc.degraded = True
            doc.degraded_reason = reason
            now_dt = datetime.now(timezone.utc)
            doc.updated_at = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            return doc

        return self.store.update_atomically(group_id, _mutate)

    def ingest_alert(
        self,
        alert: AlertEvent,
        current_time: Optional[float] = None,
        bridge_group_id: Optional[str] = None,
        bridge_filename: Optional[str] = None,
    ) -> GroupingResult:
        """Thread-safe ingestion enforcing serialization for same-second simultaneous arrival."""
        with _global_ingest_lock:
            return self._ingest_alert_locked(
                alert=alert,
                current_time=current_time,
                bridge_group_id=bridge_group_id,
                bridge_filename=bridge_filename,
            )

    def _ingest_alert_locked(
        self,
        alert: AlertEvent,
        current_time: Optional[float] = None,
        bridge_group_id: Optional[str] = None,
        bridge_filename: Optional[str] = None,
    ) -> GroupingResult:
        """Ingest incoming alert, enforcing idempotency, correlation, OKF mutations, and timers."""
        now = current_time if current_time is not None else time.time()
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Discover active incident documents
        active_docs = self.store.list_documents(
            status_filter={"open", "investigating", "ticketed", "cooling_down", "mitigation-in-progress", "stable"}
        )

        # 1. Closed alert handling
        if alert.state and alert.state.lower() == "closed":
            target_doc: Optional[OKFDocument] = None
            for d in active_docs:
                if d.has_alert(alert.incident_id):
                    target_doc = d
                    break

            if target_doc is None:
                # Correlate or match by resource if alert.incident_id wasn't recorded yet
                for d in active_docs:
                    if d.resource_name == alert.resource_name or d.origin == alert.resource_name:
                        target_doc = d
                        break

            if target_doc is not None:
                # If alert already marked closed, suppress duplicate
                for a in target_doc.alerts:
                    if a.get("incident_id") == alert.incident_id and str(a.get("state", "")).lower() == "closed":
                        return GroupingResult(
                            status="closed",
                            is_duplicate=True,
                            group_id=target_doc.group_id,
                            filename=f"incident-{target_doc.group_id}.md",
                            action="duplicate_suppressed",
                            doc=target_doc,
                        )

                cooling_scheduled = False

                def _close_mutation(d: OKFDocument) -> OKFDocument:
                    nonlocal cooling_scheduled
                    d.update_alert_state(alert.incident_id, "closed")
                    if not d.has_alert(alert.incident_id):
                        d.add_alert({
                            "incident_id": alert.incident_id,
                            "policy_name": alert.policy_name,
                            "resource_name": alert.resource_name,
                            "state": "closed",
                            "started_at": alert.started_at,
                        })
                    if d.all_alerts_closed():
                        is_ticketed = d.servicenow_sys_id is not None or str(d.status).lower() == IncidentGroupStatus.TICKETED.value
                        if is_ticketed:
                            d.status = IncidentGroupStatus.COOLING_DOWN.value
                            cooling_epoch = now + COOLING_WINDOW_SECONDS
                            cooling_dt = datetime.fromtimestamp(cooling_epoch, tz=timezone.utc)
                            d.cooling_deadline = cooling_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                            cooling_scheduled = True
                    d.updated_at = now_iso
                    return d

                updated_doc = self.store.update_atomically(target_doc.group_id, _close_mutation)
                if cooling_scheduled and updated_doc.cooling_deadline:
                    cool_dt = datetime.fromisoformat(updated_doc.cooling_deadline.replace("Z", "+00:00"))
                    try:
                        schedule_delayed_timer(
                            project_id=self.config.project_id,
                            location=self.config.location,
                            queue_name=self.config.timer_queue_name,
                            target_url=self.config.agent_url,
                            group_id=updated_doc.group_id,
                            deadline_type="cooling",
                            deadline_dt=cool_dt,
                            service_account_email=self.config.service_account_email,
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to schedule cooling timer for group %s: %s",
                            updated_doc.group_id,
                            exc,
                            exc_info=True,
                        )
                        self._mark_group_degraded(updated_doc.group_id, str(exc))
                        raise

                self._emit_decision_log(
                    event="alert_ingested",
                    alert=alert,
                    action="closed_updated",
                    group_id=updated_doc.group_id,
                    status=str(updated_doc.status),
                )
                return GroupingResult(
                    status="closed",
                    is_duplicate=False,
                    group_id=updated_doc.group_id,
                    filename=f"incident-{updated_doc.group_id}.md",
                    action="closed_updated",
                    doc=updated_doc,
                )

            # No matching active group found for closed alert
            return GroupingResult(
                status="closed",
                is_duplicate=False,
                group_id=bridge_group_id or "unknown",
                filename=bridge_filename or f"incident-{bridge_group_id}.md" if bridge_group_id else "unknown.md",
                action="closed_no_group",
            )

        # 2. Duplicate detection for open alerts: if alert.incident_id already active in doc.alerts, suppress as duplicate
        is_dup, dup_doc = check_duplicate_alert(alert, active_docs)
        if is_dup and dup_doc is not None:
            alert_in_doc = next((a for a in dup_doc.alerts if a.get("incident_id") == alert.incident_id), None)
            if alert_in_doc and str(alert_in_doc.get("state", "open")).lower() != "closed":
                self._emit_decision_log(
                    event="alert_ingested",
                    alert=alert,
                    action="duplicate_suppressed",
                    group_id=dup_doc.group_id,
                    status=str(dup_doc.status),
                )
                return GroupingResult(
                    status="duplicate",
                    is_duplicate=True,
                    group_id=dup_doc.group_id,
                    filename=f"incident-{dup_doc.group_id}.md",
                    action="duplicate_suppressed",
                    doc=dup_doc,
                    correlation={"decision": "duplicate", "best_score": 100.0, "confidence": 1.0},
                )

        # 3. Open alert: Correlate with existing groups or create new group
        matched_group_id: Optional[str] = None
        correlation_info: dict[str, Any] = {}

        if bridge_group_id and any(d.group_id == bridge_group_id for d in active_docs):
            matched_group_id = bridge_group_id
            correlation_info = {"decision": "attach", "best_score": 100.0, "confidence": 1.0}
        else:
            candidates = [candidate_from_okf(d) for d in active_docs]
            decision = self.correlation_engine.evaluate_alert(alert, candidate_groups=candidates)
            correlation_info = {
                "decision": decision.decision,
                "best_score": decision.best_score,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "is_gray_zone": decision.is_gray_zone,
            }
            if decision.decision == "attach" and decision.target_group_id:
                matched_group_id = decision.target_group_id

        # Case A: Attaches to an existing group
        if matched_group_id:
            note_appended = False
            rescheduled_settle: Optional[str] = None

            def _attach_mutation(d: OKFDocument) -> OKFDocument:
                nonlocal note_appended, rescheduled_settle
                # Safe upstream DAG origin promotion
                safe_promote_upstream_origin(d, alert)

                # Add alert
                d.add_alert({
                    "incident_id": alert.incident_id,
                    "policy_name": alert.policy_name,
                    "resource_name": alert.resource_name,
                    "state": alert.state,
                    "started_at": alert.started_at,
                    "received_at": now_iso,
                })
                d.updated_at = now_iso

                prev_status = str(d.status).lower()
                if prev_status == "cooling_down":
                    # Reactivate to ticketed
                    d.status = IncidentGroupStatus.TICKETED.value
                    d.cooling_deadline = None
                elif prev_status in ("open", "mitigation-in-progress"):
                    # Extend settle deadline capped at max_wait_deadline
                    new_settle_epoch = d.extend_settle(
                        current_time=now,
                        settle_duration=120.0,
                        cap_at_max_wait=True,
                    )
                    rescheduled_settle = d.settle_deadline

                return d

            updated_doc = self.store.update_atomically(matched_group_id, _attach_mutation)

            # Handle post-mutation side-effects (Bridge work note, timer scheduling)
            curr_status = str(updated_doc.status).lower()
            if curr_status == "ticketed" or updated_doc.servicenow_sys_id:
                if updated_doc.servicenow_sys_id:
                    note = format_late_alert_work_note(alert, updated_doc.blast_radius)
                    try:
                        self.bridge_client.append_work_note(
                            bridge_url=self.config.bridge_url,
                            request=WorkNoteRequest(
                                sys_id=updated_doc.servicenow_sys_id,
                                work_note=note,
                                group_id=updated_doc.group_id,
                            ),
                        )
                        note_appended = True
                    except Exception as exc:
                        logger.warning("Failed appending late alert work note to bridge: %s", exc)

            if rescheduled_settle and updated_doc.settle_deadline:
                settle_dt = datetime.fromisoformat(updated_doc.settle_deadline.replace("Z", "+00:00"))
                try:
                    schedule_delayed_timer(
                        project_id=self.config.project_id,
                        location=self.config.location,
                        queue_name=self.config.timer_queue_name,
                        target_url=self.config.agent_url,
                        group_id=updated_doc.group_id,
                        deadline_type="settle",
                        deadline_dt=settle_dt,
                        service_account_email=self.config.service_account_email,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to schedule settle timer for group %s: %s",
                        updated_doc.group_id,
                        exc,
                        exc_info=True,
                    )
                    self._mark_group_degraded(updated_doc.group_id, str(exc))
                    raise

            action_name = "reactivate_cooling" if str(updated_doc.status).lower() == "ticketed" and not rescheduled_settle else (
                "attached_open" if rescheduled_settle else "attached_ticketed"
            )
            self._emit_decision_log(
                event="alert_ingested",
                alert=alert,
                action=action_name,
                group_id=updated_doc.group_id,
                status=str(updated_doc.status),
                settle_deadline=updated_doc.settle_deadline,
            )
            return GroupingResult(
                status="triaged",
                is_duplicate=False,
                group_id=updated_doc.group_id,
                filename=f"incident-{updated_doc.group_id}.md",
                action=action_name,
                doc=updated_doc,
                correlation=correlation_info,
                note_appended=note_appended,
                new_settle_deadline=updated_doc.settle_deadline,
            )

        # Case B: Creates brand new incident group
        pol_slug = alert.policy_slug
        res_slug = slugify(alert.resource_name)
        clean_ts = re.sub(r"[^0-9a-zA-Z]", "", now_iso).lower()

        group_id = bridge_group_id or f"{pol_slug}-{res_slug}-{clean_ts}"
        filename = bridge_filename or f"incident-{group_id}.md"

        settle_epoch = now + 120.0
        max_wait_epoch = now + 300.0
        settle_dt = datetime.fromtimestamp(settle_epoch, tz=timezone.utc)
        max_wait_dt = datetime.fromtimestamp(max_wait_epoch, tz=timezone.utc)

        # Compute initial blast radius
        initial_blast = [alert.resource_name]
        try:
            from topology.topology_graph import get_blast_radius
        except ImportError:
            get_blast_radius = None

        if get_blast_radius is not None:
            try:
                for b in get_blast_radius(alert.resource_name):
                    if b not in initial_blast:
                        initial_blast.append(b)
            except Exception as b_exc:
                logger.debug("Failed computing blast radius: %s", b_exc)

        new_doc = OKFDocument(
            group_id=group_id,
            status=IncidentGroupStatus.OPEN.value,
            project=self.config.project_id,
            policy_name=alert.policy_name,
            resource_name=alert.resource_name,
            origin=alert.resource_name,
            blast_radius=initial_blast,
            priority="P1" if any(k in alert.policy_name.lower() for k in ["critical", "5xx"]) else "P2",
            created_at=now_iso,
            updated_at=now_iso,
            timestamp=now_iso,
            settle_deadline=settle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            max_wait_deadline=max_wait_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            alerts=[{
                "incident_id": alert.incident_id,
                "policy_name": alert.policy_name,
                "resource_name": alert.resource_name,
                "state": alert.state,
                "started_at": alert.started_at,
                "received_at": now_iso,
            }],
        )

        saved_doc = self.store.write(group_id, new_doc)

        # Schedule settle timer
        try:
            schedule_delayed_timer(
                project_id=self.config.project_id,
                location=self.config.location,
                queue_name=self.config.timer_queue_name,
                target_url=self.config.agent_url,
                group_id=group_id,
                deadline_type="settle",
                deadline_dt=settle_dt,
                service_account_email=self.config.service_account_email,
            )
        except Exception as exc:
            logger.error(
                "Failed to schedule settle timer for group %s: %s",
                group_id,
                exc,
                exc_info=True,
            )
            self._mark_group_degraded(group_id, str(exc))
            raise

        # Schedule max-wait timer
        try:
            schedule_delayed_timer(
                project_id=self.config.project_id,
                location=self.config.location,
                queue_name=self.config.timer_queue_name,
                target_url=self.config.agent_url,
                group_id=group_id,
                deadline_type="max_wait",
                deadline_dt=max_wait_dt,
                service_account_email=self.config.service_account_email,
            )
        except Exception as exc:
            logger.error(
                "Failed to schedule max_wait timer for group %s: %s",
                group_id,
                exc,
                exc_info=True,
            )
            self._mark_group_degraded(group_id, str(exc))
            raise

        self._emit_decision_log(
            event="alert_ingested",
            alert=alert,
            action="new_group",
            group_id=group_id,
            status=saved_doc.status,
            settle_deadline=saved_doc.settle_deadline,
        )

        return GroupingResult(
            status="triaged",
            is_duplicate=False,
            group_id=group_id,
            filename=filename,
            action="new_group",
            doc=saved_doc,
            correlation=correlation_info or {"decision": "new_group", "best_score": 0.0, "confidence": 1.0},
            new_settle_deadline=saved_doc.settle_deadline,
        )
