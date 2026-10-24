"""Base definitions for Specialist Agents in ADK Multi-Agent Runtime."""

from dataclasses import dataclass, field
from typing import Any, Optional
from abc import ABC, abstractmethod

from ..config import AgentConfig
from ..contracts.alert import AlertEvent
from ..contracts.report import EvidenceItem


@dataclass
class EvidenceDossier:
    """Standardized investigation findings dossier produced by a specialist."""
    specialist_name: str
    target_resource: str
    findings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence_items: list[EvidenceItem] = field(default_factory=list)
    queries_executed: dict[str, list[str]] = field(default_factory=dict)
    anomaly_detected: bool = False
    confidence: float = 0.85


class BaseSpecialist(ABC):
    """Abstract Base Class for investigation specialist agents."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    async def investigate(
        self,
        alert: AlertEvent,
        config: AgentConfig,
    ) -> EvidenceDossier:
        """Perform specialist investigation and return EvidenceDossier."""
        pass
