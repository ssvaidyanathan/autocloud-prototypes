from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import AgentConfig
from ..contracts.findings import EvidenceItem
from ..contracts.incident import Incident
from ..contracts.report import CheckRunAnnotation


@dataclass
class DiagnosticResult:
    """Standardized output of a diagnostic specialist."""

    stage: str
    specialist_name: str
    summary: str
    probable_cause: str
    confidence: float
    evidence_items: list[EvidenceItem] = field(default_factory=list)
    remediation_actions: list[str] = field(default_factory=list)
    suggested_fix: Optional[str] = None
    annotations: list[CheckRunAnnotation] = field(default_factory=list)


class BaseSpecialist(ABC):
    """Abstract base class for diagnostic specialists."""

    def __init__(self, name: str, stage: str) -> None:
        self.name = name
        self.stage = stage

    @abstractmethod
    def diagnose(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> DiagnosticResult:
        """Analyze failure logs, query read-only tools, and formulate diagnostic findings."""
        pass
