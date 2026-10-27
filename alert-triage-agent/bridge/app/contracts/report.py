from datetime import datetime, timezone
import logging
import re
from typing import Any, Optional, Union
from pydantic import BaseModel, Field, field_validator

from .links import ConsoleLinkBuilder

logger = logging.getLogger(__name__)


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


# ServiceNow short_description: '[<P>] <origin>: <title problem> (<N> alerts)'.
TITLE_MAX_LENGTH = 160
# Strict cap on the title problem phrase. The full problem sentence goes in the description.
TITLE_PROBLEM_MAX_LENGTH = 60

# Alert signal label (policy user label 'signal') -> short title problem phrase.
SIGNAL_TITLE_PROBLEMS: dict[str, str] = {
    "5xx": "Elevated HTTP 5xx error rate",
    "latency": "Elevated latency",
    "rate-limit": "Rate limit exceeded (HTTP 429)",
    "connections": "Database connection saturation",
    "cpu": "High CPU utilization",
}

# Report issue_type -> short title problem phrase (used when no signal is known).
ISSUE_TYPE_TITLE_PROBLEMS: dict[str, str] = {
    "runtime_exhaustion": "Resource exhaustion",
    "change_induced": "Deployment regression",
    "latency_spike": "Elevated latency",
    "runtime_anomaly": "Telemetry breach",
}

DEFAULT_TITLE_PROBLEM = "Anomalous telemetry breach detected"

# Work note: at most this many observation sub-bullets, then '...and N more (see attached report)'.
WORK_NOTE_MAX_OBSERVATIONS = 6


def split_observations(observations: Optional[str]) -> list[str]:
    """Split the ' | '-joined observations string into individual, non-empty items."""
    if not observations:
        return []
    return [o.strip() for o in re.split(r"\s+\|\s+", observations) if o.strip()]


_CODE_TAG_RE = re.compile(r"\[(/?code)\]", re.IGNORECASE)


def _html_text(text: str) -> str:
    """Escape text for the work-note HTML inside ServiceNow's [code] block.

    HTML special characters are escaped, and a literal [code] / [/code] in the text is
    rewritten with entity brackets so it cannot end the block early.
    """
    escaped = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _CODE_TAG_RE.sub(lambda m: f"&#91;{m.group(1)}&#93;", escaped)


_PLACEHOLDER_TITLE_PROBLEMS = ("none", "n/a", "unknown", "under investigation")


def normalize_title_problem(value: Optional[str], *, warn_on_reject: bool = False) -> Optional[str]:
    """Return a clean single-line title problem, or None when unusable.

    None is returned for empty text, placeholders and text longer than
    TITLE_PROBLEM_MAX_LENGTH, so the title falls back to the signal-derived phrase instead
    of truncating a sentence. With ``warn_on_reject`` a rejected non-empty value is logged
    at WARNING with the reason and the value; None input stays quiet (older agents send none).
    """
    if value is None:
        return None
    text = clean_problem_text(str(value)).replace("*", "").replace("`", "")
    text = re.sub(r"\s+", " ", text).strip()
    reason: Optional[str] = None
    if not text:
        reason = "empty after cleaning"
    elif text.lower() in _PLACEHOLDER_TITLE_PROBLEMS:
        reason = "placeholder value"
    elif len(text) > TITLE_PROBLEM_MAX_LENGTH:
        reason = f"len={len(text)} > {TITLE_PROBLEM_MAX_LENGTH}"
    if reason is not None:
        if warn_on_reject and str(value).strip():
            logger.warning(
                "title_problem rejected (%s): %r; deriving the title problem from the alert signal",
                reason,
                str(value)[:120],
            )
        return None
    return text


def title_problem_for_signal(signal: Optional[str]) -> Optional[str]:
    """Map an alert signal label (e.g. '5xx') to its short title problem phrase."""
    if not signal:
        return None
    return SIGNAL_TITLE_PROBLEMS.get(str(signal).strip().lower())


def format_alert_count(n_alerts: int) -> str:
    """Return '(1 alert)' or '(<N> alerts)'."""
    return f"({n_alerts} alert{'' if n_alerts == 1 else 's'})"


