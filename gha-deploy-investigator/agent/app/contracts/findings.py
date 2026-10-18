from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    excerpt: str
    timestamp: Optional[datetime] = None
    console_url: Optional[str] = None

    @field_validator("timestamp", mode="after")
    @classmethod
    def ensure_utc_timestamp(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return None
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class LogFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    step_name: str
    line_number: Optional[int] = None
    file_path: Optional[str] = None
    message: str
    raw_log: Optional[str] = None


class Findings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    queries_run: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    errors: list[str] = Field(default_factory=list)
    skills_used: list[str] = Field(default_factory=list)
    skill_first: bool = False
    fallback_used: bool = False
