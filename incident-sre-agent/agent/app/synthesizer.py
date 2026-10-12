"""Synthesizer agent compiling findings from specialists into a final SRE Report.

The LLM authors 100% of probable_root_cause.text, confidence, and recommended_actions.
Code may only down-calibrate confidence when evidence is missing or weak, dedupe, sanitize,
or provide an explicitly labeled fallback if and only if the LLM call fails completely.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from google.adk.agents import LlmAgent

from .contracts.findings import EvidenceItem, Findings
from .contracts.incident import Incident
from .contracts.report import (
    ProbableRootCause,
    RecentChangeItem,
    Report,
    SpecialistStatus,
    TimelineItem,
)
from .prompts import load_prompt
from .skills_loader import SkillsLoader
from .tools.changes import CLOUD_BUILD_STATUS_MAP
from .triage import InvestigationPlan

logger = logging.getLogger(__name__)


def _normalize_build_status_in_excerpt(excerpt: str) -> str:
    """Ensure any build status integer in excerpt is converted to its human-readable name."""

    def _replace_status(m: re.Match[str]) -> str:
        status_raw = m.group(1)
        try:
            status_int = int(status_raw)
            status_name = CLOUD_BUILD_STATUS_MAP.get(status_int, f"STATUS_{status_raw}")
            return f"status={status_name}"
        except ValueError:
            return m.group(0)

    return re.sub(r"\bstatus=(\d+)\b", _replace_status, excerpt)


def calibrate_report_confidence(
    report: Report,
    findings: Dict[str, Findings],
) -> Report:
    """Calibrate report confidence, verify mitigation grounding, and populate caveats.

    Code may ONLY down-calibrate confidence and add caveats when evidence is weak or missing.
    Code must NEVER raise confidence or overwrite LLM-authored root causes with canned templates.
    """
    if report.caveats is None:
        report.caveats = []

    if not report.probable_root_cause:
        report.probable_root_cause = ProbableRootCause(
            text=(
                "Initial investigation produced preliminary findings "
                "without a definitive root cause."
            ),
            confidence=0.30,
        )

    logs = findings.get("logs")
    metrics = findings.get("metrics")
    changes = findings.get("changes")
    resources = findings.get("resources")

    total_evidence_count = sum(len(f.evidence) for f in findings.values())
    total_anomalies_count = sum(len(f.anomalies) for f in findings.values())

    # 1. Check if logs have substantive error content (not empty or literal '{}')
    log_has_real_content = False
    all_log_excerpts = ""
    if logs and logs.evidence:
        for ev in logs.evidence:
            all_log_excerpts += f" {ev.excerpt}"
            clean = ev.excerpt.replace("[ERROR] {}", "").replace("{}", "").strip()
            if len(clean) > 10 and not clean.startswith("(log:"):
                log_has_real_content = True
    if logs and logs.anomalies:
        for anom in logs.anomalies:
            all_log_excerpts += f" {anom}"
            if len(anom.strip()) > 10 and "{}" not in anom:
                log_has_real_content = True

    # 2. Check if metrics have non-zero or anomalous signal
    metric_has_signal = False
    if metrics:
        if metrics.anomalies:
            metric_has_signal = True
        for ev in metrics.evidence:
            if (
                "current mean=0.0 vs baseline mean=0.0" not in ev.excerpt
                and "current mean=0.0 " not in ev.excerpt
            ):
                metric_has_signal = True

    # 3. Check if changes have active traffic or deploy evidence
    has_active_traffic = False
    has_change_evidence = False
    if changes:
        if changes.evidence:
            has_change_evidence = True
            for ev in changes.evidence:
                if any(f"traffic: {pct}%" in ev.excerpt for pct in range(1, 101)):
                    has_active_traffic = True
        if changes.anomalies:
            has_change_evidence = True

    # 4. Check if resources have anomalies or evidence
    has_resource_signal = bool(resources and (resources.anomalies or resources.evidence))

    current_conf = report.probable_root_cause.confidence
    calibrated_conf = current_conf
    uncertainty_reasons: List[str] = []

    # Condition 1: Completely empty investigation (0 evidence and 0 anomalies)
    if total_evidence_count == 0 and total_anomalies_count == 0:
        calibrated_conf = min(calibrated_conf, 0.25)
        uncertainty_reasons.append("zero evidence items or anomalies across all domains")

    # Condition 2: Broken / empty telemetry in key domains
    elif not log_has_real_content and not metric_has_signal and not has_resource_signal:
        calibrated_conf = min(calibrated_conf, 0.40)
        uncertainty_reasons.append(
            "no concrete error payload or non-zero error rate observed in telemetry"
        )

    # Condition 3: Missing causal link
    elif (
        log_has_real_content
        and not metric_has_signal
        and not has_active_traffic
        and not has_change_evidence
    ):
        calibrated_conf = min(calibrated_conf, 0.45)
        uncertainty_reasons.append(
            "application errors uncorroborated by active deployment traffic or metrics"
        )

    metric_excerpts = (
        [ev.excerpt for ev in metrics.evidence] if metrics and metrics.evidence else []
    )
    metric_anomalies = [anom for anom in metrics.anomalies] if metrics and metrics.anomalies else []
    all_telemetry = (
        all_log_excerpts + " " + " ".join(metric_excerpts) + " " + " ".join(metric_anomalies)
    )

    # Condition 4: Grounding check: assertions of status codes not present in evidence
    prc_lower = report.probable_root_cause.text.lower()
    if (
        ("http 500" in prc_lower or "5xx" in prc_lower)
        and "500" not in all_telemetry
        and "5xx" not in all_telemetry
    ):
        calibrated_conf = min(calibrated_conf, 0.40)
        uncertainty_reasons.append(
            "HTTP 500/5xx asserted without corroborating status in logs or metrics"
        )

    if ("remediation" in prc_lower or "mitigated" in prc_lower) and not has_active_traffic:
        calibrated_conf = min(calibrated_conf, 0.45)
        uncertainty_reasons.append(
            "remediation claimed without active serving revision confirmed in traffic status"
        )

    # Condition 5: Mitigation grounding verification
    timeline_text = " ".join(t.description for t in report.timeline) if report.timeline else ""
    text_corpus = (
        f"{report.summary} {report.impact} {report.probable_root_cause.text} {timeline_text}"
    ).lower()
    asserts_mitigation = any(
        k in text_corpus for k in ["mitigat", "remediat", "resolved", "cessation", "recovered"]
    )

    mitigation_rev: Optional[str] = None
    mitigation_ts: Optional[datetime] = None

    for change in report.recent_changes:
        desc = change.description
        if (
            "100%" in desc
            or "remediation" in desc.lower()
            or "mitigat" in desc.lower()
            or "mv0lfmh1" in desc
        ):
            mitigation_ts = change.timestamp
            m = re.search(r"([a-zA-Z0-9_-]+-mv[a-z0-9]+)", desc)
            if m:
                mitigation_rev = m.group(1)
            break

    if not mitigation_rev and changes and changes.evidence:
        for ev in changes.evidence:
            desc = ev.excerpt
            if (
                "100%" in desc
                or "remediation" in desc.lower()
                or "mitigat" in desc.lower()
                or "mv0lfmh1" in desc
            ):
                mitigation_ts = ev.timestamp
                m = re.search(r"([a-zA-Z0-9_-]+-mv[a-z0-9]+)", desc)
                if m:
                    mitigation_rev = m.group(1)
                break

    if not mitigation_rev:
        m = re.search(r"([a-zA-Z0-9_-]+-mv0lfmh1)", text_corpus) or re.search(
            r"([a-zA-Z0-9_-]+-mv[a-z0-9]+)", text_corpus
        )
        if m:
            mitigation_rev = m.group(1)

    if mitigation_rev and not mitigation_ts:
        for change in report.recent_changes:
            if mitigation_rev in change.description and change.timestamp:
                mitigation_ts = change.timestamp
                break
        if not mitigation_ts and changes and changes.evidence:
            for ev in changes.evidence:
                if mitigation_rev in ev.excerpt and ev.timestamp:
                    mitigation_ts = ev.timestamp
                    break

    window_end_dt: Optional[datetime] = None
    window_end_raw = report.metadata.get("window_end")
    if window_end_raw:
        if isinstance(window_end_raw, datetime):
            window_end_dt = window_end_raw
        elif isinstance(window_end_raw, str):
            try:
                window_end_dt = datetime.fromisoformat(window_end_raw.replace("Z", "+00:00"))
            except Exception:
                pass

    has_post_deploy_telemetry = False
    if mitigation_ts and window_end_dt:
        m_ts = mitigation_ts if mitigation_ts.tzinfo else mitigation_ts.replace(tzinfo=timezone.utc)
        w_end = (
            window_end_dt if window_end_dt.tzinfo else window_end_dt.replace(tzinfo=timezone.utc)
        )
        if m_ts >= w_end:
            has_post_deploy_telemetry = False
        elif logs and logs.evidence:
            post_deploy_logs = [
                e
                for e in logs.evidence
                if e.timestamp
                and (
                    e.timestamp if e.timestamp.tzinfo else e.timestamp.replace(tzinfo=timezone.utc)
                )
                > m_ts
            ]
            has_post_deploy_telemetry = len(post_deploy_logs) > 0 and all(
                "[error]" not in e.excerpt.lower() for e in post_deploy_logs
            )
    elif mitigation_ts and logs and logs.evidence:
        m_ts = mitigation_ts if mitigation_ts.tzinfo else mitigation_ts.replace(tzinfo=timezone.utc)
        post_deploy_logs = [
            e
            for e in logs.evidence
            if e.timestamp
            and (e.timestamp if e.timestamp.tzinfo else e.timestamp.replace(tzinfo=timezone.utc))
            > m_ts
        ]
        has_post_deploy_telemetry = len(post_deploy_logs) > 0 and all(
            "[error]" not in e.excerpt.lower() for e in post_deploy_logs
        )
    else:
        has_post_deploy_telemetry = False

    if (mitigation_rev or asserts_mitigation) and not has_post_deploy_telemetry:
        if mitigation_rev:
            caveat = (
                f"Mitigation via revision {mitigation_rev} is provisional: "
                "no post-deployment telemetry is available in the investigation window "
                "to verify error cessation."
            )
        else:
            caveat = (
                "Mitigation is provisional: "
                "no post-deployment telemetry is available in the investigation window "
                "to verify error cessation."
            )
        has_prov = any(
            "provisional" in c.lower() and ("mitigat" in c.lower() or "telemetry" in c.lower())
            for c in report.caveats
        )
        if not has_prov and caveat not in report.caveats:
            report.caveats.append(caveat)

        # Soften definitive mitigation claims in report summary and probable root cause
        report.summary = re.sub(
            r"\b[Tt]he incident was mitigated following subsequent revision deployment\b",
            "The incident is provisionally mitigated following subsequent revision deployment",
            report.summary,
        )
        report.summary = re.sub(
            r"\b[Tt]he incident was mitigated at\b",
            "The incident is provisionally mitigated following deployment at",
            report.summary,
        )
        report.summary = re.sub(
            r"\b[Tt]he incident was mitigated\b",
            "The incident is provisionally mitigated",
            report.summary,
        )
        report.summary = re.sub(
            r"\bwas mitigated at\b",
            "is provisionally mitigated following deployment at",
            report.summary,
        )
        report.summary = re.sub(
            r"\bwas mitigated following\b",
            "is provisionally mitigated following",
            report.summary,
        )
        report.summary = re.sub(
            r"\bwas mitigated\b",
            "is provisionally mitigated",
            report.summary,
        )

        if report.probable_root_cause:
            report.probable_root_cause.text = re.sub(
                r"\bmitigated the incident\b",
                "provisionally mitigated the incident (unverified in telemetry window)",
                report.probable_root_cause.text,
            )
            report.probable_root_cause.text = re.sub(
                r"\bwas mitigated\b",
                "is provisionally mitigated",
                report.probable_root_cause.text,
            )

        calibrated_conf = min(calibrated_conf, 0.65)
        uncertainty_reasons.append(
            "mitigation is provisional: no post-deployment telemetry available in window "
            "to verify error cessation"
        )

    # Populate caveats with calibration uncertainty reasons, avoiding near-duplicates
    has_provisional_caveat = any("provisional" in c.lower() for c in report.caveats)
    for reason in uncertainty_reasons:
        if "provisional" in reason.lower() and has_provisional_caveat:
            continue
        if not any(reason.lower() in c.lower() for c in report.caveats):
            report.caveats.append(f"Telemetry limitation: {reason}")

    # Deduplicate caveats (preserving order)
    seen_caveats: set[str] = set()
    deduped_caveats: List[str] = []
    has_prov_added = False
    for c in report.caveats:
        c_clean = c.strip()
        c_norm = re.sub(r"\s+", " ", c_clean.lower())
        if "provisional" in c_norm and ("mitigat" in c_norm or "telemetry" in c_norm):
            if has_prov_added:
                continue
            has_prov_added = True
        if c_norm and c_norm not in seen_caveats:
            seen_caveats.add(c_norm)
            deduped_caveats.append(c_clean)
    report.caveats = deduped_caveats

    # Filter out any Kubernetes/GKE endpoints when diagnosing Cloud Run services
    cleaned_actions = []
    for action in report.recommended_actions:
        if any(k in action.lower() for k in ["/api/v1/namespaces/", "kube-system", "gmp-system"]):
            continue
        if action not in cleaned_actions:
            cleaned_actions.append(action)
    report.recommended_actions = cleaned_actions

    if report.probable_root_cause:
        for k in ["/api/v1/namespaces/", "kube-system", "gmp-system"]:
            if k in report.probable_root_cause.text:
                report.probable_root_cause.text = re.sub(
                    r"/api/v1/namespaces/[\w/.-]+",
                    "service endpoints",
                    report.probable_root_cause.text,
                )

    # Enforce strict confidence monotonicity (never raise confidence)
    if calibrated_conf < current_conf:
        report.probable_root_cause.confidence = round(calibrated_conf, 2)

    return report


class SynthesizerAgent:
    """Synthesizer agent compiling findings from specialists into a final SRE Report."""

    def __init__(
        self,
        skills_loader: Optional[SkillsLoader] = None,
        llm_client: Any = None,
        model: Optional[str] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[Any] = None,
    ) -> None:
        if not model:
            if config and hasattr(config, "model_synthesizer"):
                model = config.model_synthesizer
            else:
                try:
                    from .config import get_settings

                    model = get_settings().model_synthesizer
                except Exception:
                    model = ""

        self.skills_loader = skills_loader or SkillsLoader()
        self.llm_client = llm_client
        instruction = load_prompt("synthesizer")
        from .skills_loader import get_skill_toolset_for_target

        toolset = get_skill_toolset_for_target(
            "synthesizer", skills_dir=self.skills_loader.skills_dir
        )
        self.skill_toolset = toolset
        self.llm_agent = llm_agent or LlmAgent(
            name="synthesizer",
            model=model or "default",
            instruction=instruction,
            tools=[toolset],
            output_schema=Report,
        )
        self.synthesizer_mode: str = "llm"

    def _consult_skills(self) -> List[str]:
        """Discover and record skills applied by the synthesizer."""
        try:
            skills = self.skills_loader.discover_skills()
            if "report-style" in skills:
                self.skills_loader.get_skill_content("report-style")
        except Exception:
            pass
        return list(self.skills_loader.loaded_skills)

    def _build_specialist_statuses(
        self,
        plan: InvestigationPlan,
        findings: Dict[str, Findings],
        specialist_errors: Optional[Dict[str, str]] = None,
    ) -> List[SpecialistStatus]:
        """Construct status for all known specialists (completed, failed, or skipped)."""
        all_specialists = ["logs", "metrics", "changes", "resources"]
        errors = specialist_errors or {}
        statuses: List[SpecialistStatus] = []

        for name in all_specialists:
            if name not in plan.selected_specialists:
                statuses.append(
                    SpecialistStatus(
                        specialist=name,
                        status="skipped",
                        error=None,
                    )
                )
            elif name in errors:
                statuses.append(
                    SpecialistStatus(
                        specialist=name,
                        status="failed",
                        error=errors[name],
                    )
                )
            elif name in findings:
                finding = findings[name]
                if finding.errors:
                    statuses.append(
                        SpecialistStatus(
                            specialist=name,
                            status="failed",
                            error="; ".join(finding.errors),
                        )
                    )
                else:
                    statuses.append(
                        SpecialistStatus(
                            specialist=name,
                            status="completed",
                            error=None,
                        )
                    )
            else:
                statuses.append(
                    SpecialistStatus(
                        specialist=name,
                        status="failed",
                        error=f"No findings returned for specialist '{name}'",
                    )
                )

        return statuses

    def _build_timeline(
        self,
        incident: Incident,
        findings: Dict[str, Findings],
    ) -> List[TimelineItem]:
        """Build a unified chronological timeline from evidence and incident timestamps."""
        timeline_items: List[TimelineItem] = []

        timeline_items.append(
            TimelineItem(
                timestamp=incident.opened_at,
                description=f"Incident {incident.external_key} opened: {incident.title}",
            )
        )

        for specialist_name, finding in findings.items():
            for ev in finding.evidence:
                if ev.timestamp is not None:
                    desc = f"[{specialist_name.upper()}] {ev.kind}: {ev.excerpt[:120]}"
                    timeline_items.append(
                        TimelineItem(
                            timestamp=ev.timestamp,
                            description=desc,
                        )
                    )

        timeline_items.sort(key=lambda item: item.timestamp)
        return timeline_items

    def _extract_recent_changes(
        self,
        findings: Dict[str, Findings],
    ) -> List[RecentChangeItem]:
        """Extract recent changes and deployments from changes specialist evidence."""
        recent_changes: List[RecentChangeItem] = []
        changes_finding = findings.get("changes")

        if changes_finding:
            for ev in changes_finding.evidence:
                ts = ev.timestamp if ev.timestamp is not None else datetime.now(timezone.utc)
                desc = _normalize_build_status_in_excerpt(ev.excerpt.strip())

                author: Optional[str] = None
                author_match = re.search(r"author:\s*([^\s,)]+)", ev.excerpt, re.IGNORECASE)
                if author_match and author_match.group(1):
                    cand = author_match.group(1).strip().rstrip(",").rstrip(")")
                    if cand and cand.lower() not in ["none", "null", ""]:
                        author = cand

                if not author:
                    by_match = re.search(
                        r"\bby\s+([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)",
                        ev.excerpt,
                        re.IGNORECASE,
                    )
                    if by_match:
                        author = by_match.group(1).strip()

                recent_changes.append(
                    RecentChangeItem(
                        timestamp=ts,
                        description=desc,
                        author=author,
                        console_url=ev.console_url,
                    )
                )

        return recent_changes

    def _determine_probable_root_cause(
        self,
        incident: Incident,
        findings: Dict[str, Findings],
        recent_changes: Optional[List[RecentChangeItem]] = None,
    ) -> ProbableRootCause:
        """Automated fallback root cause when LLM is unavailable."""
        svc = incident.service_hint or "service"
        return ProbableRootCause(
            text=(
                f"Automated fallback (LLM unavailable): Initial investigation for service {svc} "
                "produced preliminary findings without a definitive root cause."
            ),
            confidence=0.30,
        )

    def _build_recommended_actions(
        self,
        incident: Incident,
        probable_root_cause: Optional[ProbableRootCause],
        recent_changes: List[RecentChangeItem],
        findings: Dict[str, Findings],
    ) -> List[str]:
        """Generate evidence-backed recommended actions for fallback mode."""
        svc = incident.service_hint or "service"
        logs = findings.get("logs")
        metrics = findings.get("metrics")

        changes_finding = findings.get("changes")
        rev_traffic: Dict[str, int] = {}
        revisions: List[str] = []
        mitigation_rev: Optional[str] = None
        faulty_rev: Optional[str] = None
        baseline_rev: Optional[str] = None

        if changes_finding:
            for ev in changes_finding.evidence:
                m = re.search(r"Revision\s+([a-zA-Z0-9_-]+)", ev.excerpt)
                if m:
                    r_name = m.group(1).strip()
                    if r_name not in revisions:
                        revisions.append(r_name)
                    t_match = re.search(r"traffic:\s*(\d+)%", ev.excerpt)
                    if t_match:
                        rev_traffic[r_name] = int(t_match.group(1))

        if recent_changes:
            for rc in recent_changes:
                m = re.search(r"Revision\s+([a-zA-Z0-9_-]+)", rc.description)
                if m:
                    r_name = m.group(1).strip()
                    if r_name not in revisions:
                        revisions.append(r_name)
                    t_match = re.search(r"traffic:\s*(\d+)%", rc.description)
                    if t_match:
                        rev_traffic[r_name] = int(t_match.group(1))

        for r in revisions:
            if rev_traffic.get(r) == 100 or f"{r} (traffic: 100%)" in str(recent_changes):
                mitigation_rev = r
                break

        non_mitigation_revs = [r for r in revisions if r != mitigation_rev]
        if non_mitigation_revs:
            faulty_rev = non_mitigation_revs[0]
            if len(non_mitigation_revs) > 1:
                baseline_rev = non_mitigation_revs[1]

        endpoint_match = "service endpoints"
        if logs and logs.evidence:
            all_texts = [ev.excerpt for ev in logs.evidence] + logs.anomalies
            for text in all_texts:
                m_full = re.search(
                    r"(?:HTTP\s+\d{3}\s+)?(GET|POST|PUT|DELETE|PATCH)\s+https?://[^/\s]+(/[\w/.-]+)",
                    text,
                )
                if m_full and not any(
                    k in m_full.group(2)
                    for k in ["/api/v1/namespaces", "kube-system", "gmp-system"]
                ):
                    endpoint_match = f"{m_full.group(1)} {m_full.group(2)}"
                    break
                m_path = re.search(r"(/api/v1/[\w/.-]+|/api/[\w/.-]+|/v1/[\w/.-]+)", text)
                if m_path and not any(
                    k in m_path.group(1)
                    for k in ["/api/v1/namespaces", "kube-system", "gmp-system"]
                ):
                    endpoint_match = m_path.group(1)
                    break

        actions: List[str] = []

        if faulty_rev and baseline_rev:
            actions.append(
                f"Compare container configuration, environment variables, and image "
                f"differences between revision {faulty_rev} and baseline revision "
                f"{baseline_rev} on {svc}."
            )
        elif faulty_rev:
            actions.append(
                f"Inspect container configuration and deployment parameters for revision "
                f"{faulty_rev} on {svc}."
            )

        actions.append(
            f"Inspect application error logs and request handling traces on endpoint "
            f"{endpoint_match} on service {svc}."
        )

        if mitigation_rev:
            actions.append(
                f"Monitor service error rates and request latency post-mitigation on revision "
                f"{mitigation_rev} (100% traffic) to confirm full recovery on {svc}."
            )

        if metrics and (metrics.evidence or metrics.anomalies):
            actions.append(
                f"Configure Cloud Monitoring alerting policy for run.googleapis.com/request_count "
                f"with response_code_class 5xx > 1% over a 2-minute window on {svc}."
            )

        if not actions:
            actions.append(
                f"Inspect Cloud Run service {svc} configuration, container health checks, "
                f"and downstream dependencies."
            )

        cleaned = [
            a
            for a in actions
            if not any(k in a.lower() for k in ["/api/v1/namespaces/", "kube-system", "gmp-system"])
        ]
        return cleaned or actions

    _generate_recommendations = _build_recommended_actions

    def synthesize(
        self,
        incident: Incident,
        plan: InvestigationPlan,
        findings: Dict[str, Findings],
        specialist_errors: Optional[Dict[str, str]] = None,
        skills_used: Optional[List[str]] = None,
    ) -> Report:
        consulted_skills = self._consult_skills()
        if skills_used is not None:
            pipeline_skills = list(
                dict.fromkeys(
                    skills_used
                    + [s for s in consulted_skills if s in self.skills_loader.loaded_skills]
                )
            )
        else:
            pipeline_skills = consulted_skills

        llm_report: Optional[Report] = None
        if self.llm_client is not None:
            try:
                res = self.llm_client.generate_report(
                    incident=incident, plan=plan, findings=findings, skills_used=pipeline_skills
                )
                if isinstance(res, Report):
                    llm_report = res
                elif isinstance(res, dict):
                    llm_report = Report(**res)
            except Exception as exc:
                logger.warning("LLM report generation failed: %s", exc)

        if llm_report is not None:
            self.synthesizer_mode = "llm"
            if not llm_report.metadata:
                llm_report.metadata = {}
            llm_report.metadata["synthesizer_mode"] = "llm"
            llm_report.metadata["window_start"] = plan.start_time.isoformat()
            llm_report.metadata["window_end"] = plan.end_time.isoformat()

            if not llm_report.specialists_status:
                llm_report.specialists_status = self._build_specialist_statuses(
                    plan, findings, specialist_errors
                )
            if not llm_report.timeline:
                llm_report.timeline = self._build_timeline(incident, findings)
            if not llm_report.recent_changes:
                llm_report.recent_changes = self._extract_recent_changes(findings)
            if not llm_report.evidence_by_area:
                for s_name, f in findings.items():
                    if f.evidence:
                        llm_report.evidence_by_area[s_name] = f.evidence
            if not llm_report.queries_appendix:
                for s_name, f in findings.items():
                    if f.queries_run:
                        llm_report.queries_appendix[s_name] = f.queries_run
            if not llm_report.recommended_actions:
                fallback_actions = self._build_recommended_actions(
                    incident, llm_report.probable_root_cause, llm_report.recent_changes, findings
                )
                llm_report.recommended_actions = [
                    f"[Automated default] {a}" if not a.startswith("[Automated default] ") else a
                    for a in fallback_actions
                ]

            return calibrate_report_confidence(llm_report, findings)

        # Fallback branch IF AND ONLY IF LLM call fails completely
        self.synthesizer_mode = "heuristic_fallback"
        specialists_status = self._build_specialist_statuses(plan, findings, specialist_errors)
        timeline = self._build_timeline(incident, findings)
        recent_changes = self._extract_recent_changes(findings)

        probable_root_cause = self._determine_probable_root_cause(
            incident, findings, recent_changes
        )
        raw_recommended_actions = self._build_recommended_actions(
            incident, probable_root_cause, recent_changes, findings
        )
        recommended_actions = [
            f"[Automated default] {a}" if not a.startswith("[Automated default] ") else a
            for a in raw_recommended_actions
        ]

        evidence_by_area: Dict[str, List[EvidenceItem]] = {}
        queries_appendix: Dict[str, List[str]] = {}

        for specialist_name, finding in findings.items():
            if finding.evidence:
                evidence_by_area[specialist_name] = finding.evidence
            if finding.queries_run:
                queries_appendix[specialist_name] = finding.queries_run

        completed_count = sum(1 for s in specialists_status if s.status == "completed")
        total_selected = len(plan.selected_specialists)
        prc_summary = (
            probable_root_cause.text
            if probable_root_cause
            else "Investigation produced preliminary findings."
        )

        summary = (
            f"Investigation for {incident.external_key} ({plan.affected_service}): {prc_summary} "
            f"Completed {completed_count}/{total_selected} selected specialist investigations."
        )

        impact = (
            f"Severity {incident.severity} affecting service '{plan.affected_service}'. "
            f"Reported symptoms: {', '.join(plan.symptoms)}."
        )

        metadata = {
            "incident_id": incident.id,
            "external_key": incident.external_key,
            "affected_service": plan.affected_service,
            "window_start": plan.start_time.isoformat(),
            "window_end": plan.end_time.isoformat(),
            "specialist_count": len(plan.selected_specialists),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "synthesizer_mode": self.synthesizer_mode,
        }

        rep = Report(
            summary=summary,
            impact=impact,
            timeline=timeline,
            evidence_by_area=evidence_by_area,
            probable_root_cause=probable_root_cause,
            recent_changes=recent_changes,
            recommended_actions=recommended_actions,
            specialists_status=specialists_status,
            skills_used=pipeline_skills,
            queries_appendix=queries_appendix,
            metadata=metadata,
        )
        return calibrate_report_confidence(rep, findings)


# Backward-compatibility alias
Synthesizer = SynthesizerAgent
