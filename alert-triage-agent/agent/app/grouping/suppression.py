"""Agent suppression engine and late alert handling.

Implements suppression decisions owned entirely by the agent:
- Duplicate detection across OKF documents (idempotent suppression).
- Late alert formatting for ticketed groups.
- Safe upstream DAG origin promotion.
"""

from enum import Enum
import logging
from typing import Any, Optional
from pydantic import BaseModel, Field

from ..contracts.alert import AlertEvent
from .models import OKFDocument

logger = logging.getLogger(__name__)


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


def format_late_alert_work_note(alert: AlertEvent, blast_radius: Optional[list[str]] = None) -> str:
    """Format work note for a late-arriving or flapping correlated alert."""
    blast_str = ", ".join(blast_radius) if blast_radius else alert.resource_name
    return (
        f"[code]<b>Correlated Alert Ingested:</b> {alert.policy_name} on "
        f"<code>{alert.resource_name}</code> (Alert ID: {alert.incident_id})<br/>"
        f"<b>Updated Blast Radius:</b> {blast_str}[/code]"
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
        from topology.topology_graph import get_blast_radius, get_upstream_dependencies
    except ImportError:
        try:
            from agent.topology.topology_graph import get_blast_radius, get_upstream_dependencies
        except ImportError:
            get_upstream_dependencies = None
            get_blast_radius = None

    if get_upstream_dependencies is not None:
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
                if get_blast_radius is not None:
                    for b in get_blast_radius(doc.origin):
                        if b not in doc.blast_radius:
                            doc.blast_radius.append(b)
                return True
        except Exception as exc:
            logger.debug("Failed checking upstream dependencies during safe origin update: %s", exc)

    if alert.resource_name not in doc.blast_radius:
        doc.blast_radius.append(alert.resource_name)
    return False
