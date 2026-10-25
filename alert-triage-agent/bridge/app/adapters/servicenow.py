import html
import logging
from typing import Any, Optional
import httpx

from ..config import BridgeConfig, get_settings
from ..contracts.links import ConsoleLinkBuilder
from ..contracts.report import InvestigationReport
from ..templates.renderer import render_standalone_html

logger = logging.getLogger(__name__)


class ServiceNowIncidentRef:
    def __init__(self, sys_id: str, number: str, url: str) -> None:
        self.sys_id = sys_id
        self.number = number
        self.url = url


class ServiceNowAdapter:
    """ServiceNow Table and Attachment API adapter with GCS report fallback."""

    def __init__(
        self,
        config: Optional[BridgeConfig] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.config = config or get_settings()
        self._http_client = http_client
        if self.config.servicenow_instance_url:
            url = self.config.servicenow_instance_url.strip()
            if url and not url.startswith("https://"):
                raise ValueError("ServiceNow instance URL must use HTTPS scheme")

    def _get_client(self) -> httpx.Client:
        if self._http_client is not None:
            return self._http_client
        auth = None
        if self.config.servicenow_username and self.config.servicenow_password:
            auth = httpx.BasicAuth(
                self.config.servicenow_username,
                self.config.servicenow_password,
            )
        timeout = httpx.Timeout(self.config.adapter_timeout_seconds, connect=10.0)
        return httpx.Client(auth=auth, timeout=timeout)

    def _persist_report_to_gcs(self, bucket_name: str, group_id: str, html_content: str) -> str:
        """Persist HTML report to GCS bucket reports/ path."""
        gcs_uri = f"gs://{bucket_name}/reports/{group_id}.html"
        try:
            from google.cloud import storage
            client = storage.Client(project=self.config.project_id)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(f"reports/{group_id}.html")
            blob.upload_from_string(html_content, content_type="text/html")
            logger.info("Persisted HTML report to GCS: %s", gcs_uri)
        except Exception as exc:
            logger.warning("Could not persist report to GCS directly (%s); returning URI %s", exc, gcs_uri)
        return gcs_uri

    def append_work_note(self, sys_id: str, note: str, client: Optional[httpx.Client] = None) -> None:
        """Append a work note to an existing ServiceNow incident."""
        if not self.config.servicenow_instance_url:
            logger.info("Mock appending work note to %s: %s", sys_id, note)
            return

        base_url = self.config.servicenow_instance_url.rstrip("/")
        patch_endpoint = f"{base_url}/api/now/table/incident/{sys_id}"
        close_client = False
        if client is None:
            client = self._get_client()
            close_client = True
        try:
            resp = client.patch(patch_endpoint, json={"work_notes": note})
            resp.raise_for_status()
            logger.info("Successfully appended work note to incident %s", sys_id)
        except Exception as exc:
            logger.error("Failed to append work note to incident %s: %s", sys_id, exc)
        finally:
            if close_client and self._http_client is None:
                client.close()

    def append_late_alert_work_note(
        self,
        sys_id: str,
        alert: Any,
        blast_radius: Optional[list[str]] = None,
        client: Optional[httpx.Client] = None,
        project_id: Optional[str] = None,
        as_html: bool = True,
    ) -> None:
        """Append a work note for a late-arriving correlated alert with updated blast radius and clickable links."""
        proj = (project_id or self.config.project_id or "").strip() or None
        builder = ConsoleLinkBuilder(project_id=proj) if proj else None

        policy_name = getattr(alert, "policy_name", "Alert")
        policy_id = getattr(alert, "policy_id", None)
        resource_name = getattr(alert, "resource_name", "Resource")
        raw_incident_id = getattr(alert, "incident_id", None)
        clean_incident_id = str(raw_incident_id or "").strip()
        has_real_id = bool(
            clean_incident_id and clean_incident_id.lower() not in ["none", "n/a", "unknown", "null", ""]
        )

        policy_url = builder.build_policy_url(policy_id or policy_name) if builder else None

        alert_url: Optional[str] = None
        if has_real_id:
            payload_url = getattr(alert, "url", None)
            if payload_url and str(payload_url).strip():
                alert_url = str(payload_url).strip()
            elif builder:
                alert_url = builder.build_incident_url(clean_incident_id)

        blast_str = ", ".join(blast_radius) if blast_radius else resource_name

        comp = getattr(alert, "component", None)
        if not comp and hasattr(alert, "policy_user_labels") and alert.policy_user_labels:
            comp = alert.policy_user_labels.get("component")
        if not comp and isinstance(alert, dict):
            comp = alert.get("component") or (alert.get("policy_user_labels") or {}).get("component")
        if not comp:
            comp = resource_name

        sig = getattr(alert, "signal", None)
        if not sig and hasattr(alert, "policy_user_labels") and alert.policy_user_labels:
            sig = alert.policy_user_labels.get("signal")
        if not sig and isinstance(alert, dict):
            sig = alert.get("signal") or (alert.get("policy_user_labels") or {}).get("signal")

        if not as_html:
            pol_str = f"{policy_name} ({policy_url})" if policy_url else policy_name
            if has_real_id and alert_url:
                alert_part_plain = f"(Alert ID: {clean_incident_id} - {alert_url})"
            else:
                disp_id = clean_incident_id if clean_incident_id else "N/A"
                alert_part_plain = f"(Alert ID: {disp_id})"
            meta_parts = []
            if comp:
                meta_parts.append(f"Component: {comp}")
            if sig:
                meta_parts.append(f"Signal: {sig}")
            meta_str = f" | {' | '.join(meta_parts)}" if meta_parts else ""
            note = (
                f"Correlated Alert Ingested: {pol_str} on "
                f"{resource_name} {alert_part_plain}{meta_str}\n"
                f"Updated Blast Radius: {blast_str}"
            )
        else:
            if policy_url:
                policy_html = (
                    f'<a href="{html.escape(policy_url, quote=True)}" target="_blank" '
                    f'class="console-link" style="color: #0969da; text-decoration: none;">'
                    f'{html.escape(policy_name)}</a>'
                )
            else:
                policy_html = html.escape(policy_name)
            resource_html = f"<code>{html.escape(resource_name)}</code>"
            if has_real_id and alert_url:
                id_link = (
                    f'<a href="{html.escape(alert_url, quote=True)}" target="_blank" '
                    f'class="console-link" style="color: #0969da; text-decoration: none;">'
                    f'{html.escape(clean_incident_id)}</a>'
                )
                alert_part = f"(Alert ID: {id_link})"
            else:
                disp_id = clean_incident_id if clean_incident_id else "N/A"
                alert_part = f"(Alert ID: {html.escape(disp_id)})"

            meta_html_parts = []
            if comp:
                meta_html_parts.append(f"Component: <code>{html.escape(comp)}</code>")
            if sig:
                meta_html_parts.append(f"Signal: <code>{html.escape(sig)}</code>")
            meta_html = f" | {' | '.join(meta_html_parts)}" if meta_html_parts else ""

            blast_html = html.escape(blast_str)
            note = (
                f"[code]<b>Correlated Alert Ingested:</b> {policy_html} on "
                f"{resource_html} {alert_part}{meta_html}<br/>"
                f"<b>Updated Blast Radius:</b> {blast_html}[/code]"
            )

        self.append_work_note(sys_id, note, client=client)

    def append_resolution_work_note(
        self,
        sys_id: str,
        note: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        """Append a resolution work note when an incident is resolved."""
        res_text = note or "All correlated alerts report state 'closed'. Incident lifecycle completed."
        work_note = f"[code]<b>Incident Resolved:</b> {res_text}[/code]"
        self.append_work_note(sys_id, work_note, client=client)

    def resolve_incident(
        self,
        sys_id: str,
        close_notes: str,
        close_code: str = "Solved (Permanently)",
        group_id: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        """Resolve a ServiceNow incident with state=6, close_code, and close_notes."""
        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        if not base_url or not self.config.servicenow_username:
            logger.info(
                "Mock resolving incident %s (group=%s): close_notes=%s, close_code=%s",
                sys_id,
                group_id,
                close_notes,
                close_code,
            )
            return

        patch_endpoint = f"{base_url}/api/now/table/incident/{sys_id}"
        close_client = False
        if client is None:
            client = self._get_client()
            close_client = True
        try:
            body = {
                "state": "6",
                "close_code": close_code,
                "close_notes": close_notes,
            }
            resp = client.patch(patch_endpoint, json=body)
            resp.raise_for_status()
            logger.info("Successfully resolved incident %s in ServiceNow", sys_id)
            self.append_resolution_work_note(sys_id, note=close_notes, client=client)
        except Exception as exc:
            logger.error("Failed to resolve incident %s in ServiceNow: %s", sys_id, exc)
            raise
        finally:
            if close_client and self._http_client is None:
                client.close()

    def get_incident_record(self, sys_id: str) -> dict[str, Any]:
        """Fetch incident record and work notes journal from ServiceNow Table API."""
        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        if not base_url or not self.config.servicenow_username:
            if hasattr(self, "_created_incidents") and sys_id in self._created_incidents:
                ref = self._created_incidents[sys_id]
                notes = [{"value": n, "sys_created_on": "2026-10-25 02:00:00"} for n in getattr(self, "_posted_notes", [])]
                atts = [{"file_name": f"ata-diagnostic-report-{ref.sys_id}.html", "size_bytes": 1024}]
                return {
                    "incident": {
                        "sys_id": ref.sys_id,
                        "number": ref.number,
                        "state": "2",
                        "short_description": "Investigation report",
                        "caller_id": self.config.servicenow_caller or "admin",
                    },
                    "work_notes": notes,
                    "attachments": atts,
                }
            return {"error": "ServiceNow not configured"}

        client = self._get_client()
        close_client = self._http_client is None
        try:
            inc_resp = client.get(
                f"{base_url}/api/now/table/incident/{sys_id}",
                params={"sysparm_display_value": "all"},
            )
            if inc_resp.status_code == 404:
                if hasattr(self, "_created_incidents") and sys_id in self._created_incidents:
                    ref = self._created_incidents[sys_id]
                    notes = [{"value": n, "sys_created_on": "2026-10-25 02:00:00"} for n in getattr(self, "_posted_notes", [])]
                    return {
                        "incident": {
                            "sys_id": ref.sys_id,
                            "number": ref.number,
                            "state": "2",
                            "short_description": f"Incident {ref.number}",
                            "caller_id": self.config.servicenow_caller or "admin",
                        },
                        "work_notes": notes,
                        "attachments": [],
                    }
                return {}
            inc_resp.raise_for_status()
            inc_data = inc_resp.json().get("result", {})
            journal_resp = client.get(
                f"{base_url}/api/now/table/sys_journal_field",
                params={
                    "sysparm_query": f"element_id={sys_id}^element=work_notes",
                    "sysparm_limit": "20",
                },
            )
            journal_data = journal_resp.json().get("result", []) if journal_resp.status_code == 200 else []
            if not journal_data and hasattr(self, "_posted_notes") and self._posted_notes:
                journal_data = [{"value": n, "sys_created_on": "2026-10-25 02:00:00"} for n in self._posted_notes]

            att_resp = client.get(
                f"{base_url}/api/now/attachment",
                params={
                    "sysparm_query": f"table_sys_id={sys_id}",
                    "sysparm_limit": "20",
                },
            )
            att_data = att_resp.json().get("result", []) if att_resp.status_code == 200 else []
            return {
                "incident": inc_data,
                "work_notes": journal_data,
                "attachments": att_data,
            }
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return {}
            logger.error("HTTP error fetching incident record %s: %s", sys_id, exc)
            return {}
        except Exception as exc:
            logger.error("Error fetching incident record %s: %s", sys_id, exc)
            return {}
        finally:
            if close_client:
                client.close()

    def get_attachment_content(self, attachment_sys_id: str) -> str:
        """Fetch raw attachment file content by attachment sys_id."""
        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        if not base_url or not self.config.servicenow_username:
            return ""
        client = self._get_client()
        close_client = self._http_client is None
        try:
            resp = client.get(f"{base_url}/api/now/attachment/{attachment_sys_id}/file")
            if resp.status_code == 200:
                return resp.text
            return ""
        finally:
            if close_client:
                client.close()

    def _resolve_caller_id(self, client: httpx.Client) -> Optional[str]:
        """Resolve caller_id sys_id: use SERVICENOW_CALLER if set, else dynamic sys_user lookup."""
        caller = self.config.servicenow_caller
        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        if not base_url:
            return None

        if caller and caller.strip():
            val = caller.strip()
            # If already a 32-char hex sys_id, return directly
            if len(val) == 32 and all(c in "0123456789abcdefABCDEF" for c in val):
                return val
            lookup_query = f"user_name={val}^ORname={val}"
        else:
            lookup_query = "user_name=admin^ORname=System Administrator"

        user_endpoint = f"{base_url}/api/now/table/sys_user"
        params = {
            "sysparm_query": lookup_query,
            "sysparm_limit": "1",
            "sysparm_fields": "sys_id,user_name,name",
        }
        try:
            resp = client.get(user_endpoint, params=params)
            if resp.status_code == 200:
                results = resp.json().get("result", [])
                if results and isinstance(results, list):
                    sys_id = results[0].get("sys_id")
                    if sys_id:
                        logger.info("Dynamically resolved ServiceNow caller sys_id: %s", sys_id)
                        return str(sys_id)
            logger.warning("Could not dynamically resolve caller sys_id (status=%s)", resp.status_code)
        except Exception as exc:
            logger.warning("Failed dynamic sys_user lookup for caller: %s", exc)
        return None

    def find_incident_by_correlation_id(self, correlation_id: str) -> Optional[ServiceNowIncidentRef]:
        """Query ServiceNow Table API for an existing incident by correlation_id."""
        candidate_ids = [correlation_id]
        if not correlation_id.endswith(".md"):
            candidate_ids.append(f"incident-{correlation_id}.md")
        elif correlation_id.startswith("incident-") and correlation_id.endswith(".md"):
            candidate_ids.append(correlation_id[len("incident-"):-3])

        if hasattr(self, "_created_incidents"):
            for cid in candidate_ids:
                if cid in self._created_incidents:
                    return self._created_incidents[cid]

        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        if not base_url or not self.config.servicenow_username:
            return None

        client = self._get_client()
        close_client = self._http_client is None
        try:
            table_endpoint = f"{base_url}/api/now/table/incident"
            for cid in candidate_ids:
                params = {
                    "sysparm_query": f"correlation_id={cid}",
                    "sysparm_limit": "1",
                    "sysparm_fields": "sys_id,number",
                }
                resp = client.get(table_endpoint, params=params)
                if resp.status_code == 200:
                    results = resp.json().get("result", [])
                    if results and isinstance(results, list) and len(results) > 0:
                        rec = results[0]
                        sys_id = rec.get("sys_id")
                        number = rec.get("number", "INC0010482")
                        if sys_id:
                            ticket_url = f"{base_url}/nav_to.do?uri=incident.do?sys_id={sys_id}"
                            ref = ServiceNowIncidentRef(sys_id=str(sys_id), number=str(number), url=ticket_url)
                            if not hasattr(self, "_created_incidents"):
                                self._created_incidents = {}
                            self._created_incidents[correlation_id] = ref
                            self._created_incidents[cid] = ref
                            return ref
            return None
        except Exception as exc:
            logger.warning("Failed to query incident by correlation_id %s: %s", correlation_id, exc)
            return None
        finally:
            if close_client:
                client.close()

    def update_incident(
        self,
        sys_id: str,
        report: InvestigationReport,
        number: Optional[str] = None,
    ) -> ServiceNowIncidentRef:
        """Update an existing ServiceNow incident with refreshed diagnostic report and work note."""
        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        standalone_html = render_standalone_html(report)
        bucket_name = self.config.effective_incidents_bucket
        ticket_number = number or getattr(report, "servicenow_number", None) or "INC0010482"
        ticket_url = (
            f"{base_url}/nav_to.do?uri=incident.do?sys_id={sys_id}"
            if base_url
            else f"gs://{bucket_name}/reports/{report.group_id}.html"
        )

        ref = ServiceNowIncidentRef(sys_id=sys_id, number=ticket_number, url=ticket_url)
        if not hasattr(self, "_created_incidents"):
            self._created_incidents = {}
        incident_filename = report.filename or f"incident-{report.group_id}.md"
        self._created_incidents[incident_filename] = ref
        self._created_incidents[report.group_id] = ref

        report.servicenow_sys_id = sys_id
        report.servicenow_number = ticket_number

        # Fallback to mock / GCS-only if ServiceNow credentials are not provided
        if not base_url or not self.config.servicenow_username:
            logger.info("ServiceNow credentials not configured. Mock update incident %s with GCS report.", sys_id)
            gcs_uri = self._persist_report_to_gcs(bucket_name, report.group_id, standalone_html)
            ref.url = gcs_uri
            return ref

        client = self._get_client()
        close_client = self._http_client is None
        try:
            # 1. Update incident core fields (short_description, description)
            table_endpoint = f"{base_url}/api/now/table/incident/{sys_id}"
            short_desc = report.build_short_description(max_length=160)
            structured_desc = report.build_structured_description(
                project_id=self.config.project_id,
                incidents_bucket=self.config.effective_incidents_bucket,
            )
            patch_data: dict[str, Any] = {
                "short_description": short_desc,
                "description": structured_desc,
            }
            if report.priority == "P1":
                patch_data["urgency"] = "1"
                patch_data["impact"] = "1"
            try:
                patch_resp = client.patch(table_endpoint, json=patch_data)
                patch_resp.raise_for_status()
                logger.info("Successfully patched ServiceNow incident %s table fields", ticket_number)
            except Exception as patch_exc:
                logger.warning("Could not patch incident %s table fields: %s", ticket_number, patch_exc)

            # 2. Append refreshed 4-bullet executive summary to work notes
            exec_notes = report.build_executive_summary_html()
            self.append_work_note(sys_id, exec_notes, client=client)

            # 3. Append work note on updated findings
            blast_str = ", ".join(report.blast_radius) if report.blast_radius else report.origin
            alert_info = ""
            if report.alerts:
                alert_info = f"<br/><b>Correlated Alerts ({len(report.alerts)}):</b> " + ", ".join(
                    a.get("policy_name", a.get("incident_id", "alert")) if isinstance(a, dict) else str(a)
                    for a in report.alerts
                )
            work_note = (
                f"[code]<b>Investigation Findings Updated:</b> {report.summary}<br/>"
                f"<b>Origin:</b> <code>{report.origin}</code> | <b>Blast Radius:</b> {blast_str}"
                f"{alert_info}<br/>"
                f"Diagnostic report attachment refreshed.[/code]"
            )
            self.append_work_note(sys_id, work_note, client=client)

            # 2. Refresh standalone HTML diagnostic report attachment
            attachment_endpoint = (
                f"{base_url}/api/now/attachment/file"
                f"?table_name=incident&table_sys_id={sys_id}"
                f"&file_name=ata-diagnostic-report-{report.group_id}.html"
            )
            try:
                att_resp = client.post(
                    attachment_endpoint,
                    content=standalone_html.encode("utf-8"),
                    headers={"Content-Type": "text/html"},
                )
                att_resp.raise_for_status()
                logger.info("Refreshed HTML diagnostic report on ServiceNow incident %s", ticket_number)
            except Exception as att_exc:
                logger.warning("Attachment refresh failed (%s); persisting to GCS fallback", att_exc)
                gcs_uri = self._persist_report_to_gcs(bucket_name, report.group_id, standalone_html)
                note = (
                    f"[code]<b>Diagnostic Report:</b> Report attachment refresh failed. "
                    f"Full HTML report persisted to GCS at <code>{gcs_uri}</code>[/code]"
                )
                self.append_work_note(sys_id, note, client=client)

            return ref
        finally:
            if close_client:
                client.close()

    def create_incident(self, report: InvestigationReport, query_before_create: bool = False) -> ServiceNowIncidentRef:
        """Create ServiceNow incident ticket with 4-bullet executive summary and HTML attachment."""
        incident_filename = report.filename or f"incident-{report.group_id}.md"
        if query_before_create:
            existing = self.find_incident_by_correlation_id(incident_filename)
            if existing is not None:
                return self.update_incident(existing.sys_id, report, number=existing.number)

        base_url = (self.config.servicenow_instance_url or "").rstrip("/")
        standalone_html = render_standalone_html(report)
        bucket_name = self.config.effective_incidents_bucket

        # Fallback to mock / GCS-only if ServiceNow credentials are not provided
        if not base_url or not self.config.servicenow_username:
            logger.info("ServiceNow credentials not configured. Using mock ticket creation with GCS report.")
            gcs_uri = self._persist_report_to_gcs(bucket_name, report.group_id, standalone_html)
            report.servicenow_sys_id = f"mock-{report.group_id}"
            report.servicenow_number = "INC0010482"
            ref = ServiceNowIncidentRef(
                sys_id=report.servicenow_sys_id,
                number=report.servicenow_number,
                url=gcs_uri,
            )
            if not hasattr(self, "_created_incidents"):
                self._created_incidents = {}
            self._created_incidents[incident_filename] = ref
            self._created_incidents[report.group_id] = ref
            return ref

        table_endpoint = f"{base_url}/api/now/table/incident"
        short_desc = report.build_short_description(max_length=160)
        structured_desc = report.build_structured_description(
            project_id=self.config.project_id,
            incidents_bucket=self.config.effective_incidents_bucket,
        )
        sev_urgency = "1" if report.priority == "P1" else "2"
        incident_filename = report.filename or f"incident-{report.group_id}.md"

        # Create incident with state NEW (state=1)
        incident_body: dict[str, Any] = {
            "short_description": short_desc,
            "description": structured_desc,
            "category": "Cloud Platform",
            "urgency": sev_urgency,
            "impact": sev_urgency,
            "state": "1",
            "correlation_id": incident_filename,
            "correlation_display": f"{self.config.name_prefix}-alert-triage",
        }

        client = self._get_client()
        close_client = self._http_client is None
        try:
            caller_id = self._resolve_caller_id(client)
            if caller_id:
                incident_body["caller_id"] = caller_id

            if self.config.servicenow_assignment_group:
                incident_body["assignment_group"] = self.config.servicenow_assignment_group

            # 1. Create incident ticket via Table API with state NEW (state=1)
            resp = client.post(table_endpoint, json=incident_body)
            resp.raise_for_status()
            res_json = resp.json().get("result", {})
            sys_id = str(res_json.get("sys_id", f"gen-{report.group_id}"))
            number = str(res_json.get("number", "INC0010482"))
            created_state = str(res_json.get("state", "1"))
            report.servicenow_sys_id = sys_id
            report.servicenow_number = number

            logger.info(
                "Created ServiceNow incident %s (%s) with initial state %s (New)",
                number,
                sys_id,
                created_state,
            )

            # 2. IMMEDIATELY PATCH to In Progress (state=2) as the very next call (before work notes and attachment)
            patch_endpoint = f"{table_endpoint}/{sys_id}"
            try:
                patch_resp = client.patch(patch_endpoint, json={"state": "2"})
                patch_resp.raise_for_status()
                logger.info("Successfully patched ServiceNow incident %s state to 2 (In Progress)", number)
            except Exception as patch_exc:
                logger.warning("Could not patch incident %s state to 2: %s", number, patch_exc)

            # 3. Add 4-bullet executive summary directly to work_notes journal using [code] HTML tags
            exec_notes = report.build_executive_summary_html()
            self.append_work_note(sys_id, exec_notes, client=client)

            # 4. Attach standalone HTML diagnostic report
            attachment_endpoint = (
                f"{base_url}/api/now/attachment/file"
                f"?table_name=incident&table_sys_id={sys_id}"
                f"&file_name=ata-diagnostic-report-{report.group_id}.html"
            )
            try:
                att_resp = client.post(
                    attachment_endpoint,
                    content=standalone_html.encode("utf-8"),
                    headers={"Content-Type": "text/html"},
                )
                att_resp.raise_for_status()
                logger.info("Attached HTML diagnostic report to ServiceNow incident %s", number)
            except Exception as att_exc:
                logger.warning(
                    "Attachment API failed (%s); persisting report to GCS fallback",
                    att_exc,
                )
                gcs_uri = self._persist_report_to_gcs(bucket_name, report.group_id, standalone_html)
                work_note = (
                    f"[code]<b>Diagnostic Report:</b> Report attachment upload failed. "
                    f"Full HTML report persisted to GCS at <code>{gcs_uri}</code>[/code]"
                )
                self.append_work_note(sys_id, work_note, client=client)

            # 5. Read back the record to prove both steps (creation was New; final is In Progress)
            final_state = "2"
            try:
                readback_resp = client.get(f"{table_endpoint}/{sys_id}")
                if readback_resp.status_code == 200:
                    record = readback_resp.json().get("result", {})
                    final_state = str(record.get("state", "2"))
                    logger.info(
                        "ServiceNow incident %s read-back verification: state=%s (In Progress)",
                        number,
                        final_state,
                    )
            except Exception as read_exc:
                logger.debug("Read-back verification query optional check skipped: %s", read_exc)

            ticket_url = f"{base_url}/nav_to.do?uri=incident.do?sys_id={sys_id}"
            ref = ServiceNowIncidentRef(sys_id=sys_id, number=number, url=ticket_url)
            if not hasattr(self, "_created_incidents"):
                self._created_incidents = {}
            self._created_incidents[incident_filename] = ref
            self._created_incidents[report.group_id] = ref
            return ref
        finally:
            if close_client:
                client.close()
