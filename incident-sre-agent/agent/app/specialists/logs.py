from datetime import datetime
from typing import Any, List, Optional

from google.adk.agents import LlmAgent
from google.adk.tools.function_tool import FunctionTool

from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident
from ..prompts import load_prompt
from ..tools.logs import list_log_names, query_logs, summarize_error_groups
from .base import BaseSpecialist


class LogsSpecialist(BaseSpecialist):
    """Specialist analyzing Cloud Logging and Error Reporting."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        client: Any = None,
        model: Optional[str] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[Any] = None,
    ) -> None:
        if not model:
            if config and hasattr(config, "model_specialist"):
                model = config.model_specialist
            else:
                try:
                    from ..config import get_settings

                    model = get_settings().model_specialist
                except Exception:
                    model = ""

        adk_tools: list[Any] = [
            FunctionTool(query_logs),
            FunctionTool(summarize_error_groups),
            FunctionTool(list_log_names),
        ]
        self.adk_tools = adk_tools
        instruction = load_prompt("logs")
        agent = llm_agent or LlmAgent(
            name="logs_specialist",
            model=model or "default",
            instruction=instruction,
            tools=adk_tools,
            output_schema=Findings,
        )
        super().__init__(
            name="LogsSpecialist",
            description="Investigates application and system logs for error spikes.",
            project_id=project_id,
            client=client,
            llm_agent=agent,
            config=config,
        )

    def get_tools(self) -> List[Any]:
        return list(self.adk_tools)

    def run_investigation(
        self,
        incident: Incident,
        start_time: datetime,
        end_time: datetime,
        **kwargs: Any,
    ) -> Findings:
        queries_run: List[str] = []
        evidence: List[EvidenceItem] = []
        anomalies: List[str] = []
        errors: List[str] = []

        effective_service = incident.service_hint or (
            getattr(kwargs.get("plan"), "affected_service", None)
            if kwargs.get("plan")
            and getattr(kwargs.get("plan"), "affected_service", None) != "unknown-service"
            else None
        )
        service = effective_service or "all services"

        # 1. Summarize Error Groups
        try:
            time_window_hours = max(1, int((end_time - start_time).total_seconds() // 3600))
            eg_result = summarize_error_groups(
                project_id=self.project_id,
                time_window_hours=time_window_hours,
                start_time=start_time,
                end_time=end_time,
                service_name=effective_service,
                client=self.client,
            )
            queries_run.append(
                f"summarize_error_groups("
                f"start_time={start_time.isoformat()}, end_time={end_time.isoformat()})"
            )

            for grp in eg_result.get("error_groups", []):
                msg = grp.get("message", "")
                if not msg or msg.strip() == "{}":
                    msg = f"Error group {grp.get('group_id', 'unknown')}"
                count = grp.get("count", 0)
                anomalies.append(f"Error group: '{msg[:100]}' occurred {count} times")
                evidence.append(
                    self.create_evidence_item(
                        kind="error_group",
                        excerpt=f"Error group {grp.get('group_id')}: {msg} (count: {count})",
                        console_url=eg_result.get("console_url"),
                    )
                )
        except Exception as e:
            errors.append(f"Failed to query error groups: {str(e)}")

        # 2. Query Logs for Warning and Error Severity
        log_filter = "severity >= WARNING"
        if effective_service:
            from ..tools.common import get_candidate_service_names

            candidates = get_candidate_service_names(
                effective_service, getattr(self.config, "name_prefix", None)
            )
            svc_clauses = []
            for cand in candidates:
                svc_clauses.append(f'resource.labels.service_name = "{cand}"')
            for cand in candidates:
                svc_clauses.append(f'resource.labels.container_name = "{cand}"')
            for cand in candidates:
                svc_clauses.append(f'textPayload =~ "{cand}"')
            svc_clauses = list(dict.fromkeys(svc_clauses))
            log_filter = f"severity >= WARNING AND ({' OR '.join(svc_clauses)})"

        try:
            resource_names = [f"projects/{self.project_id}"]
            logs_result = query_logs(
                filter_str=log_filter,
                resource_names=resource_names,
                max_entries=50,
                start_time=start_time,
                end_time=end_time,
                client=self.client,
            )
            queries_run.append(logs_result.get("query_filter", log_filter))

            entries = logs_result.get("entries", [])
            for entry in entries[:15]:
                ts_val: Optional[datetime] = None
                raw_ts = entry.get("timestamp")
                if raw_ts:
                    try:
                        ts_val = datetime.fromisoformat(raw_ts)
                    except Exception:
                        ts_val = None

                payload_excerpt = entry.get("payload_excerpt", "")
                if not payload_excerpt or payload_excerpt.strip() == "{}":
                    payload_excerpt = "No error/warning payload details"

                sev = str(entry.get("severity", "INFO")).upper()
                kind = "warning_log" if sev == "WARNING" else "log_entry"

                evidence.append(
                    self.create_evidence_item(
                        kind=kind,
                        excerpt=(f"[{sev}] {payload_excerpt} (log: {entry.get('log_name')})"),
                        timestamp=ts_val,
                        console_url=logs_result.get("console_url"),
                    )
                )

            error_entries = [
                e for e in entries if str(e.get("severity", "")).upper() in ["ERROR", "CRITICAL"]
            ]
            warning_entries = [
                e for e in entries if str(e.get("severity", "")).upper() == "WARNING"
            ]

            if len(error_entries) > 0:
                # Extract observed HTTP statuses if available
                statuses = set()
                for entry_dict in error_entries:
                    exc = entry_dict.get("payload_excerpt", "")
                    if "HTTP " in exc:
                        parts = exc.split("HTTP ")
                        if len(parts) > 1:
                            statuses.add(parts[1].split()[0])
                status_suffix = f" (statuses: {', '.join(sorted(statuses))})" if statuses else ""
                anomalies.append(
                    f"Found {len(error_entries)} ERROR/CRITICAL log entries for "
                    f"{service}{status_suffix} between {start_time.isoformat()} and "
                    f"{end_time.isoformat()}"
                )

            # Extract warning log patterns into anomalies when relevant
            latency_warnings = [
                e
                for e in warning_entries
                if any(
                    k in str(e.get("payload_excerpt", "")).lower()
                    for k in ["delay", "latency", "slow", "timeout", "injecting"]
                )
            ]
            if latency_warnings:
                first_msg = latency_warnings[0].get("payload_excerpt", "")
                anomalies.append(
                    f"Application warning logs indicate latency spike / processing delay "
                    f"on {service}: {first_msg}"
                )
            elif warning_entries:
                anomalies.append(
                    f"Found {len(warning_entries)} WARNING log entries for {service} "
                    f"between {start_time.isoformat()} and {end_time.isoformat()}"
                )

        except Exception as e:
            errors.append(f"Failed to query logs: {str(e)}")

        confidence = 0.85 if anomalies else 0.40
        summary = (
            f"Logs analysis for {service}: found {len(anomalies)} anomalous patterns "
            f"and {len(evidence)} evidence items."
        )

        return self.create_findings(
            summary=summary,
            evidence=evidence,
            anomalies=anomalies,
            queries_run=queries_run,
            confidence=confidence,
            errors=errors,
        )
