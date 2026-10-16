import logging
import uuid
from typing import Any, Optional

import httpx

from .callback import send_callback_report
from .config import AgentConfig, get_settings
from .contracts.request import InvestigationRequest
from .contracts.result import InvestigationResult
from .synthesizer import synthesize_report
from .tools.github_tools import fetch_job_logs, fetch_workflow_run_jobs

logger = logging.getLogger(__name__)


class InvestigationDispatcher:
    """Orchestrates investigation of GitHub Actions pipeline failures."""

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        github_client: Optional[httpx.Client] = None,
        callback_client: Optional[httpx.Client] = None,
    ) -> None:
        self.config = config or get_settings()
        self._github_client = github_client
        self._callback_client = callback_client

    def investigate(self, request: InvestigationRequest) -> InvestigationResult:
        """Run full investigation workflow on GitHub Actions incident."""
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
                # If no job had failure conclusion, take the first job or default
                failed_job = jobs[0] if jobs else {"id": 0, "name": "pipeline"}
                failed_step = {"name": "Pipeline execution", "conclusion": "failure"}

            if not failed_step:
                failed_step = {"name": "Execution", "conclusion": "failure"}

            # 2. Update failing stage on incident
            stage_name = failed_step.get("name", "Execution")
            incident.github_failing_stage = stage_name

            # 3. Fetch logs for failed job
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

            # 4. Synthesize diagnostic report
            report = synthesize_report(
                incident=incident,
                failed_job=failed_job,
                failed_step=failed_step,
                log_text=log_text,
            )

            # 5. Build InvestigationResult
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

            # 6. Deliver callback to bridge
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

            return result

        except Exception as exc:
            logger.exception("Investigation failed for incident %s: %s", incident.id, exc)
            failed_result = InvestigationResult(
                investigation_id=f"inv-{uuid.uuid4().hex[:12]}",
                incident_id=incident.id,
                incident_source=incident.source,
                incident_external_id=incident.external_id,
                incident_external_key=incident.external_key,
                status="failed",
                error=str(exc),
                incident=incident,
            )
            if request.callback_url:
                try:
                    send_callback_report(
                        result=failed_result,
                        callback_url=request.callback_url,
                        client=self._callback_client,
                        config=self.config,
                    )
                except Exception:
                    pass
            return failed_result


__all__ = [
    "InvestigationDispatcher",
]
