import logging
import re
from typing import Optional
from pydantic import BaseModel, Field

from .report import InvestigationReport

logger = logging.getLogger(__name__)

# Ticket priority -> ServiceNow (urgency, impact). Chosen so that ServiceNow's default
# priority matrix (dl_u_priority, verified read-only on the PDI 2026-10-28) shows the same
# number as the ticket title: 1/1 -> 1 Critical, 1/2 -> 2 High, 2/2 -> 3 Moderate, 2/3 -> 4 Low.
# An instance with a customised matrix must review this table (see CONFIGURATION.md).
SERVICENOW_URGENCY_IMPACT: dict[str, tuple[str, str]] = {
    "P1": ("1", "1"),
    "P2": ("1", "2"),
    "P3": ("2", "2"),
    "P4": ("2", "3"),
}


# Used when the priority cannot be parsed at all; the same as InvestigationReport's default.
SERVICENOW_FALLBACK_PRIORITY = "P2"


def servicenow_urgency_impact(priority: Optional[str]) -> tuple[str, str]:
    """(urgency, impact) for a ticket priority; never raises, so a ticket is never lost over it.

    A priority outside P1..P4 is clamped and logged at WARNING: P0 (or any rank above P1) maps
    to P1 so the most severe label is never de-escalated, and P5+ maps to P4. A value that is not
    'P<n>' at all maps to SERVICENOW_FALLBACK_PRIORITY.
    """
    key = str(priority or "").strip().upper()
    if key in SERVICENOW_URGENCY_IMPACT:
        return SERVICENOW_URGENCY_IMPACT[key]
    m = re.fullmatch(r"P(\d+)", key)
    if m:
        rank = int(m.group(1))
        clamped = "P1" if rank < 1 else "P4"
    else:
        clamped = SERVICENOW_FALLBACK_PRIORITY
    logger.warning(
        "Priority %r is outside the ServiceNow mapping (P1..P4); using %s urgency/impact", priority, clamped
    )
    return SERVICENOW_URGENCY_IMPACT[clamped]


class CreateTicketRequest(BaseModel):
    """Payload to mint a ServiceNow incident at settle/max-wait deadline."""
    group_id: str = Field(..., description="Canonical incident group identifier used as correlation_id")
    report: InvestigationReport = Field(..., description="Full diagnostic report produced by synthesizer")
    idempotency_key: Optional[str] = Field(default=None, description="Optional idempotency key")


class WorkNoteRequest(BaseModel):
    """Payload to append a work note or journal entry to a ticket."""
    sys_id: str = Field(..., description="ServiceNow incident sys_id")
    work_note: str = Field(..., description="Formatted work note string")
    group_id: Optional[str] = Field(default=None, description="Optional canonical incident group identifier")


class UpdateReportRequest(BaseModel):
    """Payload to refresh ticket description and diagnostic report."""
    sys_id: str = Field(..., description="ServiceNow incident sys_id")
    group_id: str = Field(..., description="Canonical incident group identifier")
    report: InvestigationReport = Field(..., description="Updated diagnostic report")


class UpdateTicketFieldsRequest(BaseModel):
    """Payload to update a ticket's title (and raise its priority) when alerts join its group.

    Sent when alerts attach to an already-ticketed group: the short description is rebuilt
    from the group (alert count, problem) and ``priority`` is set only when a new alert is
    more severe than the ticket (the agent never lowers it). ``work_note`` states the change.
    """
    sys_id: str = Field(..., description="ServiceNow incident sys_id")
    group_id: str = Field(..., description="Canonical incident group identifier")
    short_description: str = Field(..., description="New ticket short description")
    priority: Optional[str] = Field(default=None, description="Escalated priority (P1..P4); None leaves urgency/impact unchanged")
    work_note: str = Field(..., description="Work note naming the new alerts and the changes")


class ResolveTicketRequest(BaseModel):
    """Payload to resolve an incident in ServiceNow."""
    sys_id: str = Field(..., description="ServiceNow incident sys_id")
    group_id: str = Field(..., description="Canonical incident group identifier")
    close_notes: str = Field(..., description="Resolution summary")
    close_code: str = Field(default="Solved (Permanently)", description="ServiceNow close code")


class TicketResponse(BaseModel):
    """Response returned when querying, creating, or updating a ticket."""
    status: str = Field(..., description="Status of the operation (e.g. created, already_exists, updated)")
    sys_id: str = Field(..., description="ServiceNow incident sys_id")
    number: str = Field(..., description="ServiceNow incident number")
    url: str = Field(..., description="ServiceNow incident deep link URL")
    group_id: str = Field(..., description="Canonical incident group identifier")


class ActionStatusResponse(BaseModel):
    """Generic status response for ticket actions (e.g. work-note, resolve)."""
    status: str = Field(..., description="Status of the operation (e.g. success, updated, resolved)")
    sys_id: Optional[str] = Field(default=None, description="ServiceNow incident sys_id")
    group_id: Optional[str] = Field(default=None, description="Canonical incident group identifier")
    message: Optional[str] = Field(default=None, description="Optional detail or error message")


class TicketWorkNote(BaseModel):
    """A single work note journal entry with text and timestamp."""
    text: str = Field(..., description="Work note text")
    timestamp: Optional[str] = Field(default=None, description="Timestamp when note was recorded")
    value: Optional[str] = Field(default=None, description="Compatibility alias for text")
    created_on: Optional[str] = Field(default=None, description="Compatibility alias for timestamp")


class TicketAttachment(BaseModel):
    """Metadata for an incident attachment."""
    name: str = Field(..., description="Attachment file name")
    size_bytes: Optional[int] = Field(default=None, description="Attachment file size in bytes")
    file_name: Optional[str] = Field(default=None, description="Compatibility alias for name")
    size: Optional[int] = Field(default=None, description="Compatibility alias for size_bytes")


class TicketViewResponse(BaseModel):
    """Read-only view of ServiceNow ticket fields. NEVER contains credentials."""
    number: str = Field(..., description="ServiceNow incident number")
    state: str = Field(..., description="ServiceNow incident state")
    short_description: str = Field(..., description="Short description of incident")
    caller: Optional[str] = Field(default=None, description="Caller name or identifier")
    work_notes: list[TicketWorkNote] = Field(default_factory=list, description="Work notes text with timestamps")
    attachments: list[TicketAttachment] = Field(default_factory=list, description="Attachment names and sizes")

