from datetime import datetime, timezone
import re
from typing import Any, Optional, Union
from pydantic import BaseModel, Field

from .links import ConsoleLinkBuilder


class ProbableRootCause(BaseModel):
    text: str
    confidence: float = 0.90


class TimelineItem(BaseModel):
    timestamp: str
    description: str
    event_type: Optional[str] = "Event"


class EvidenceItem(BaseModel):
    headline: str
    content: str


class ActionItem(BaseModel):
    title: str = ""
    command: Optional[str] = None
    verification: Optional[str] = None
    rollback: Optional[str] = None


def balance_brackets(text: str) -> str:
    """Remove unbalanced parentheses and brackets from text."""
    # Balance '(' and ')'
    stack = []
    remove_indices = set()
    for i, ch in enumerate(text):
        if ch == "(":
            stack.append(i)
        elif ch == ")":
            if stack:
                stack.pop()
            else:
                remove_indices.add(i)
    remove_indices.update(stack)

    # Balance '[' and ']'
    b_stack = []
    for i, ch in enumerate(text):
        if i in remove_indices:
            continue
        if ch == "[":
            b_stack.append(i)
        elif ch == "]":
            if b_stack:
                b_stack.pop()
            else:
                remove_indices.add(i)
    remove_indices.update(b_stack)

    # Balance '{' and '}'
    c_stack = []
    for i, ch in enumerate(text):
        if i in remove_indices:
            continue
        if ch == "{":
            c_stack.append(i)
        elif ch == "}":
            if c_stack:
                c_stack.pop()
            else:
                remove_indices.add(i)
    remove_indices.update(c_stack)

    return "".join(ch for i, ch in enumerate(text) if i not in remove_indices)


def clean_problem_text(problem: str) -> str:
    """Clean problem text by removing monitoring labels, policies, dangling brackets, and trailing punctuation."""
    if not problem:
        return ""

    # Strip monitoring labels: labels{...}
    problem = re.sub(r"labels\s*\{[^}]*\}?", "", problem)

    # Strip GCP alert policy paths (numeric and identifier forms)
    problem = re.sub(r"projects/[^/]+/alertPolicies/[^\s,)}]+", "", problem)
    problem = re.sub(r"projects/[^/]+/alertPolicies/?", "", problem)

    # Strip URLs
    problem = re.sub(r"https?://\S+", "", problem)

    # Repeatedly remove empty bracket pairs like (), [], {}, (  ), etc.
    while True:
        new_prob = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", problem)
        if new_prob == problem:
            break
        problem = new_prob

    # Balance any leftover open/close brackets
    problem = balance_brackets(problem)

    # Repeatedly remove empty bracket pairs again in case balancing created any
    while True:
        new_prob = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", problem)
        if new_prob == problem:
            break
        problem = new_prob

    # Collapse multiple whitespaces
    problem = re.sub(r"\s+", " ", problem).strip()

    # Strip trailing punctuation and dangling opening brackets/parentheses
    problem = re.sub(r"[\s.,:;_\-/([{]+$", "", problem).strip()

    return problem


