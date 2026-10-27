"""Correlation Engine and Gemini Gray-Zone Resolution for Alert Triage Agent.

Implements:
1. Candidate incident group scoring against incoming alerts.
2. Decision thresholds:
   - Score >= 65: Auto-attach to existing group.
   - Score < 35: Auto-new group (distinct independent incident).
   - 35 <= Score < 65: Gray zone invoking Gemini (MODEL_NAME at GEMINI_LOCATION, Vertex AI) with a
     structured schema. When Gemini cannot answer, a deterministic heuristic decides; the failure is
     logged at WARNING and the decision carries source="deterministic_fallback" plus the reason.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal, Optional
import yaml
from pydantic import BaseModel, Field

from ..contracts.alert import AlertEvent, is_ata_alert
from .scoring import (
    ScoreBreakdown,
    THRESHOLD_AUTO_ATTACH,
    THRESHOLD_AUTO_NEW,
    classify_anomaly_category,
    compute_multi_factor_score,
)

logger = logging.getLogger(__name__)


GRAY_ZONE_SOURCE_GEMINI = "gemini"
GRAY_ZONE_SOURCE_FALLBACK = "deterministic_fallback"
_MAX_REASON_CHARS = 200


class GeminiGrayZoneDecision(BaseModel):
    """Response schema sent to Gemini (response_schema) for gray-zone resolution."""
    decision: Literal["attach", "new_group"] = Field(description="'attach' to existing group or 'new_group' for independent incident")
    target_group_id: Optional[str] = Field(default=None, description="Group ID to attach to if decision is attach")
    confidence: float = Field(default=0.85, ge=0.0, le=1.0, description="Confidence score 0.0 to 1.0")
    rationale: str = Field(description="Detailed engineering rationale for decision")


class GrayZoneResolution(GeminiGrayZoneDecision):
    """Gray-zone decision plus who made it: Gemini, or the deterministic fallback (and why)."""
    source: Literal["gemini", "deterministic_fallback"] = GRAY_ZONE_SOURCE_FALLBACK
    fallback_reason: Optional[str] = None


def _short_reason(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _MAX_REASON_CHARS else text[: _MAX_REASON_CHARS - 3] + "..."


def _call_gemini(prompt: str, model_name: str, project_id: str, location: str) -> Optional[str]:
    """One Vertex AI generate_content call (google-genai SDK); returns response.text."""
    from google import genai
    from google.genai import types

    client = genai.Client(vertexai=True, project=project_id, location=location)
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GeminiGrayZoneDecision,
        ),
    )
    return getattr(response, "text", None) if response is not None else None


@dataclass
class CandidateGroup:
    group_id: str
    filename: str
    file_path: Path
    origin: str
    resources: list[str]
    category: str
    anchor_time: datetime
    alerts: list[dict[str, Any]] = field(default_factory=list)
    raw_frontmatter: dict[str, Any] = field(default_factory=dict)


@dataclass
class CorrelationDecision:
    decision: str  # "attach" | "new_group"
    target_group_id: Optional[str] = None
    target_filename: Optional[str] = None
    best_score: float = 0.0
    breakdown: Optional[ScoreBreakdown] = None
    is_gray_zone: bool = False
    confidence: float = 0.95
    rationale: str = ""
    # Gray zone only: "gemini" or "deterministic_fallback", and why Gemini did not decide.
    gray_zone_source: Optional[str] = None
    gray_zone_fallback_reason: Optional[str] = None


def _parse_group_from_file(file_path: Path) -> Optional[CandidateGroup]:
    """Parse candidate incident group from markdown file with YAML frontmatter."""
    try:
        content = file_path.read_text(encoding="utf-8")
        parts = content.split("---")
        if len(parts) < 3:
            return None

        meta = yaml.safe_load(parts[1])
        if not isinstance(meta, dict) or meta.get("type") != "incident":
            return None

        status = meta.get("status", "")
        if str(status).lower() not in {"mitigation-in-progress", "open", "investigating", "ticketed", "stable", "cooling_down"}:
            return None

        pol = meta.get("policy_name", "")
        res = meta.get("resource_name", "")
        group_id = meta.get("group_id") or file_path.stem

        # Parse anchor time
        ts_str = meta.get("timestamp") or meta.get("started_at")
        anchor_dt = datetime.now(timezone.utc)
        if ts_str:
            try:
                anchor_dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            except Exception:
                pass

        # Collect all resource nodes in this incident group
        resources = [res] if res else []
        blast = meta.get("blast_radius", [])
        if isinstance(blast, list):
            for b in blast:
                if b and b not in resources:
                    resources.append(str(b))

        alerts = meta.get("alerts", [])
        if isinstance(alerts, list):
            for a in alerts:
                if isinstance(a, dict) and a.get("resource"):
                    r = str(a["resource"])
                    if r not in resources:
                        resources.append(r)

        cat = classify_anomaly_category(pol)

        return CandidateGroup(
            group_id=group_id,
            filename=file_path.name,
            file_path=file_path,
            origin=meta.get("origin", res),
            resources=resources,
            category=cat,
            anchor_time=anchor_dt,
            alerts=alerts if isinstance(alerts, list) else [],
            raw_frontmatter=meta,
        )
    except Exception as exc:
        logger.debug("Skipping unparseable candidate group %s: %s", file_path, exc)
        return None


def candidate_from_okf(doc: Any, file_path: Optional[Path] = None) -> CandidateGroup:
    """Build a CandidateGroup from an OKFDocument instance."""
    group_id = getattr(doc, "group_id", "")
    pol = getattr(doc, "policy_name", "") or ""
    res = getattr(doc, "resource_name", "") or ""
    origin = getattr(doc, "origin", None) or res
    blast = getattr(doc, "blast_radius", []) or []
    resources = [res] if res else []
    for b in blast:
        if b and str(b) not in resources:
            resources.append(str(b))
    alerts = getattr(doc, "alerts", []) or []
    for a in alerts:
        if isinstance(a, dict):
            r = a.get("resource") or a.get("resource_name")
            if r and str(r) not in resources:
                resources.append(str(r))
    ts_str = getattr(doc, "created_at", None) or getattr(doc, "updated_at", None)
    anchor_dt = datetime.now(timezone.utc)
    if ts_str:
        try:
            anchor_dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        except Exception:
            pass
    cat = classify_anomaly_category(pol)
    filename = file_path.name if file_path else f"incident-{group_id}.md"
    return CandidateGroup(
        group_id=group_id,
        filename=filename,
        file_path=file_path or Path(filename),
        origin=origin,
        resources=resources,
        category=cat,
        anchor_time=anchor_dt,
        alerts=alerts,
        raw_frontmatter=doc.model_dump() if hasattr(doc, "model_dump") else {},
    )


def resolve_gray_zone_with_gemini(
    alert: AlertEvent,
    candidate: CandidateGroup,
    breakdown: ScoreBreakdown,
    model_name: str,
    project_id: Optional[str],
    location: str,
) -> GrayZoneResolution:
    """Ask Gemini (model_name at location, Vertex AI) to resolve a gray-zone alert.

    Any failure (SDK missing, permission denied, model not found, empty or invalid answer) is
    logged at WARNING and the deterministic heuristic decides, with source="deterministic_fallback"
    and the reason in fallback_reason. The result never claims Gemini decided when it did not.
    """
    prompt = f"""You are the senior SRE lead triaging GCP production alerts.
