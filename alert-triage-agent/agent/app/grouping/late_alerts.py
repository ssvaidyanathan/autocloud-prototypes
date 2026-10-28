"""Alerts that join an already-ticketed incident group.

Two rules (QA round 2, S1 / INC0010328):

1. Ticket refresh. When alerts attach to a group that already has a ServiceNow ticket, the
   ticket's short description is rebuilt from the group (the alert count always equals the
   group's alerts) and the priority is raised when any group alert's severity label is higher
   than the priority recorded as applied to the ticket (so a failed escalation is retried). The
   priority is never lowered automatically. Every change goes out with a work note naming the
   new alerts, the old and new title and the escalation, and is logged.

2. Time distance for new signal types. A ticketed group's investigation is finished; an alert
   of a signal type the group does not have yet, whose onset is more than
   ``late_new_signal_max_onset_gap_seconds`` after the group's first alert, opens a new group
   instead of joining it. The correlation score cannot express this: same-service latency/5xx
   get 35 (topology) + 30 (causal) = the 65-pt attach threshold before any temporal points.
"""

from dataclasses import dataclass, field
from datetime import datetime
import html
import json
import logging
import re
from typing import Any, Optional

from ..contracts.alert import AlertEvent
from ..contracts.instructions import UpdateTicketFieldsRequest, servicenow_urgency_impact
from ..contracts.report import InvestigationReport, title_problem_for_signal
from ..correlation.scoring import classify_anomaly_category
from .models import OKFDocument, parse_iso_timestamp

logger = logging.getLogger(__name__)

# ServiceNow short_description length used for ticket titles (bridge create/update).
TICKET_TITLE_MAX_LENGTH = 160
DEFAULT_TICKET_PRIORITY = "P2"


def _priority_rank(priority: Optional[str]) -> int:
    m = re.fullmatch(r"[Pp](\d+)", str(priority or "").strip())
    return int(m.group(1)) if m else 99


def _labels(entry: dict[str, Any]) -> dict[str, Any]:
    return entry.get("labels") or entry.get("policy_user_labels") or {}


def alert_priority(entry: dict[str, Any]) -> Optional[str]:
    """'P<n>' from an alert entry's policy severity label (p1, p2, ...), or None."""
    sev = entry.get("severity") or _labels(entry).get("severity") or (entry.get("policy_user_labels") or {}).get("severity")
    rank = _priority_rank(sev)
    return f"P{rank}" if rank != 99 else None


def _signal(entry: dict[str, Any]) -> Optional[str]:
    sig = entry.get("signal") or _labels(entry).get("signal")
    return str(sig) if sig else None


def _category(policy: str, signal: Optional[str], metric_type: str = "") -> str:
    return classify_anomaly_category(policy or "", metric_type or "", signal=signal or "")


@dataclass
class TicketRefresh:
    """A planned ServiceNow ticket update for alerts that joined a ticketed group."""
    sys_id: str
    group_id: str
    ticket_number: str
    new_alert_ids: list[str]
    previous_short_description: str
    previous_title_reconstructed: bool
    short_description: str
    previous_priority: str
    priority: str
    escalated: bool
    title_problem: str
    origin: str
    alert_count: int
    work_note: str = ""
    escalating_alert: Optional[str] = None
    escalating_alert_is_new: bool = True
    escalating_severity: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)


def _title_report(doc: OKFDocument, origin: str, priority: str, alerts: list[dict[str, Any]],
                  title_problem: Optional[str]) -> InvestigationReport:
    return InvestigationReport(
        group_id=doc.group_id,
        summary="",
        observations="",
        title_problem=title_problem,
        origin=origin,
        priority=priority,
        alerts=list(alerts),
    )


