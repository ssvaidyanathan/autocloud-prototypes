import logging
import uuid
import warnings
from typing import Any, Optional

import httpx
from google.adk.agents import BaseAgent, ParallelAgent
from pydantic import ConfigDict, Field

from .callback import send_callback_report
from .config import AgentConfig, get_settings
from .contracts.request import InvestigationRequest
from .contracts.result import InvestigationResult
from .skills_loader import SkillsLoader
from .specialists import (
    BaseSpecialist,
    IaCConfigSpecialist,
    PipelineLogsSpecialist,
    RuntimeHealthSpecialist,
)
from .synthesizer import SynthesizerAgent
from .tools.github_tools import fetch_job_logs, fetch_workflow_run_jobs
from .triage import TriageAgent, classify_stage

logger = logging.getLogger(__name__)


class Dispatcher(BaseAgent):
    """Multi-agent dispatcher orchestrating Triage -> Specialist Fan-Out -> Synthesis."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    config: AgentConfig = Field(default_factory=get_settings)
    specialists: dict[str, Any] = Field(default_factory=dict)
    triage_agent: Any = None
    synthesizer: Any = None
    skills_loader: Any = None
    parallel_agent: Optional[Any] = None

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        specialists: Optional[dict[str, BaseSpecialist]] = None,
        triage_agent: Optional[TriageAgent] = None,
        synthesizer: Optional[SynthesizerAgent] = None,
        skills_loader: Optional[SkillsLoader] = None,
        github_client: Optional[httpx.Client] = None,
        callback_client: Optional[httpx.Client] = None,
        **kwargs: Any,
    ) -> None:
        cfg = config or get_settings()
        loader = skills_loader or SkillsLoader()

        if specialists is not None:
            specs = dict(specialists)
        else:
            specs = {
                "pipeline_logs": PipelineLogsSpecialist(config=cfg),
                "runtime_health": RuntimeHealthSpecialist(config=cfg),
                "iac_config": IaCConfigSpecialist(config=cfg),
            }

        triage = triage_agent or TriageAgent(
            skills_loader=loader,
            config=cfg,
        )
        synth = synthesizer or SynthesizerAgent(
            skills_loader=loader,
            config=cfg,
        )

        specialist_sub_agents = [
            spec.llm_agent
            for spec in specs.values()
            if hasattr(spec, "llm_agent") and spec.llm_agent is not None
        ]

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            parallel = ParallelAgent(
                name="specialists_parallel",
                sub_agents=specialist_sub_agents,
            )

        sub_agents = []
        if hasattr(triage, "llm_agent") and triage.llm_agent is not None:
            sub_agents.append(triage.llm_agent)
        sub_agents.append(parallel)
        if hasattr(synth, "llm_agent") and synth.llm_agent is not None:
            sub_agents.append(synth.llm_agent)

        from .agent_config import get_agent_config

        super().__init__(
            name="dispatcher",
            description=get_agent_config().description,
            sub_agents=sub_agents,
            **kwargs,
        )
        self.config = cfg
        self.specialists = specs
        self.triage_agent = triage
        self.synthesizer = synth
        self.skills_loader = loader
        self.parallel_agent = parallel
        self._github_client = github_client
        self._callback_client = callback_client

    def investigate(self, request: InvestigationRequest) -> InvestigationResult:
        """Run full multi-agent investigation workflow on GitHub Actions incident."""
        incident = request.incident
        repo = incident.github_repo or incident.service_hint or ""
        run_id = incident.github_run_id
        if run_id is None:
            try:
                run_id = int(incident.external_id)
            except (ValueError, TypeError):
                run_id = 0

        logger.info(
            "Beginning investigation for incident %s (repo=%s, run_id=%s)",
            incident.id,
            repo,
            run_id,
        )

        try:
            # 1. Fetch workflow jobs from GitHub API
            jobs_data = fetch_workflow_run_jobs(
                repo=repo,
                run_id=run_id,
                client=self._github_client,
                config=self.config,
            )

            jobs = jobs_data.get("jobs", [])
            failed_job: Optional[dict[str, Any]] = None
            failed_step: Optional[dict[str, Any]] = None

            # Locate first failing step
            for job in jobs:
                if job.get("conclusion") in ("failure", "timed_out"):
                    failed_job = job
                    steps = job.get("steps", [])
                    for step in steps:
                        if step.get("conclusion") in ("failure", "timed_out"):
                            failed_step = step
                            break
                    if failed_job and not failed_step and steps:
                        failed_step = steps[-1]
                    break

            if not failed_job:
                failed_job = jobs[0] if jobs else {"id": 0, "name": "pipeline"}
                failed_step = {"name": "Pipeline execution", "conclusion": "failure"}

            if not failed_step:
                failed_step = {"name": "Execution", "conclusion": "failure"}

            # 2. Fetch logs for failed job
            job_id = int(failed_job.get("id", 0))
            log_text = ""
            if job_id:
                try:
                    log_text = fetch_job_logs(
                        repo=repo,
                        job_id=job_id,
                        client=self._github_client,
                        config=self.config,
                    )
                except Exception as log_exc:
                    logger.warning("Could not fetch job logs for job %s: %s", job_id, log_exc)
                    log_text = f"Error fetching runner logs: {log_exc}"

            # 3. Triage phase: produce structured InvestigationPlan
            plan = self.triage_agent.plan(
                incident=incident,
                failing_job=failed_job,
                failing_step=failed_step,
                log_text=log_text,
            )
            stage = plan.stage
            incident.github_failing_stage = stage

            # 4. Route to specialist(s) based on triage plan
            target_specialist_name = (
                plan.selected_specialists[0]
                if plan.selected_specialists
                else "pipeline_logs"
            )
            specialist = self.specialists.get(target_specialist_name)
            if specialist is None:
                if stage == "deploy":
                    specialist = self.specialists.get("runtime_health")
                elif stage == "iac_config":
                    specialist = self.specialists.get("iac_config")
                else:
                    specialist = self.specialists.get("pipeline_logs")

            if specialist is None:
                specialist = PipelineLogsSpecialist(config=self.config)

            diag_result = specialist.diagnose(
                incident=incident,
                failing_job=failed_job,
                failing_step=failed_step,
                log_text=log_text,
                client=self._github_client,
                config=self.config,
                repo=repo,
                head_sha=incident.github_head_sha,
                job_id=job_id,
                run_id=incident.github_run_id,
            )

            # 5. Synthesize final Report
            report = self.synthesizer.synthesize(
                incident=incident,
                failed_job=failed_job,
                failed_step=failed_step,
                log_text=log_text,
                diagnostic_result=diag_result,
                plan=plan,
            )

            # 6. Build InvestigationResult
            result = InvestigationResult(
                investigation_id=f"inv-{uuid.uuid4().hex[:12]}",
                incident_id=incident.id,
                incident_source=incident.source,
                incident_external_id=incident.external_id,
                incident_external_key=incident.external_key,
                status="completed",
                report=report,
                incident=incident,
            )
        except Exception as exc:
            logger.exception("Investigation failed for incident %s: %s", incident.id, exc)
            result = InvestigationResult(
                investigation_id=f"inv-{uuid.uuid4().hex[:12]}",
                incident_id=incident.id,
                incident_source=incident.source,
                incident_external_id=incident.external_id,
                incident_external_key=incident.external_key,
                status="failed",
                error=str(exc),
                incident=incident,
            )

        # 7. Deliver callback to bridge
        if request.callback_url:
            try:
                send_callback_report(
                    result=result,
                    callback_url=request.callback_url,
                    client=self._callback_client,
                    config=self.config,
                )
            except Exception as cb_exc:
                logger.error("Failed to deliver callback report: %s", cb_exc)
                raise RuntimeError(f"Callback delivery failed: {cb_exc}") from cb_exc

        return result


# Backwards compatibility alias
InvestigationDispatcher = Dispatcher

__all__ = [
    "Dispatcher",
    "InvestigationDispatcher",
    "classify_stage",
]
