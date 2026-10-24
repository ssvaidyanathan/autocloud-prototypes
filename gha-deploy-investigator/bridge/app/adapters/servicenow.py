import hmac
import logging
import time
from typing import Any, Callable, Optional

import httpx
from fastapi import Request

from ..config import BridgeConfig, get_settings
from ..contracts.incident import Incident
from ..contracts.report import PullRequestRef, Report
from ..templates.renderer import (
    chunk_servicenow_work_note,
    render_servicenow_html,
    render_standalone_html,
)
from .base import BaseAdapter, IncidentRef, parse_utc_datetime

logger = logging.getLogger(__name__)


class _ReusableClientWrapper:
    """Wrapper that prevents closing an externally supplied client in a context manager."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __enter__(self) -> httpx.Client:
        return self._client

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def build_servicenow_description(
    incident: Incident, report: Optional[Report] = None
) -> str:
    """Build a facts-only summary description for ServiceNow incidents.

    Contains facts only: repository, branch, short commit SHA, GitHub actor,
    failing stage, and workflow run URL (if available).
    No root cause, remediation, unrelated observations, or pointer lines;
    all diagnostic findings live exclusively in Work Notes and the attachment.
    """
    repo = incident.github_repo or incident.service_hint or "N/A"
    branch = incident.github_branch or "N/A"
    short_sha = incident.github_head_sha[:8] if incident.github_head_sha else "N/A"
    actor = incident.github_actor or incident.reporter or "N/A"

    lines = [
        f"Repository: {repo}",
        f"Branch: {branch}",
        f"Commit SHA: {short_sha}",
        f"GitHub Actor: {actor}",
    ]

    stage = incident.github_failing_stage or (
        report.metadata.get("failing_stage") if report and report.metadata else None
    )
    if stage:
        lines.append(f"Failing Stage: {stage}")

    if incident.external_url:
        lines.append(f"Workflow Run URL: {incident.external_url}")

    return "\n".join(lines)


def format_fix_pr_work_note(pull_request: PullRequestRef) -> str:
    """Work note announcing an opened auto-fix pull request."""
    return (
        f"[Auto-Fix Initiated] Pull Request #{pull_request.pr_number} created on branch "
        f"{pull_request.branch_name} to remediate failure: {pull_request.pr_url}"
    )


class ServiceNowAdapter(BaseAdapter):
    """Adapter for ServiceNow Table and Attachment APIs."""

    def __init__(
        self,
        config: Optional[BridgeConfig] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.config = config or get_settings()
        self._http_client = http_client
        self._caller_cache: dict[str, str] = {}

    def build_description(
        self, incident: Incident, report: Optional[Report] = None
    ) -> str:
        """Build facts-only summary description for ServiceNow incidents."""
        return build_servicenow_description(incident, report)

    def _resolve_caller_sys_id(
        self,
        client: Any,
        caller: Optional[str],
    ) -> Optional[str]:
        """Resolve ServiceNow caller username or email to sys_id via sys_user table."""
        if not caller:
            return None
        if caller in self._caller_cache:
            return self._caller_cache[caller]

        admin_sys_id = "6816f79cc0a8016401c5a33be04be441"
        base_url = (
            self.config.servicenow_instance_url or "https://example.service-now.com"
        ).rstrip("/")
        sys_user_endpoint = f"{base_url}/api/now/table/sys_user"

        try:
            query = f"user_name={caller}^ORemail={caller}"
            resp = client.get(sys_user_endpoint, params={"sysparm_query": query})
            if resp.status_code == 200:
                data = resp.json().get("result", [])
                if isinstance(data, list) and data:
                    sys_id = data[0].get("sys_id")
                    if sys_id:
                        self._caller_cache[caller] = str(sys_id)
                        return str(sys_id)
                elif isinstance(data, dict) and data.get("sys_id"):
                    sys_id = str(data["sys_id"])
                    self._caller_cache[caller] = sys_id
                    return sys_id
        except Exception as exc:
            logger.warning("Failed to resolve caller '%s' in sys_user: %s", caller, exc)

        if caller == "admin":
            self._caller_cache[caller] = admin_sys_id
            return admin_sys_id

        self._caller_cache[caller] = caller
        return caller

    def _get_client(self) -> Any:
        if self._http_client is not None:
            return _ReusableClientWrapper(self._http_client)
        auth = None
        if self.config.servicenow_username and self.config.servicenow_password:
            auth = httpx.BasicAuth(
                self.config.servicenow_username,
                self.config.servicenow_password,
            )
        timeout_seconds = getattr(self.config, "adapter_timeout_seconds", 60.0)
        timeout = httpx.Timeout(timeout_seconds, connect=10.0)
        return httpx.Client(auth=auth, timeout=timeout)

    def _execute_with_retry(
        self,
        func: Callable[[], Any],
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
    ) -> Any:
        """Execute a ServiceNow operation with retry and backoff
        for transient timeouts and connection errors.
        """
        for attempt in range(max_retries + 1):
            try:
                return func()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < max_retries:
                    sleep_time = backoff_seconds * (2**attempt)
                    logger.warning(
                        "ServiceNow API call failed with %s (attempt %d/%d). Retrying in %.1fs...",
                        type(exc).__name__,
                        attempt + 1,
                        max_retries + 1,
                        sleep_time,
                    )
                    time.sleep(sleep_time)
                else:
                    logger.error(
                        "ServiceNow API call failed after %d retries: %s",
                        max_retries,
                        exc,
                    )
                    raise


    def verify(self, request: Request, payload: bytes) -> bool:
        """Verify inbound webhook Bearer token."""
        expected_token = self.config.servicenow_webhook_token
        if not expected_token:
            if self.config.is_local:
                return True
            return False

        auth_header = request.headers.get("Authorization", "")
        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False

        token = parts[1].strip()
        return hmac.compare_digest(token, expected_token)

    def to_incident(self, payload: dict[str, Any]) -> Incident:
        """Normalize ServiceNow incident record into Incident contract."""
        data = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        if not isinstance(data, dict):
            raise ValueError("Payload must be a JSON object")

        sys_id = data.get("sys_id") or ""
        number = data.get("number") or ""
        if not sys_id and not number:
            raise ValueError("ServiceNow payload missing both 'sys_id' and 'number'")

        primary_id = sys_id or number
        external_key = number or sys_id
        inst_url = (self.config.servicenow_instance_url or "").rstrip("/")
        ext_url = f"{inst_url}/nav_to.do?uri=incident.do?sys_id={primary_id}" if inst_url else None

        severity_map = {"1": "P1", "2": "P2", "3": "P3", "4": "P4", "5": "P4"}
        raw_sev = str(data.get("priority") or data.get("urgency") or "3")
        severity = severity_map.get(raw_sev, "P3")

        opened_at = parse_utc_datetime(data.get("opened_at") or data.get("sys_created_on"))

        return Incident(
            id=f"sn-{primary_id}",
            source="servicenow",
            external_id=primary_id,
            external_key=external_key,
            external_url=ext_url,
            title=data.get("short_description") or "ServiceNow Incident",
            description=data.get("description") or "",
            severity=severity,  # type: ignore[arg-type]
            service_hint=data.get("category"),
            labels=["servicenow"],
            opened_at=opened_at,
            reporter=data.get("caller_id"),
        )

    def create_incident_with_report(
        self,
        incident: Incident,
        report: Report,
        pull_request: Optional[PullRequestRef] = None,
    ) -> IncidentRef:
        """Create a ServiceNow ticket or update an existing one using correlation_id."""
        base_url = (
            self.config.servicenow_instance_url or "https://example.service-now.com"
        ).rstrip("/")
        table_endpoint = f"{base_url}/api/now/table/incident"

        repo_full = incident.github_repo or incident.service_hint or "unknown"
        run_id = (
            incident.github_run_id if incident.github_run_id is not None else incident.external_id
        )
        run_attempt = (
            incident.github_run_attempt if incident.github_run_attempt is not None else 1
        )
        correlation_id = f"gdi:{repo_full}:{run_id}:{run_attempt}"
        correlation_display = getattr(
            self.config, "servicenow_correlation_display", "gha-deploy-investigator"
        )

        def _do_create_or_patch() -> IncidentRef:
            with self._get_client() as client:
                # Query before create: idempotency check using correlation_id
                query_resp = client.get(
                    table_endpoint,
                    params={"sysparm_query": f"correlation_id={correlation_id}"},
                )
                query_resp.raise_for_status()
                existing_records = query_resp.json().get("result", [])

                existing_incident = None
                if isinstance(existing_records, list) and existing_records:
                    existing_incident = existing_records[0]
                elif isinstance(existing_records, dict) and existing_records.get("sys_id"):
                    existing_incident = existing_records

                rendered_html = render_servicenow_html(report)
                work_notes = chunk_servicenow_work_note(rendered_html, max_chars=12000)[0]

                caller_id = self._resolve_caller_sys_id(client, self.config.servicenow_caller)
                assignment_group = self.config.servicenow_assignment_group

                short_description = (
                    f"[gdi-demo] [CI/CD Failure] {incident.github_repo or incident.title} "
                    f"({incident.github_failing_stage or 'Pipeline'})"
                )

                description = self.build_description(incident, report)

                target_state = getattr(
                    self.config,
                    "servicenow_incident_state_after_report",
                    "2",
                )

                if existing_incident:
                    sys_id = existing_incident.get("sys_id")
                    if not sys_id:
                        raise ValueError(
                            "ServiceNow existing incident missing 'sys_id' "
                            f"for correlation_id={correlation_id}"
                        )
                    number = existing_incident.get("number")
                    if not number:
                        raise ValueError(
                            "ServiceNow existing incident missing 'number' "
                            f"for correlation_id={correlation_id}"
                        )
                    patch_endpoint = f"{table_endpoint}/{sys_id}"

                    # 1. Update existing incident facts-only metadata and state if needed
                    patch_body: dict[str, Any] = {
                        "short_description": short_description,
                        "description": description,
                    }
                    if target_state and existing_incident.get("state") != target_state:
                        patch_body["state"] = target_state
                    if caller_id:
                        patch_body["caller_id"] = caller_id
                    if assignment_group:
                        patch_body["assignment_group"] = assignment_group

                    patch_resp = client.patch(patch_endpoint, json=patch_body)
                    patch_resp.raise_for_status()

                    # 2. Post findings as work notes
                    notes_resp = client.patch(patch_endpoint, json={"work_notes": work_notes})
                    notes_resp.raise_for_status()

                    # 3. Upload standalone dossier report as attachment
                    ref = IncidentRef(
                        id=f"sn-{sys_id}",
                        source="servicenow",
                        external_id=sys_id,
                        external_key=number,
                        external_url=f"{base_url}/nav_to.do?uri=incident.do?sys_id={sys_id}",
                    )
                    attachment_endpoint = (
                        f"{base_url}/api/now/attachment/file"
                        f"?table_name=incident&table_sys_id={sys_id}"
                        f"&file_name=ci_cd_investigation_report_{number}.html"
                    )
                    standalone_html = render_standalone_html(
                        report,
                        incident_ref=ref,
                        incident=incident,
                        config=self.config,
                        pull_request=pull_request,
                    )
                    att_resp = client.post(
                        attachment_endpoint,
                        content=standalone_html.encode("utf-8"),
                        headers={"Content-Type": "text/html"},
                    )
                    att_resp.raise_for_status()

                    logger.info(
                        "Found existing ServiceNow incident %s (%s) for correlation_id=%s; "
                        "updated incident, posted notes, attached report",
                        number,
                        sys_id,
                        correlation_id,
                    )

                    return ref

                sev_to_num = {"P1": "1", "P2": "2", "P3": "3", "P4": "4"}
                urgency = sev_to_num.get(incident.severity, "3")
                impact = sev_to_num.get(incident.severity, "3")

                # 1. Create incident directly with configured state (facts-only description)
                incident_body = {
                    "short_description": short_description,
                    "description": description,
                    "category": "DevOps",
                    "urgency": urgency,
                    "impact": impact,
                    "correlation_display": correlation_display,
                    "correlation_id": correlation_id,
                }
                if target_state:
                    incident_body["state"] = target_state
                if caller_id:
                    incident_body["caller_id"] = caller_id
                if assignment_group:
                    incident_body["assignment_group"] = assignment_group

                resp = client.post(table_endpoint, json=incident_body)
                resp.raise_for_status()
                res_json = resp.json().get("result", {})
                sys_id = res_json.get("sys_id")
                number = res_json.get("number")
                if not sys_id:
                    raise ValueError(
                        "ServiceNow incident creation response missing 'sys_id'"
                    )
                if not number:
                    raise ValueError(
                        "ServiceNow incident creation response missing 'number'"
                    )
                patch_endpoint = f"{table_endpoint}/{sys_id}"

                # 2. Post findings as work notes
                notes_resp = client.patch(patch_endpoint, json={"work_notes": work_notes})
                notes_resp.raise_for_status()

                # 3. Upload standalone dossier report as attachment
                ref = IncidentRef(
                    id=f"sn-{sys_id}",
                    source="servicenow",
                    external_id=sys_id,
                    external_key=number,
                    external_url=f"{base_url}/nav_to.do?uri=incident.do?sys_id={sys_id}",
                )
                attachment_endpoint = (
                    f"{base_url}/api/now/attachment/file"
                    f"?table_name=incident&table_sys_id={sys_id}"
                    f"&file_name=ci_cd_investigation_report_{number}.html"
                )
                standalone_html = render_standalone_html(
                    report,
                    incident_ref=ref,
                    incident=incident,
                    config=self.config,
                    pull_request=pull_request,
                )
                att_resp = client.post(
                    attachment_endpoint,
                    content=standalone_html.encode("utf-8"),
                    headers={"Content-Type": "text/html"},
                )
                att_resp.raise_for_status()

                logger.info(
                    "Created ServiceNow incident %s (%s) directly in state '%s', "
                    "posted work notes, and attached report dossier",
                    number,
                    sys_id,
                    target_state,
                )

                return ref


        return self._execute_with_retry(_do_create_or_patch)

    def post_ack(self, ref: IncidentRef) -> None:
        """Update ticket state to in-progress."""
        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        if not base_url:
            return
        endpoint = f"{base_url}/api/now/table/incident/{ref.external_id}"

        def _do_ack() -> None:
            with self._get_client() as client:
                resp = client.patch(
                    endpoint,
                    json={"state": self.config.servicenow_in_progress_state},
                )
                resp.raise_for_status()

        try:
            self._execute_with_retry(_do_ack)
        except Exception as exc:
            logger.warning("Failed to post ack to ServiceNow: %s", exc)

    def post_report(self, ref: IncidentRef, report: Report) -> None:
        """Update ticket with report work notes and attachment."""
        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        if not base_url:
            return
        endpoint = f"{base_url}/api/now/table/incident/{ref.external_id}"
        rendered_html = render_servicenow_html(report)
        work_notes = chunk_servicenow_work_note(rendered_html, max_chars=12000)[0]

        def _do_report() -> None:
            with self._get_client() as client:
                resp = client.patch(endpoint, json={"work_notes": work_notes})
                resp.raise_for_status()

        self._execute_with_retry(_do_report)

    def post_fix_pr_note(self, ref: IncidentRef, pull_request: PullRequestRef) -> None:
        """Post a work note linking the auto-fix pull request; incident stays In Progress."""
        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        if not base_url:
            return
        endpoint = f"{base_url}/api/now/table/incident/{ref.external_id}"
        note = format_fix_pr_work_note(pull_request)

        def _do_note() -> None:
            with self._get_client() as client:
                resp = client.patch(endpoint, json={"work_notes": note})
                resp.raise_for_status()

        self._execute_with_retry(_do_note)

    def post_failure(self, ref: IncidentRef, error: str) -> None:
        """Post failure note to ServiceNow."""
        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        if not base_url:
            return
        endpoint = f"{base_url}/api/now/table/incident/{ref.external_id}"

        def _do_failure() -> None:
            with self._get_client() as client:
                resp = client.patch(
                    endpoint,
                    json={"work_notes": f"[code]<b>Investigation failed:</b> {error}[/code]"},
                )
                resp.raise_for_status()

        self._execute_with_retry(_do_failure)
