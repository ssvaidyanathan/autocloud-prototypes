import uuid
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .incident import Incident
from .report import Report


class InvestigationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    incident_id: str
    incident_source: str
    incident_external_id: str = ""
    incident_external_key: str = ""
    status: Literal["completed", "failed"]
    report: Optional[Report] = None
    error: Optional[str] = None
    incident: Optional[Incident] = None

    @model_validator(mode="after")
    def validate_pairwise_status_payload(self) -> "InvestigationResult":
        if self.incident is not None:
            if not self.incident_external_id:
                self.incident_external_id = self.incident.external_id
            if not self.incident_external_key:
                self.incident_external_key = self.incident.external_key

        if self.status == "completed":
            if self.report is None:
                raise ValueError("report must not be None when status is 'completed'")
        elif self.status == "failed":
            if not self.error or not self.error.strip():
                raise ValueError("error must not be None and non-empty when status is 'failed'")
        return self

    @property
    def skills_used(self) -> list[str]:
        """Aggregate list of skills used from report, if present."""
        return self.report.skills_used if self.report else []