def plan_ticket_refresh(doc: OKFDocument, new_alert_ids: list[str]) -> Optional[TicketRefresh]:
    """Plan the ticket update for ``new_alert_ids`` (already in ``doc.alerts``), or None.

    None when the group has no ticket or none of the ids is in the group.
    """
    if not doc.servicenow_sys_id:
        return None
    new_ids = [i for i in dict.fromkeys(new_alert_ids or []) if i]
    alerts = [a for a in (doc.alerts or []) if isinstance(a, dict)]
    new_entries = [a for a in alerts if a.get("incident_id") in new_ids]
    if not new_entries:
        return None
    prior_alerts = [a for a in alerts if a.get("incident_id") not in new_ids]

    origin = doc.ticket_origin or doc.origin or doc.resource_name or "unknown"
    prev_priority = doc.ticket_priority or doc.priority or DEFAULT_TICKET_PRIORITY
    prev_problem = doc.ticket_title_problem or _title_report(
        doc, origin, prev_priority, prior_alerts, None
    ).resolve_title_problem()
    reconstructed = not doc.ticket_short_description
    prev_title = doc.ticket_short_description or _title_report(
        doc, origin, prev_priority, prior_alerts, prev_problem
    ).build_short_description(max_length=TICKET_TITLE_MAX_LENGTH)

    # Over ALL group alerts, against the priority recorded as applied to the ticket: an
    # escalation whose update-fields call failed (bridge error, rollback 404) is not recorded,
    # so the next refresh still sees the group's more severe alert and applies it. On a tie a
    # new alert is named. Still only ever raised.
    most_severe = min(
        alerts, key=lambda a: (_priority_rank(alert_priority(a)), a.get("incident_id") not in new_ids)
    )
    new_rank = _priority_rank(alert_priority(most_severe))
    escalated = new_rank < _priority_rank(prev_priority)
    if escalated:
        priority = f"P{new_rank}"
        # The title names the problem of the most severe alert (S1: 5xx/p1 over latency/p2).
        problem = title_problem_for_signal(_signal(most_severe)) or prev_problem
    else:
        priority = prev_priority
        problem = prev_problem

    report = _title_report(doc, origin, priority, alerts, problem)
    plan = TicketRefresh(
        sys_id=doc.servicenow_sys_id,
        group_id=doc.group_id,
        ticket_number=doc.servicenow_number or doc.servicenow_sys_id,
        new_alert_ids=[a.get("incident_id") for a in new_entries],
        previous_short_description=prev_title,
        previous_title_reconstructed=reconstructed,
        short_description=report.build_short_description(max_length=TICKET_TITLE_MAX_LENGTH),
        previous_priority=prev_priority,
        priority=priority,
        escalated=escalated,
        title_problem=problem,
        origin=origin,
        alert_count=report.alert_count(),
        escalating_alert=most_severe.get("incident_id") if escalated else None,
        escalating_alert_is_new=most_severe.get("incident_id") in new_ids,
        escalating_severity=alert_priority(most_severe),
    )
    plan.work_note = _work_note(plan, new_entries)
    return plan


def _work_note(plan: TicketRefresh, new_entries: list[dict[str, Any]]) -> str:
    e = html.escape
    items = []
    for a in new_entries:
        sev = (alert_priority(a) or "no severity label").lower()
        sig = _signal(a) or "unknown signal"
        items.append(
            f"<code>{e(str(a.get('incident_id')))}</code> ({e(str(a.get('policy_name') or a.get('policy') or 'alert'))} "
            f"on {e(str(a.get('resource_name') or a.get('resource') or 'unknown'))}, severity {e(sev)}, signal {e(sig)})"
        )
    n = len(new_entries)
    prev_label = "Previous short description (reconstructed from the group file)" if plan.previous_title_reconstructed else "Previous short description"
    if plan.escalated:
        urgency, impact = servicenow_urgency_impact(plan.priority)
        level = f"urgency {urgency}, impact {impact}"
        if plan.escalating_alert_is_new:
            why = f"new alert <code>{e(str(plan.escalating_alert))}</code> is more severe than the ticket"
        else:
            why = (
                f"alert <code>{e(str(plan.escalating_alert))}</code>, already in this group, has severity "
                f"{e(str(plan.escalating_severity or '').lower())}, above the ticket priority "
                f"{e(plan.previous_priority)} recorded on the group file"
            )
        prio = (
            f"<b>Priority escalated:</b> {e(plan.previous_priority)} -&gt; {e(plan.priority)} "
            f"({why}); {level} set."
        )
    else:
        prio = (
            f"<b>Priority unchanged:</b> {e(plan.priority)} (no alert in the group is more severe than the "
            "ticket; priority is never lowered automatically)."
        )
    return (
        f"[code]<b>Ticket Updated:</b> {n} correlated alert{'s' if n != 1 else ''} joined this incident after "
        f"the ticket was created: {'; '.join(items)}.<br/>"
        f"<b>Short description:</b> '{e(plan.short_description)}' (the group now has {plan.alert_count} "
        f"alert{'s' if plan.alert_count != 1 else ''}). {prev_label}: '{e(plan.previous_short_description)}'.<br/>"
        f"{prio}<br/>"
        "The investigation report, root cause and description were written before these alerts joined and "
        "were not re-run; review them against the new alerts.[/code]"
    )


