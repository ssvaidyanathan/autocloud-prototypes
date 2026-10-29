import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import httpx
from fastapi import Request

from ..config import BridgeConfig, get_settings
from ..contracts.incident import Incident
from ..contracts.report import Report
from ..templates.renderer import (
    render_servicenow_ack_html,
    render_servicenow_failure_html,
    render_servicenow_html,
    render_standalone_html,
)
from .base import BaseAdapter, IncidentRef, parse_utc_datetime

logger = logging.getLogger(__name__)


def render_ack_note() -> str:
    """Render standardized ServiceNow acknowledgement note wrapped in [code]...[/code]."""
    return f"[code]{render_servicenow_ack_html()}[/code]"


def render_failure_note(error: str) -> str:
    """Render standardized ServiceNow failure note wrapped in [code]...[/code]."""
    return f"[code]{render_servicenow_failure_html(error)}[/code]"


def render_work_note(report: Report) -> str:
    """Render ServiceNow work note HTML format."""
    return render_servicenow_html(report)


class _ReusableClientWrapper:
    """Wrapper that prevents closing an externally supplied client in a with statement."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __enter__(self) -> httpx.Client:
        return self._client

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class ServiceNowAdapter(BaseAdapter):
    """Adapter for ServiceNow Incident Management table and attachment APIs."""

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
        """Verify inbound webhook request Authorization header using constant-time check."""
        expected_token = self.config.servicenow_webhook_token
        if not expected_token:
            # If no webhook token is configured, allow in development
            logger.warning("No SERVICENOW_WEBHOOK_TOKEN configured; accepting request.")
            return True

        auth_header = request.headers.get("Authorization", "")
        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            logger.warning("ServiceNow webhook missing or invalid Bearer Authorization header")
            return False

        token = parts[1].strip()
        return hmac.compare_digest(token, expected_token)

    def to_incident(self, payload: dict[str, Any]) -> Incident:
        """Normalize ServiceNow incident record into Incident contract with UTC timestamps."""
        # Payload may be root record or nested under 'result'
        data = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        if not isinstance(data, dict):
            raise ValueError(
                "Missing required ServiceNow incident identifier: "
                "'sys_id' or 'number' must be non-empty"
            )

        raw_sys_id = data.get("sys_id")
        raw_number = data.get("number")
        clean_sys_id = str(raw_sys_id).strip() if raw_sys_id is not None else ""
        clean_number = str(raw_number).strip() if raw_number is not None else ""

        if not clean_sys_id and not clean_number:
            raise ValueError(
                "Missing required ServiceNow incident identifier: "
                "'sys_id' or 'number' must be non-empty"
            )

        sys_id = clean_sys_id or clean_number
        number = clean_number or clean_sys_id
        short_desc = str(data.get("short_description") or "ServiceNow Incident")
        description = str(data.get("description") or short_desc)

        # Map priority to P1-P4
        raw_priority = str(data.get("priority", "3")).strip()
        priority_map: dict[str, Literal["P1", "P2", "P3", "P4"]] = {
            "1": "P1",
            "2": "P2",
            "3": "P3",
            "4": "P4",
            "5": "P4",
        }
        severity: Literal["P1", "P2", "P3", "P4"] = priority_map.get(raw_priority, "P3")

        # CMDB CI as service hint, fallback to parsing short_description and description
        service_hint = data.get("cmdb_ci")
        if isinstance(service_hint, dict):
            service_hint = service_hint.get("display_value") or service_hint.get("value")
        service_hint_str = (
            str(service_hint).strip() if service_hint and str(service_hint).strip() else None
        )

        if not service_hint_str:
            combined_text = f"{short_desc} {description}".lower()
            match = re.search(
                r"\b([a-z0-9]+(?:-[a-z0-9]+)*(?:-api|-svc|-service))\b", combined_text
            )
            if match:
                service_hint_str = match.group(1)

        # Caller
        caller = data.get("caller_id") or data.get("caller")
        if isinstance(caller, dict):
            caller = caller.get("display_value") or caller.get("value")
        caller_str = str(caller) if caller else None

        # Labels from category, urgency, impact
        labels: list[str] = []
        if data.get("category"):
            labels.append(f"category:{data['category']}")
        if data.get("urgency"):
            labels.append(f"urgency:{data['urgency']}")
        if data.get("impact"):
            labels.append(f"impact:{data['impact']}")

        # Ensure UTC-aware opened_at
        opened_at = parse_utc_datetime(data.get("opened_at") or data.get("sys_created_on"))
        now_utc = datetime.now(timezone.utc)
        if (now_utc - opened_at) > timedelta(minutes=60):
            logger.info(
                "ServiceNow opened_at (%s) is > 60m in past relative to webhook arrival (%s); "
                "recognizing as local timezone string without offset and anchoring to now_utc",
                opened_at,
                now_utc,
            )
            opened_at = now_utc

        # Instance deep link URL
        base_url = self.config.servicenow_instance_url or ""
        external_url = (
            f"{base_url.rstrip('/')}/nav_to.do?uri=incident.do?sys_id={sys_id}"
            if base_url
            else None
        )

        return Incident(
            id=f"snow-{sys_id}",
            source="servicenow",
            external_id=sys_id,
            external_key=number,
            external_url=external_url,
            title=short_desc,
            description=description,
            severity=severity,
            service_hint=service_hint_str,
            labels=labels,
            opened_at=opened_at,
            reporter=caller_str,
        )

    def post_ack(self, ref: IncidentRef) -> None:
        """Acknowledge investigation underway and transition incident to In Progress."""
        base_url = self.config.servicenow_instance_url
        if not base_url:
            logger.warning("SERVICENOW_INSTANCE_URL not set; skipping ServiceNow ack post.")
            return

        url = f"{base_url.rstrip('/')}/api/now/table/incident/{ref.external_id}"
        body = {
            "state": self.config.servicenow_in_progress_state,
            "work_notes": render_ack_note(),
        }

        with self._get_client() as client:
            try:
                resp = client.patch(url, json=body)
                resp.raise_for_status()
                logger.info("Successfully posted ServiceNow ack for %s", ref.external_key)
            except Exception as exc:
                logger.error("Failed to post ServiceNow ack for %s: %s", ref.external_key, exc)
                raise

    def post_report(self, ref: IncidentRef, report: Report) -> None:
        """Render HTML work note as single note (<12,000 chars) and attach report."""
        base_url = self.config.servicenow_instance_url
        if not base_url:
            logger.warning("SERVICENOW_INSTANCE_URL not set; skipping ServiceNow report post.")
            return

        with self._get_client() as client:
            # 1. Render and post strictly ONE consolidated work note (< 12,000 chars)
            html = render_servicenow_html(report)
            work_note = f"[code]{html}[/code]" if not html.startswith("[code]") else html

            max_chars = getattr(self.config, "servicenow_chunk_size", 12000)
            if len(work_note) > max_chars:
                logger.warning(
                    "ServiceNow work note length (%d) exceeds limit (%d); truncating safely",
                    len(work_note),
                    max_chars,
                )
                closing = "</div>[/code]"
                work_note = work_note[: max_chars - len(closing)] + closing

            patch_url = f"{base_url.rstrip('/')}/api/now/table/incident/{ref.external_id}"
            try:
                resp = client.patch(patch_url, json={"work_notes": work_note})
                resp.raise_for_status()
                logger.info("Successfully posted ServiceNow work note for %s", ref.external_key)
            except Exception as exc:
                logger.error(
                    "Failed to post ServiceNow work note for %s: %s",
                    ref.external_key,
                    exc,
                )
                raise

            # 2. Attach standalone HTML report via ServiceNow Attachment API
            standalone_html = render_standalone_html(report, incident_ref=ref)
            file_name = f"sre_investigation_report_{ref.external_key}.html"
            attach_url = (
                f"{base_url.rstrip('/')}/api/now/attachment/file"
                f"?table_name=incident&table_sys_id={ref.external_id}&file_name={file_name}"
            )

            try:
                headers = {"Content-Type": "text/html"}
                resp = client.post(
                    attach_url, content=standalone_html.encode("utf-8"), headers=headers
                )
                resp.raise_for_status()
                logger.info("Successfully attached HTML report to ServiceNow %s", ref.external_key)
            except Exception as exc:
                logger.warning(
                    "Failed to attach report to ServiceNow incident %s: %s",
                    ref.external_key,
                    exc,
                )

    def post_failure(self, ref: IncidentRef, error: str) -> None:
        """Post investigation failure work note to ServiceNow incident."""
        base_url = self.config.servicenow_instance_url
        if not base_url:
            logger.warning("SERVICENOW_INSTANCE_URL not set; skipping ServiceNow failure post.")
            return

        url = f"{base_url.rstrip('/')}/api/now/table/incident/{ref.external_id}"
        body = {
            "work_notes": render_failure_note(error),
        }

        with self._get_client() as client:
            try:
                resp = client.patch(url, json=body)
                resp.raise_for_status()
                logger.info("Successfully posted ServiceNow failure note for %s", ref.external_key)
            except Exception as exc:
                logger.error(
                    "Failed to post ServiceNow failure note for %s: %s", ref.external_key, exc
                )
                raise
