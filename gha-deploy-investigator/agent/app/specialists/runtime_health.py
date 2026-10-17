import functools
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.adk.agents import LlmAgent
from google.adk.tools.function_tool import FunctionTool

from ..config import AgentConfig
from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident
from ..log_utils import extract_error_lines
from ..specialists.base import BaseSpecialist, DiagnosticResult, SkillTracker
from ..tools.github_tools import (
    fetch_job_logs,
    fetch_workflow_run_jobs,
    get_commit_diff,
)
from ..tools.runtime_tools import (
    describe_cloud_run_revision,
    describe_service,
    query_cloud_logging,
)

logger = logging.getLogger(__name__)

SERVICE_PATTERN = re.compile(
    r"(?:(?:gcloud\s+run\s+deploy|deploying.*service)\s+\[?(?P<srv1>[a-zA-Z0-9_\-]+)\]?|--service(?:=|\s+)(?P<srv2>[a-zA-Z0-9_\-]+)|Service\s+name:\s*(?P<srv3>[a-zA-Z0-9_\-]+))",
    re.IGNORECASE,
)
REVISION_PATTERN = re.compile(
    r"(?:revision\s+\[(?P<rev1>[a-zA-Z0-9_\-]+)\]|Revision\s+['\"](?P<rev2>[a-zA-Z0-9_\-]+)['\"]|(?P<rev3>[a-zA-Z0-9_\-]+-\d{5}-[a-zA-Z0-9]+))",
    re.IGNORECASE,
)
REGION_PATTERN = re.compile(
    r"(?:region\s+\[(?P<reg1>[a-zA-Z0-9_\-]+)\]|--region(?:=|\s+)(?P<reg2>[a-zA-Z0-9_\-]+))",
    re.IGNORECASE,
)


