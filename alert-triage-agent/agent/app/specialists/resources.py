"""Resources specialist agent inspecting cloud infrastructure configurations."""

from typing import Any
from .base import BaseSpecialist, EvidenceDossier
from ..config import AgentConfig
from ..contracts.alert import AlertEvent


class ResourcesSpecialist(BaseSpecialist):
    """Specialist inspecting Cloud Run, Apigee, and Cloud SQL infrastructure."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("ResourcesSpecialist")

    async def investigate(
        self,
        alert: AlertEvent,
        config: AgentConfig,
    ) -> EvidenceDossier:
        from .cloud_sql import CloudSQLSpecialist

        return await CloudSQLSpecialist().investigate(alert, config)


__all__ = ["ResourcesSpecialist"]
