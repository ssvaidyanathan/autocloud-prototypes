import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from fastapi import Request

from ..config import BridgeConfig, get_settings
from ..contracts.incident import Incident
from ..contracts.report import Report
from .base import BaseAdapter, IncidentRef, parse_utc_datetime

logger = logging.getLogger(__name__)


@dataclass
class GenericAdapterState:
    acks: list[IncidentRef] = field(default_factory=list)
    reports: list[tuple[IncidentRef, Report]] = field(default_factory=list)
    failures: list[tuple[IncidentRef, str]] = field(default_factory=list)


class GenericAdapter(BaseAdapter):
    """Adapter for generic JSON webhook integrations with HMAC-SHA256 verification."""

    def __init__(self, config: Optional[BridgeConfig] = None) -> None:
        self.config = config or get_settings()
        self.state = GenericAdapterState()

    def verify(self, request: Request, payload: bytes) -> bool:
        """Verify HMAC-SHA256 signature header using constant-time comparison."""
        key = self.config.generic_hmac_key
        if not key:
            logger.warning("No GENERIC_HMAC_KEY configured; accepting request.")
            return True

        sig_header = request.headers.get("X-Hub-Signature-256") or request.headers.get(
            "X-Signature"
        )
        if not sig_header:
            logger.warning("Generic webhook missing signature header")
            return False

        expected_sig = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()

        # Extract signature value if prefixed with 'sha256='
        candidate = sig_header
        if candidate.lower().startswith("sha256="):
            candidate = candidate[7:]

        return hmac.compare_digest(candidate.strip(), expected_sig)

    def to_incident(self, payload: dict[str, Any]) -> Incident:
        """Normalize generic JSON payload into Incident contract."""
        data = payload.get("incident", payload)

        external_id = str(data.get("external_id") or data.get("id") or "gen-1")
        external_key = str(data.get("external_key") or data.get("key") or external_id)
        incident_id = str(data.get("id") or f"gen-{external_id}")

        raw_sev = str(data.get("severity", "P3")).upper()
        sev_map: dict[str, Literal["P1", "P2", "P3", "P4"]] = {
            "P1": "P1",
            "P2": "P2",
            "P3": "P3",
            "P4": "P4",
        }
        severity = sev_map.get(raw_sev, "P3")

        labels = data.get("labels", [])
        if not isinstance(labels, list):
            labels = []

        opened_at = parse_utc_datetime(data.get("opened_at"))

        return Incident(
            id=incident_id,
            source="generic",
            external_id=external_id,
            external_key=external_key,
            external_url=data.get("external_url"),
            title=str(data.get("title") or "Generic Incident"),
            description=str(data.get("description") or ""),
            severity=severity,
            service_hint=data.get("service_hint"),
            labels=labels,
            opened_at=opened_at,
            reporter=data.get("reporter"),
        )

    def post_ack(self, ref: IncidentRef) -> None:
        """Record acknowledgement."""
        logger.info("GenericAdapter: acknowledged incident %s", ref.external_key)
        self.state.acks.append(ref)

    def post_report(self, ref: IncidentRef, report: Report) -> None:
        """Record report delivery."""
        logger.info("GenericAdapter: delivered report for incident %s", ref.external_key)
        self.state.reports.append((ref, report))

    def post_failure(self, ref: IncidentRef, error: str) -> None:
        """Record failure."""
        logger.warning(
            "GenericAdapter: reported failure for incident %s: %s", ref.external_key, error
        )
        self.state.failures.append((ref, error))
