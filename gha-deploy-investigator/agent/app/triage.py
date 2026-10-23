import logging
import re
from datetime import datetime, timezone
from typing import Any, List, Optional

from google.adk.agents import LlmAgent
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import AgentConfig, get_settings
from .contracts.incident import Incident
from .skills_loader import SkillsLoader, get_skill_toolset_for_target

logger = logging.getLogger(__name__)

VALID_SPECIALISTS = {"pipeline_logs", "runtime_health", "iac_config"}

STAGE_DEPLOY_RE = re.compile(
    r"\b(deploy|deployments?|rollout|release|cloudrun|gcloud)\b", re.IGNORECASE
)
STAGE_IAC_RE = re.compile(
    r"\b(terraform|iac|manifest|manifests|tf)\b", re.IGNORECASE
)
STAGE_LINT_RE = re.compile(
    r"\b(lint|linters?|ruff|flake8|mypy|eslint|fmt|syntax)\b", re.IGNORECASE
)
STAGE_CHECK_RE = re.compile(r"\bcheck\b", re.IGNORECASE)
STAGE_TEST_RE = re.compile(
    r"\b(tests?|pytest|jest|unit|integration|spec)\b", re.IGNORECASE
)
STAGE_BUILD_RE = re.compile(
    r"\b(build|builds?|docker|image|images?|container|containers?|package|compile|push)\b",
    re.IGNORECASE,
)


def classify_stage(job_name: str, step_name: str) -> str:
    """Classify pipeline failure into standardized stage categories using word boundaries."""
    combined = f"{job_name} {step_name}"
    if STAGE_DEPLOY_RE.search(combined):
        return "deploy"
    if STAGE_IAC_RE.search(combined):
        return "iac_config"
    if STAGE_LINT_RE.search(combined) or STAGE_CHECK_RE.search(combined):
        return "lint"
    if STAGE_TEST_RE.search(combined):
        return "test"
    if STAGE_BUILD_RE.search(combined):
        return "build"
    return "pipeline_logs"


class InvestigationPlan(BaseModel):
    """Structured plan formulating the scope, specialists, and skills for an incident."""

    model_config = ConfigDict(extra="allow")

    affected_service: str
    stage: str = "pipeline_logs"
    symptoms: List[str] = Field(default_factory=list)
    selected_specialists: List[str] = Field(default_factory=list)
    initial_hypotheses: List[str] = Field(default_factory=list)
    skills_to_load: List[str] = Field(default_factory=list)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def ensure_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return None
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class TriageAgent:
    """Triage agent analyzing CI/CD failure context to produce an InvestigationPlan."""

    def __init__(
        self,
        skills_loader: Optional[SkillsLoader] = None,
        llm_client: Any = None,
        model: Optional[str] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
        **kwargs: Any,
    ) -> None:
        self.config = config or get_settings()
        self.skills_loader = skills_loader or SkillsLoader()
        self.model = model or getattr(self.config, "gemini_model", None)
        self.llm_client = llm_client

        toolset = get_skill_toolset_for_target(
            "dispatcher", skills_dir=self.skills_loader.skills_dir
        )
        self.skill_toolset = toolset
        from .agent_config import get_agent_config

        instruction = get_agent_config().get_instruction("triage")
        self.llm_agent = llm_agent or LlmAgent(
            name="triage",
            model=self.model or "default",
            instruction=instruction,
            tools=[toolset],
            output_schema=InvestigationPlan,
        )
        self.triage_mode: str = "heuristic_fallback"

    def plan(
        self,
        incident: Incident,
        failing_job: Optional[dict[str, Any]] = None,
        failing_step: Optional[dict[str, Any]] = None,
        log_text: str = "",
    ) -> InvestigationPlan:
        """Produce an InvestigationPlan for the incoming incident."""
        now_utc = datetime.now(timezone.utc)
        job_name = failing_job.get("name", "pipeline") if failing_job else "pipeline"
        step_name = failing_step.get("name", "execution") if failing_step else "execution"
        stage = classify_stage(job_name, step_name)

        affected_service = incident.github_repo or incident.service_hint or "unknown-service"

        symptoms: List[str] = []
        hypotheses: List[str] = []
        skills_to_load: List[str] = []

        if stage in ("lint", "check"):
            specialist = "pipeline_logs"
            skill = "lint-troubleshooting"
            symptoms.append(f"Static analysis/linter check failure in '{step_name}'")
            hypotheses.append("Code style or lint rule violation detected during static check.")
        elif stage == "test":
            specialist = "pipeline_logs"
            skill = "test-failure-triage"
            symptoms.append(f"Automated test failure or assertion error in '{step_name}'")
            hypotheses.append("Unit or integration test failed due to logic defect or assertion.")
        elif stage == "build":
            specialist = "pipeline_logs"
            skill = "container-build-troubleshooting"
            symptoms.append(f"Container image build/packaging failure in '{step_name}'")
            hypotheses.append("Docker build command, missing context file, or dependency conflict.")
        elif stage == "deploy":
            specialist = "runtime_health"
            skill = "cloudrun-deploy-troubleshooting"
            symptoms.append(f"Deployment rollout failure in '{step_name}'")
            hypotheses.append(
                "Cloud Run revision startup failure, probe timeout, or OOM saturation."
            )
        elif stage == "iac_config":
            specialist = "iac_config"
            skill = "cloudrun-deploy-troubleshooting"
            symptoms.append(f"IaC manifest or Terraform configuration error in '{step_name}'")
            hypotheses.append(
                "Syntax error or invalid attribute schema in Terraform configuration."
            )
        else:
            specialist = "pipeline_logs"
            skill = "container-build-troubleshooting"
            symptoms.append(f"Workflow pipeline failure in step '{step_name}'")
            hypotheses.append("Unknown failure during pipeline execution.")

        skills_to_load.append(skill)
        selected_specialists = [specialist]

        plan = InvestigationPlan(
            affected_service=affected_service,
            stage=stage,
            symptoms=symptoms,
            selected_specialists=selected_specialists,
            initial_hypotheses=hypotheses,
            skills_to_load=skills_to_load,
            start_time=incident.opened_at,
            end_time=now_utc,
        )

        # Check if LLM client was provided and executed
        if self.llm_client is not None and hasattr(self.llm_client, "generate_plan"):
            try:
                llm_plan = self.llm_client.generate_plan(incident)
                if isinstance(llm_plan, InvestigationPlan):
                    self.triage_mode = "llm"
                    return llm_plan
            except Exception as exc:
                logger.warning("LLM triage plan generation failed: %s", exc)

        self.triage_mode = "heuristic_fallback"
        return plan


__all__ = [
    "InvestigationPlan",
    "TriageAgent",
    "VALID_SPECIALISTS",
    "classify_stage",
]
