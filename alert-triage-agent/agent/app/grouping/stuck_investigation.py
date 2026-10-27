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
plus a watchdog. After ``INVESTIGATION_MAX_RECOVERIES`` recoveries the group is marked
``degraded`` instead (ERROR): degraded groups are not attachable, so new alerts open a new
group rather than joining one that is not being ticketed.

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

OUTCOME_RECOVERED = "recovered"
OUTCOME_DEGRADED = "degraded"
OUTCOME_NOT_STUCK = "not_stuck"
OUTCOME_GROUP_GONE = "group_gone"


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


def recover_stuck_investigation(
    store: OKFStore,
    group_id: str,
    now: float,
    settings: AgentConfig,
    trigger: str,
) -> RecoveryResult:
    """Recover (re-enqueue) or degrade a stuck group. No-op if the fresh document is not stuck.

    Raises TimerScheduleError if the recovery settle wake cannot be scheduled; the group is
    then marked degraded first, so it is not left looking recovered.
    """
    stale = settings.investigation_stale_seconds
    max_recoveries = settings.investigation_max_recoveries
    now_iso = _iso(now)
    outcome = OUTCOME_NOT_STUCK
    record: dict[str, Any] = {}

    def _mutate(d: OKFDocument) -> OKFDocument:
        nonlocal outcome, record
        outcome = OUTCOME_NOT_STUCK
        record = {}
        if not is_stuck_investigation(d, now, stale):
            return d
        prior = list(getattr(d, "investigation_recoveries", None) or [])
        last_attempt = getattr(d, "investigation_started_at", None)
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
            d.status = IncidentGroupStatus.DEGRADED.value
            d.degraded = True
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
        logger.error(
            "Failed to schedule the recovery settle wake for stuck group %s: %s", group_id, exc, exc_info=True
        )

        def _degrade(d: OKFDocument) -> OKFDocument:
            d.status = IncidentGroupStatus.DEGRADED.value
            d.degraded = True
            d.degraded_reason = f"stuck investigation recovery could not schedule a settle wake: {exc}"
            d.updated_at = now_iso
            return d

        store.update_atomically(group_id, _degrade, create_if_missing=False)
        raise

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
        parts.append(
            f"restarted at {r.get('at')} ({r.get('trigger')}; last wake attempt "
            f"{r.get('last_wake_attempt_at') or 'not recorded'})"
        )
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
