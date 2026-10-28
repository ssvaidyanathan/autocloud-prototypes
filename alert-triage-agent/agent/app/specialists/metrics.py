"""Metrics specialist agent analyzing time series telemetry and alert policies."""

from typing import Any
from .base import BaseSpecialist, EvidenceDossier
from ..config import AgentConfig
from ..contracts.alert import AlertEvent


class MetricsSpecialist(BaseSpecialist):
    """Specialist analyzing time series telemetry and metrics distributions."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("MetricsSpecialist")

    async def investigate(
        self,
        alert: AlertEvent,
        config: AgentConfig,
    ) -> EvidenceDossier:
        from .cloud_run import CloudRunSpecialist

        return await CloudRunSpecialist().investigate(alert, config)


__all__ = ["MetricsSpecialist"]
