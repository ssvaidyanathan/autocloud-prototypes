from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import threading
import time
from typing import Any, Optional

from ..adapters.servicenow import ServiceNowAdapter, ServiceNowIncidentRef
from ..config import BridgeConfig, get_settings
from ..contracts.alert import AlertEvent, slugify
from ..contracts.report import InvestigationReport
from .dispatcher import TaskDispatcher

try:
    from topology.topology_graph import get_blast_radius, calculate_hop_distance, _canonicalize_node, get_upstream_dependencies
except ImportError:
    get_blast_radius = None
    calculate_hop_distance = None
    _canonicalize_node = None
    get_upstream_dependencies = None

logger = logging.getLogger(__name__)


class IncidentGroupStatus(str, Enum):
    OPEN = "open"
    MITIGATION_IN_PROGRESS = "mitigation-in-progress"
    COOLING_DOWN = "cooling-down"
    STABLE = "stable"
    INVESTIGATING = "investigating"
    TICKETED = "ticketed"
    RESOLVED = "resolved"


@dataclass
class IncidentGroup:
    """Represents an active or resolved correlated incident group."""
    group_id: str
    filename: str
    status: IncidentGroupStatus = IncidentGroupStatus.OPEN
    created_at: float = field(default_factory=time.time)
    settle_deadline: float = 0.0
    max_wait_deadline: float = 0.0
    alerts: dict[str, AlertEvent] = field(default_factory=dict)
    origin: str = "unknown"
    blast_radius: list[str] = field(default_factory=list)
    servicenow_sys_id: Optional[str] = None
    servicenow_number: Optional[str] = None
    servicenow_url: Optional[str] = None
    last_settle_reset: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    archived: bool = False
    archived_at: Optional[float] = None
    resolved_at: Optional[float] = None
    cool_down_duration: float = 180.0
    cool_down_deadline: Optional[float] = None
    latest_report: Optional[InvestigationReport] = None

    def __post_init__(self) -> None:
        if self.settle_deadline == 0.0:
            self.settle_deadline = self.created_at + 120.0
        if self.max_wait_deadline == 0.0:
            self.max_wait_deadline = self.created_at + 300.0
        if self.last_settle_reset == 0.0:
            self.last_settle_reset = self.created_at
        if self.cool_down_duration == 0.0:
            self.cool_down_duration = 180.0

    def archive(self, current_time: Optional[float] = None) -> None:
        """Mark group as resolved and archived."""
        now = current_time if current_time is not None else time.time()
        self.status = IncidentGroupStatus.RESOLVED
        self.archived = True
        self.archived_at = now
        if not self.resolved_at:
            self.resolved_at = now

    def add_alert(self, alert: AlertEvent, current_time: Optional[float] = None) -> None:
        """Add or update an alert in the group, resetting the 120s settle deadline."""
        now = current_time if current_time is not None else time.time()
        self.alerts[alert.incident_id] = alert
        self.settle_deadline = now + 120.0
        self.last_settle_reset = now

        canon_alert = _canonicalize_node(alert.resource_name) if _canonicalize_node else alert.resource_name
        canon_curr_origin = _canonicalize_node(self.origin) if _canonicalize_node else self.origin

        # Upstream origin promotion: if incoming alert is on an upstream dependency of current origin,
        # promote origin to the upstream component and expand blast radius
        if get_upstream_dependencies is not None and canon_alert and canon_curr_origin:
            try:
                upstreams = get_upstream_dependencies(canon_curr_origin)
                if canon_alert in upstreams:
                    logger.info("Promoting group %s origin from %s to upstream %s", self.group_id, self.origin, alert.resource_name)
                    self.origin = alert.resource_name
                    if get_blast_radius is not None:
                        for b in get_blast_radius(self.origin):
                            if b not in self.blast_radius:
                                self.blast_radius.append(b)
            except Exception as exc:
                logger.debug("Upstream origin promotion check error: %s", exc)

        if alert.resource_name and alert.resource_name not in self.blast_radius:
            self.blast_radius.append(alert.resource_name)

    def evaluate_deadline(self, current_time: Optional[float] = None) -> bool:
        """Check if group has stabilized via 120s quiet period or 300s max wait deadline."""
        now = current_time if current_time is not None else time.time()
        if self.status in (IncidentGroupStatus.OPEN, IncidentGroupStatus.MITIGATION_IN_PROGRESS):
            # Check 300s max wait timeout first
            if now >= self.max_wait_deadline:
                logger.info(
                    "Group %s reached 300s max wait deadline (now=%.2f, max_wait=%.2f)",
                    self.group_id,
                    now,
                    self.max_wait_deadline,
                )
                self.status = IncidentGroupStatus.STABLE
                return True

            # Check 120s quiet settle period
            if now >= self.settle_deadline:
                logger.info(
                    "Group %s reached 120s settle period (now=%.2f, settle=%.2f)",
                    self.group_id,
                    now,
                    self.settle_deadline,
                )
                self.status = IncidentGroupStatus.STABLE
                return True

        return False

    def update_alert_state(self, incident_id: str, state: str, current_time: Optional[float] = None) -> bool:
        """Update an individual alert's state; returns True if all alerts are closed and resolved immediately (no cool-down)."""
        now = current_time if current_time is not None else time.time()
        if incident_id in self.alerts:
            self.alerts[incident_id].state = state.lower()
        if self.alerts and all(a.state.lower() == "closed" for a in self.alerts.values()):
            if self.cool_down_duration > 0.0:
                self.status = IncidentGroupStatus.COOLING_DOWN
                self.cool_down_deadline = now + self.cool_down_duration
                return False
            self.status = IncidentGroupStatus.RESOLVED
            self.resolved_at = now
            return True
        return False


