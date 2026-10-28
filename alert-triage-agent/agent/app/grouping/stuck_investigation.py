"""Recovery for incident groups stuck in 'investigating' without a ServiceNow ticket.

A settle or max-wait wake moves a group to ``investigating`` and then investigates and
creates the ticket. If that wake keeps failing (for example, the bridge or ServiceNow is
down) until ata-timer-queue's retry window ends, Cloud Tasks deletes the task. No further
wake arrives, and the group would stay ``investigating`` with no ticket while new alerts
attach to it.

Every wake attempt that (re)starts the investigation stamps ``investigation_started_at``.
A group is *stuck* when it is ``investigating``, has no ``servicenow_sys_id``, and its
last wake attempt is more than ``INVESTIGATION_STALE_SECONDS`` old (or was never
recorded). Two triggers detect it:

1. The next alert ingest (``GroupingManager``), before correlation.
2. An ``investigation_watchdog`` wake scheduled when the group first enters
   ``investigating``, which re-schedules itself while the investigation is still active.

Recovery is logged at ERROR, recorded on the group (``investigation_recoveries``) and
stated in the ticket. It re-enqueues a settle wake, which gets a fresh retry window,
plus a watchdog. If that settle wake cannot be scheduled, the attempt still counts: the
record is kept (marked ``settle_wake_scheduled: false``), the attempt stamp is restored so
the group stays detectable as stuck, and the failure is logged at ERROR. The watchdog path
re-raises (Cloud Tasks retries the watchdog wake); the ingest path does not, so one group's
failure cannot fail the ingest of an unrelated alert.

Attempts on one group are at least ``INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS`` apart. A
trigger sooner than that after the last recorded attempt starts no attempt and does not
degrade (``OUTCOME_THROTTLED``, logged ``RECOVERY_THROTTLED`` at WARNING): the ingest path
logs once per group per interval and continues; the watchdog path re-arms itself for the
moment the next attempt is allowed.

After ``INVESTIGATION_MAX_RECOVERIES`` recoveries the group is marked ``degraded`` instead
(ERROR, listing its alert ids and policies): degraded groups are not attachable, so new
alerts open a new group rather than joining one that is not being ticketed. The next group
that opens for the same scope (``degraded_group_in_scope``) lists the degraded group's
alerts in its ticket (``degraded_predecessor_observation``), so they are not dropped.

The decision is taken inside ``OKFStore.update_atomically`` on the freshly read document,
so concurrent triggers recover a group at most once per stale period.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any, Optional

from ..config import AgentConfig
from ..contracts.report import split_observations
from ..timers.scheduler import schedule_delayed_timer
from .models import IncidentGroupStatus, OKFDocument, parse_iso_timestamp
from .store import OKFStore

logger = logging.getLogger(__name__)

WATCHDOG_DEADLINE_TYPE = "investigation_watchdog"
RECOVERY_OBSERVATION_PREFIX = "Investigation recovery:"
DEGRADED_PREDECESSOR_PREFIX = "Degraded incident group"
DEGRADED_CAUSE_STUCK = "stuck_investigation"

OUTCOME_RECOVERED = "recovered"
OUTCOME_DEGRADED = "degraded"
OUTCOME_NOT_STUCK = "not_stuck"
OUTCOME_GROUP_GONE = "group_gone"
OUTCOME_SCHEDULE_FAILED = "recovery_schedule_failed"
OUTCOME_THROTTLED = "recovery_throttled"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def investigation_started_epoch(doc: OKFDocument) -> Optional[float]:
    """Epoch of the last wake attempt that (re)started the investigation, or None if unrecorded."""
    dt = parse_iso_timestamp(getattr(doc, "investigation_started_at", None))
    return dt.timestamp() if dt else None


def is_stuck_investigation(doc: OKFDocument, now: float, stale_seconds: float) -> bool:
    """True when ``doc`` is investigating, unticketed, and its last wake attempt is stale or unrecorded."""
    if str(doc.status).lower() != IncidentGroupStatus.INVESTIGATING.value:
        return False
    if doc.servicenow_sys_id:
        return False
    started = investigation_started_epoch(doc)
    if started is None:
        return True
    return now - started >= stale_seconds


def mark_investigation_started(doc: OKFDocument, now: float) -> bool:
    """Set ``investigating`` and stamp the wake attempt. Returns True if the group was not investigating before."""
    first_entry = str(doc.status).lower() != IncidentGroupStatus.INVESTIGATING.value
    doc.status = IncidentGroupStatus.INVESTIGATING.value
    doc.investigation_started_at = _iso(now)
    return first_entry


def schedule_watchdog(settings: AgentConfig, group_id: str, deadline_epoch: float) -> str:
    """Schedule an ``investigation_watchdog`` wake at ``deadline_epoch`` (raises TimerScheduleError)."""
    return schedule_delayed_timer(
        project_id=settings.project_id,
        location=settings.location,
        queue_name=settings.timer_queue_name,
        target_url=settings.agent_url,
        group_id=group_id,
        deadline_type=WATCHDOG_DEADLINE_TYPE,
        deadline_dt=datetime.fromtimestamp(deadline_epoch, tz=timezone.utc),
        service_account_email=settings.service_account_email,
    )


@dataclass
class RecoveryResult:
    outcome: str
    doc: Optional[OKFDocument]
    # OUTCOME_THROTTLED: epoch from which the next attempt is allowed.
    retry_after_epoch: Optional[float] = None


def _last_attempt_epoch(recoveries: list[Any]) -> Optional[float]:
    for r in reversed(recoveries):
        if isinstance(r, dict):
            dt = parse_iso_timestamp(r.get("at"))
            return dt.timestamp() if dt else None
    return None


def recover_stuck_investigation(
    store: OKFStore,
    group_id: str,
    now: float,
    settings: AgentConfig,
    trigger: str,
    reraise_schedule_failure: bool = True,
    throttle_warning_once_per_interval: bool = False,
) -> RecoveryResult:
    """Recover (re-enqueue) or degrade a stuck group. No-op if the fresh document is not stuck.

    If the recovery settle wake cannot be scheduled, the attempt still counts toward
    ``INVESTIGATION_MAX_RECOVERIES``: its record is kept, the group's attempt stamp is
    restored so it stays detectable as stuck, and the failure is logged at ERROR. The error
    is then re-raised (watchdog path: Cloud Tasks retries the wake) unless
    ``reraise_schedule_failure`` is False (ingest path), which returns
    ``OUTCOME_SCHEDULE_FAILED`` with the restored document instead.

    Within ``INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS`` of the last recorded attempt no
    attempt is started and the group is not degraded: returns ``OUTCOME_THROTTLED`` with
    ``retry_after_epoch``, logged at WARNING (once per group per interval when
    ``throttle_warning_once_per_interval``, ingest path; on every call otherwise, watchdog path).
    """
    stale = settings.investigation_stale_seconds
    max_recoveries = settings.investigation_max_recoveries
    min_interval = settings.investigation_recovery_min_interval_seconds
    now_iso = _iso(now)
    outcome = OUTCOME_NOT_STUCK
    record: dict[str, Any] = {}
    throttle: dict[str, Any] = {}

    def _mutate(d: OKFDocument) -> OKFDocument:
        nonlocal outcome, record, throttle
        outcome = OUTCOME_NOT_STUCK
        record = {}
        throttle = {}
        if not is_stuck_investigation(d, now, stale):
            return d
        prior = list(getattr(d, "investigation_recoveries", None) or [])
        last_attempt = getattr(d, "investigation_started_at", None)
        last_recovery = _last_attempt_epoch(prior)
        if last_recovery is not None and now - last_recovery < min_interval:
            outcome = OUTCOME_THROTTLED
            warned = parse_iso_timestamp(getattr(d, "investigation_recovery_throttle_warned_at", None))
            warn = not throttle_warning_once_per_interval or warned is None or warned.timestamp() < last_recovery
            if warn and throttle_warning_once_per_interval:
                d.investigation_recovery_throttle_warned_at = now_iso
            throttle = {
                "warn": warn,
                "retry_after_epoch": last_recovery + min_interval,
                "last_recovery_at": _iso(last_recovery),
                "recoveries_used": len(prior),
            }
            return d
        record = {
            "at": now_iso,
            "trigger": trigger,
            "last_wake_attempt_at": last_attempt,
            "stale_seconds": stale,
            "max_recoveries": max_recoveries,
        }
        if len(prior) >= max_recoveries:
            outcome = OUTCOME_DEGRADED
            record["recoveries_used"] = len(prior)
            record["alerts"] = alert_refs(d.alerts)
            d.status = IncidentGroupStatus.DEGRADED.value
            d.degraded = True
            d.degraded_cause = DEGRADED_CAUSE_STUCK
            d.degraded_reason = (
                f"investigating without a ServiceNow ticket; last wake attempt {last_attempt or 'not recorded'}, "
                f"stale after {stale:g}s; {len(prior)} of {max_recoveries} recoveries already used"
            )
        else:
            outcome = OUTCOME_RECOVERED
            record["recovery_number"] = len(prior) + 1
            prior.append(record)
            d.investigation_recoveries = prior
            d.investigation_started_at = now_iso
            d.settle_deadline = now_iso
        d.updated_at = now_iso
        return d

    updated = store.update_atomically(group_id, _mutate, create_if_missing=False)
    if updated is None:
        return RecoveryResult(outcome=OUTCOME_GROUP_GONE, doc=None)
    if outcome == OUTCOME_NOT_STUCK:
        return RecoveryResult(outcome=outcome, doc=updated)
    if outcome == OUTCOME_THROTTLED:
        if throttle["warn"]:
            logger.warning(
                "RECOVERY_THROTTLED: %s",
                json.dumps({
                    "event": "stuck_investigation_recovery_throttled",
                    "group_id": group_id,
                    "trigger": trigger,
                    "last_recovery_at": throttle["last_recovery_at"],
                    "next_attempt_allowed_at": _iso(throttle["retry_after_epoch"]),
                    "min_interval_seconds": min_interval,
                    "recoveries_used": throttle["recoveries_used"],
                    "max_recoveries": max_recoveries,
                }),
            )
        return RecoveryResult(outcome=outcome, doc=updated, retry_after_epoch=throttle["retry_after_epoch"])

    logger.error(
        "STUCK_INVESTIGATION: %s",
        json.dumps({"event": "stuck_investigation", "group_id": group_id, "action": outcome, **record}),
    )
    if outcome == OUTCOME_DEGRADED:
        return RecoveryResult(outcome=outcome, doc=updated)

    try:
        schedule_delayed_timer(
            project_id=settings.project_id,
            location=settings.location,
            queue_name=settings.timer_queue_name,
            target_url=settings.agent_url,
            group_id=group_id,
            deadline_type="settle",
            deadline_dt=datetime.fromtimestamp(now, tz=timezone.utc),
            service_account_email=settings.service_account_email,
        )
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"

        def _restore(d: OKFDocument) -> OKFDocument:
            # Keep the attempt (it counts toward the limit); make the group stuck again.
            for r in getattr(d, "investigation_recoveries", None) or []:
                if isinstance(r, dict) and r.get("at") == now_iso and r.get("trigger") == trigger:
                    r["settle_wake_scheduled"] = False
                    r["settle_wake_error"] = error_text
            if getattr(d, "investigation_started_at", None) == now_iso:
                d.investigation_started_at = record.get("last_wake_attempt_at")
            d.updated_at = now_iso
            return d

        restored = store.update_atomically(group_id, _restore, create_if_missing=False)
        logger.error(
            "Failed to schedule the recovery settle wake for stuck group %s (recovery %s of %s): %s; the attempt "
            "is counted and the group stays detectable as stuck, so the %s",
            group_id,
            record.get("recovery_number"),
            max_recoveries,
            error_text,
            "watchdog wake is retried by Cloud Tasks" if reraise_schedule_failure
            else "next alert ingest or watchdog wake retries it",
            exc_info=True,
        )
        if reraise_schedule_failure:
            raise
        if restored is None:
            return RecoveryResult(outcome=OUTCOME_GROUP_GONE, doc=None)
        return RecoveryResult(outcome=OUTCOME_SCHEDULE_FAILED, doc=restored)

    try:
        schedule_watchdog(settings, group_id, now + stale)
    except Exception as exc:
        # The settle wake above is the recovery; the watchdog is the backstop if it fails too.
        logger.error(
            "Failed to schedule the investigation watchdog for recovered group %s: %s; "
            "the next alert ingest still detects the group if it stays stuck",
            group_id,
            exc,
            exc_info=True,
        )
    return RecoveryResult(outcome=outcome, doc=updated)


def recovery_observation(recoveries: Optional[list[Any]]) -> Optional[str]:
    """Ticket observation stating each recovery of a stuck investigation, or None."""
    items = [r for r in (recoveries or []) if isinstance(r, dict)]
    if not items:
        return None
    parts = []
    for r in items:
        detail = f"({r.get('trigger')}; last wake attempt {r.get('last_wake_attempt_at') or 'not recorded'})"
        if r.get("settle_wake_scheduled") is False:
            parts.append(f"restart attempted at {r.get('at')} {detail} but its settle wake could not be scheduled")
        else:
            parts.append(f"restarted at {r.get('at')} {detail}")
    stale = items[-1].get("stale_seconds")
    stale_txt = f"{float(stale):g}s" if isinstance(stale, (int, float)) else "the stale threshold"
    return (
        f"{RECOVERY_OBSERVATION_PREFIX} this group was investigating without a ServiceNow ticket for more than "
        f"{stale_txt} after its last wake attempt; the investigation was " + "; ".join(parts) + "."
    )


def apply_recovery_note(report: Any, recoveries: Optional[list[Any]]) -> None:
    """Append the recovery observation to ``report.observations`` (idempotent)."""
    line = recovery_observation(recoveries)
    if not line:
        return
    items = split_observations(report.observations or "")
    items = [i for i in items if not i.startswith(RECOVERY_OBSERVATION_PREFIX)]
    items.append(line)
    report.observations = " | ".join(items)


# Alerts of a group degraded after the recovery limit (carried into the next group's ticket) ---


def alert_refs(alerts: Optional[list[Any]]) -> list[dict[str, Any]]:
    """``[{incident_id, policy_name}]`` for each alert entry with an incident id."""
    out = []
    for a in alerts or []:
        if isinstance(a, dict) and a.get("incident_id"):
            out.append({"incident_id": a.get("incident_id"), "policy_name": a.get("policy_name")})
    return out


def _resources(doc: OKFDocument) -> set[str]:
    names = {doc.resource_name, doc.origin, *(doc.blast_radius or [])}
    names.update(a.get("resource_name") for a in doc.alerts or [] if isinstance(a, dict))
    return {n for n in names if n}


def degraded_group_in_scope(degraded: OKFDocument, new_resources: set[str]) -> bool:
    """True if ``degraded`` was degraded after the recovery limit, is not yet carried into another
    group, and shares a resource (resource, origin, blast radius or alert resource) with the new group.
    """
    if str(degraded.status).lower() != IncidentGroupStatus.DEGRADED.value:
        return False
    if getattr(degraded, "degraded_cause", None) != DEGRADED_CAUSE_STUCK:
        return False
    if getattr(degraded, "carried_to_group_id", None):
        return False
    return bool(_resources(degraded) & {r for r in new_resources if r})


def degraded_predecessor_entry(degraded: OKFDocument) -> dict[str, Any]:
    """What the new group records about a degraded group whose alerts it carries."""
    attempts = len(getattr(degraded, "investigation_recoveries", None) or [])
    return {
        "group_id": degraded.group_id,
        "alerts": alert_refs(degraded.alerts),
        "reason": f"investigation stuck; {attempts} recovery attempts failed",
        "degraded_reason": degraded.degraded_reason,
    }


def degraded_predecessor_observation(predecessors: Optional[list[Any]]) -> Optional[str]:
    """Ticket observation listing each degraded group's alerts carried into this group, or None."""
    lines = []
    for p in predecessors or []:
        if not isinstance(p, dict) or not p.get("group_id"):
            continue
        alerts = ", ".join(
            f"{a.get('incident_id')} ({a.get('policy_name') or 'policy not recorded'})"
            for a in p.get("alerts") or [] if isinstance(a, dict)
        ) or "none recorded"
        lines.append(
            f"{DEGRADED_PREDECESSOR_PREFIX} {p['group_id']} ({p.get('reason')}) was not ticketed; its alerts: {alerts}."
        )
    return " | ".join(lines) or None


