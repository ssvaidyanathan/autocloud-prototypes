from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident


class BaseSpecialist(ABC):
    """Abstract base class for domain-specific SRE specialists."""

    def __init__(
        self,
        name: str,
        description: str,
        project_id: Optional[str] = None,
        client: Any = None,
        llm_agent: Optional[Any] = None,
        config: Optional[Any] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.config = config
        if not project_id:
            try:
                from ..config import get_settings

                project_id = get_settings().gcp_project
            except Exception:
                import os

                project_id = os.getenv("GCP_PROJECT", "")
        self.project_id = project_id or ""
        self.client = client
        self.llm_agent = llm_agent

    @property
    def tools(self) -> List[Any]:
        """Return ADK FunctionTools assigned to this specialist."""
        return self.get_tools()

    def get_tools(self) -> List[Any]:
        """Return list of FunctionTools for this specialist."""
        return []

    @abstractmethod
    def run_investigation(
        self,
        incident: Incident,
        start_time: datetime,
        end_time: datetime,
        **kwargs: Any,
    ) -> Findings:
        """Domain-specific investigation logic returning structured Findings."""
        raise NotImplementedError

    def investigate(
        self,
        incident: Incident,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        **kwargs: Any,
    ) -> Findings:
        """Execute investigation safely with time boundary resolution and error boundary."""
        # Ensure UTC tz-aware datetimes
        resolved_end = end_time or incident.opened_at
        if resolved_end.tzinfo is None:
            resolved_end = resolved_end.replace(tzinfo=timezone.utc)

        resolved_start = start_time or (resolved_end - timedelta(hours=1))
        if resolved_start.tzinfo is None:
            resolved_start = resolved_start.replace(tzinfo=timezone.utc)

        try:
            return self.run_investigation(
                incident=incident,
                start_time=resolved_start,
                end_time=resolved_end,
                **kwargs,
            )
        except Exception as exc:
            # Resilient error containment: specialist failure never crashes overall workflow
            return Findings(
                summary=f"{self.name} investigation failed: {str(exc)}",
                evidence=[],
                anomalies=[],
                queries_run=[],
                confidence=0.0,
                errors=[f"Error in {self.name}: {type(exc).__name__}: {str(exc)}"],
            )

    @staticmethod
    def create_evidence_item(
        kind: str,
        excerpt: str,
        timestamp: Optional[datetime] = None,
        console_url: Optional[str] = None,
    ) -> EvidenceItem:
        """Create a validated EvidenceItem adhering to contracts."""
        return EvidenceItem(
            kind=kind,
            excerpt=excerpt[:500] if len(excerpt) > 500 else excerpt,
            timestamp=timestamp,
            console_url=console_url,
        )

    @staticmethod
    def create_findings(
        summary: str,
        evidence: Optional[List[EvidenceItem]] = None,
        anomalies: Optional[List[str]] = None,
        queries_run: Optional[List[str]] = None,
        confidence: float = 0.5,
        errors: Optional[List[str]] = None,
    ) -> Findings:
        """Construct a validated Findings contract."""
        clamped_confidence = max(0.0, min(1.0, confidence))
        return Findings(
            summary=summary,
            evidence=evidence or [],
            anomalies=anomalies or [],
            queries_run=queries_run or [],
            confidence=clamped_confidence,
            errors=errors or [],
        )
