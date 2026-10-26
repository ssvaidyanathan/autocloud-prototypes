from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .findings import EvidenceItem


class TimelineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    description: str

    @field_validator("timestamp", mode="after")
    @classmethod
    def ensure_utc_timestamp(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class ProbableRootCause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    confidence: float = Field(ge=0.0, le=1.0)


class RecentChangeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    description: str
    author: Optional[str] = None
    console_url: Optional[str] = None

    @field_validator("timestamp", mode="after")
    @classmethod
    def ensure_utc_timestamp(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class SpecialistStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    specialist: str
    status: Literal["completed", "failed", "skipped"]
    error: Optional[str] = None


class CheckRunAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int
    end_line: int
    annotation_level: Literal["notice", "warning", "failure"] = "failure"
    message: str
    title: Optional[str] = None
    raw_details: Optional[str] = None


class FilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    action: Literal["modify", "create", "delete"]
    content: Optional[str] = None
    diff: Optional[str] = None


class FixProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["proposed", "no_safe_fix", "unsupported"]
    strategy: Literal["deterministic_linter", "llm_patch", "config_tuning", "commit_hunk_revert"]
    rationale: str
    patches: list[FilePatch] = Field(default_factory=list)
    target_branch_base: str


class PullRequestRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_number: int
    pr_url: str
    branch_name: str
    head_sha: str
    status: Literal["opened", "ci_pending", "ci_passed", "ci_failed"]


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    impact: str = ""
    timeline: list[TimelineItem] = Field(default_factory=list)
    evidence_by_area: dict[str, list[EvidenceItem]] = Field(default_factory=dict)
    probable_root_cause: Optional[ProbableRootCause] = None
    recent_changes: list[RecentChangeItem] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    causal_remediation: list[str] = Field(default_factory=list)
    unrelated_observations: list[str] = Field(default_factory=list)
    specialists_status: list[SpecialistStatus] = Field(default_factory=list)
    skills_used: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    annotations: list[CheckRunAnnotation] = Field(default_factory=list)
    queries_appendix: dict[str, list[str]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    fix_proposal: Optional[FixProposal] = None
