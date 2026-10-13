from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, model_validator

from .report import Report


class InvestigationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    incident_id: str
    incident_source: str
    incident_external_id: str
    incident_external_key: str
    status: Literal["completed", "failed"]
    report: Optional[Report] = None
    error: Optional[str] = None

    @model_validator(mode="after")
    def validate_pairwise_status_payload(self) -> "InvestigationResult":
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