class WindowingStateMachine:
    """Tracks active incident groups across 120s settle and 300s max wait windows."""

    def __init__(
        self,
        config: Optional[BridgeConfig] = None,
        dispatcher: Optional[TaskDispatcher] = None,
        adapter: Optional[ServiceNowAdapter] = None,
        settle_duration: float = 120.0,
        max_wait_duration: float = 300.0,
        cool_down_duration: Optional[float] = None,
    ) -> None:
        self.config = config or get_settings()
        self.dispatcher = dispatcher
        self.adapter = adapter
        self.settle_duration = settle_duration
        self.max_wait_duration = max_wait_duration
        if cool_down_duration is not None:
            self.cool_down_duration = cool_down_duration
        else:
            self.cool_down_duration = getattr(self.config, "cool_down_duration", 180.0)
        self.groups: dict[str, IncidentGroup] = {}
        self.archived_groups: dict[str, IncidentGroup] = {}
        self.seen_alerts: dict[str, str] = {}  # incident_id -> state
        self._lock = threading.RLock()
        self._group_locks: dict[str, threading.Lock] = {}

    def get_group_lock(self, group_id: str) -> threading.Lock:
        with self._lock:
            if group_id not in self._group_locks:
                self._group_locks[group_id] = threading.Lock()
            return self._group_locks[group_id]

    def find_matching_group(self, alert: AlertEvent) -> Optional[IncidentGroup]:
        """Find an existing active group correlated with the given alert."""
        with self._lock:
            # For closed alerts: check active groups first, then archived groups
            if alert.state.lower() == "closed":
                for group in self.groups.values():
                    if alert.incident_id in group.alerts:
                        return group
                for group in self.groups.values():
                    if (
                        alert.resource_name == group.origin
                        or alert.resource_name in group.blast_radius
                        or any(a.policy_slug == alert.policy_slug for a in group.alerts.values())
                    ):
                        return group
                for group in self.archived_groups.values():
                    if alert.incident_id in group.alerts:
                        return group
                return None

            # For OPEN / firing alerts: ONLY match active (unresolved, unarchived) groups
            for group in self.groups.values():
                if group.status != IncidentGroupStatus.RESOLVED and not group.archived:
                    if alert.incident_id in group.alerts:
                        return group

            canon_alert_res = _canonicalize_node(alert.resource_name) if _canonicalize_node else alert.resource_name
            alert_is_429 = "429" in alert.policy_name or "spikearrest" in alert.policy_name.lower()

            for group in self.groups.values():
                if group.status != IncidentGroupStatus.RESOLVED and not group.archived:
                    group_is_429 = any(
                        "429" in a.policy_name or "spikearrest" in a.policy_name.lower()
                        for a in group.alerts.values()
                    ) or "429" in group.group_id

                    # Incompatible failure classes: SpikeArrest 429 never merges with backend failures
                    if alert_is_429 != group_is_429:
                        continue

                    canon_origin = _canonicalize_node(group.origin) if _canonicalize_node else group.origin
                    canon_blast = [_canonicalize_node(b) if _canonicalize_node else b for b in group.blast_radius]

                    is_coupled = False
                    if calculate_hop_distance is not None:
                        dist = calculate_hop_distance(canon_alert_res, canon_origin)
                        if dist is not None and dist <= 2:
                            is_coupled = True
                        else:
                            for b in canon_blast:
                                d = calculate_hop_distance(canon_alert_res, b)
                                if d is not None and d <= 1:
                                    is_coupled = True
                                    break

                    if (
                        alert.resource_name == group.origin
                        or canon_alert_res == canon_origin
                        or alert.resource_name in group.blast_radius
                        or canon_alert_res in canon_blast
                        or is_coupled
                        or any(a.policy_slug == alert.policy_slug for a in group.alerts.values())
                    ):
                        return group

            return None

    def archive_group(
        self,
        group_id: str,
        current_time: Optional[float] = None,
    ) -> Optional[IncidentGroup]:
        """Archive a group: mark resolved and archived, move from active to archived_groups."""
        now = current_time if current_time is not None else time.time()
        with self._lock:
            group = self.groups.get(group_id) or self.archived_groups.get(group_id)
            if group is None:
                return None
            group.archive(current_time=now)
            # Remove all references to this group from self.groups
            keys_to_remove = [k for k, g in self.groups.items() if g is group or k == group.group_id or k == group_id]
            for k in keys_to_remove:
                self.groups.pop(k, None)
            self.archived_groups[group.group_id] = group
            logger.info("Archived group %s (status=%s)", group.group_id, group.status)
            return group

    def reset(self, current_time: Optional[float] = None) -> dict[str, Any]:
        """Resolve and archive all active groups, clear deduplication state, and return summary."""
        now = current_time if current_time is not None else time.time()
        with self._lock:
            archived_ids: list[str] = []
            unique_groups = list({id(g): g for g in self.groups.values()}.values())

            for group in unique_groups:
                group.archive(current_time=now)
                self.archived_groups[group.group_id] = group
                archived_ids.append(group.group_id)

                if group.servicenow_sys_id and self.adapter:
                    try:
                        self.adapter.append_resolution_work_note(group.servicenow_sys_id)
                    except Exception as exc:
                        logger.warning("Failed to append reset resolution note to %s: %s", group.servicenow_sys_id, exc)

                if group.filename:
                    try:
                        update_gcs_okf_servicenow(
                            filename=group.filename,
                            servicenow_number=group.servicenow_number or "",
                            servicenow_sys_id=group.servicenow_sys_id or "",
                            status="resolved",
                            config=self.config,
                        )
                    except Exception as exc:
                        logger.warning("Failed to update GCS OKF status on reset for %s: %s", group.filename, exc)

            self.groups.clear()
            self.seen_alerts.clear()
            if self.dispatcher and hasattr(self.dispatcher, "clear"):
                self.dispatcher.clear()

            logger.info("Windowing state machine reset: archived %d groups", len(archived_ids))
            return {
                "status": "ok",
                "resolved_count": len(archived_ids),
                "archived_groups": archived_ids,
                "active_groups": 0,
            }

    def _log_decision(
        self,
        event: str,
        alert: AlertEvent,
        decision: str,
        group: Optional[IncidentGroup],
    ) -> None:
        """Emit exactly one structured JSON log line per alert evaluation."""
        entry = {
            "event": event,
            "incident_id": alert.incident_id,
            "policy_name": alert.policy_name,
            "resource_name": alert.resource_name,
            "state": alert.state.lower(),
            "decision": decision,
            "target_group_id": group.group_id if group else None,
            "origin": group.origin if group else None,
            "blast_radius": group.blast_radius if group else [],
            "settle_deadline_epoch": group.settle_deadline if group else None,
        }
        logger.info("JSON_DECISION: %s", json.dumps(entry))

    def _log_findings_decision(
        self,
        report_group_id: str,
        matched_group_id: str,
        action: str,
        servicenow_number: Optional[str],
    ) -> None:
        """Emit exactly one structured JSON log line on callback in handle_investigation_findings."""
        entry = {
            "event": "findings_received",
            "report_group_id": report_group_id,
            "matched_in_memory_group": matched_group_id,
            "action": action,
            "servicenow_number": servicenow_number or "",
        }
        logger.info("JSON_DECISION: %s", json.dumps(entry))

    def ingest_alert(
        self,
        alert: AlertEvent,
        current_time: Optional[float] = None,
    ) -> tuple[Optional[IncidentGroup], bool]:
        """Ingest incoming alert, enforcing idempotency and windowing timers.

        Returns:
            (group, is_duplicate)
        """
        now = current_time if current_time is not None else time.time()

        with self._lock:
            # Idempotency check: exactly same incident_id and state already seen
            if self.seen_alerts.get(alert.incident_id) == alert.state.lower():
                logger.info("Duplicate alert %s with state %s ignored", alert.incident_id, alert.state)
                group = self.find_matching_group(alert)
                self._log_decision(
                    event="alert_ingested",
                    alert=alert,
                    decision="duplicate_ignored",
                    group=group,
                )
                return group, True

            self.seen_alerts[alert.incident_id] = alert.state.lower()

            # Alert closure handling
            if alert.state.lower() == "closed":
                group = self.find_matching_group(alert)
                decision = "alert_closed"
                if group is not None:
                    was_resolved = group.update_alert_state(alert.incident_id, "closed", current_time=now)
                    logger.info("Alert %s marked closed in group %s (resolved=%s, status=%s)", alert.incident_id, group.group_id, was_resolved, group.status)
                    if was_resolved:
                        self.archive_group(group.group_id, current_time=now)
                        if group.servicenow_sys_id and self.adapter:
                            self.adapter.append_resolution_work_note(group.servicenow_sys_id)
                        if group.filename:
                            update_gcs_okf_servicenow(
                                filename=group.filename,
                                servicenow_number=group.servicenow_number or "",
                                servicenow_sys_id=group.servicenow_sys_id or "",
                                status="resolved",
                                config=self.config,
                            )
                self._log_decision(
                    event="alert_ingested",
                    alert=alert,
                    decision=decision,
                    group=group,
                )
                return group, False

            # Open alert handling: correlate or create new group
            existing_group = self.find_matching_group(alert)
            if existing_group is not None:
                # If group was in cooling down, reactivate it!
                if existing_group.status == IncidentGroupStatus.COOLING_DOWN:
                    logger.info("Reactivating cooling-down group %s upon arrival of alert %s", existing_group.group_id, alert.incident_id)
                    existing_group.cool_down_deadline = None
                    existing_group.status = (
                        IncidentGroupStatus.TICKETED
                        if existing_group.servicenow_sys_id
                        else IncidentGroupStatus.MITIGATION_IN_PROGRESS
                    )

                existing_group.add_alert(alert, current_time=now)
                logger.info(
                    "Alert %s joined existing group %s; reset settle deadline to %.2f",
                    alert.incident_id,
                    existing_group.group_id,
                    existing_group.settle_deadline,
                )
                # If group is already ticketed in ServiceNow, append late alert work note
                if (existing_group.status == IncidentGroupStatus.TICKETED or existing_group.servicenow_sys_id) and existing_group.servicenow_sys_id and self.adapter:
                    self.adapter.append_late_alert_work_note(
                        existing_group.servicenow_sys_id,
                        alert,
                        existing_group.blast_radius,
                    )
                self._log_decision(
                    event="alert_ingested",
                    alert=alert,
                    decision="join_group",
                    group=existing_group,
                )
                return existing_group, False

            # New group creation
            group_id = f"{alert.policy_slug}-{alert.resource_slug}-{int(now)}"
            iso_time = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"incident-{alert.policy_slug}-{alert.resource_slug}-{iso_time}.md"

            initial_blast = [alert.resource_name]
            if get_blast_radius is not None:
                try:
                    for b in get_blast_radius(alert.resource_name):
                        if b not in initial_blast:
                            initial_blast.append(b)
                except Exception as b_exc:
                    logger.debug("Failed computing blast radius for new group: %s", b_exc)

            group = IncidentGroup(
                group_id=group_id,
                filename=filename,
                status=IncidentGroupStatus.MITIGATION_IN_PROGRESS,
                created_at=now,
                settle_deadline=now + self.settle_duration,
                max_wait_deadline=now + self.max_wait_duration,
                alerts={alert.incident_id: alert},
                origin=alert.resource_name,
                blast_radius=initial_blast,
                last_settle_reset=now,
                cool_down_duration=self.cool_down_duration,
            )
            self.groups[group_id] = group
            logger.info(
                "Created new incident group %s (settle=%.2f, max_wait=%.2f)",
                group_id,
                group.settle_deadline,
                group.max_wait_deadline,
            )

            self._log_decision(
                event="alert_ingested",
                alert=alert,
                decision="create_group",
                group=group,
            )
            return group, False

    def check_deadlines(self, current_time: Optional[float] = None) -> list[IncidentGroup]:
        """Check active groups for settle period, max wait timeout, or cool-down expiry."""
        now = current_time if current_time is not None else time.time()
        stabilized: list[IncidentGroup] = []

        with self._lock:
            for group in list(self.groups.values()):
                # Check cooling-down expiry
                if group.status == IncidentGroupStatus.COOLING_DOWN:
                    if group.cool_down_deadline is not None and now >= group.cool_down_deadline:
                        logger.info(
                            "Group %s cool-down expired (now=%.2f, deadline=%.2f); resolving and archiving",
                            group.group_id,
                            now,
                            group.cool_down_deadline,
                        )
                        self.archive_group(group.group_id, current_time=now)
                        if group.servicenow_sys_id and self.adapter:
                            self.adapter.append_resolution_work_note(group.servicenow_sys_id)
                        if group.filename:
                            update_gcs_okf_servicenow(
                                filename=group.filename,
                                servicenow_number=group.servicenow_number or "",
                                servicenow_sys_id=group.servicenow_sys_id or "",
                                status="resolved",
                                config=self.config,
                            )
                    continue

                if group.status in (IncidentGroupStatus.OPEN, IncidentGroupStatus.MITIGATION_IN_PROGRESS):
                    if group.evaluate_deadline(current_time=now):
                        stabilized.append(group)
                        group.status = IncidentGroupStatus.INVESTIGATING
                        # Trigger investigation task dispatch
                        if self.dispatcher:
                            self.dispatcher.dispatch_investigation(group, current_time=now)

            return stabilized

    @staticmethod
    def _update_group_origin_safely(group: IncidentGroup, new_origin: Optional[str]) -> None:
        """Update group origin upstream only, preventing downstream demotion."""
        if not new_origin:
            return
        if not group.origin:
            group.origin = new_origin
            return
        if new_origin == group.origin:
            return
        try:
            from topology.topology_graph import get_upstream_dependencies
            upstream_of_curr = get_upstream_dependencies(group.origin)
            if new_origin in upstream_of_curr:
                logger.info("Promoting group %s origin upstream: %s -> %s", group.group_id, group.origin, new_origin)
                group.origin = new_origin
        except Exception as exc:
            logger.debug("Failed checking upstream dependencies during origin update: %s", exc)

    def handle_investigation_findings(
        self,
        report: InvestigationReport,
    ) -> Optional[ServiceNowIncidentRef]:
        """Handle investigation findings callback, create ServiceNow incident ticket idempotently, and update status."""
        target_file = report.filename or f"incident-{report.group_id}.md"
        adapter = self.adapter or ServiceNowAdapter(config=self.config)

        group_lock = self.get_group_lock(report.group_id)
        with group_lock:
            group = self.groups.get(report.group_id)
            if group is None:
                # Check archived_groups
                group = self.archived_groups.get(report.group_id)
            if group is None:
                # Check filename or group_id alias
                for g in list(self.groups.values()) + list(self.archived_groups.values()):
                    if g.filename == report.group_id or g.filename == report.filename or g.group_id == report.group_id:
                        group = g
                        break

            # If not in memory, check if existing incident can be recovered from persisted OKF or ServiceNow
            if group is None:
                okf_sys_id, okf_number = read_okf_servicenow(
                    filename=target_file,
                    group_id=report.group_id,
                    config=self.config,
                )
                existing_ref = None
                if not okf_sys_id:
                    correlation_ids = [target_file, report.group_id]
                    for cid in correlation_ids:
                        if cid:
                            existing_ref = adapter.find_incident_by_correlation_id(cid)
                            if existing_ref is not None:
                                okf_sys_id = existing_ref.sys_id
                                okf_number = existing_ref.number
                                break

                if okf_sys_id:
                    group = IncidentGroup(
                        group_id=report.group_id,
                        filename=target_file,
                        status=IncidentGroupStatus.TICKETED,
                        created_at=time.time(),
                        origin=report.origin,
                        blast_radius=report.blast_radius,
                        servicenow_sys_id=okf_sys_id,
                        servicenow_number=okf_number,
                    )
                    with self._lock:
                        self.groups[report.group_id] = group
                else:
                    decision_log = {
                        "event": "findings_rejected_unknown_group",
                        "report_group_id": report.group_id,
                        "filename": report.filename,
                        "error": "unknown_group_id",
                    }
                    logger.error("JSON_DECISION: %s", json.dumps(decision_log))
                    raise ValueError(f"Unknown group_id in investigation report: {report.group_id}")

            target_file = report.filename or group.filename or f"incident-{report.group_id}.md"
            group.filename = target_file
            group.latest_report = report

            # Check 1: In-memory ticket check (already created)
            if group.servicenow_sys_id:
                logger.info(
                    "Group %s already ticketed with %s; updating existing incident",
                    group.group_id,
                    group.servicenow_number or group.servicenow_sys_id,
                )
                if report.origin:
                    self._update_group_origin_safely(group, report.origin)
                if report.blast_radius:
                    for b in report.blast_radius:
                        if b not in group.blast_radius:
                            group.blast_radius.append(b)
                ref = adapter.update_incident(
                    group.servicenow_sys_id,
                    report,
                    number=group.servicenow_number,
                )
                update_gcs_okf_servicenow(
                    filename=target_file,
                    servicenow_number=ref.number,
                    servicenow_sys_id=ref.sys_id,
                    status="mitigation-in-progress",
                    config=self.config,
                )
                self._log_findings_decision(report.group_id, group.group_id, "ticket_updated", ref.number)
                return ref

            # Check 2: Report-provided ticket check
            if getattr(report, "servicenow_sys_id", None):
                group.servicenow_sys_id = report.servicenow_sys_id
                group.servicenow_number = report.servicenow_number
                group.status = IncidentGroupStatus.TICKETED
                ref = adapter.update_incident(
                    group.servicenow_sys_id,
                    report,
                    number=group.servicenow_number,
                )
                update_gcs_okf_servicenow(
                    filename=target_file,
                    servicenow_number=ref.number,
                    servicenow_sys_id=ref.sys_id,
                    status="mitigation-in-progress",
                    config=self.config,
                )
                self._log_findings_decision(report.group_id, group.group_id, "ticket_updated", ref.number)
                return ref

            # Check 3: Persisted OKF check (recovers ticket after bridge restart mid-group)
            okf_sys_id, okf_number = read_okf_servicenow(
                filename=target_file,
                group_id=report.group_id,
                config=self.config,
            )
            if okf_sys_id:
                logger.info(
                    "Recovered ServiceNow ticket %s (%s) from OKF for group %s after restart",
                    okf_number,
                    okf_sys_id,
                    group.group_id,
                )
                group.servicenow_sys_id = okf_sys_id
                group.servicenow_number = okf_number
                base_url = (self.config.servicenow_instance_url or "").rstrip("/")
                group.servicenow_url = (
                    f"{base_url}/nav_to.do?uri=incident.do?sys_id={okf_sys_id}"
                    if base_url
                    else f"gs://{self.config.effective_incidents_bucket}/reports/{group.group_id}.html"
                )
                group.status = IncidentGroupStatus.TICKETED
                ref = adapter.update_incident(
                    group.servicenow_sys_id,
                    report,
                    number=group.servicenow_number,
                )
                self._log_findings_decision(report.group_id, group.group_id, "ticket_updated", ref.number)
                return ref

            # Check 4: Deterministic correlation_id query before create
            correlation_ids = [target_file, report.group_id, group.group_id]
            for cid in correlation_ids:
                if cid:
                    existing_ref = adapter.find_incident_by_correlation_id(cid)
                    if existing_ref is not None:
                        logger.info(
                            "Found existing ServiceNow incident %s (%s) via correlation_id %s",
                            existing_ref.number,
                            existing_ref.sys_id,
                            cid,
                        )
                        group.servicenow_sys_id = existing_ref.sys_id
                        group.servicenow_number = existing_ref.number
                        group.servicenow_url = existing_ref.url
                        group.status = IncidentGroupStatus.TICKETED
                        update_gcs_okf_servicenow(
                            filename=target_file,
                            servicenow_number=group.servicenow_number,
                            servicenow_sys_id=group.servicenow_sys_id,
                            status="mitigation-in-progress",
                            config=self.config,
                        )
                        ref = adapter.update_incident(
                            group.servicenow_sys_id,
                            report,
                            number=group.servicenow_number,
                        )
                        self._log_findings_decision(report.group_id, group.group_id, "ticket_updated", ref.number)
                        return ref

            # Defer ticket creation if group is still in settling window
            if group.status in (IncidentGroupStatus.OPEN, IncidentGroupStatus.MITIGATION_IN_PROGRESS):
                logger.info(
                    "Group %s is still in settling window (status=%s, settle_deadline=%.2f); deferring ServiceNow ticket creation until settle stabilization",
                    group.group_id,
                    group.status,
                    group.settle_deadline,
                )
                if report.origin:
                    self._update_group_origin_safely(group, report.origin)
                if report.blast_radius:
                    for b in report.blast_radius:
                        if b not in group.blast_radius:
                            group.blast_radius.append(b)
                update_gcs_okf_servicenow(
                    filename=target_file,
                    servicenow_number="",
                    servicenow_sys_id="",
                    status="mitigation-in-progress",
                    config=self.config,
                )
                self._log_findings_decision(report.group_id, group.group_id, "triage_stored_settle_deferred", None)
                return None

            # First callback for stabilized group: update origin/blast_radius and create incident ticket exactly once
            if report.origin:
                self._update_group_origin_safely(group, report.origin)
            if report.blast_radius:
                for b in report.blast_radius:
                    if b not in group.blast_radius:
                        group.blast_radius.append(b)

            ref = adapter.create_incident(report)

            group.servicenow_sys_id = ref.sys_id
            group.servicenow_number = ref.number
            group.servicenow_url = ref.url
            group.status = IncidentGroupStatus.TICKETED

            # Persist in OKF before returning ref
            update_gcs_okf_servicenow(
                filename=target_file,
                servicenow_number=ref.number,
                servicenow_sys_id=ref.sys_id,
                status="mitigation-in-progress",
                config=self.config,
            )

            logger.info(
                "Group %s transitioned to TICKETED with ServiceNow incident %s",
                group.group_id,
                ref.number,
            )
            self._log_findings_decision(report.group_id, group.group_id, "ticket_created", ref.number)
            return ref

    def get_group(self, group_id: str) -> Optional[IncidentGroup]:
        with self._lock:
            return self.groups.get(group_id) or self.archived_groups.get(group_id)

    def list_groups(self, include_archived: bool = False) -> list[IncidentGroup]:
        with self._lock:
            unique_active = list({id(g): g for g in self.groups.values() if g.status != IncidentGroupStatus.RESOLVED and not g.archived}.values())
            if include_archived:
                unique_archived = list({id(g): g for g in self.archived_groups.values()}.values())
                return unique_active + unique_archived
            return unique_active

    def list_archived_groups(self) -> list[IncidentGroup]:
        with self._lock:
            return list({id(g): g for g in self.archived_groups.values()}.values())


