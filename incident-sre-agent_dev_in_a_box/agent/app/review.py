from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from .contracts.findings import EvidenceItem, Findings
from .contracts.incident import Incident
from .specialists.base import BaseSpecialist
from .triage import InvestigationPlan


class FollowUpAction(BaseModel):
    """Specification of a targeted follow-up query triggered by review evaluation."""

    model_config = ConfigDict(extra="forbid")

    specialist: str
    start_time: datetime
    end_time: datetime
    reason: str
    query_focus: Optional[str] = None

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class ReviewEvaluator:
    """Evaluates initial specialist findings and coordinates at most one follow-up round."""

    def __init__(self, max_rounds: int = 1) -> None:
        self.max_rounds = max_rounds
        self.rounds_executed = 0

    def evaluate(
        self,
        incident: Incident,
        plan: InvestigationPlan,
        findings: Dict[str, Findings],
    ) -> Optional[FollowUpAction]:
        """Inspect specialist findings and determine if a targeted follow-up is warranted."""
        # Enforce maximum round guardrail
        if self.rounds_executed >= self.max_rounds:
            return None

        # Check for change events or deployment anomalies to correlate with logs
        changes_finding = findings.get("changes")
        logs_finding = findings.get("logs")
        metrics_finding = findings.get("metrics")

        # 1. Target Logs around a recent deployment or revision timestamp
        if changes_finding and changes_finding.evidence:
            for ev in changes_finding.evidence:
                if ev.timestamp is not None:
                    # Narrow 10-minute window around the change timestamp
                    ts = ev.timestamp
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    else:
                        ts = ts.astimezone(timezone.utc)

                    target_start = ts - timedelta(minutes=5)
                    target_end = ts + timedelta(minutes=5)

                    return FollowUpAction(
                        specialist="logs",
                        start_time=target_start,
                        end_time=target_end,
                        reason=f"Targeted log drill-down around recent change at {ts.isoformat()}",
                        query_focus="severity >= ERROR",
                    )

        # 2. Target Metrics or Logs around a high-frequency error group timestamp
        if logs_finding and logs_finding.evidence:
            for ev in logs_finding.evidence:
                is_err = (
                    "error" in ev.kind.lower()
                    or "error" in ev.excerpt.lower()
                    or "exception" in ev.excerpt.lower()
                )
                if ev.timestamp is not None and is_err:
                    ts = ev.timestamp
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    else:
                        ts = ts.astimezone(timezone.utc)

                    # Drill down in metrics around this timestamp
                    if metrics_finding and not metrics_finding.errors:
                        return FollowUpAction(
                            specialist="metrics",
                            start_time=ts - timedelta(minutes=10),
                            end_time=ts + timedelta(minutes=5),
                            reason=(
                                f"Correlate metrics with log error at {ts.strftime('%H:%M:%SZ')}"
                            ),
                            query_focus="request_count, server_latencies",
                        )

        # 3. Target Logs if metrics discovered anomalous baseline shifts
        if metrics_finding and metrics_finding.anomalies:
            for anomaly in metrics_finding.anomalies:
                if "shift" in anomaly.lower() or "alert" in anomaly.lower():
                    # Focus logs on the last 15 minutes of the plan window
                    target_end = plan.end_time
                    target_start = max(plan.start_time, target_end - timedelta(minutes=15))
                    return FollowUpAction(
                        specialist="logs",
                        start_time=target_start,
                        end_time=target_end,
                        reason=f"Log investigation triggered by metric anomaly: {anomaly[:60]}",
                        query_focus="severity >= WARNING",
                    )

        return None

    def execute_follow_up(
        self,
        action: FollowUpAction,
        specialist: BaseSpecialist,
        incident: Incident,
    ) -> Findings:
        """Execute the targeted follow-up query with guardrail tracking."""
        if self.rounds_executed >= self.max_rounds:
            raise RuntimeError(
                f"Cannot execute follow-up: max rounds ({self.max_rounds}) already reached."
            )

        self.rounds_executed += 1
        return specialist.investigate(
            incident=incident,
            start_time=action.start_time,
            end_time=action.end_time,
        )

    @staticmethod
    def merge_findings(original: Findings, follow_up: Findings) -> Findings:
        """Merge follow-up findings into the existing specialist findings."""
        # Combine evidence preserving uniqueness by excerpt
        existing_excerpts = {e.excerpt for e in original.evidence}
        combined_evidence: List[EvidenceItem] = list(original.evidence)
        for ev in follow_up.evidence:
            if ev.excerpt not in existing_excerpts:
                combined_evidence.append(ev)
                existing_excerpts.add(ev.excerpt)

        # Combine anomalies
        existing_anomalies = set(original.anomalies)
        combined_anomalies: List[str] = list(original.anomalies)
        for a in follow_up.anomalies:
            if a not in existing_anomalies:
                combined_anomalies.append(a)
                existing_anomalies.add(a)

        # Combine queries run
        combined_queries = list(original.queries_run) + [
            f"[follow-up] {q}" for q in follow_up.queries_run
        ]

        # Combine errors
        combined_errors = list(original.errors) + list(follow_up.errors)

        # Confidence: take higher confidence or weighted average
        merged_confidence = max(original.confidence, follow_up.confidence)

        # Combine skills_used
        combined_skills = list(
            dict.fromkeys(list(original.skills_used) + list(follow_up.skills_used))
        )
        combined_skill_first = original.skill_first or follow_up.skill_first
        combined_fallback = original.fallback_used or follow_up.fallback_used

        summary = (
            f"{original.summary} Follow-up investigation added {len(follow_up.evidence)} "
            f"evidence items and {len(follow_up.anomalies)} anomalies."
        )

        return Findings(
            summary=summary,
            evidence=combined_evidence,
            anomalies=combined_anomalies,
            queries_run=combined_queries,
            confidence=merged_confidence,
            errors=combined_errors,
            skills_used=combined_skills,
            skill_first=combined_skill_first,
            fallback_used=combined_fallback,
        )