A candidate alert falls in the correlation gray zone (Score: {breakdown.total_score} / 100).
Evaluate whether Alert A should be attached to existing incident Group G, or split into a new independent incident.

Candidate Alert:
- Incident ID: {alert.incident_id}
- Policy: {alert.policy_name}
- Resource: {alert.resource_name}
- Condition: {alert.condition_name}
- Started At: {alert.started_at}

Existing Group:
- Group ID: {candidate.group_id}
- Origin: {candidate.origin}
- Resources: {candidate.resources}
- Anchor Time: {candidate.anchor_time.isoformat()}
- Anomaly Category: {candidate.category}

Correlation Evidence:
- Topology Distance: {breakdown.hop_distance} hops (Score: {breakdown.topo_score}/35)
- Causal Evaluation: {breakdown.causal_category} (Score: {breakdown.causal_score}/30)
- Temporal Delta: {round(breakdown.delta_minutes, 1)} min (Score: {breakdown.time_score}/20)
- Change Alignment: {breakdown.change_aligned} (Score: {breakdown.change_score}/15)

Respond with JSON adhering to:
{{
  "decision": "attach" | "new_group",
  "target_group_id": "{candidate.group_id}" (if attach, otherwise null),
  "confidence": <float 0.0 to 1.0>,
  "rationale": "<engineering justification>"
}}
"""
    reason: str
    if not project_id:
        reason = "PROJECT_ID is not set"
        logger.warning("Gemini gray-zone resolution for alert %s not attempted: %s; using deterministic fallback", alert.incident_id, reason)
    else:
        try:
            text = _call_gemini(prompt, model_name=model_name, project_id=project_id, location=location)
            if text and text.strip():
                parsed = GeminiGrayZoneDecision(**json.loads(text))
                return GrayZoneResolution(**parsed.model_dump(), source=GRAY_ZONE_SOURCE_GEMINI)
            reason = f"empty response from {model_name}"
            logger.warning(
                "Gemini %s at %s returned an empty response for alert %s; using deterministic fallback",
                model_name, location, alert.incident_id,
            )
        except Exception as exc:
            reason = _short_reason(f"{type(exc).__name__}: {exc}")
            logger.warning(
                "Gemini %s at %s failed for alert %s (%s: %s); using deterministic fallback",
                model_name, location, alert.incident_id, type(exc).__name__, exc,
            )
    return _deterministic_resolution(alert, candidate, breakdown, reason)


def _deterministic_resolution(
    alert: AlertEvent, candidate: CandidateGroup, breakdown: ScoreBreakdown, reason: str
) -> GrayZoneResolution:
    """Heuristic gray-zone decision, used only when Gemini could not answer."""
    fallback = {"source": GRAY_ZONE_SOURCE_FALLBACK, "fallback_reason": reason}
    # In gray zone (35 <= score < 65):
    # If topology is 1 hop and causal is plausible/direct, attach
    if breakdown.hop_distance in (0, 1) and breakdown.causal_score >= 15.0:
        return GrayZoneResolution(
            decision="attach",
            target_group_id=candidate.group_id,
            confidence=0.82,
            rationale=(
                f"Alert on {alert.resource_name} is topologically adjacent ({breakdown.hop_distance} hop) "
                f"and temporally aligned ({round(breakdown.delta_minutes, 1)}m) with {candidate.origin}."
            ),
            **fallback,
        )

    # If topology is 2 hops in direct cascade and within 10 minutes, attach
    if breakdown.hop_distance == 2 and breakdown.causal_score >= 30.0 and breakdown.delta_minutes <= 10.0:
        return GrayZoneResolution(
            decision="attach",
            target_group_id=candidate.group_id,
            confidence=0.88,
            rationale=(
                f"Alert on {alert.resource_name} is causally coupled in direct cascade ({breakdown.causal_category}) "
                f"within 2 hops and {round(breakdown.delta_minutes, 1)}m of {candidate.origin}."
            ),
            **fallback,
        )

    # Otherwise if distance is 2+ hops and delta > 5 min, split
    return GrayZoneResolution(
        decision="new_group",
        target_group_id=None,
        confidence=0.78,
        rationale=(
            f"Alert on {alert.resource_name} has weak topological coupling ({breakdown.hop_distance} hops) "
            f"and timing delta {round(breakdown.delta_minutes, 1)}m from {candidate.origin}; triaged independently."
        ),
        **fallback,
    )


class CorrelationEngine:
    """Deterministic Multi-Factor Correlation Engine with Gray-Zone Handling."""

    def __init__(
        self,
        incident_store_path: str,
        model_name: str,
        project_id: Optional[str],
        gemini_location: str,
    ):
        self.incident_store_path = Path(incident_store_path)
        self.model_name = model_name
        self.project_id = project_id
        self.gemini_location = gemini_location

    def list_candidate_groups(self) -> list[CandidateGroup]:
        """Discover candidate open incident groups in the store path."""
        if not self.incident_store_path.exists():
            return []

        candidates: list[CandidateGroup] = []
        for p in self.incident_store_path.glob("*.md"):
            group = _parse_group_from_file(p)
            if group is not None:
                candidates.append(group)
        return candidates

    def _emit_decision_log(self, alert: AlertEvent, decision: CorrelationDecision, candidate_count: int) -> None:
        """Emit exactly one structured JSON log line per correlation evaluation."""
        factors = {}
        if decision.breakdown:
            factors = {
                "topology_score": getattr(decision.breakdown, "topo_score", 0.0),
                "temporal_score": getattr(decision.breakdown, "time_score", 0.0),
                "causal_score": getattr(decision.breakdown, "causal_score", 0.0),
            }
        entry = {
            "event": "correlation_evaluated",
            "incident_id": alert.incident_id,
            "policy_name": alert.policy_name,
            "resource_name": alert.resource_name,
            "decision": decision.decision,
            "target_group_id": decision.target_group_id,
            "best_score": decision.best_score,
            "factors": factors,
            "candidate_groups_evaluated": candidate_count,
            "rationale": decision.rationale,
        }
        if decision.is_gray_zone:
            entry["gray_zone_source"] = decision.gray_zone_source
            entry["gray_zone_fallback_reason"] = decision.gray_zone_fallback_reason
        logger.info("JSON_DECISION: %s", json.dumps(entry))

    def evaluate_alert(
        self,
        alert: AlertEvent,
        deployment_time: Optional[datetime] = None,
        candidate_groups: Optional[list[CandidateGroup]] = None,
    ) -> CorrelationDecision:
        """Evaluate an alert against candidate open groups and make correlation decision."""
        if not is_ata_alert(alert):
            logger.warning(
                "Defensively filtering non-ata alert %s (policy: %s, resource: %s) in correlation engine",
                alert.incident_id,
                alert.policy_name,
                alert.resource_name,
            )
            decision = CorrelationDecision(
                decision="drop",
                target_group_id=None,
                target_filename=None,
                best_score=0.0,
                breakdown=None,
                is_gray_zone=False,
                confidence=1.0,
                rationale="Defensively filtered: alert is outside this stack's NAME_PREFIX scope.",
            )
            self._emit_decision_log(alert, decision, 0)
            return decision

        if candidate_groups is None:
            candidate_groups = self.list_candidate_groups()

        if not candidate_groups:
            decision = CorrelationDecision(
                decision="new_group",
                target_group_id=None,
                best_score=0.0,
                rationale="No existing open incident groups found; initializing new group.",
            )
            self._emit_decision_log(alert, decision, 0)
            return decision

        sig_hint = getattr(alert, "policy_user_labels", {}).get("signal", "") if getattr(alert, "policy_user_labels", None) else ""
        alert_cat = classify_anomaly_category(alert.policy_name, alert.metric_type or "", signal=sig_hint)
        try:
            alert_dt = datetime.fromisoformat(alert.started_at.replace("Z", "+00:00"))
        except Exception:
            alert_dt = datetime.now(timezone.utc)

        best_score = -1.0
        best_candidate: Optional[CandidateGroup] = None
        best_breakdown: Optional[ScoreBreakdown] = None

        for cand in candidate_groups:
            breakdown = compute_multi_factor_score(
                alert_resource=alert.resource_name,
                alert_category=alert_cat,
                alert_time=alert_dt,
                group_resources=cand.resources,
                group_category=cand.category,
                group_anchor_time=cand.anchor_time,
                deployment_time=deployment_time,
            )
            if breakdown.total_score > best_score:
                best_score = breakdown.total_score
                best_candidate = cand
                best_breakdown = breakdown

        if best_candidate is None or best_breakdown is None:
            decision = CorrelationDecision(
                decision="new_group",
                best_score=0.0,
                rationale="Evaluation against open groups yielded no candidates.",
            )
            self._emit_decision_log(alert, decision, len(candidate_groups))
            return decision

        # Decision thresholds
        if best_score >= THRESHOLD_AUTO_ATTACH:
            decision = CorrelationDecision(
                decision="attach",
                target_group_id=best_candidate.group_id,
                target_filename=best_candidate.filename,
                best_score=best_score,
                breakdown=best_breakdown,
                is_gray_zone=False,
                confidence=0.92,
                rationale=(
                    f"High-confidence correlation score {best_score} >= {THRESHOLD_AUTO_ATTACH}. "
                    f"Topological distance {best_breakdown.hop_distance} hop(s), causal {best_breakdown.causal_category}."
                ),
            )
        elif best_score < THRESHOLD_AUTO_NEW:
            decision = CorrelationDecision(
                decision="new_group",
                target_group_id=None,
                best_score=best_score,
                breakdown=best_breakdown,
                is_gray_zone=False,
                confidence=0.89,
                rationale=(
                    f"Disjoint correlation score {best_score} < {THRESHOLD_AUTO_NEW}. "
                    f"Topological distance {best_breakdown.hop_distance} hop(s), causal {best_breakdown.causal_category}; "
                    "created independent incident."
                ),
            )
        else:
            # Gray Zone: 35 <= best_score < 65
            logger.info("Alert %s falls in correlation gray zone with score %s; invoking Gemini", alert.incident_id, best_score)
            res = resolve_gray_zone_with_gemini(
                alert=alert,
                candidate=best_candidate,
                breakdown=best_breakdown,
                model_name=self.model_name,
                project_id=self.project_id,
                location=self.gemini_location,
            )
            if res.source == GRAY_ZONE_SOURCE_GEMINI:
                rationale = f"Gray-zone Gemini resolution: {res.rationale}"
            else:
                rationale = f"Gray-zone deterministic fallback (Gemini unavailable: {res.fallback_reason}): {res.rationale}"

            decision = CorrelationDecision(
                decision=res.decision,
                target_group_id=best_candidate.group_id if res.decision == "attach" else None,
                target_filename=best_candidate.filename if res.decision == "attach" else None,
                best_score=best_score,
                breakdown=best_breakdown,
                is_gray_zone=True,
                confidence=res.confidence,
                rationale=rationale,
                gray_zone_source=res.source,
                gray_zone_fallback_reason=res.fallback_reason,
            )

        self._emit_decision_log(alert, decision, len(candidate_groups))
        return decision
