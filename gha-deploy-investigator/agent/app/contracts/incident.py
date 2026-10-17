from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Incident(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: Literal["servicenow", "jira", "github", "generic"]
    external_id: str
    external_key: str
    external_url: Optional[str] = None
    title: str
    description: str
    severity: Literal["P1", "P2", "P3", "P4"]
    service_hint: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    opened_at: datetime
    reporter: Optional[str] = None

    # GitHub Actions specific context fields
    github_repo: Optional[str] = None
    github_run_id: Optional[int] = None
    github_head_sha: Optional[str] = None
    github_branch: Optional[str] = None
    github_pr_number: Optional[int] = None
    github_failing_stage: Optional[str] = None
    github_run_attempt: Optional[int] = 1
    github_actor: Optional[str] = None
    github_commit_author: Optional[str] = None

    @field_validator("opened_at", mode="after")
    @classmethod
    def ensure_utc_opened_at(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)
