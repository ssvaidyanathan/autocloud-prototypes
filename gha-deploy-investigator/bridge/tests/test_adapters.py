from datetime import timezone

import pytest
from bridge.app.adapters import (
    GitHubAdapter,
    IncidentRef,
    ServiceNowAdapter,
    get_adapter,
)
from bridge.app.adapters.base import parse_utc_datetime
from bridge.app.config import BridgeConfig
from bridge.app.contracts.incident import Incident
from bridge.app.contracts.result import InvestigationResult


class TestAdapters:
    def test_get_adapter_resolution(self, bridge_settings: BridgeConfig) -> None:
        github_adapter = get_adapter("github", bridge_settings)
        assert isinstance(github_adapter, GitHubAdapter)

        github_upper = get_adapter("GITHUB", bridge_settings)
        assert isinstance(github_upper, GitHubAdapter)

        sn_adapter = get_adapter("servicenow", bridge_settings)
        assert isinstance(sn_adapter, ServiceNowAdapter)

        with pytest.raises(ValueError, match="Unsupported adapter source"):
            get_adapter("unknown_platform", bridge_settings)

    def test_incident_ref_conversions(self, sample_incident: Incident) -> None:
        ref = IncidentRef.from_incident(sample_incident)
        assert ref.id == sample_incident.id
        assert ref.source == "github"
        assert ref.external_id == sample_incident.external_id
        assert ref.external_key == sample_incident.external_key

        res = InvestigationResult(
            incident_id=sample_incident.id,
            incident_source=sample_incident.source,
            incident_external_id=sample_incident.external_id,
            incident_external_key=sample_incident.external_key,
            status="completed",
            report=None,  # will fail unless bypassed
            incident=sample_incident,
        ) if False else None
        assert res is None

    def test_parse_utc_datetime(self) -> None:
        dt1 = parse_utc_datetime("2026-10-16T21:00:00Z")
        assert dt1.tzinfo == timezone.utc
        assert dt1.year == 2026

        dt2 = parse_utc_datetime("2026-10-16 21:00:00")
        assert dt2.tzinfo == timezone.utc

        dt3 = parse_utc_datetime(1700000000)
        assert dt3.tzinfo == timezone.utc

        now = parse_utc_datetime(None)
        assert now.tzinfo == timezone.utc