class InvestigationReport(BaseModel):
    """Investigation Report data contract."""
    group_id: str
    filename: str = ""
    summary: str
    observations: str
    recommended_actions: list[Union[ActionItem, dict[str, Any], str]] = Field(default_factory=list)
    probable_root_cause: ProbableRootCause = Field(
        default_factory=lambda: ProbableRootCause(text="Under investigation", confidence=0.85)
    )
    origin: str = "unknown"
    blast_radius: list[str] = Field(default_factory=list)
    priority: str = "P2"
    priority_rationale: Optional[str] = None
    status: str = "In Progress"
    issue_type: Optional[str] = None
    change_induced: Optional[str] = None
    first_seen: Optional[str] = None
    last_alert: Optional[str] = None
    report_generated_at: Optional[str] = None
    project_id: str = "cloud-assist-fde-2"
    region: str = "us-west1"
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    triage_decision: dict[str, Any] = Field(default_factory=dict)
    dependency_path: Optional[str] = None
    topology_nodes: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[TimelineItem] = Field(default_factory=list)
    evidence_by_area: dict[str, list[EvidenceItem]] = Field(default_factory=dict)
    metrics_comparison: list[dict[str, Any]] = Field(default_factory=list)
    top_log_signatures: list[dict[str, Any]] = Field(default_factory=list)
    apigee_analytics: dict[str, Any] = Field(default_factory=dict)
    revision_diffs: list[dict[str, Any]] = Field(default_factory=list)
    root_cause_analysis: dict[str, Any] = Field(default_factory=dict)
    longer_term_fixes: list[str] = Field(default_factory=list)
    queries_appendix: dict[str, list[str]] = Field(default_factory=dict)
    tools_used: list[str] = Field(default_factory=list)
    skills_loaded: list[str] = Field(default_factory=list)
    agent_run_metadata: dict[str, Any] = Field(default_factory=dict)
    servicenow_sys_id: Optional[str] = None
    servicenow_number: Optional[str] = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    def get_normalized_actions(self) -> list[ActionItem]:
        """Normalize recommended actions into structured ActionItem instances."""
        normalized: list[ActionItem] = []
        for item in self.recommended_actions:
            if isinstance(item, ActionItem):
                normalized.append(item)
            elif isinstance(item, dict):
                normalized.append(ActionItem(**item))
            elif isinstance(item, str):
                text = item.strip()
                if not text:
                    continue
                if text.lower().startswith("rollback:"):
                    rb_text = text[len("rollback:"):].strip()
                    normalized.append(ActionItem(title="Rollback", rollback=rb_text))
                    continue

                verif = None
                m_verif = re.search(r"\(verification:\s*([^)]+)\)", text, re.IGNORECASE)
                if m_verif:
                    verif = m_verif.group(1).strip()
                    text = re.sub(r"\(verification:\s*[^)]+\)", "", text, flags=re.IGNORECASE).strip()

                cmd = None
                title = text
                if text.startswith("gcloud ") or text.startswith("kubectl ") or text.startswith("curl "):
                    cmd = text
                    title = "Execute CLI command"
                elif text.startswith("SELECT ") or text.startswith("UPDATE ") or text.startswith("ALTER "):
                    cmd = text
                    title = "Execute SQL statement"
                elif ": gcloud " in text:
                    parts = text.split(": gcloud ", 1)
                    title = parts[0].strip()
                    cmd = "gcloud " + parts[1].strip()
                elif ": SELECT " in text:
                    parts = text.split(": SELECT ", 1)
                    title = parts[0].strip()
                    cmd = "SELECT " + parts[1].strip()

                normalized.append(ActionItem(title=title, command=cmd, verification=verif))
        return normalized

    def build_executive_summary_bullets(self) -> list[str]:
        """Format the standardized 4-bullet executive summary."""
        conf_pct = int(self.probable_root_cause.confidence * 100) if self.probable_root_cause else 90
        blast_str = ", ".join(self.blast_radius) if self.blast_radius else "None detected"
        actions = self.get_normalized_actions()
        recs_parts: list[str] = []
        for a in actions:
            p = a.title or ""
            if a.command:
                p = f"{p}: `{a.command}`" if p else f"`{a.command}`"
            if a.verification:
                p += f" (Verification: {a.verification})"
            if a.rollback and a.title.lower() != "rollback":
                p += f" [Rollback: {a.rollback}]"
            elif a.rollback and not a.command and not a.title:
                p = f"Rollback: {a.rollback}"
            recs_parts.append(p)
        recs_str = "; ".join(recs_parts) if recs_parts else "Monitor telemetry"
        return [
            f"* **Problem:** {self.summary} (Severity: {self.priority})",
            f"* **Systems:** Origin: `{self.origin}` ({conf_pct}% confidence) | Blast Radius: {blast_str}",
            f"* **Observations:** {self.observations}",
            f"* **Recommended Actions:** {recs_str}",
        ]

    def build_executive_summary_html(self) -> str:
        """Format 4-bullet executive summary inside [code] HTML tags for ServiceNow work_notes."""
        conf_pct = int(self.probable_root_cause.confidence * 100) if self.probable_root_cause else 90
        blast_str = ", ".join(self.blast_radius) if self.blast_radius else "None detected"

        prob_text = f"{self.summary} (Severity: {self.priority})"

        primary_alert = self.alerts[0] if self.alerts else {}
        comp = None
        sig = None
        if isinstance(primary_alert, dict):
            comp = primary_alert.get("component")
            sig = primary_alert.get("signal")
        elif hasattr(primary_alert, "component"):
            comp = getattr(primary_alert, "component", None)
            sig = getattr(primary_alert, "signal", None)

        if not comp:
            comp = self.raw_metadata.get("component") or (self.raw_metadata.get("labels") or {}).get("component") or (self.raw_metadata.get("policy_user_labels") or {}).get("component")
        if not sig:
            sig = self.raw_metadata.get("signal") or (self.raw_metadata.get("labels") or {}).get("signal") or (self.raw_metadata.get("policy_user_labels") or {}).get("signal")

        comp_sig_parts = []
        if comp:
            comp_sig_parts.append(f"Component: <code>{comp}</code>")
        if sig:
            comp_sig_parts.append(f"Signal: <code>{sig}</code>")
        comp_sig_str = f" | {' | '.join(comp_sig_parts)}" if comp_sig_parts else ""

        sys_text = f"Origin: <code>{self.origin}</code> ({conf_pct}% confidence){comp_sig_str} | Blast Radius: {blast_str}"
        obs_text = self.observations

        # Clean any raw asterisks or markdown formatting
        prob_text = prob_text.replace("**", "").replace("*", "")
        sys_text = sys_text.replace("**", "").replace("*", "")
        obs_text = obs_text.replace("**", "").replace("*", "")

        actions = self.get_normalized_actions()
        action_items_html: list[str] = []
        rollback_notes: list[str] = []
        code_style = "background:#f4f4f4;border:1px solid #ddd;padding:6px;font-family:monospace;display:block"

        for act in actions:
            if act.rollback and not act.command and (not act.title or act.title.lower() == "rollback"):
                rollback_notes.append(act.rollback.replace("**", "").replace("*", ""))
                continue

            title_part = (act.title or "").replace("**", "").replace("*", "")
            verif_part = f" (Verification: {act.verification})" if act.verification else ""
            cmd_html = ""
            if act.command:
                escaped_cmd = act.command.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                cmd_html = f'\n        <pre><code style="{code_style}">{escaped_cmd}</code></pre>'

            if act.rollback and act.title.lower() != "rollback":
                rollback_notes.append(act.rollback.replace("**", "").replace("*", ""))

            action_items_html.append(f"      <li>{title_part}{verif_part}{cmd_html}</li>")

        if action_items_html:
            recs_inner = "\n    <ol>\n" + "\n".join(action_items_html) + "\n    </ol>"
        else:
            recs_inner = " Monitor telemetry"

        if rollback_notes:
            rb_joined = "; ".join(rollback_notes)
            recs_inner += f'\n    <div style="margin-top:6px"><b>Rollback:</b> {rb_joined}</div>'

        return (
            "[code]\n"
            "<p><b>Executive Summary:</b></p>\n"
            "<ul>\n"
            f"  <li><b>Problem:</b> {prob_text}</li>\n"
            f"  <li><b>Systems:</b> {sys_text}</li>\n"
            f"  <li><b>Observations:</b> {obs_text}</li>\n"
            f"  <li><b>Recommended Actions:</b>{recs_inner}</li>\n"
            "</ul>\n"
            "[/code]"
        )

    def build_short_description(self, max_length: int = 160) -> str:
        """Format clean short_description: '[<Priority>] <canonical-origin>: <problem in plain words> (<N> alerts)'."""
        n_alerts = len(self.alerts) if self.alerts else len(self.raw_metadata.get("alerts", []))
        if n_alerts == 0:
            n_alerts = max(1, len([t for t in self.timeline if "alert" in t.description.lower()]) or 1)

        raw_problem = self.summary or ""
        problem = clean_problem_text(raw_problem)
        if not problem or problem.lower() in ["none", "n/a", "unknown"]:
            signal = None
            if self.issue_type:
                issue_map = {
                    "runtime_exhaustion": "Resource exhaustion",
                    "change_induced": "Deployment regression",
                    "latency_spike": "Elevated latency",
                    "runtime_anomaly": "Telemetry breach",
                }
                signal = issue_map.get(self.issue_type)
            if not signal and self.probable_root_cause and self.probable_root_cause.text:
                rc_text = clean_problem_text(self.probable_root_cause.text)
                if rc_text and rc_text.lower() not in ["under investigation", "unknown"]:
                    signal = rc_text
            if not signal:
                signal = "Anomalous telemetry breach detected"
            problem = signal

        prefix = f"[{self.priority}] {self.origin}: "
        suffix = f" ({n_alerts} alert{'s' if n_alerts != 1 else ''})"

        allowed_len = max_length - len(prefix) - len(suffix)
        if allowed_len <= 3:
            problem = problem[:max(0, allowed_len)]
        elif len(problem) > allowed_len:
            truncated = problem[: allowed_len - 3]
            truncated = clean_problem_text(truncated)
            problem = truncated + "..."
            while len(problem) > allowed_len and len(truncated) > 0:
                truncated = truncated[:-1]
                truncated = clean_problem_text(truncated)
                problem = truncated + "..."

        return f"{prefix}{problem}{suffix}"

    def build_structured_description(
        self,
        project_id: str = "cloud-assist-fde-2",
        incidents_bucket: Optional[str] = None,
    ) -> str:
        """Format ServiceNow plain-text description: labels on own lines, - or numbered sub-items, ZERO * and ZERO #."""
        bucket = incidents_bucket or f"{project_id}-ata-incidents"
        filename = self.filename or f"incident-{self.group_id}.md"

        rationale = self.priority_rationale or self.raw_metadata.get("priority_rationale")
        if not rationale:
            if self.priority == "P1":
                rationale = "Critical threshold breach on core service path"
            else:
                rationale = "Non-critical anomalous threshold deviation"

        blast_str = ", ".join(self.blast_radius) if self.blast_radius else self.origin

        issue_type = self.issue_type or self.raw_metadata.get("issue_type")
        if not issue_type:
            s_lower = (self.summary or "").lower()
            if any(k in s_lower for k in ["conn", "pool", "exhaust", "saturation"]):
                issue_type = "runtime_exhaustion"
            elif any(k in s_lower for k in ["deploy", "revision", "release"]):
                issue_type = "change_induced"
            elif "timeout" in s_lower or "latency" in s_lower:
                issue_type = "latency_spike"
            else:
                issue_type = "runtime_anomaly"

        change_ind = self.change_induced or self.raw_metadata.get("change_induced") or "No"

        all_ts: list[str] = []
        if self.timeline:
            all_ts.extend(t.timestamp for t in self.timeline if t.timestamp)
        alerts_list = self.alerts or self.raw_metadata.get("alerts", [])
        if isinstance(alerts_list, list):
            for a in alerts_list:
                if isinstance(a, dict) and a.get("started_at"):
                    all_ts.append(a["started_at"])

        first_seen = min(all_ts) if all_ts else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        last_alert = max(all_ts) if all_ts else first_seen

        builder = ConsoleLinkBuilder(project_id=project_id, region=getattr(self, "region", "us-west1"))

        alert_lines = []
        if isinstance(alerts_list, list) and alerts_list:
            for a in alerts_list:
                if isinstance(a, dict):
                    pol = a.get("policy") or self.summary
                    res = a.get("resource") or self.origin
                    inc_id = a.get("incident_id") or "0.unknown"
                    st = a.get("state") or "open"
                    sa = a.get("started_at") or first_seen
                    url = a.get("url") or builder.build_incident_url(inc_id)
                    alert_lines.append(f"  - {pol} | {res} | {inc_id} | {st} | {sa} | {url}")
        else:
            inc_id = self.raw_metadata.get("incident_id", "0.unknown")
            url = self.raw_metadata.get("url") or builder.build_incident_url(inc_id)
            alert_lines.append(f"  - {self.summary} | {self.origin} | {inc_id} | open | {first_seen} | {url}")

        alert_list_str = "\n".join(alert_lines)

        # Parse observations into clean bullet items
        obs_lines = []
        if self.observations:
            raw_obs = [o.strip() for o in re.split(r"\s*\|\s*|\s*;\s*", self.observations) if o.strip()]
            for o in raw_obs:
                cleaned_o = re.sub(r"\[.*?\]\s*", "", o).strip()
                if cleaned_o:
                    obs_lines.append(f"  - {cleaned_o}")
        if not obs_lines:
            obs_lines.append(f"  - Anomalous telemetry detected on {self.origin}")
        obs_str = "\n".join(obs_lines)

        # Format recommended actions with numbers, indented command lines, and rollback note
        action_lines = []
        rollback_lines = []
        actions = self.get_normalized_actions()
        for idx, act in enumerate(actions, 1):
            if act.rollback and not act.command and (not act.title or act.title.lower() == "rollback"):
                rollback_lines.append(f"  - Rollback: {act.rollback}")
                continue
            title_part = act.title or "Action"
            verif_part = f" (Verification: {act.verification})" if act.verification else ""
            action_lines.append(f"  {idx}. {title_part}{verif_part}")
            if act.command:
                action_lines.append(f"       $ {act.command}")
            if act.rollback and act.title.lower() != "rollback":
                rollback_lines.append(f"  - Rollback: {act.rollback}")

        if not action_lines:
            action_lines.append(f"  1. Review system telemetry and recent logs for {self.origin} (Verification: Confirm error rates return to normal)")
        if not rollback_lines:
            rollback_lines.append("  - Rollback: Revert latest configuration or revision change if degradation persists after verification.")

        recs_str = "\n".join(action_lines) + "\n" + "\n".join(rollback_lines)

        # Determine dependency path if available
        dep_path = self.raw_metadata.get("dependency_path") or self.dependency_path
        if not dep_path:
            if self.blast_radius and len(self.blast_radius) > 1:
                dep_path = " -> ".join(reversed(self.blast_radius))
            else:
                dep_path = f"Client -> Ingress -> {self.origin}"

        origin_type = "Cloud SQL PostgreSQL Instance" if "db" in self.origin else ("Apigee API Proxy" if "v1" in self.origin or "apigee" in self.origin else "Cloud Run Service")

        text = (
            f"Incident Group ID: {self.group_id}\n"
            f"Priority: {self.priority} (Rationale: {rationale})\n"
            f"Origin: {self.origin}\n"
            f"Blast Radius: {blast_str}\n"
            f"Issue Type: {issue_type}\n"
            f"Change-Induced: {change_ind}\n"
            f"First Seen / Last Alert (UTC): {first_seen} / {last_alert}\n"
            f"Alert List:\n"
            f"{alert_list_str}\n"
            f"GCP Project: {project_id}\n"
            f"Incident File: gs://{bucket}/knowledge/incident-manager/{filename}\n"
            f"\n"
            f"----------------------------------------------------------------------\n"
            f"Executive Summary:\n"
            f"\n"
            f"Problem:\n"
            f"  - {self.summary}\n"
            f"Systems:\n"
            f"  - Origin: {self.origin} [{origin_type}]\n"
            f"  - Blast Radius: {blast_str}\n"
            f"  - Dependency Path: {dep_path}\n"
            f"Observations:\n"
            f"{obs_str}\n"
            f"Recommended Actions:\n"
            f"{recs_str}\n"
            f"----------------------------------------------------------------------\n"
            f"Full diagnostic report attached as HTML (ata-diagnostic-report-{self.group_id}.html)."
        )

        # Strictly enforce ZERO markdown asterisks (*) and ZERO hashes (#)
        text = text.replace("*", "").replace("#", "").replace("`", "")
        return text

    def build_description(
        self,
        project_id: str = "cloud-assist-fde-2",
        incidents_bucket: Optional[str] = None,
    ) -> str:
        """Format the ticket long description using clean plain-text standard."""
        return self.build_structured_description(project_id=project_id, incidents_bucket=incidents_bucket)