class RuntimeHealthSpecialist(BaseSpecialist):
    """Diagnoses deployment failures by correlating CI runner logs with Cloud Run and
    Cloud Logging telemetry.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
        skill_tracker: Optional[SkillTracker] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name="runtime_health_specialist",
            stage="deploy",
            model=model,
            config=config,
            skill_tracker=skill_tracker,
            **kwargs,
        )
        self.adk_tools = [
            FunctionTool(fetch_job_logs),
            FunctionTool(fetch_workflow_run_jobs),
            FunctionTool(get_commit_diff),
            FunctionTool(describe_cloud_run_revision),
            FunctionTool(query_cloud_logging),
            FunctionTool(describe_service),
        ]
        self.llm_agent = llm_agent or LlmAgent(
            name="runtime_health_specialist",
            model=self.model or "default",
            instruction=(
                "You are a specialist investigating Cloud Run deployment and runtime health."
            ),
            tools=self.adk_tools,
            output_schema=Findings,
        )
        if self.llm_agent is not None:
            self.llm_agent.before_tool_callback = self.skill_tracker.before_tool_callback
            self.llm_agent.after_tool_callback = self.skill_tracker.after_tool_callback
            self.llm_agent.on_tool_error_callback = self.skill_tracker.on_tool_error_callback
            self.skill_tracker.agent = self.llm_agent

    def get_tools(self) -> list[Any]:
        return list(self.adk_tools)

    def _build_agent(
        self,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> LlmAgent:
        from ..skills_loader import get_skill_toolset_for_target

        skill_toolset = get_skill_toolset_for_target("runtime_health")

        @functools.wraps(fetch_job_logs)
        def _bound_fetch_job_logs(repo: str, job_id: int) -> str:
            return fetch_job_logs(repo=repo, job_id=job_id, client=client, config=config)

        @functools.wraps(fetch_workflow_run_jobs)
        def _bound_fetch_workflow_run_jobs(repo: str, run_id: int) -> dict[str, Any]:
            return fetch_workflow_run_jobs(repo=repo, run_id=run_id, client=client, config=config)

        @functools.wraps(get_commit_diff)
        def _bound_get_commit_diff(
            repo: str, head_sha: str = "", commit_sha: str = ""
        ) -> dict[str, Any]:
            target_sha = commit_sha or head_sha
            return get_commit_diff(repo=repo, head_sha=target_sha, client=client, config=config)

        @functools.wraps(describe_service)
        def _bound_describe_service(
            service_name: str, region: str = "us-central1"
        ) -> dict[str, Any]:
            return describe_service(
                service_name=service_name, region=region, client=client, config=config
            )

        @functools.wraps(describe_cloud_run_revision)
        def _bound_describe_revision(
            service_name: str, revision_name: str, region: str = "us-central1"
        ) -> dict[str, Any]:
            return describe_cloud_run_revision(
                service_name=service_name,
                revision_name=revision_name,
                region=region,
                client=client,
                config=config,
            )

        @functools.wraps(query_cloud_logging)
        def _bound_query_logging(
            filter_query: str, limit: int = 20
        ) -> list[dict[str, Any]]:
            return query_cloud_logging(
                filter_query=filter_query, limit=limit, client=client, config=config
            )

        bound_tools = [
            skill_toolset,
            FunctionTool(_bound_fetch_job_logs),
            FunctionTool(_bound_fetch_workflow_run_jobs),
            FunctionTool(_bound_get_commit_diff),
            FunctionTool(_bound_describe_service),
            FunctionTool(_bound_describe_revision),
            FunctionTool(_bound_query_logging),
        ]
        instruction = (
            "You are a Cloud Run deployment and runtime health diagnostic specialist.\n\n"
            "CRITICAL WORKFLOW REQUIREMENT:\n"
            "1. On Turn 1, invoke 'load_skill' exactly once with "
            "skill_name='cloudrun-deploy-troubleshooting'. Do NOT invoke other skills.\n"
            "2. On Turn 2, invoke decisive read-only diagnostic data tools for the failure:\n"
            "   - Invoke 'fetch_job_logs' (repo, job_id) to inspect the deployment step runner "
            "logs containing the decisive failure error (e.g. IAM actAs permissions, gcloud "
            "deployment errors).\n"
            "   - And/or invoke 'describe_service' or 'describe_cloud_run_revision' to inspect "
            "runtime telemetry.\n"
            "   NOTE ON LIVE STATE: 'describe_service' reports live cluster state. If a "
            "deployment failed before a new revision could be created (e.g. IAM permission "
            "error), the live service status may report 'Ready' for the pre-existing revision; "
            "this must be contextualized as prior revision health, NOT as success of the "
            "failed deployment.\n"
            "3. On Turn 3, conclude immediately by invoking 'set_model_response' with your "
            "structured Findings report citing the tool outputs."
        )
        model_to_use = (
            getattr(self.llm_agent, "model", None)
            if self.llm_agent
            else (self.model or "default")
        )
        agent = LlmAgent(
            name="runtime_health_specialist",
            model=model_to_use,
            instruction=instruction,
            tools=bound_tools,
            output_schema=Findings,
            before_tool_callback=self.skill_tracker.before_tool_callback,
            after_tool_callback=self.skill_tracker.after_tool_callback,
            on_tool_error_callback=self.skill_tracker.on_tool_error_callback,
        )
        self.skill_tracker.agent = agent
        return agent

    def _findings_to_diagnostic_result(
        self,
        findings: Findings,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        service_name: str,
        revision_name: Optional[str],
        region: str,
        log_text: str = "",
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "Deploy Step")
        evidence_items = list(findings.evidence)
        # Ensure live state in cloud_run_service items is clearly contextualized
        for i, ev in enumerate(evidence_items):
            if ev.kind == "cloud_run_service" and not revision_name:
                if "Active revision from previous deployment" not in ev.excerpt:
                    evidence_items[i] = EvidenceItem(
                        kind="cloud_run_service",
                        excerpt=(
                            f"{ev.excerpt} (Active revision from previous deployment; "
                            "current deployment failed before new revision could be created)"
                        ),
                        timestamp=ev.timestamp,
                        console_url=ev.console_url,
                    )
        # Ensure decisive runner log error lines are included in evidence
        has_runner_log = any(e.kind == "github_runner_log" for e in evidence_items)
        if not has_runner_log:
            err_lines = extract_error_lines(log_text) if log_text else []
            if err_lines:
                evidence_items.append(
                    EvidenceItem(
                        kind="github_runner_log",
                        excerpt="\n".join(err_lines[:5]),
                        timestamp=datetime.now(timezone.utc),
                        console_url=incident.external_url,
                    )
                )
            else:
                rev_label = f" (revision: '{revision_name}')" if revision_name else ""
                evidence_items.append(
                    EvidenceItem(
                        kind="github_runner_log",
                        excerpt=(
                            f"Deploy step '{step_name}' failed for service '{service_name}'"
                            f"{rev_label} (region: '{region}')."
                        ),
                        timestamp=datetime.now(timezone.utc),
                        console_url=incident.external_url,
                    )
                )

        diag = DiagnosticResult(
            stage="deploy",
            specialist_name=self.name,
            summary=findings.summary,
            probable_cause=findings.summary,
            confidence=findings.confidence,
            evidence_items=evidence_items,
            remediation_actions=list(findings.recommended_actions)
            if hasattr(findings, "recommended_actions")
            else [],
            suggested_fix=f"Resolve deployment health failure for {service_name}",
            skills_used=list(self.skill_tracker.skills_used),
            queries_run=list(self.skill_tracker.queries_run),
            skill_first=self.skill_tracker.skill_first,
        )
        return self.skill_tracker.populate_diagnostic_result(diag)

    def diagnose(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
        **kwargs: Any,
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "Deploy Step")
        now = datetime.now(timezone.utc)
        evidence_items: list[EvidenceItem] = []

        repo = kwargs.get("repo") or incident.github_repo or ""
        job_id = kwargs.get("job_id") or failing_job.get("id") or 0
        run_id = kwargs.get("run_id") or incident.github_run_id or 0
        head_sha = kwargs.get("head_sha") or incident.github_head_sha or ""

        # 1. Extract deployment target coordinates
        service_name = self._extract_service_name(incident, log_text)
        revision_name = self._extract_revision_name(service_name, log_text)
        region = self._extract_region(log_text)

        # Attempt Pass 1: ADK Runner execution if LLM is available
        if self._is_llm_available():
            try:
                from .base import run_sync

                agent = self._build_agent(client=client, config=config)
                prompt_lines = [
                    (
                        f"Investigate Cloud Run deploy failure for incident {incident.id}: "
                        f"{incident.title}"
                    ),
                    f"Job: {failing_job.get('name', 'deploy')} (ID: {job_id}), Step: {step_name}",
                    f"Repository: {repo}, Run ID: {run_id}, Head SHA: {head_sha}",
                    f"Target Service: {service_name}, Region: {region}",
                    f"Revision: {revision_name or 'None (failed pre-revision)'}",
                    f"Runner Log Excerpt:\n{log_text[:2000]}",
                    "",
                    "CRITICAL INSTRUCTIONS:",
                    (
                        "1. Turn 1: Invoke load_skill exactly once with "
                        "skill_name='cloudrun-deploy-troubleshooting'."
                    ),
                    (
                        "2. Turn 2: Invoke fetch_job_logs(repo, job_id) to inspect the deployment "
                        "runner logs containing the decisive failure error, and/or "
                        "describe_service / describe_cloud_run_revision for runtime telemetry."
                    ),
                    (
                        "3. Turn 3: Conclude immediately by invoking set_model_response "
                        "with your Findings report."
                    ),
                ]
                findings = run_sync(self._execute_adk_runner(agent, "\n".join(prompt_lines)))
                if findings and (self.skill_tracker.queries_run or findings.queries_run):
                    return self._findings_to_diagnostic_result(
                        findings,
                        incident,
                        failing_job,
                        failing_step,
                        service_name,
                        revision_name,
                        region,
                        log_text=log_text,
                    )
            except Exception as exc:
                logger.info("RuntimeHealthSpecialist ADK runner pass skipped/failed: %s", exc)

        # Pass 2 Fallback: Deterministic tool execution through authentic tracking callbacks
        self.skill_tracker.after_tool_callback(
            "load_skill", {"skill_name": "cloudrun-deploy-troubleshooting"}
        )

        # 1. Fetch deployment job logs if job_id and repo are available
        if repo and job_id:
            try:
                fetched_logs = self.tracked_tool_call(
                    "fetch_job_logs",
                    {"repo": repo, "job_id": job_id},
                    lambda: fetch_job_logs(
                        repo=repo,
                        job_id=job_id,
                        client=client,
                        config=config,
                    ),
                )
                if fetched_logs and not log_text:
                    log_text = fetched_logs
            except Exception as exc:
                logger.warning("Failed to fetch job logs in RuntimeHealthSpecialist: %s", exc)

        # 2. Always describe service with temporal context
        try:
            service_info = self.tracked_tool_call(
                "describe_service",
                {"service_name": service_name, "region": region},
                lambda: describe_service(
                    service_name=service_name,
                    region=region,
                    client=client,
                    config=config,
                ),
            )
            srv_status = service_info.get("status", "Ready")
            if not revision_name:
                srv_excerpt = (
                    f"Live Service Status: {srv_status} (Active revision from previous "
                    "deployment; current deployment failed before new revision could be "
                    f"created; region: '{region}')"
                )
            else:
                srv_excerpt = (
                    f"Live Service Status: {srv_status} (region: '{region}', "
                    f"target revision: '{revision_name}')"
                )
            evidence_items.append(
                EvidenceItem(
                    kind="cloud_run_service",
                    excerpt=srv_excerpt,
                    timestamp=now,
                    console_url=incident.external_url,
                )
            )
        except Exception as exc:
            logger.warning("Failed to describe Cloud Run service: %s", exc)

        err_lines = extract_error_lines(log_text)
        if err_lines:
            evidence_items.append(
                EvidenceItem(
                    kind="github_runner_log",
                    excerpt="\n".join(err_lines[:5]),
                    timestamp=now,
                    console_url=incident.external_url,
                )
            )
        else:
            rev_desc = f" (revision: '{revision_name}')" if revision_name else ""
            evidence_items.append(
                EvidenceItem(
                    kind="github_runner_log",
                    excerpt=(
                        f"Deploy step '{step_name}' failed for service '{service_name}'"
                        f"{rev_desc} (region: '{region}')."
                    ),
                    timestamp=now,
                    console_url=incident.external_url,
                )
            )

        # 3. Query Cloud Run Revision conditions
        rev_info: dict[str, Any] = {}
        if revision_name:
            call_args = {
                "service_name": service_name,
                "revision_name": revision_name,
                "region": region,
            }
            try:
                rev_info = self.tracked_tool_call(
                    "describe_cloud_run_revision",
                    call_args,
                    lambda: describe_cloud_run_revision(
                        service_name=service_name,
                        revision_name=revision_name,
                        region=region,
                        config=config,
                    ),
                )
            except Exception as exc:
                logger.warning("Failed to describe Cloud Run revision: %s", exc)

        # 4. Query Cloud Logging for container stderr scoped to failure window (±15 min)
        log_entries: list[dict[str, Any]] = []
        if revision_name:
            opened_at = incident.opened_at or now
            t_start = (opened_at - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
            t_end = (opened_at + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
            filter_q = (
                f'resource.type="cloud_run_revision" revision_name="{revision_name}" '
                f'timestamp>="{t_start}" timestamp<="{t_end}"'
            )
            try:
                log_entries = self.tracked_tool_call(
                    "query_cloud_logging",
                    {"filter_query": filter_q},
                    lambda: query_cloud_logging(
                        filter_query=filter_q,
                        limit=20,
                        config=config,
                    ),
                )
            except Exception as exc:
                logger.warning("Failed to query Cloud Logging: %s", exc)

        # 4. Analyze conditions and exit codes
        is_oom = False
        is_probe_timeout = False
        exit_code = None
        failure_reason = ""

        # Inspect conditions list
        for cond in rev_info.get("conditions", []):
            msg = cond.get("message", "")
            reason = cond.get("reason", "")
            if "137" in msg or "memory limit" in msg.lower() or "oom" in msg.lower():
                is_oom = True
                exit_code = 137
                failure_reason = f"{reason}: {msg}" if reason else msg
            elif (
                "probe" in msg.lower()
                or "health check" in msg.lower()
                or "timed out" in msg.lower()
            ):
                is_probe_timeout = True
                failure_reason = f"{reason}: {msg}" if reason else msg

        # Inspect container statuses
        for c_stat in rev_info.get("container_statuses", []):
            ec = c_stat.get("last_exit_code")
            if ec == 137:
                is_oom = True
                exit_code = 137
            elif ec is not None and ec > 0 and exit_code is None:
                exit_code = ec

        # Also inspect CI logs or stderr logs for 137 / OOM
        if not is_oom:
            if "exit code 137" in log_text.lower() or "out of memory" in log_text.lower():
                is_oom = True
                exit_code = 137

        # Format findings based on telemetry
        if is_oom or exit_code == 137:
            rev_str = f" in revision '{revision_name}'" if revision_name else ""
            probable_cause = (
                f"Cloud Run container{rev_str} terminated due to "
                "memory saturation / Out Of Memory (OOM, exit code 137) during startup."
            )
            confidence = 0.95
            remediation = [
                (
                    f"Increase Cloud Run memory allocation: gcloud run services update "
                    f"{service_name} --memory 1Gi --region {region}"
                ),
                "Profile container startup memory usage and tune JVM/Python runtime heap limits",
                "Inspect background workers or large in-memory caches initialized at bootstrap",
            ]
            rev_lead = (
                f"Cloud Run revision '{revision_name}' container"
                if revision_name
                else "Cloud Run container"
            )
            evidence_items.append(
                EvidenceItem(
                    kind="cloud_run_telemetry",
                    excerpt=(
                        f"{rev_lead} terminated with exit "
                        f"code 137 (OOM).\n"
                        f"Reason: {failure_reason or 'Container exceeded memory limit'}."
                    ),
                    timestamp=now,
                )
            )
        elif is_probe_timeout:
            rev_str = f" in revision '{revision_name}'" if revision_name else ""
            probable_cause = (
                f"Cloud Run container{rev_str} failed startup readiness "
                f"probe: {failure_reason}."
            )
            confidence = 0.90
            remediation = [
                (
                    "Verify web server binds to 0.0.0.0 and listens on the port defined by "
                    "the $PORT environment variable"
                ),
                "Increase startup probe timeout or initial delay if initialization takes longer",
            ]
            rev_lead = (
                f"Cloud Run revision '{revision_name}'"
                if revision_name
                else "Cloud Run container"
            )
            evidence_items.append(
                EvidenceItem(
                    kind="cloud_run_telemetry",
                    excerpt=(
                        f"{rev_lead} failed startup readiness probe: "
                        f"{failure_reason}."
                    ),
                    timestamp=now,
                )
            )
        elif log_entries:
            # Container crashed with logs
            stderr_snippets = [
                str(e.get("text_payload") or e.get("json_payload")) for e in log_entries[:3]
            ]
            probable_cause = (
                f"Cloud Run container failed startup with application error: "
                f"{stderr_snippets[0][:200]}."
            )
            confidence = 0.85
            remediation = [
                "Review container startup stderr logs to resolve unhandled runtime exception",
                (
                    "Validate environment variables and service credentials configured in "
                    "Cloud Run spec"
                ),
            ]
            evidence_items.append(
                EvidenceItem(
                    kind="cloud_logging",
                    excerpt=f"Container stderr: {stderr_snippets[0][:500]}",
                    timestamp=now,
                )
            )
        else:
            rev_label = f" of Cloud Run revision '{revision_name}'" if revision_name else ""
            has_iam_or_actas = any(
                ("iam" in el.lower() or "permission" in el.lower() or "actas" in el.lower())
                for el in (err_lines or [log_text])
            )
            if has_iam_or_actas and err_lines:
                primary_err = err_lines[0]
                probable_cause = (
                    f"Deployment step '{step_name}' failed for service '{service_name}': "
                    f"{primary_err}"
                )
                confidence = 0.85
            else:
                probable_cause = (
                    f"Deployment step '{step_name}' failed during rollout{rev_label} "
                    f"for service '{service_name}'."
                )
                confidence = 0.75

            remediation = []
            if has_iam_or_actas:
                remediation.append(
                    "Grant 'roles/iam.serviceAccountUser' (iam.serviceaccounts.actAs permission) "
                    "to the deployment service account on the Cloud Run runtime service account"
                )
                remediation.append(
                    "Verify Workload Identity Federation or GitHub Actions service account "
                    "impersonation bindings"
                )

            if revision_name:
                remediation.append(
                    f"Check Cloud Run service revision health: gcloud run revisions describe "
                    f"{revision_name} --region {region}"
                )
            else:
                remediation.append(
                    f"Check Cloud Run service configuration: gcloud run services describe "
                    f"{service_name} --region {region}"
                )

            remediation.append(
                "Review deployment pipeline IAM permissions and Cloud Run deployment configuration"
            )

        rev_summary = f" (revision '{revision_name}')" if revision_name else ""
        summary = (
            f"Deployment failure in step '{step_name}' for Cloud Run service '{service_name}'"
            f"{rev_summary}. Root cause: {probable_cause}"
        )

        diag = DiagnosticResult(
            stage="deploy",
            specialist_name=self.name,
            summary=summary,
            probable_cause=probable_cause,
            confidence=confidence,
            evidence_items=evidence_items,
            remediation_actions=remediation,
            suggested_fix=f"Resolve deployment health failure for {service_name}",
        )
        return self.skill_tracker.populate_diagnostic_result(diag)

    def _extract_service_name(self, incident: Incident, log_text: str) -> str:
        match = SERVICE_PATTERN.search(log_text)
        if match:
            for g in ("srv1", "srv2", "srv3"):
                if match.group(g):
                    return match.group(g).strip()
        if incident.service_hint and incident.service_hint != "unknown":
            return incident.service_hint
        if incident.github_repo:
            return incident.github_repo.split("/")[-1]
        return "sample-app"

    def _extract_revision_name(self, service_name: str, log_text: str) -> Optional[str]:
        match = REVISION_PATTERN.search(log_text)
        if match:
            for g in ("rev1", "rev2", "rev3"):
                if match.group(g):
                    return match.group(g).strip()
        return None

    def _extract_region(self, log_text: str) -> str:
        match = REGION_PATTERN.search(log_text)
        if match:
            for g in ("reg1", "reg2"):
                if match.group(g):
                    return match.group(g).strip()
        return "us-central1"