def refresh_ticket_for_late_alerts(
    doc: OKFDocument,
    new_alert_ids: list[str],
    bridge_client: Any,
    config: Any,
    store: Any,
) -> Optional[TicketRefresh]:
    """Send the planned ticket update to the bridge and record it on the group when applied.

    Failures are logged at ERROR (the ticket title/priority are then stale) and never raised:
    the alert is already in the group.
    """
    plan = plan_ticket_refresh(doc, new_alert_ids)
    if plan is None:
        return None
    logger.info("JSON_DECISION: %s", json.dumps({
        "event": "ticket_refresh_planned",
        "group_id": plan.group_id,
        "ticket": plan.ticket_number,
        "new_alerts": plan.new_alert_ids,
        "short_description_from": plan.previous_short_description,
        "short_description_to": plan.short_description,
        "priority_from": plan.previous_priority,
        "priority_to": plan.priority,
        "escalated": plan.escalated,
    }))
    if plan.escalated:
        logger.warning(
            "Group %s ticket %s: priority escalated %s -> %s by late alert %s",
            plan.group_id, plan.ticket_number, plan.previous_priority, plan.priority, plan.escalating_alert,
        )
    try:
        bridge_client.update_ticket_fields(
            bridge_url=config.bridge_url,
            request=UpdateTicketFieldsRequest(
                sys_id=plan.sys_id,
                group_id=plan.group_id,
                short_description=plan.short_description,
                priority=plan.priority if plan.escalated else None,
                work_note=plan.work_note,
            ),
        )
    except Exception as exc:
        logger.error(
            "Failed updating ticket %s of group %s for late alert(s) %s (%s); its short description "
            "and priority are stale (should be %r, %s)",
            plan.ticket_number, plan.group_id, ", ".join(plan.new_alert_ids), type(exc).__name__,
            plan.short_description, plan.priority,
        )
        return None

    def _record(d: OKFDocument) -> OKFDocument:
        d.ticket_short_description = plan.short_description
        d.ticket_priority = plan.priority
        d.ticket_title_problem = plan.title_problem
        d.ticket_origin = d.ticket_origin or plan.origin
        if _priority_rank(plan.priority) < _priority_rank(d.priority):
            d.priority = plan.priority
        return d

    try:
        store.update_atomically(plan.group_id, _record, create_if_missing=False)
    except Exception as exc:
        logger.error(
            "Ticket %s of group %s was updated but the group file could not record it: %s",
            plan.ticket_number, plan.group_id, type(exc).__name__,
        )
    logger.info(
        "Group %s ticket %s updated for late alert(s) %s: %r (priority %s)",
        plan.group_id, plan.ticket_number, ", ".join(plan.new_alert_ids), plan.short_description, plan.priority,
    )
    return plan


def _group_onset(doc: OKFDocument) -> Optional[datetime]:
    starts = [parse_iso_timestamp(a.get("started_at")) for a in (doc.alerts or []) if isinstance(a, dict)]
    starts = [s for s in starts if s is not None]
    return min(starts) if starts else parse_iso_timestamp(doc.created_at)


def exclude_late_new_signal_groups(
    alert: AlertEvent,
    docs: list[OKFDocument],
    window_seconds: float,
) -> tuple[list[OKFDocument], list[dict[str, Any]]]:
    """Split ``docs`` into correlation candidates and ticketed groups excluded for ``alert``.

    A group is excluded when it has a ticket, none of its alerts has the alert's anomaly
    category (signal type), and the alert's onset is more than ``window_seconds`` after the
    group's first alert. Unknown onsets never exclude.
    """
    alert_dt = parse_iso_timestamp(alert.started_at)
    if alert_dt is None:
        return list(docs), []
    labels = getattr(alert, "policy_user_labels", None) or {}
    alert_cat = _category(alert.policy_name, labels.get("signal"), getattr(alert, "metric_type", "") or "")
    kept: list[OKFDocument] = []
    excluded: list[dict[str, Any]] = []
    for d in docs:
        if not d.servicenow_sys_id:
            kept.append(d)
            continue
        cats = {
            _category(str(a.get("policy_name") or a.get("policy") or ""), _signal(a))
            for a in (d.alerts or []) if isinstance(a, dict)
        } or {_category(d.policy_name or "", None)}
        onset = _group_onset(d)
        if alert_cat in cats or onset is None:
            kept.append(d)
            continue
        gap = (alert_dt - onset).total_seconds()
        if gap <= window_seconds:
            kept.append(d)
            continue
        entry = {
            "group_id": d.group_id,
            "reason": "new_signal_type_after_ticketed_group_window",
            "alert_category": alert_cat,
            "group_categories": sorted(cats),
            "onset_gap_seconds": round(gap, 1),
            "window_seconds": window_seconds,
        }
        excluded.append(entry)
        logger.info("JSON_DECISION: %s", json.dumps({"event": "late_new_signal_group_excluded", "incident_id": alert.incident_id, **entry}))
    return kept, excluded
