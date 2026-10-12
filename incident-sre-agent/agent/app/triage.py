import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from google.adk.agents import LlmAgent
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts.incident import Incident
from .prompts import load_prompt
from .skills_loader import SkillsLoader

VALID_SPECIALISTS = {"logs", "metrics", "changes", "resources"}


class InvestigationPlan(BaseModel):
    """Structured plan formulating the scope and specialists for an incident investigation."""

    model_config = ConfigDict(extra="forbid")

    affected_service: str
    symptoms: List[str] = Field(default_factory=list)
    start_time: datetime
    end_time: datetime
    selected_specialists: List[str] = Field(default_factory=list)
    initial_hypotheses: List[str] = Field(default_factory=list)

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_plan(self) -> "InvestigationPlan":
        if self.start_time > self.end_time:
            raise ValueError(
                f"start_time ({self.start_time}) cannot be after end_time ({self.end_time})"
            )
        for s in self.selected_specialists:
            if s not in VALID_SPECIALISTS:
                raise ValueError(
                    f"Invalid specialist '{s}'. Must be one of {sorted(VALID_SPECIALISTS)}"
                )
        return self


class TriageAgent:
    """Triage agent analyzing incident tickets to produce an InvestigationPlan."""

    def __init__(
        self,
        skills_loader: Optional[SkillsLoader] = None,
        max_query_window_hours: Optional[int] = None,
        llm_client: Any = None,
        model: Optional[str] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[Any] = None,
    ) -> None:
        if not model:
            if config and hasattr(config, "model_dispatcher"):
                model = config.model_dispatcher
            else:
                try:
                    from .config import get_settings

                    model = get_settings().model_dispatcher
                except Exception:
                    model = ""

        if max_query_window_hours is None:
            if config and hasattr(config, "max_query_window_hours"):
                max_query_window_hours = config.max_query_window_hours
            else:
                try:
                    from .config import get_settings

                    max_query_window_hours = get_settings().max_query_window_hours
                except Exception:
                    max_query_window_hours = 24

        self.skills_loader = skills_loader or SkillsLoader()
        self.max_query_window_hours = max_query_window_hours
        self.llm_client = llm_client
        instruction = load_prompt("triage")
        from .skills_loader import get_skill_toolset_for_target

        toolset = get_skill_toolset_for_target(
            "dispatcher", skills_dir=self.skills_loader.skills_dir
        )
        self.skill_toolset = toolset
        self.llm_agent = llm_agent or LlmAgent(
            name="triage",
            model=model or "default",
            instruction=instruction,
            tools=[toolset],
            output_schema=InvestigationPlan,
        )
        self.triage_mode: str = "llm"

    def _consult_playbook(self) -> List[str]:
        """Optionally consult the triage-playbook skill for rules and guidance."""
        try:
            skills = self.skills_loader.discover_skills()
            if "triage-playbook" in skills:
                content = self.skills_loader.get_skill_content("triage-playbook")
                return [f"Loaded triage playbook: {len(content.instructions)} chars"]
        except Exception:
            pass
        return []

    def _extract_affected_service(self, incident: Incident) -> str:
        """Identify the primary affected service from service_hint, title, or description."""
        if incident.service_hint and incident.service_hint.strip():
            return incident.service_hint.strip()

        combined_text = f"{incident.title} {incident.description}".lower()

        # Check for generic service name pattern e.g. *-api, *-svc, *-service
        match = re.search(r"\b([a-z0-9]+(?:-[a-z0-9]+)*(?:-api|-svc|-service))\b", combined_text)
        if match:
            return match.group(1)

        # Dynamic Cloud Run service discovery with prefix awareness
        try:
            import os

            from .tools.clients import get_cloud_run_services_client
            from .tools.resources import describe_cloud_run_service

            project_id = os.getenv("GCP_PROJECT_ID", "")
            region = os.getenv("GCP_REGION", "")
            try:
                from .config import get_settings

                settings = get_settings()
                proj_cfg = getattr(settings, "gcp_project", "") or getattr(
                    settings, "gcp_project_id", ""
                )
                project_id = project_id or str(proj_cfg)
                region = region or str(getattr(settings, "gcp_region", ""))
            except Exception:
                pass

            client = get_cloud_run_services_client()
            if client is not None and project_id and region:
                parent = f"projects/{project_id}/locations/{region}"
                try:
                    services = client.list_services(parent=parent)
                    prefix = os.getenv("NAME_PREFIX", "")
                    for s in services:
                        s_name = getattr(s, "name", "").split("/")[-1]
                        if not s_name:
                            continue
                        cand_names = [s_name]
                        if prefix and s_name.startswith(f"{prefix}-"):
                            cand_names.append(s_name[len(prefix) + 1 :])
                        for cand in cand_names:
                            if cand.lower() in combined_text:
                                return cand
                except Exception:
                    pass

            if project_id:
                potential_names = re.findall(r"\b([a-z0-9]+(?:-[a-z0-9]+)+)\b", combined_text)
                for cand in potential_names:
                    try:
                        desc = describe_cloud_run_service(
                            project_id=project_id, service_name=cand, region=region, client=client
                        )
                        if desc and desc.get("uri"):
                            return str(desc.get("service_name") or cand)
                    except Exception:
                        continue
        except Exception:
            pass

        return "unknown-service"

    def _extract_symptoms(self, incident: Incident) -> List[str]:
        """Classify primary symptoms from ticket content."""
        symptoms: List[str] = []
        text = f"{incident.title} {incident.description}".lower()

        if any(w in text for w in ["500", "502", "503", "504", "5xx", "error", "exception"]):
            symptoms.append("HTTP 5xx / application errors")
        if any(
            w in text for w in ["latency", "slow", "timeout", "timed out", "delay", "p95", "p99"]
        ):
            symptoms.append("High latency / timeouts")
        if any(w in text for w in ["oom", "memory", "cpu", "limit exceeded", "leak", "saturation"]):
            symptoms.append("Resource saturation / OOM")
        if any(w in text for w in ["deploy", "release", "revision", "rollout", "build"]):
            symptoms.append("Recent deployment regression")
        if any(w in text for w in ["database", "db", "sql", "connection pool", "exhausted"]):
            symptoms.append("Database / connection pool exhaustion")
        if any(w in text for w in ["403", "permission", "iam", "forbidden", "denied"]):
            symptoms.append("IAM permission / authorization error")

        if not symptoms:
            symptoms.append(f"Unspecified degradation: {incident.title[:60]}")

        return symptoms

    def _compute_time_window(
        self,
        incident: Incident,
        start_time_override: Optional[datetime] = None,
        end_time_override: Optional[datetime] = None,
    ) -> tuple[datetime, datetime]:
        """Compute UTC investigation window with strict 24h maximum clamping."""
        opened_at = incident.opened_at
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        else:
            opened_at = opened_at.astimezone(timezone.utc)

        # Default end_time to now (UTC) per real-time investigation requirement
        now_utc = datetime.now(timezone.utc)
        end_time = end_time_override or now_utc
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        else:
            end_time = end_time.astimezone(timezone.utc)

        if start_time_override is not None:
            start_time = start_time_override
        else:
            # Active incident investigation: cover now_utc - 60m
            # (or opened_at - 15m if opened_at is within last 2 hours)
            if (now_utc - timedelta(hours=2)) <= opened_at <= (now_utc + timedelta(minutes=5)):
                start_time = opened_at - timedelta(minutes=15)
            else:
                start_time = now_utc - timedelta(minutes=60)

        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        else:
            start_time = start_time.astimezone(timezone.utc)

        # Enforce 24-hour maximum window constraint
        max_delta = timedelta(hours=self.max_query_window_hours)
        if (end_time - start_time) > max_delta:
            start_time = end_time - max_delta

        if start_time > end_time:
            start_time = end_time - timedelta(minutes=60)

        return start_time, end_time

    def _select_specialists(self, incident: Incident, symptoms: List[str]) -> List[str]:
        """Select specialists based on severity, symptoms, and playbooks."""
        # For critical P1 and P2 incidents, dispatch all specialists for thorough coverage
        if incident.severity in ("P1", "P2"):
            return ["logs", "metrics", "changes", "resources"]

        selected: set[str] = set()
        symptom_str = " ".join(symptoms).lower()
        full_text = f"{symptom_str} {incident.title} {incident.description}".lower()

        # Multi-specialist triage: for HTTP 5xx / application errors / latency,
        # select BOTH logs and metrics
        if any(
            s in full_text
            for s in [
                "error",
                "exception",
                "5xx",
                "500",
                "502",
                "503",
                "504",
                "application",
                "latency",
                "slow",
                "timeout",
                "timed out",
                "delay",
                "p95",
                "p99",
                "metrics",
            ]
        ):
            selected.add("logs")
            selected.add("metrics")

        # Include resources when saturation/database/connection keywords appear
        if any(
            s in full_text
            for s in [
                "saturation",
                "database",
                "db",
                "sql",
                "connection",
                "pool",
                "exhaust",
                "resource",
                "oom",
                "cpu",
                "memory",
                "limit exceeded",
                "leak",
            ]
        ):
            selected.add("resources")

        if any(
            s in full_text
            for s in [
                "deploy",
                "revision",
                "release",
                "rollout",
                "build",
                "change",
                "iam",
                "regression",
            ]
        ):
            selected.add("changes")

        # Fallback to at least logs and metrics
        if not selected:
            selected.update(["logs", "metrics"])

        # Preserve canonical order
        return [s for s in ["logs", "metrics", "changes", "resources"] if s in selected]

    def _generate_hypotheses(
        self,
        service: str,
        symptoms: List[str],
        specialists: List[str],
    ) -> List[str]:
        """Formulate initial investigation hypotheses."""
        hypotheses: List[str] = []
        symptom_str = " ".join(symptoms).lower()

        if "changes" in specialists:
            hypotheses.append(
                f"Recent revision deployment or config change in {service} triggered the anomaly."
            )
        if "logs" in specialists and "5xx" in symptom_str:
            hypotheses.append(
                f"Application exception or downstream timeout occurring in {service} endpoints."
            )
        if "resources" in specialists and ("oom" in symptom_str or "saturation" in symptom_str):
            hypotheses.append(
                f"Resource limits or database connection saturation affecting {service}."
            )
        if not hypotheses:
            hypotheses.append(
                f"Investigating telemetry anomalies and errors for service {service}."
            )

        return hypotheses

    def plan(
        self,
        incident: Incident,
        start_time_override: Optional[datetime] = None,
        end_time_override: Optional[datetime] = None,
    ) -> InvestigationPlan:
        """Produce an InvestigationPlan for the incoming incident."""
        self._consult_playbook()

        if self.llm_client is not None:
            try:
                # If LLM client provides plan generation
                llm_res = self.llm_client.generate_plan(incident)
                now_utc = datetime.now(timezone.utc)
                if isinstance(llm_res, InvestigationPlan):
                    self.triage_mode = "llm"
                    llm_res.end_time = end_time_override or now_utc
                    if start_time_override is not None:
                        llm_res.start_time = start_time_override
                    else:
                        opened_at = incident.opened_at
                        if opened_at.tzinfo is None:
                            opened_at = opened_at.replace(tzinfo=timezone.utc)
                        else:
                            opened_at = opened_at.astimezone(timezone.utc)
                        if (
                            (now_utc - timedelta(hours=2))
                            <= opened_at
                            <= (now_utc + timedelta(minutes=5))
                        ):
                            llm_res.start_time = opened_at - timedelta(minutes=15)
                        else:
                            llm_res.start_time = now_utc - timedelta(minutes=60)
                    return llm_res
                if isinstance(llm_res, dict):
                    self.triage_mode = "llm"
                    plan_obj = InvestigationPlan(**llm_res)
                    plan_obj.end_time = end_time_override or now_utc
                    if start_time_override is not None:
                        plan_obj.start_time = start_time_override
                    else:
                        opened_at = incident.opened_at
                        if opened_at.tzinfo is None:
                            opened_at = opened_at.replace(tzinfo=timezone.utc)
                        else:
                            opened_at = opened_at.astimezone(timezone.utc)
                        if (
                            (now_utc - timedelta(hours=2))
                            <= opened_at
                            <= (now_utc + timedelta(minutes=5))
                        ):
                            plan_obj.start_time = opened_at - timedelta(minutes=15)
                        else:
                            plan_obj.start_time = now_utc - timedelta(minutes=60)
                    return plan_obj
            except Exception:
                # Fallback to deterministic triage on LLM failure
                pass

        self.triage_mode = "heuristic_fallback"
        service = self._extract_affected_service(incident)
        symptoms = self._extract_symptoms(incident)
        start_time, end_time = self._compute_time_window(
            incident,
            start_time_override=start_time_override,
            end_time_override=end_time_override,
        )
        selected_specialists = self._select_specialists(incident, symptoms)
        hypotheses = self._generate_hypotheses(service, symptoms, selected_specialists)

        return InvestigationPlan(
            affected_service=service,
            symptoms=symptoms,
            start_time=start_time,
            end_time=end_time,
            selected_specialists=selected_specialists,
            initial_hypotheses=hypotheses,
        )
