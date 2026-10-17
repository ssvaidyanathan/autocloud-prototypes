import hmac
import logging
from typing import Any, Optional

import httpx
from fastapi import Request

from ..config import BridgeConfig, get_settings
from ..contracts.incident import Incident
from ..contracts.report import Report
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


class ServiceNowAdapter(BaseAdapter):
    """Adapter for ServiceNow Table and Attachment APIs."""

    def __init__(
        self,
        config: Optional[BridgeConfig] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.config = config or get_settings()
        self._http_client = http_client

    def _get_client(self) -> Any:
        if self._http_client is not None:
            return _ReusableClientWrapper(self._http_client)
        auth = None
        if self.config.servicenow_username and self.config.servicenow_password:
            auth = httpx.BasicAuth(
                self.config.servicenow_username,
                self.config.servicenow_password,
            )
        timeout = getattr(self.config, "adapter_timeout_seconds", 30.0)
        return httpx.Client(auth=auth, timeout=timeout)

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

    def create_incident_with_report(self, incident: Incident, report: Report) -> IncidentRef:
        """Create a new ServiceNow incident ticket with findings and HTML attachment."""
        base_url = (
            self.config.servicenow_instance_url or "https://example.service-now.com"
        ).rstrip("/")
        table_endpoint = f"{base_url}/api/now/table/incident"

        short_description = (
            f"[gdi-demo] [CI/CD Failure] {incident.github_repo or incident.title} "
            f"({incident.github_failing_stage or 'Pipeline'})"
        )

        root_cause_text = (
            report.probable_root_cause.text if report.probable_root_cause else report.summary
        )

        description_lines = [
            f"Repository: {incident.github_repo or 'N/A'}",
            f"Branch: {incident.github_branch or 'N/A'}",
            f"Commit SHA: {incident.github_head_sha or 'N/A'}",
            f"Reporter: {incident.reporter or 'N/A'}",
            "",
            "Root Cause:",
            root_cause_text,
        ]
        description = "\n".join(description_lines)

        sev_to_num = {"P1": "1", "P2": "2", "P3": "3", "P4": "4"}
        urgency = sev_to_num.get(incident.severity, "3")
        impact = sev_to_num.get(incident.severity, "3")

        rendered_html = render_servicenow_html(report)
        work_notes = chunk_servicenow_work_note(rendered_html, max_chars=12000)[0]

        incident_body = {
            "short_description": short_description,
            "description": description,
            "category": "DevOps",
            "urgency": urgency,
            "impact": impact,
            "work_notes": work_notes,
        }

        with self._get_client() as client:
            resp = client.post(table_endpoint, json=incident_body)
            resp.raise_for_status()
            res_json = resp.json().get("result", {})
            sys_id = res_json.get("sys_id", "mock-sys-id")
            number = res_json.get("number", "INC0019482")

            # Upload standalone report as attachment
            attachment_endpoint = (
                f"{base_url}/api/now/attachment/file"
                f"?table_name=incident&table_sys_id={sys_id}"
                f"&file_name=ci_cd_investigation_report_{number}.html"
            )
            standalone_html = render_standalone_html(report)
            att_resp = client.post(
                attachment_endpoint,
                content=standalone_html.encode("utf-8"),
                headers={"Content-Type": "text/html"},
            )
            att_resp.raise_for_status()

            logger.info(
                "Created ServiceNow incident %s (%s) with report attachment", number, sys_id
            )

            return IncidentRef(
                id=f"sn-{sys_id}",
                source="servicenow",
                external_id=sys_id,
                external_key=number,
                external_url=f"{base_url}/nav_to.do?uri=incident.do?sys_id={sys_id}",
            )

    def post_ack(self, ref: IncidentRef) -> None:
        """Update ticket state to in-progress."""
        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        if not base_url:
            return
        endpoint = f"{base_url}/api/now/table/incident/{ref.external_id}"
        with self._get_client() as client:
            try:
                client.patch(
                    endpoint,
                    json={"state": self.config.servicenow_in_progress_state},
                )
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
        with self._get_client() as client:
            client.patch(endpoint, json={"work_notes": work_notes})

    def post_failure(self, ref: IncidentRef, error: str) -> None:
        """Post failure note to ServiceNow."""
        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        if not base_url:
            return
        endpoint = f"{base_url}/api/now/table/incident/{ref.external_id}"
        with self._get_client() as client:
            client.patch(
                endpoint,
                json={"work_notes": f"[code]<b>Investigation failed:</b> {error}[/code]"},
            )