def read_okf_servicenow(
    filename: Optional[str] = None,
    group_id: Optional[str] = None,
    config: Optional[BridgeConfig] = None,
    storage_client: Optional[Any] = None,
    store_path: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Read servicenow_sys_id and servicenow_number from local or GCS OKF file."""
    import yaml
    from pathlib import Path

    cfg = config or get_settings()
    clean_name = Path(filename).name if filename else None

    # 1. Search local filesystem
    search_dirs: list[Path] = []
    if store_path:
        search_dirs.append(Path(store_path))
    cfg_store = getattr(cfg, "incident_store_path", None)
    if cfg_store:
        search_dirs.append(Path(cfg_store))
    else:
        search_dirs.extend([
            Path("/knowledge/incident-manager"),
            Path("/tmp/knowledge/incident-manager"),
        ])

    for s_dir in search_dirs:
        if not s_dir.exists():
            continue
        if clean_name:
            target = s_dir / clean_name
            if target.exists():
                try:
                    content = target.read_text(encoding="utf-8")
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        meta = yaml.safe_load(parts[1]) or {}
                        sys_id = meta.get("servicenow_sys_id")
                        num = meta.get("servicenow_number")
                        if sys_id:
                            return str(sys_id), str(num or "")
                except Exception as e:
                    logger.debug("Failed reading local OKF %s: %s", target, e)

        try:
            for f in s_dir.glob("*.md"):
                if (clean_name and f.name == clean_name) or (group_id and group_id in f.name):
                    try:
                        content = f.read_text(encoding="utf-8")
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            meta = yaml.safe_load(parts[1]) or {}
                            if group_id and meta.get("group_id") and meta.get("group_id") != group_id:
                                continue
                            sys_id = meta.get("servicenow_sys_id")
                            num = meta.get("servicenow_number")
                            if sys_id:
                                return str(sys_id), str(num or "")
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("Error searching local OKF dir %s: %s", s_dir, e)

    # 2. Search GCS if not in local mode or storage_client provided
    if not cfg.is_local or storage_client is not None:
        try:
            bucket_name = getattr(cfg, "incidents_bucket", None) or f"{cfg.project_id}-{cfg.name_prefix}-incidents"
            if storage_client is None:
                from google.cloud import storage
                storage_client = storage.Client(project=cfg.project_id)

            bucket = storage_client.bucket(bucket_name)
            possible_keys = []
            if clean_name:
                possible_keys.extend([
                    clean_name,
                    f"knowledge/incident-manager/{clean_name}",
                ])

            for key in possible_keys:
                blob = bucket.blob(key)
                if blob.exists():
                    content = blob.download_as_text()
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        meta = yaml.safe_load(parts[1]) or {}
                        sys_id = meta.get("servicenow_sys_id")
                        num = meta.get("servicenow_number")
                        if sys_id:
                            return str(sys_id), str(num or "")

            if group_id or clean_name:
                blobs = list(bucket.list_blobs(prefix="knowledge/incident-manager/"))
                for b in blobs:
                    if (clean_name and clean_name in b.name) or (group_id and group_id in b.name):
                        content = b.download_as_text()
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            meta = yaml.safe_load(parts[1]) or {}
                            sys_id = meta.get("servicenow_sys_id")
                            num = meta.get("servicenow_number")
                            if sys_id:
                                return str(sys_id), str(num or "")
        except Exception as exc:
            logger.debug("Failed reading GCS OKF for %s: %s", filename, exc)

    return None, None


def update_gcs_okf_servicenow(
    filename: str,
    servicenow_number: str,
    servicenow_sys_id: str,
    status: str = "mitigation-in-progress",
    config: Optional[BridgeConfig] = None,
    storage_client: Optional[Any] = None,
    store_path: Optional[str] = None,
) -> bool:
    """Update GCS OKF incident .md file in gs://<project_id>-ata-incidents/ with ServiceNow details."""
    import yaml
    from pathlib import Path

    cfg = config or get_settings()
    bucket_name = getattr(cfg, "incidents_bucket", None) or f"{cfg.project_id}-{cfg.name_prefix}-incidents"
    clean_name = Path(filename).name if filename else "incident.md"

    # Also update local file if present
    local_paths = []
    if store_path:
        local_paths.append(Path(f"{store_path}/{clean_name}"))
    cfg_store = getattr(cfg, "incident_store_path", None)
    if cfg_store:
        local_paths.append(Path(f"{cfg_store}/{clean_name}"))
    else:
        local_paths.extend([
            Path(f"/knowledge/incident-manager/{clean_name}"),
            Path(f"/tmp/knowledge/incident-manager/{clean_name}"),
        ])

    updated_local = False
    for lp in local_paths:
        if lp.exists():
            try:
                content = lp.read_text(encoding="utf-8")
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    meta = yaml.safe_load(parts[1]) or {}
                    meta["servicenow_number"] = servicenow_number
                    meta["servicenow_sys_id"] = servicenow_sys_id
                    meta["status"] = status
                    new_content = f"---\n{yaml.dump(meta, sort_keys=False).strip()}\n---{parts[2]}"
                    lp.write_text(new_content, encoding="utf-8")
                    logger.info("Updated local OKF file %s with ticket %s", lp, servicenow_number)
                    updated_local = True
            except Exception as e:
                logger.warning("Local OKF file update error: %s", e)

    if not updated_local:
        target_dir = Path(store_path) if store_path else (Path(cfg_store) if cfg_store else Path("/tmp/knowledge/incident-manager"))
        fallback_file = target_dir / clean_name
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "type": "incident",
                "filename": clean_name,
                "servicenow_number": servicenow_number,
                "servicenow_sys_id": servicenow_sys_id,
                "status": status,
            }
            fallback_file.write_text(f"---\n{yaml.dump(meta, sort_keys=False).strip()}\n---\n# Incident Record\n", encoding="utf-8")
            logger.info("Created fallback local OKF file %s with ticket %s", fallback_file, servicenow_number)
            updated_local = True
        except Exception as e:
            logger.debug("Could not create fallback local OKF: %s", e)

    try:
        if storage_client is None:
            if cfg.is_local:
                return updated_local
            from google.cloud import storage
            storage_client = storage.Client(project=cfg.project_id)

        bucket = storage_client.bucket(bucket_name)
        possible_keys = [
            clean_name,
            f"knowledge/incident-manager/{clean_name}",
        ]

        updated = False
        for key in possible_keys:
            blob = bucket.blob(key)
            if blob.exists():
                content = blob.download_as_text()
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    meta = yaml.safe_load(parts[1]) or {}
                    meta["servicenow_number"] = servicenow_number
                    meta["servicenow_sys_id"] = servicenow_sys_id
                    meta["status"] = status
                    new_content = f"---\n{yaml.dump(meta, sort_keys=False).strip()}\n---{parts[2]}"
                    blob.upload_from_string(new_content, content_type="text/markdown")
                    logger.info(
                        "Updated GCS OKF file gs://%s/%s with ServiceNow %s (status=%s)",
                        bucket_name,
                        key,
                        servicenow_number,
                        status,
                    )
                    updated = True

        if not updated:
            blobs = list(bucket.list_blobs(prefix="knowledge/incident-manager/"))
            for b in blobs:
                if b.name.endswith(clean_name) or clean_name in b.name:
                    content = b.download_as_text()
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        meta = yaml.safe_load(parts[1]) or {}
                        meta["servicenow_number"] = servicenow_number
                        meta["servicenow_sys_id"] = servicenow_sys_id
                        meta["status"] = status
                        new_content = f"---\n{yaml.dump(meta, sort_keys=False).strip()}\n---{parts[2]}"
                        b.upload_from_string(new_content, content_type="text/markdown")
                        logger.info("Updated GCS OKF blob gs://%s/%s", bucket_name, b.name)
                        updated = True
        return updated or updated_local
    except Exception as exc:
        logger.warning("Failed to update GCS OKF file for %s: %s", filename, exc)
        return updated_local
