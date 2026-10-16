"""Gemini LLM Client for structured ADK plan and report generation."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from google.genai import types
from pydantic import Field

from .config import AgentConfig, get_settings
from .contracts.findings import Findings
from .contracts.incident import Incident
from .contracts.report import (
    ProbableRootCause,
    Report,
    SpecialistStatus,
)
from .prompts import load_prompt
from .skills_loader import SkillsLoader
from .synthesizer import calibrate_report_confidence
from .tools.clients import get_genai_client
from .triage import InvestigationPlan

logger = logging.getLogger(__name__)


class SynthesisReportSchema(Report):
    """Synthesis schema requiring probable_root_cause to prevent first-pass omissions."""

    probable_root_cause: ProbableRootCause = Field(
        ...,
        description=(
            "Mandatory probable root cause object containing 'text' (grounded causal prose) "
            "and 'confidence' (float 0.0-1.0). Must NEVER be omitted or null."
        ),
    )


class GeminiLlmClient:
    """Client for generating structured InvestigationPlan and Report using Vertex AI Gemini."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        location: Optional[str] = None,
        model: Optional[str] = None,
        config: Optional[AgentConfig] = None,
        skills_loader: Optional[SkillsLoader] = None,
        client: Optional[Any] = None,
    ) -> None:
        self.config = config or get_settings()
        self.project_id = project_id or self.config.gcp_project
        self.location = location or self.config.vertex_location
        self.model = model or self.config.model_dispatcher
        self.skills_loader = skills_loader or SkillsLoader()

        if client is not None:
            self.client = client
        else:
            self.client = get_genai_client(
                project_id=self.project_id,
                location=self.location,
            )
            if self.client is None:
                raise RuntimeError(
                    f"Failed to instantiate Vertex AI GenAI client for project '{self.project_id}' "
                    f"at location '{self.location}'"
                )

    def generate_plan(self, incident: Incident) -> InvestigationPlan:
        """Generate structured InvestigationPlan using Gemini on Vertex AI."""
        instruction = load_prompt("triage")
        skill_text = ""
        try:
            skill_content = self.skills_loader.get_skill_content("triage-playbook")
            skill_text = f"\n\nApplicable Playbook Instructions:\n{skill_content.instructions}"
        except Exception:
            pass

        user_content = (
            f"Analyze incident ticket and generate an InvestigationPlan adhering to the schema:\n"
            f"ID: {incident.id}\n"
            f"External Key: {incident.external_key}\n"
            f"Title: {incident.title}\n"
            f"Description: {incident.description}\n"
            f"Severity: {incident.severity}\n"
            f"Service Hint: {incident.service_hint or 'None'}\n"
            f"Opened At (UTC): {incident.opened_at.isoformat()}\n"
        )

        response = self.client.models.generate_content(
            model=self.model,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=instruction + skill_text,
                response_mime_type="application/json",
                response_schema=InvestigationPlan,
                temperature=0.1,
            ),
        )

        if not response or not response.text:
            raise RuntimeError("Vertex AI Gemini returned empty response for triage plan")

        plan = InvestigationPlan.model_validate_json(response.text)
        plan.onset_time = None

        # Real-time window anchoring
        now_utc = datetime.now(timezone.utc)
        plan.end_time = now_utc

        opened_at = incident.opened_at
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        else:
            opened_at = opened_at.astimezone(timezone.utc)

        if (now_utc - timedelta(hours=2)) <= opened_at <= (now_utc + timedelta(minutes=5)):
            plan.start_time = opened_at - timedelta(minutes=15)
        else:
            plan.start_time = now_utc - timedelta(minutes=60)

        if (plan.end_time - plan.start_time) > timedelta(hours=24):
            plan.start_time = plan.end_time - timedelta(hours=24)
        if plan.start_time > plan.end_time:
            plan.start_time = plan.end_time - timedelta(minutes=60)

        return plan

    def synthesize_report(
        self,
        incident: Incident,
        plan: InvestigationPlan,
        findings: Dict[str, Findings],
        skills_used: Optional[List[str]] = None,
    ) -> Report:
        """Synthesize specialist findings into a structured Report using Gemini on Vertex AI."""
        return self.generate_report(
            incident=incident,
            plan=plan,
            findings=findings,
            skills_used=skills_used,
        )

    def generate_report(
        self,
        incident: Incident,
        plan: InvestigationPlan,
        findings: Dict[str, Findings],
        skills_used: Optional[List[str]] = None,
    ) -> Report:
        """Synthesize specialist findings into a structured Report using Gemini on Vertex AI."""
        instruction = load_prompt("synthesizer")
        skill_text = ""
        try:
            skill_content = self.skills_loader.get_skill_content("report-style")
            skill_text = f"\n\nReport Style Guidelines:\n{skill_content.instructions}"
        except Exception:
            pass

        onset_time = getattr(plan, "onset_time", None) if plan else None
        onset_utc = None
        if onset_time:
            onset_utc = (
                onset_time
                if onset_time.tzinfo is not None
                else onset_time.replace(tzinfo=timezone.utc)
            )

        def is_prior_ev(excerpt: str, ts: Optional[datetime]) -> bool:
            if "[prior_incident]" in excerpt.lower():
                return True
            if onset_utc and ts is not None:
                utc_ts = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
                if utc_ts < onset_utc:
                    return True
            return False

        findings_summary: Dict[str, Any] = {}
        for spec_name, f in findings.items():
            ev_list = []
            for e in f.evidence:
                dumped = e.model_dump(mode="json")
                if is_prior_ev(e.excerpt, e.timestamp):
                    dumped["prior_status"] = (
                        "[PRIOR RESOLVED INCIDENT EVIDENCE - DO NOT USE AS ACTIVE MECHANISM]"
                    )
                    dumped["excerpt"] = (
                        f"[PRIOR RESOLVED INCIDENT EVIDENCE - DO NOT USE AS ACTIVE MECHANISM] "
                        f"{dumped.get('excerpt', '')}"
                    )
                ev_list.append(dumped)
            findings_summary[spec_name] = {
                "summary": f.summary,
                "anomalies": f.anomalies,
                "evidence": ev_list,
                "confidence": f.confidence,
                "errors": f.errors,
                "queries_run": f.queries_run,
            }

        prior_tag = "[PRIOR RESOLVED INCIDENT EVIDENCE - DO NOT USE AS ACTIVE MECHANISM]"
        logs_evidence = []
        if "logs" in findings:
            for ev in findings["logs"].evidence:
                if is_prior_ev(ev.excerpt, ev.timestamp):
                    logs_evidence.append(f"  - {prior_tag} {ev.excerpt}")
                else:
                    logs_evidence.append(f"  - {ev.excerpt}")
            for anom in findings["logs"].anomalies:
                if "[prior_incident]" in anom.lower():
                    logs_evidence.append(f"  - {prior_tag} [Anomaly] {anom}")
                else:
                    logs_evidence.append(f"  - [Anomaly] {anom}")
        metrics_evidence = []
        if "metrics" in findings:
            for ev in findings["metrics"].evidence:
                if is_prior_ev(ev.excerpt, ev.timestamp):
                    metrics_evidence.append(f"  - {prior_tag} {ev.excerpt}")
                else:
                    metrics_evidence.append(f"  - {ev.excerpt}")
            for anom in findings["metrics"].anomalies:
                if "[prior_incident]" in anom.lower():
                    metrics_evidence.append(f"  - {prior_tag} [Anomaly] {anom}")
                else:
                    metrics_evidence.append(f"  - [Anomaly] {anom}")

        logs_section = "\n".join(logs_evidence) if logs_evidence else "  None recorded"
        metrics_section = "\n".join(metrics_evidence) if metrics_evidence else "  None recorded"

        dep_statuses: List[str] = []
        for dep_key, dep_f in findings.items():
            if dep_key.startswith("dependency:"):
                dep_svc = dep_key.split(":", 1)[1]
                if dep_f.anomalies or dep_f.errors:
                    dep_statuses.append(
                        f"  - Downstream dependency '{dep_svc}': FAILING/ERRORS "
                        f"({len(dep_f.errors)} errors, {len(dep_f.anomalies)} anomalies). "
                        "Probable root cause originates in this downstream dependency."
                    )
                else:
                    dep_statuses.append(
                        f"  - Downstream dependency '{dep_svc}': HEALTHY "
                        "(0 errors, no anomalies). Delay originates in primary service "
                        "request processing path, not downstream dependencies."
                    )
        dep_section = "\n".join(dep_statuses) if dep_statuses else "  None inspected"

        user_content = (
            "Synthesize the incident investigation findings into a complete "
            "Report adhering to schema:\n"
            f"Incident External Key: {incident.external_key}\n"
            f"Incident Title: {incident.title}\n"
            f"Severity: {incident.severity}\n"
            f"Affected Service: {plan.affected_service}\n"
            f"Symptoms: {plan.symptoms}\n"
            f"Window Start: {plan.start_time.isoformat()}\n"
            f"Window End: {plan.end_time.isoformat()}\n\n"
            "Collected Logs Telemetry:\n"
            f"{logs_section}\n\n"
            "Collected Metrics Telemetry:\n"
            f"{metrics_section}\n\n"
            "Downstream Dependency Health Status:\n"
            f"{dep_section}\n\n"
            f"Specialist Findings:\n{json.dumps(findings_summary, default=str, indent=2)}\n\n"
            "CRITICAL INSTRUCTIONS:\n"
            "1. 'probable_root_cause' MUST accurately name the failing service, the affected "
            "endpoint, and the specific failure symptom.\n"
            "2. Never return null or an empty object for probable_root_cause.\n"
            "3. Do not use formulaic templates like 'Regression introduced by recent change "
            "(...)'. Write professional, natural SRE prose.\n"
            "4. If a downstream dependency was inspected and verified healthy "
            "(0 errors, no anomalies), do NOT attribute root cause to that downstream dependency; "
            "the delay originates in the primary service's request processing path.\n"
            "5. CRITICAL: Evidence and error logs belonging to prior resolved incidents "
            "(occurring before detected onset) must NEVER be used as the root cause mechanism for "
            "the active incident. If the active incident is pure latency with 0 HTTP errors, "
            "attribute root cause to primary service internal request processing delay/thread "
            "starvation, never resolved database pool exhaustion or prior exceptions.\n"
            "6. Never include Kubernetes/GKE endpoints (e.g. /api/v1/namespaces/...)"
            " when diagnosing Cloud Run services.\n"
        )

        model_synth = self.config.model_synthesizer or self.model
        response = self.client.models.generate_content(
            model=model_synth,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=instruction + skill_text,
                response_mime_type="application/json",
                response_schema=SynthesisReportSchema,
                temperature=0.2,
            ),
        )

        if not response or not response.text:
            raise RuntimeError("Vertex AI Gemini returned empty response for synthesis report")

        report = Report.model_validate_json(response.text)

        # Backfill required metadata and completion tracking
        if not report.metadata:
            report.metadata = {}
        report.metadata["synthesizer_mode"] = "llm"
        report.metadata["incident_id"] = incident.id
        report.metadata["external_key"] = incident.external_key
        report.metadata["affected_service"] = plan.affected_service
        report.metadata["detected_onset"] = (
            plan.onset_time.isoformat() if plan and plan.onset_time is not None else None
        )
        report.metadata["window_start"] = plan.start_time.isoformat()
        report.metadata["window_end"] = plan.end_time.isoformat()
        report.metadata.setdefault("generated_at", datetime.now(timezone.utc).isoformat())

        # Backfill evidence and queries if model omitted them
        for spec_name, f in findings.items():
            if f.queries_run and spec_name not in report.queries_appendix:
                report.queries_appendix[spec_name] = f.queries_run
            if f.evidence and spec_name not in report.evidence_by_area:
                report.evidence_by_area[spec_name] = f.evidence

        # Populate recent_changes from changes findings if omitted or empty
        if not report.recent_changes and "changes" in findings:
            from .synthesizer import SynthesizerAgent

            synth = SynthesizerAgent()
            report.recent_changes = synth._extract_recent_changes(findings)

        # Ensure probable_root_cause is non-null, non-empty, and pure prose
        is_templated = bool(
            report.probable_root_cause
            and (
                report.probable_root_cause.text.startswith(
                    "Regression introduced by recent change ("
                )
                or "resulting in Error group grp-" in report.probable_root_cause.text
            )
        )
        if (
            report.probable_root_cause is None
            or not report.probable_root_cause.text.strip()
            or is_templated
        ):
            generated_prc = None
            calibrated_confidence = (
                report.probable_root_cause.confidence
                if report.probable_root_cause and report.probable_root_cause.confidence is not None
                else 0.5
            )
            try:
                prc_prompt = (
                    f"Based on the following incident evidence for service "
                    f"'{plan.affected_service}', write 1-2 fluent, professional "
                    "sentences of natural SRE prose explaining the technical failure "
                    "mechanism, triggering deployment (citing Cloud Run revision and author if "
                    "present), and user impact. Do NOT use formulaic templates like "
                    "'Regression introduced by recent change (...)', parenthetical dumps, "
                    "or bullet points. "
                    "CRITICAL: Evidence and error logs belonging to prior resolved incidents "
                    "(occurring before detected onset) must NEVER be used as root cause mechanism "
                    "for the active incident. If the active incident is pure latency with 0 HTTP "
                    "errors, attribute root cause to primary service internal request processing "
                    "delay/thread starvation, never resolved database pool exhaustion or prior "
                    "exceptions. Return ONLY the prose text.\n\n"
                    f"Findings:\n{json.dumps(findings_summary, default=str)}"
                )
                prc_resp = self.client.models.generate_content(
                    model=model_synth,
                    contents=prc_prompt,
                    config=types.GenerateContentConfig(temperature=0.2),
                )
                if prc_resp and prc_resp.text and prc_resp.text.strip():
                    generated_prc = ProbableRootCause(
                        text=prc_resp.text.strip(),
                        confidence=min(calibrated_confidence, 0.5),
                    )
            except Exception:
                pass

            if generated_prc is not None:
                report.probable_root_cause = generated_prc
                if report.caveats is None:
                    report.caveats = []
                report.caveats.append(
                    "Root cause was regenerated in secondary LLM pass due to missing initial field."
                )
            else:
                from .synthesizer import SynthesizerAgent

                synth = SynthesizerAgent()
                recent_changes = report.recent_changes or synth._extract_recent_changes(findings)
                report.metadata["synthesizer_mode"] = "heuristic_fallback"
                report.probable_root_cause = synth._determine_probable_root_cause(
                    incident=incident,
                    findings=findings,
                    recent_changes=recent_changes,
                )

        # Ensure recommended_actions is populated
        if not report.recommended_actions:
            from .synthesizer import SynthesizerAgent

            synth = SynthesizerAgent()
            fallback_actions = synth._generate_recommendations(
                incident=incident,
                probable_root_cause=report.probable_root_cause,
                recent_changes=report.recent_changes,
                findings=findings,
            )
            report.recommended_actions = [
                f"[Automated default] {action}"
                if not action.startswith("[Automated default] ")
                else action
                for action in fallback_actions
            ]

        # Ensure skills_used contains only actually consulted skills
        if skills_used is not None:
            report.skills_used = list(dict.fromkeys(skills_used))
        elif self.skills_loader and hasattr(self.skills_loader, "loaded_skills"):
            report.skills_used = list(dict.fromkeys(self.skills_loader.loaded_skills))
        else:
            report.skills_used = list(dict.fromkeys(report.skills_used))

        # Ensure caveats is initialized
        if report.caveats is None:
            report.caveats = []

        # Ensure all specialists have a status entry
        recorded_specs = {s.specialist for s in report.specialists_status}
        for spec_name in ["logs", "metrics", "changes", "resources"]:
            if spec_name not in recorded_specs:
                if spec_name in plan.selected_specialists:
                    spec_finding = findings.get(spec_name)
                    if spec_finding is None:
                        status: Literal["completed", "failed", "skipped"] = "failed"
                        err_msg: Optional[str] = "No findings returned by specialist"
                    elif spec_finding.errors:
                        status = "failed"
                        err_msg = "; ".join(spec_finding.errors)
                    else:
                        status = "completed"
                        err_msg = None

                    report.specialists_status.append(
                        SpecialistStatus(
                            specialist=spec_name,
                            status=status,
                            error=err_msg,
                        )
                    )
                else:
                    report.specialists_status.append(
                        SpecialistStatus(specialist=spec_name, status="skipped", error=None)
                    )

        return calibrate_report_confidence(report, findings, plan=plan)
