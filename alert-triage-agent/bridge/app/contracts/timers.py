from typing import Optional
from pydantic import BaseModel, Field


class TimerWakeRequest(BaseModel):
    """Payload sent by Cloud Tasks delayed timer to POST /timer/wake."""
    group_id: str = Field(..., description="Canonical incident group identifier")
    deadline_type: str = Field(..., description="Deadline type: settle, max_wait, cooling, or investigation_watchdog")
    scheduled_epoch: int = Field(..., description="Unix timestamp in seconds for scheduled deadline")


class TimerWakeResponse(BaseModel):
    """Response returned by agent after processing /timer/wake."""
    status: str = Field(..., description="Status of the wake evaluation: executed, ignored, or error")
    group_id: str = Field(..., description="Canonical incident group identifier")
    reason: Optional[str] = Field(default=None, description="Optional reason if ignored or rejected")
