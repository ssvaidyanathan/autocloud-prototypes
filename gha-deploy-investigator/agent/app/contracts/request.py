from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_validator

from .incident import Incident


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident: Incident
    callback_url: str
    requested_at: datetime

    @field_validator("requested_at", mode="after")
    @classmethod
    def ensure_utc_requested_at(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)
