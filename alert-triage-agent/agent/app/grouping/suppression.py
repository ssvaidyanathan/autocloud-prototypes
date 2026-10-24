"""Agent suppression engine and late alert handling.

Implements suppression decisions owned entirely by the agent:
- Duplicate detection across OKF documents (idempotent suppression).
- Late alert formatting for ticketed groups.
- Safe upstream DAG origin promotion.
"""

import html
from enum import Enum
import logging
from typing import Any, Optional
from pydantic import BaseModel, Field

from ..contracts.alert import AlertEvent
from ..contracts.links import ConsoleLinkBuilder
from .models import OKFDocument

logger = logging.getLogger(__name__)

try:
    from topology.topology_graph import get_blast_radius, get_upstream_dependencies
except ImportError:
    try:
        from alert_triage_agent.topology.topology_graph import get_blast_radius, get_upstream_dependencies
    except ImportError as exc:
        logger.error("Failed to import topology_graph: %s", exc)
        raise


class SuppressionType(str, Enum):
    """Reason for alert suppression or attachment action."""
    DUPLICATE = "duplicate"
    CLOSED_HANDLED = "closed_handled"
    REACTIVATE_COOLING = "reactivate_cooling"
    ATTACH_TICKETED = "attach_ticketed"
    ATTACH_OPEN = "attach_open"
    NEW_GROUP = "new_group"


class SuppressionResult(BaseModel):
    """Result of a suppression and grouping evaluation."""
    action: SuppressionType
    is_suppressed: bool = False
    group_id: Optional[str] = None
    reason: Optional[str] = None
    note_appended: bool = False


def check_duplicate_alert(alert: AlertEvent, documents: list[OKFDocument]) -> tuple[bool, Optional[OKFDocument]]:
    """Check if alert.incident_id is already present in any OKF document's alerts list."""
    if not alert.incident_id:
        return False, None
    for doc in documents:
        if doc.has_alert(alert.incident_id):
            return True, doc
    return False, None


def format_late_alert_work_note(
    alert: AlertEvent,
    blast_radius: Optional[list[str]] = None,
    project_id: Optional[str] = None,
    as_html: bool = True,
) -> str:
    """Format work note for a late-arriving or flapping correlated alert with clickable links."""
    proj = project_id.strip() if project_id and project_id.strip() else None
    builder = ConsoleLinkBuilder(project_id=proj) if proj else None

    policy_name = getattr(alert, "policy_name", None) or "Alert"
    policy_id = getattr(alert, "policy_id", None)
    resource_name = getattr(alert, "resource_name", None) or "unknown-resource"
    raw_incident_id = getattr(alert, "incident_id", None)
    clean_incident_id = str(raw_incident_id or "").strip()
    has_real_id = bool(
        clean_incident_id and clean_incident_id.lower() not in ["none", "n/a", "unknown", "null", ""]
    )

    policy_url = builder.build_policy_url(policy_id or policy_name) if builder else None

    alert_url: Optional[str] = None
    if has_real_id:
        payload_url = getattr(alert, "url", None)
        if payload_url and str(payload_url).strip():
            alert_url = str(payload_url).strip()
        elif builder:
            alert_url = builder.build_incident_url(clean_incident_id)

    blast_str = ", ".join(blast_radius) if blast_radius else resource_name

    comp = getattr(alert, "component", None)
    if not comp and hasattr(alert, "policy_user_labels") and alert.policy_user_labels:
        comp = alert.policy_user_labels.get("component")
    if not comp and isinstance(alert, dict):
        comp = alert.get("component") or (alert.get("policy_user_labels") or {}).get("component")
    if not comp:
        comp = resource_name

    sig = getattr(alert, "signal", None)
    if not sig and hasattr(alert, "policy_user_labels") and alert.policy_user_labels:
        sig = alert.policy_user_labels.get("signal")
    if not sig and isinstance(alert, dict):
        sig = alert.get("signal") or (alert.get("policy_user_labels") or {}).get("signal")

    if not as_html:
        pol_str = f"{policy_name} ({policy_url})" if policy_url else policy_name
        if has_real_id and alert_url:
            alert_part_plain = f"(Alert ID: {clean_incident_id} - {alert_url})"
        else:
            disp_id = clean_incident_id if clean_incident_id else "N/A"
            alert_part_plain = f"(Alert ID: {disp_id})"
        meta_parts = []
        if comp:
            meta_parts.append(f"Component: {comp}")
        if sig:
            meta_parts.append(f"Signal: {sig}")
        meta_str = f" | {' | '.join(meta_parts)}" if meta_parts else ""
        return (
            f"Correlated Alert Ingested: {pol_str} on "
            f"{resource_name} {alert_part_plain}{meta_str}\n"
            f"Updated Blast Radius: {blast_str}"
        )

    if policy_url:
        policy_html = (
            f'<a href="{html.escape(policy_url, quote=True)}" target="_blank" '
            f'class="console-link" style="color: #0969da; text-decoration: none;">'
            f'{html.escape(policy_name)}</a>'
        )
    else:
        policy_html = html.escape(policy_name)
    resource_html = f"<code>{html.escape(resource_name)}</code>"

    if has_real_id and alert_url:
        id_link = (
            f'<a href="{html.escape(alert_url, quote=True)}" target="_blank" '
            f'class="console-link" style="color: #0969da; text-decoration: none;">'
            f'{html.escape(clean_incident_id)}</a>'
        )
        alert_part = f"(Alert ID: {id_link})"
    else:
        disp_id = clean_incident_id if clean_incident_id else "N/A"
        alert_part = f"(Alert ID: {html.escape(disp_id)})"

    meta_html_parts = []
    if comp:
        meta_html_parts.append(f"Component: <code>{html.escape(comp)}</code>")
    if sig:
        meta_html_parts.append(f"Signal: <code>{html.escape(sig)}</code>")
    meta_html = f" | {' | '.join(meta_html_parts)}" if meta_html_parts else ""

    blast_html = html.escape(blast_str)

    return (
        f"[code]<b>Correlated Alert Ingested:</b> {policy_html} on "
        f"{resource_html} {alert_part}{meta_html}<br/>"
        f"<b>Updated Blast Radius:</b> {blast_html}[/code]"
    )


def safe_promote_upstream_origin(doc: OKFDocument, alert: AlertEvent) -> bool:
    """Safely promote doc.origin to upstream service only when upstream exhibits its own anomaly.
    
    If alert.resource_name is an upstream dependency of doc.origin, promotes origin
    to alert.resource_name and expands blast_radius. Never demotes origin to downstream victim.
    """
    if not alert.resource_name:
        return False

    curr_origin = doc.origin
    if not curr_origin:
        doc.origin = alert.resource_name
        if alert.resource_name not in doc.blast_radius:
            doc.blast_radius.append(alert.resource_name)
        return True

    if alert.resource_name == curr_origin:
        return False

    try:
        upstreams = get_upstream_dependencies(curr_origin)
        if alert.resource_name in upstreams:
            logger.info(
                "Promoting group %s origin from %s to upstream %s",
                doc.group_id,
                curr_origin,
                alert.resource_name,
            )
            doc.origin = alert.resource_name
            for b in get_blast_radius(doc.origin):
                if b not in doc.blast_radius:
                    doc.blast_radius.append(b)
            return True
    except Exception as exc:
            logger.debug("Failed checking upstream dependencies during safe origin update: %s", exc)

    if alert.resource_name not in doc.blast_radius:
        doc.blast_radius.append(alert.resource_name)
    return False