def _truncate_at_word(text: str, limit: int) -> str:
    """Cut text to at most ``limit`` characters on a word boundary (never mid-word, no '...')."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    cut = text[: limit + 1]
    space = cut.rfind(" ")
    if space <= 0:
        return ""
    return clean_problem_text(cut[:space])


class InvestigationReport(BaseModel):
    """Investigation Report data contract."""
    group_id: str
    filename: str = ""
    summary: str
    # Short problem phrase for the ServiceNow title, at most TITLE_PROBLEM_MAX_LENGTH chars.
    # Over-long or empty values are dropped (None) and the title falls back to the alert signal.
    title_problem: Optional[str] = None
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

    @field_validator("title_problem", mode="before")
    @classmethod
    def _validate_title_problem(cls, value: Any) -> Optional[str]:
        return normalize_title_problem(value, warn_on_reject=True)

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
        def _val(value: Any) -> str:
            return _html_text(str(value).replace("**", "").replace("*", ""))

        if comp:
            comp_sig_parts.append(f"Component: <code>{_val(comp)}</code>")
        if sig:
            comp_sig_parts.append(f"Signal: <code>{_val(sig)}</code>")

        # Systems and Observations are rendered as sub-bullets, one item per line.
        # Every interpolated value is escaped; only the markup around it is literal HTML.
        sys_items = [f"Origin: <code>{_val(self.origin)}</code> ({conf_pct}% confidence)"]
        if comp_sig_parts:
            sys_items.append(", ".join(comp_sig_parts))
        sys_items.append(f"Blast Radius: {_val(blast_str)}")

        obs_items = [
            _html_text(o.replace("**", "").replace("*", "")) for o in split_observations(self.observations)
        ]
        hidden = len(obs_items) - WORK_NOTE_MAX_OBSERVATIONS
        if hidden > 0:
            obs_items = obs_items[:WORK_NOTE_MAX_OBSERVATIONS]
            obs_items.append(f"...and {hidden} more (see attached report)")

        def _sub_list(items: list[str]) -> str:
            if not items:
                return " None recorded"
            lis = "\n".join(f"      <li>{i}</li>" for i in items)
            return f"\n    <ul>\n{lis}\n    </ul>"

        # Clean any raw asterisks or markdown formatting, then escape.
        prob_text = _html_text(prob_text.replace("**", "").replace("*", ""))

        actions = self.get_normalized_actions()
        action_items_html: list[str] = []
        rollback_notes: list[str] = []
        code_style = "background:#f4f4f4;border:1px solid #ddd;padding:6px;font-family:monospace;display:block"

        for act in actions:
            if act.rollback and not act.command and (not act.title or act.title.lower() == "rollback"):
                rollback_notes.append(_html_text(act.rollback.replace("**", "").replace("*", "")))
                continue

            title_part = _html_text((act.title or "").replace("**", "").replace("*", ""))
            verif_part = f" (Verification: {_html_text(act.verification)})" if act.verification else ""
            cmd_html = ""
            if act.command:
                escaped_cmd = _html_text(act.command)
                cmd_html = f'\n        <pre><code style="{code_style}">{escaped_cmd}</code></pre>'

            if act.rollback and act.title.lower() != "rollback":
                rollback_notes.append(_html_text(act.rollback.replace("**", "").replace("*", "")))

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
            f"  <li><b>Systems:</b>{_sub_list(sys_items)}</li>\n"
            f"  <li><b>Observations:</b>{_sub_list(obs_items)}</li>\n"
            f"  <li><b>Recommended Actions:</b>{recs_inner}</li>\n"
            "</ul>\n"
            "[/code]"
        )

    def alert_count(self) -> int:
        """Number of alerts in the incident group carried by this report (at least 1)."""
        alerts_list = self.alerts or self.raw_metadata.get("alerts") or []
        ids: list[str] = []
        anonymous = 0
        for a in alerts_list if isinstance(alerts_list, list) else []:
            inc_id = a.get("incident_id") if isinstance(a, dict) else getattr(a, "incident_id", None)
            if inc_id:
                if inc_id not in ids:
                    ids.append(inc_id)
            else:
                anonymous += 1
        return max(1, len(ids) + anonymous)

    def _alert_signal(self) -> Optional[str]:
        """Signal label of the origin's alert (else the first alert, else raw_metadata)."""
        alerts_list = self.alerts or self.raw_metadata.get("alerts") or []
        dict_alerts = [a for a in alerts_list if isinstance(a, dict)] if isinstance(alerts_list, list) else []
        ordered = [a for a in dict_alerts if (a.get("component") or a.get("resource_name") or a.get("resource")) == self.origin]
        ordered += [a for a in dict_alerts if a not in ordered]
        for a in ordered:
            labels = a.get("labels") or {}
            pul = a.get("policy_user_labels") or {}
            sig = a.get("signal") or labels.get("signal") or pul.get("signal")
            if sig:
                return str(sig)
        for a in ordered:
            policy = str(a.get("policy_name") or a.get("policy") or "").lower()
            for key in SIGNAL_TITLE_PROBLEMS:
                if policy.endswith(f"-{key}"):
                    return key
        rm = self.raw_metadata
        return rm.get("signal") or (rm.get("labels") or {}).get("signal") or (rm.get("policy_user_labels") or {}).get("signal")

    def infer_issue_type(self) -> str:
        """issue_type of the report, else inferred from summary keywords."""
        issue_type = self.issue_type or self.raw_metadata.get("issue_type")
        if issue_type:
            return issue_type
        s_lower = (self.summary or "").lower()
        if any(k in s_lower for k in ["conn", "pool", "exhaust", "saturation"]):
            return "runtime_exhaustion"
        if any(k in s_lower for k in ["deploy", "revision", "release"]):
            return "change_induced"
        if "timeout" in s_lower or "latency" in s_lower:
            return "latency_spike"
        return "runtime_anomaly"

    def resolve_title_problem(self) -> str:
        """Short problem phrase for the title: title_problem, else alert signal, else issue_type."""
        if self.title_problem:
            return self.title_problem
        signal = self._alert_signal()
        from_signal = title_problem_for_signal(signal)
        if from_signal:
            logger.info(
                "Title problem for group %s: no title_problem, using alert signal %r -> %r",
                self.group_id,
                signal,
                from_signal,
            )
            return from_signal
        explicit = self.issue_type or self.raw_metadata.get("issue_type")
        if explicit in ISSUE_TYPE_TITLE_PROBLEMS:
            return self._title_fallback("issue_type", ISSUE_TYPE_TITLE_PROBLEMS[explicit], signal)
        # A summary that is already short is used as is; a sentence is never truncated.
        from_summary = normalize_title_problem(self.summary)
        if from_summary:
            return self._title_fallback("short summary", from_summary, signal)
        inferred = self.infer_issue_type()
        if inferred != "runtime_anomaly":
            return self._title_fallback(
                f"issue_type inferred from summary ({inferred})", ISSUE_TYPE_TITLE_PROBLEMS[inferred], signal
            )
        return self._title_fallback("default phrase", DEFAULT_TITLE_PROBLEM, signal)

    def _title_fallback(self, branch: str, phrase: str, signal: Optional[str]) -> str:
        """Log (WARNING) that the title problem came from a fallback branch, then return it."""
        logger.warning(
            "Title problem for group %s taken from fallback branch '%s' -> %r "
            "(no usable title_problem; alert signal %r has no title phrase)",
            self.group_id,
            branch,
            phrase,
            signal,
        )
        return phrase

    def build_short_description(self, max_length: int = TITLE_MAX_LENGTH) -> str:
        """Format the ServiceNow title: '[<Priority>] <origin>: <short problem> (<N> alerts)'.

        Example: '[P1] <origin>: Elevated HTTP 5xx error rate (2 alerts)'.
        The problem is a short phrase (see resolve_title_problem), never the synthesized
        sentence; the full problem is in the description. The alert count suffix is always
        kept, and anything shortened is cut on a word boundary.
        """
        prefix = f"[{self.priority}] {self.origin}"
        suffix = " " + format_alert_count(self.alert_count())
        problem = self.resolve_title_problem()

        allowed = max_length - len(prefix) - len(": ") - len(suffix)
        problem = _truncate_at_word(problem, allowed)
        if problem:
            return f"{prefix}: {problem}{suffix}"
        # No room for the problem: keep priority, origin and count.
        if len(prefix) + len(suffix) > max_length:
            prefix = prefix[: max(0, max_length - len(suffix))].rstrip()
        return f"{prefix}{suffix}"

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

        issue_type = self.infer_issue_type()

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
                # Drop bracketed tags, but keep redaction tokens and log timestamps: removing
                # them garbled log excerpts ('[REDACTED_IP]:50318' became ':50318').
                cleaned_o = re.sub(r"\[(?!REDACTED_)(?!\d{4}-\d{2}-\d{2}T)[^\]]*\]\s*", "", o).strip()
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

        # Topology path(s) computed by the agent from TOPOLOGY.md. Without one the line is
        # omitted: the blast radius is an unordered set and cannot stand in for a path.
        dep_path = self.dependency_path or self.raw_metadata.get("dependency_path")
        dep_path_line = f"  - Dependency Path: {dep_path}\n" if dep_path else ""

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
            f"{dep_path_line}"
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

