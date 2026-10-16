import asyncio
import logging
import uuid
import warnings
from typing import Any, Dict, Mapping, Optional

from google.adk.agents import BaseAgent, ParallelAgent
from pydantic import ConfigDict, Field

from .config import AgentConfig, get_settings
from .contracts.findings import Findings
from .contracts.request import InvestigationRequest
from .contracts.result import InvestigationResult
from .dependencies import extract_downstream_dependencies, inspect_downstream_dependency
from .llm import GeminiLlmClient
from .onset import detect_onset_from_telemetry
from .review import ReviewEvaluator
from .skills_loader import SkillsLoader
from .specialists.base import BaseSpecialist
from .specialists.changes import ChangesSpecialist
from .specialists.logs import LogsSpecialist
from .specialists.metrics import MetricsSpecialist
from .specialists.resources import ResourcesSpecialist
from .synthesizer import SynthesizerAgent
from .tools.clients import is_local_mode
from .triage import InvestigationPlan, TriageAgent

logger = logging.getLogger(__name__)


class Dispatcher(BaseAgent):
    """Multi-agent dispatcher orchestrating Triage -> Fan-out -> Review -> Synthesis."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    config: AgentConfig = Field(default_factory=get_settings)
    specialists: Dict[str, Any] = Field(default_factory=dict)
    triage_agent: Any = None
    review_evaluator: Any = None
    synthesizer: Any = None
    skills_loader: Any = None
    parallel_agent: Optional[Any] = None
    llm_client: Optional[Any] = None

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        specialists: Optional[Mapping[str, BaseSpecialist]] = None,
        triage_agent: Optional[TriageAgent] = None,
        review_evaluator: Optional[ReviewEvaluator] = None,
        synthesizer: Optional[SynthesizerAgent] = None,
        skills_loader: Optional[SkillsLoader] = None,
        client: Any = None,
        llm_client: Any = None,
        **kwargs: Any,
    ) -> None:
        cfg = config or get_settings()

        # Fail-fast assertion in production mode when no real credentials or clients provided
        if not is_local_mode() and client is None and specialists is None:
            try:
                import google.auth

                google.auth.default()
            except Exception as exc:
                raise RuntimeError(
                    "Dispatcher initialization failed in production: "
                    f"missing GCP credentials ({exc})"
                ) from exc

        loader = skills_loader or SkillsLoader()

        # Default-wire GeminiLlmClient in production
        if llm_client is None and not is_local_mode():
            try:
                llm_client = GeminiLlmClient(config=cfg, skills_loader=loader)
            except Exception as exc:
                logger.warning("Failed to initialize GeminiLlmClient in Dispatcher: %s", exc)
                llm_client = None

        # Initialize specialists
        if specialists is not None:
            specs = dict(specialists)
        else:
            project_id = cfg.gcp_project
            model_spec = cfg.model_specialist
            specs = {
                "logs": LogsSpecialist(
                    project_id=project_id, client=client, model=model_spec, config=cfg
                ),
                "metrics": MetricsSpecialist(
                    project_id=project_id, client=client, model=model_spec, config=cfg
                ),
                "changes": ChangesSpecialist(
                    project_id=project_id, client=client, model=model_spec, config=cfg
                ),
                "resources": ResourcesSpecialist(
                    project_id=project_id, client=client, model=model_spec, config=cfg
                ),
            }

        model_disp = cfg.model_dispatcher
        model_synth = cfg.model_synthesizer
        triage = triage_agent or TriageAgent(
            skills_loader=loader,
            max_query_window_hours=cfg.max_query_window_hours,
            model=model_disp,
            llm_client=llm_client,
            config=cfg,
        )
        review = review_evaluator or ReviewEvaluator(max_rounds=cfg.max_review_rounds)
        synth = synthesizer or SynthesizerAgent(
            skills_loader=loader,
            model=model_synth,
            llm_client=llm_client,
            config=cfg,
        )

        # Wire ADK sub-agents: Triage, Parallel Specialists, and Synthesizer
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

        super().__init__(
            name="dispatcher",
            description="Multi-agent SRE investigation orchestrator",
            sub_agents=sub_agents,
            **kwargs,
        )
        self.config = cfg
        self.specialists = specs
        self.triage_agent = triage
        self.review_evaluator = review
        self.synthesizer = synth
        self.skills_loader = loader
        self.parallel_agent = parallel
        self.llm_client = llm_client
        self.client = client

    async def _run_specialist_safely(
        self,
        name: str,
        specialist: BaseSpecialist,
        plan: InvestigationPlan,
        incident: Any,
    ) -> tuple[str, Findings, Optional[str]]:
        """Run a single specialist asynchronously with strict error containment."""
        try:
            timeout_s = getattr(self.config, "specialist_timeout_seconds", 120.0)
            if hasattr(specialist, "investigate_async"):
                finding = await specialist.investigate_async(
                    incident=incident,
                    start_time=plan.start_time,
                    end_time=plan.end_time,
                    plan=plan,
                    timeout_seconds=timeout_s,
                )
            else:
                finding = await asyncio.to_thread(
                    specialist.investigate,
                    incident=incident,
                    start_time=plan.start_time,
                    end_time=plan.end_time,
                    plan=plan,
                    timeout_seconds=timeout_s,
                )
            return name, finding, None
        except Exception as exc:
            logger.exception("Specialist %s raised unhandled exception", name)
            fallback = Findings(
                summary=f"Specialist {name} failed: {str(exc)}",
                evidence=[],
                anomalies=[],
                queries_run=[],
                confidence=0.0,
                errors=[f"Unhandled error in {name}: {str(exc)}"],
                fallback_used=True,
            )
            return name, fallback, str(exc)

    async def _execute_fan_out(
        self,
        plan: InvestigationPlan,
        incident: Any,
    ) -> tuple[Dict[str, Findings], Dict[str, str]]:
        """Run all selected specialists concurrently."""
        tasks = []
        selected_names = [s for s in plan.selected_specialists if s in self.specialists]

        for name in selected_names:
            specialist = self.specialists[name]
            tasks.append(self._run_specialist_safely(name, specialist, plan, incident))

        if not tasks:
            return {}, {}

        results = await asyncio.gather(*tasks, return_exceptions=True)

        findings: Dict[str, Findings] = {}
        specialist_errors: Dict[str, str] = {}

        for idx, res in enumerate(results):
            if isinstance(res, BaseException):
                name = selected_names[idx]
                err_str = str(res)
                findings[name] = Findings(
                    summary=f"Specialist failed: {err_str}",
                    evidence=[],
                    anomalies=[],
                    queries_run=[],
                    confidence=0.0,
                    errors=[err_str],
                    fallback_used=True,
                )
                specialist_errors[name] = err_str
            else:
                name, finding, err = res
                findings[name] = finding
                if err:
                    specialist_errors[name] = err
                elif finding.errors:
                    specialist_errors[name] = "; ".join(finding.errors)

        return findings, specialist_errors

    async def _execute_review_step(
        self,
        plan: InvestigationPlan,
        incident: Any,
        findings: Dict[str, Findings],
        specialist_errors: Dict[str, str],
    ) -> None:
        """Evaluate findings and execute <= 1 round of targeted follow-up."""
        action = self.review_evaluator.evaluate(incident, plan, findings)
        if action is not None and action.specialist in self.specialists:
            target_specialist = self.specialists[action.specialist]
            try:
                follow_up_finding = await asyncio.to_thread(
                    self.review_evaluator.execute_follow_up,
                    action=action,
                    specialist=target_specialist,
                    incident=incident,
                )
                if action.specialist in findings:
                    findings[action.specialist] = self.review_evaluator.merge_findings(
                        findings[action.specialist], follow_up_finding
                    )
                else:
                    findings[action.specialist] = follow_up_finding

                # Clear previous error if follow-up succeeded cleanly
                if action.specialist in specialist_errors and not follow_up_finding.errors:
                    del specialist_errors[action.specialist]
            except Exception as exc:
                logger.warning("Follow-up execution failed for %s: %s", action.specialist, exc)
                specialist_errors[action.specialist] = f"Follow-up failed: {str(exc)}"

    async def _investigate_lifecycle(
        self,
        request: InvestigationRequest,
        inv_id: str,
    ) -> InvestigationResult:
        """Internal execution lifecycle of the investigation."""
        incident = request.incident

        # 1. Triage: formulate InvestigationPlan
        plan = self.triage_agent.plan(incident)

        if not incident.service_hint or incident.service_hint == "unknown-service":
            if plan.affected_service and plan.affected_service != "unknown-service":
                incident.service_hint = plan.affected_service

        # Onset Detection & Window Anchoring (Item 8.3)
        if plan.affected_service and plan.affected_service != "unknown-service":
            try:
                onset_res = await asyncio.to_thread(
                    detect_onset_from_telemetry,
                    service_name=plan.affected_service,
                    end_time=plan.end_time,
                    window_minutes=self.config.onset_window_minutes,
                    pre_buffer_minutes=self.config.onset_pre_buffer_minutes,
                    project_id=self.config.gcp_project,
                    logging_client=self.client,
                    metrics_client=self.client,
                    config=self.config,
                    prior_quiescent_minutes=self.config.prior_incident_quiescent_minutes,
                )
                if onset_res and onset_res.onset_time:
                    plan.onset_time = onset_res.onset_time
                    plan.start_time = onset_res.anchored_start_time
                    if onset_res.prior_incidents:
                        setattr(plan, "prior_incidents", onset_res.prior_incidents)
            except Exception as exc:
                logger.warning("Onset detection failed; continuing with triage window: %s", exc)

        # 2. Parallel Fan-out: run selected specialists concurrently
        findings, specialist_errors = await self._execute_fan_out(plan, incident)

        # 3. Review: evaluate findings & execute <= 1 follow-up
        await self._execute_review_step(plan, incident, findings, specialist_errors)

        # 4. Dependency Traversal & Downstream Attribution (Item 8.3)
        if plan.affected_service and plan.affected_service != "unknown-service":
            downstream_services = extract_downstream_dependencies(
                primary_service=plan.affected_service,
                findings=findings,
                max_services=self.config.dependency_max_services,
            )

            async def _inspect_dep(dep_name: str) -> tuple[str, Optional[Findings], Optional[str]]:
                try:
                    res = await asyncio.to_thread(
                        inspect_downstream_dependency,
                        service_name=dep_name,
                        start_time=plan.start_time,
                        end_time=plan.end_time,
                        project_id=self.config.gcp_project,
                        client=self.client,
                        config=self.config,
                        max_depth=self.config.dependency_fanout_max_depth,
                    )
                    err_msg = "; ".join(res.errors) if res.errors else None
                    return dep_name, res, err_msg
                except Exception as exc:
                    logger.warning(
                        "Downstream dependency inspection failed for %s: %s",
                        dep_name,
                        exc,
                    )
                    return dep_name, None, str(exc)

            if downstream_services:
                dep_results = await asyncio.gather(
                    *[_inspect_dep(s) for s in downstream_services],
                    return_exceptions=False,
                )
                for dep_svc, dep_f, dep_err in dep_results:
                    if dep_f is not None:
                        findings[f"dependency:{dep_svc}"] = dep_f
                    if dep_err:
                        specialist_errors[f"dependency:{dep_svc}"] = dep_err

        # 5. Synthesis: produce Report with actually loaded and consulted skills
        loaded_skills = list(self.skills_loader.loaded_skills) if self.skills_loader else []
        report = self.synthesizer.synthesize(
            incident=incident,
            plan=plan,
            findings=findings,
            specialist_errors=specialist_errors,
            skills_used=loaded_skills,
        )

        return InvestigationResult(
            investigation_id=inv_id,
            incident_id=incident.id,
            incident_source=incident.source,
            incident_external_id=incident.external_id,
            incident_external_key=incident.external_key,
            status="completed",
            report=report,
        )

    async def investigate(
        self,
        request: InvestigationRequest,
        investigation_id: Optional[str] = None,
    ) -> InvestigationResult:
        """Run full investigation workflow bounded by configured timeout."""
        inv_id = investigation_id or f"inv-{uuid.uuid4().hex[:8]}"
        timeout = getattr(self.config, "effective_timeout_seconds", self.config.timeout_seconds)

        try:
            return await asyncio.wait_for(
                self._investigate_lifecycle(request, inv_id),
                timeout=float(timeout),
            )
        except asyncio.TimeoutError:
            logger.error("Investigation %s timed out after %s seconds", inv_id, timeout)
            return InvestigationResult(
                investigation_id=inv_id,
                incident_id=request.incident.id,
                incident_source=request.incident.source,
                incident_external_id=request.incident.external_id,
                incident_external_key=request.incident.external_key,
                status="failed",
                error=f"Investigation timed out after {timeout} seconds",
            )
        except Exception as exc:
            logger.exception("Investigation %s failed with unhandled error", inv_id)
            return InvestigationResult(
                investigation_id=inv_id,
                incident_id=request.incident.id,
                incident_source=request.incident.source,
                incident_external_id=request.incident.external_id,
                incident_external_key=request.incident.external_key,
                status="failed",
                error=f"Investigation failed: {str(exc)}",
            )

    # Alias for convenience
    run_investigation = investigate
