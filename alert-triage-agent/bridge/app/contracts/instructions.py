from typing import Optional
from pydantic import BaseModel, Field

from .report import InvestigationReport


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

