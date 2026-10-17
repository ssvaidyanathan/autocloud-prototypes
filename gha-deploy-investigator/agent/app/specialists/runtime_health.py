import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import AgentConfig
from ..contracts.findings import EvidenceItem
from ..contracts.incident import Incident
from ..specialists.base import BaseSpecialist, DiagnosticResult
from ..tools.runtime_tools import describe_cloud_run_revision, query_cloud_logging

logger = logging.getLogger(__name__)

SERVICE_PATTERN = re.compile(
    r"(?:deploying.*service\s+\[(?P<srv1>[a-zA-Z0-9_\-]+)\]|--service(?:=|\s+)(?P<srv2>[a-zA-Z0-9_\-]+)|Service\s+name:\s*(?P<srv3>[a-zA-Z0-9_\-]+))",
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

    def __init__(self) -> None:
        super().__init__(name="runtime_health_specialist", stage="deploy")

    def diagnose(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "Deploy Step")
        now = datetime.now(timezone.utc)
        evidence_items: list[EvidenceItem] = []

        # 1. Extract deployment target coordinates
        service_name = self._extract_service_name(incident, log_text)
        revision_name = self._extract_revision_name(service_name, log_text)
        region = self._extract_region(log_text)

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

        # Check for IAM / permission errors first
        is_iam_denied = any(
            token in log_text.lower()
            for token in (
                "iam.serviceaccounts.actas",
                "permission_denied",
                "permission denied",
                "caller does not have required permission",
                "does not have permission",
            )
        )
        if is_iam_denied:
            iam_lines = [
                line.strip()
                for line in log_text.splitlines()
                if any(
                    t in line.lower()
                    for t in (
                        "iam.serviceaccounts.actas",
                        "permission_denied",
                        "permission denied",
                        "caller does not have required permission",
                        "does not have permission",
                    )
                )
            ]
            culprit = iam_lines[0] if iam_lines else "IAM permission denied on service account"
            if "iam.serviceaccounts.actas" in culprit.lower():
                probable_cause = (
                    f"Deployment step '{step_name}' failed due to missing IAM permission "
                    f"'iam.serviceaccounts.actAs' on Cloud Run runtime service account: {culprit}"
                )
            else:
                probable_cause = (
                    f"Deployment step '{step_name}' failed due to IAM permission denial: {culprit}"
                )
            confidence = 0.95
            remediation = [
                (
                    "Grant 'roles/iam.serviceAccountUser' (actAs) to the deploying identity "
                    "on the Cloud Run runtime service account"
                ),
                (
                    "Ensure deployment service account has 'roles/run.admin' or "
                    "'roles/run.developer' on project or target Cloud Run service"
                ),
                "Verify GitHub Actions Workload Identity Federation role bindings "
                "and impersonation",
            ]
            evidence_items.append(
                EvidenceItem(
                    kind="github_runner_log",
                    excerpt=f"IAM permission denial: {culprit}",
                    timestamp=now,
                )
            )
            rev_summary = f" (revision '{revision_name}')" if revision_name else ""
            summary = (
                f"Deployment failure in step '{step_name}' for Cloud Run service '{service_name}'"
                f"{rev_summary}. Root cause: {probable_cause}"
            )
            return DiagnosticResult(
                stage="deploy",
                specialist_name=self.name,
                summary=summary,
                probable_cause=probable_cause,
                confidence=confidence,
                evidence_items=evidence_items,
                remediation_actions=remediation,
                suggested_fix=f"Grant required IAM permissions for {service_name}",
            )

        # 2. Query Cloud Run Revision conditions
        rev_info: dict[str, Any] = {}
        if revision_name:
            try:
                rev_info = describe_cloud_run_revision(
                    service_name=service_name,
                    revision_name=revision_name,
                    region=region,
                    config=config,
                )
            except Exception as exc:
                logger.warning("Failed to describe Cloud Run revision: %s", exc)

        # 3. Query Cloud Logging for container stderr
        log_entries: list[dict[str, Any]] = []
        if revision_name:
            try:
                log_entries = query_cloud_logging(
                    filter_query=(
                        f'resource.type="cloud_run_revision" revision_name="{revision_name}"'
                    ),
                    limit=20,
                    config=config,
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
            probable_cause = (
                f"Cloud Run container in revision '{revision_name}' terminated due to "
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
            evidence_items.append(
                EvidenceItem(
                    kind="cloud_run_telemetry",
                    excerpt=(
                        f"Cloud Run revision '{revision_name}' container terminated with exit "
                        f"code 137 (OOM).\n"
                        f"Reason: {failure_reason or 'Container exceeded memory limit'}."
                    ),
                    timestamp=now,
                )
            )
        elif is_probe_timeout:
            probable_cause = (
                f"Cloud Run container in revision '{revision_name}' failed startup readiness "
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
            evidence_items.append(
                EvidenceItem(
                    kind="cloud_run_telemetry",
                    excerpt=(
                        f"Cloud Run revision '{revision_name}' failed startup readiness probe: "
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
            probable_cause = (
                f"Deployment step '{step_name}' failed during rollout{rev_label} "
                f"for service '{service_name}'."
            )
            confidence = 0.75
            rev_cmd = (
                f"gcloud run revisions describe {revision_name} --region {region}"
                if revision_name
                else f"gcloud run revisions list --service {service_name} --region {region}"
            )
            remediation = [
                f"Check Cloud Run service revision health: {rev_cmd}",
                "Review deployment pipeline IAM permissions and Cloud Run deployment configuration",
            ]

        rev_summary = f" (revision '{revision_name}')" if revision_name else ""
        summary = (
            f"Deployment failure in step '{step_name}' for Cloud Run service '{service_name}'"
            f"{rev_summary}. Root cause: {probable_cause}"
        )

        return DiagnosticResult(
            stage="deploy",
            specialist_name=self.name,
            summary=summary,
            probable_cause=probable_cause,
            confidence=confidence,
            evidence_items=evidence_items,
            remediation_actions=remediation,
            suggested_fix=f"Resolve deployment health failure for {service_name}",
        )

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
