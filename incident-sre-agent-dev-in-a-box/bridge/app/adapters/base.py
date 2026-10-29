import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Request

from ..contracts.incident import Incident
from ..contracts.report import Report
from ..contracts.result import InvestigationResult

logger = logging.getLogger(__name__)


@dataclass
class IncidentRef:
    """Lightweight reference identifying an incident across ticketing platforms."""

    id: str
    source: str
    external_id: str
    external_key: str
    external_url: Optional[str] = None

    @classmethod
    def from_incident(cls, incident: Incident) -> "IncidentRef":
        return cls(
            id=incident.id,
            source=incident.source,
            external_id=incident.external_id,
            external_key=incident.external_key,
            external_url=incident.external_url,
        )

    @classmethod
    def from_result(cls, result: InvestigationResult) -> "IncidentRef":
        return cls(
            id=result.incident_id,
            source=result.incident_source,
            external_id=result.incident_external_id,
            external_key=result.incident_external_key,
        )


def parse_utc_datetime(raw: Any) -> datetime:
    """Parse various timestamp representations into a timezone-aware UTC datetime.

    Falls back to datetime.now(timezone.utc) if missing, empty, or unparseable.
    """
    if raw is None or raw == "":
        return datetime.now(timezone.utc)

    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)

    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)

    if isinstance(raw, str):
        val = raw.strip()
        # Handle Jira format with timezone offset without colon, e.g. +0000 or -0800
        # Replace trailing +0000 with +00:00
        val = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", val)
        val = val.replace("Z", "+00:00")

        # Try standard ISO parsing
        try:
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

        # Try ServiceNow default format "YYYY-MM-DD HH:MM:SS"
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(val, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue

    return datetime.now(timezone.utc)


class BaseAdapter(ABC):
    """Abstract base adapter for ticketing system integration."""

    @abstractmethod
    def verify(self, request: Request, payload: bytes) -> bool:
        """Verify inbound webhook request authentication."""
        pass

    @abstractmethod
    def to_incident(self, payload: dict[str, Any]) -> Incident:
        """Normalize platform-specific payload into canonical Incident contract."""
        pass

    @abstractmethod
    def post_ack(self, ref: IncidentRef) -> None:
        """Post initial acknowledgement and transition ticket state."""
        pass

    @abstractmethod
    def post_report(self, ref: IncidentRef, report: Report) -> None:
        """Post full investigation report and attachments to ticket."""
        pass

    @abstractmethod
    def post_failure(self, ref: IncidentRef, error: str) -> None:
        """Post investigation failure details to ticket."""
        pass
