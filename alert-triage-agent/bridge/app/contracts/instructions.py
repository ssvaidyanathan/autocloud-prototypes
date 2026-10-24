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