def apply_degraded_predecessor_note(report: Any, predecessors: Optional[list[Any]]) -> None:
    """Append the degraded-predecessor observation(s) to ``report.observations`` (idempotent)."""
    line = degraded_predecessor_observation(predecessors)
    if not line:
        return
    items = split_observations(report.observations or "")
    items = [i for i in items if not i.startswith(DEGRADED_PREDECESSOR_PREFIX)]
    items.extend(split_observations(line))
    report.observations = " | ".join(items)


def claim_degraded_predecessors(
    store: OKFStore, predecessors: list[dict[str, Any]], new_group_id: str, now_iso: str
) -> None:
    """Record on each degraded group which new group carries its alerts (so it is carried once).

    A group already claimed by another new group (a concurrent instance) keeps that claim and
    is logged at WARNING: its alerts are then listed by both tickets, never by neither.
    """
    for p in predecessors:
        gid = p["group_id"]
        holder: dict[str, Optional[str]] = {"to": None}

        def _claim(d: OKFDocument) -> OKFDocument:
            holder["to"] = getattr(d, "carried_to_group_id", None)
            if not holder["to"]:
                d.carried_to_group_id = new_group_id
                d.updated_at = now_iso
            return d

        try:
            store.update_atomically(gid, _claim, create_if_missing=False)
        except Exception as exc:
            logger.error(
                "Could not record that group %s carries the alerts of degraded group %s (%s: %s); "
                "a later new group may list them again",
                new_group_id, gid, type(exc).__name__, exc,
            )
            continue
        if holder["to"] and holder["to"] != new_group_id:
            logger.warning(
                "Degraded group %s was already carried into group %s; group %s lists its alerts too",
                gid, holder["to"], new_group_id,
            )
        logger.warning(
            "DEGRADED_ALERTS_CARRIED: %s",
            json.dumps({"event": "degraded_alerts_carried", "degraded_group_id": gid,
                        "new_group_id": new_group_id, "alerts": p.get("alerts")}),
        )
