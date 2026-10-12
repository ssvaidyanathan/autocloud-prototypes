import time
from datetime import datetime, timezone

import pytest
from agent.app.config import AgentConfig
from agent.app.contracts.findings import EvidenceItem, Findings
from agent.app.contracts.incident import Incident
from agent.app.contracts.request import InvestigationRequest
from agent.app.contracts.result import InvestigationResult
from agent.app.dispatcher import Dispatcher
from agent.app.specialists.base import BaseSpecialist
from agent.app.triage import InvestigationPlan


def _make_request() -> InvestigationRequest:
    incident = Incident(
        id="inc-disp-1",
        source="servicenow",
        external_id="INC0088",
        external_key="INC0088",
        title="Outage on shop-api during checkout",
        description="Checkout service failing with 500 status codes",
        severity="P2",
        service_hint="shop-api",
        opened_at=datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc),
    )
    return InvestigationRequest(
        incident=incident,
        callback_url="https://bridge-test.run.app/callback/report",
        requested_at=datetime(2026, 10, 9, 2, 1, 0, tzinfo=timezone.utc),
    )


class MockSpecialist(BaseSpecialist):
    def __init__(self, name: str, findings: Findings, delay: float = 0.0) -> None:
        super().__init__(name=name, description=f"Mock for {name}")
        self._findings = findings
        self._delay = delay
        self.last_kwargs: dict[str, object] = {}
        self.last_incident: Incident | None = None

    def run_investigation(
        self, incident: Incident, start_time: datetime, end_time: datetime, **kwargs: object
    ) -> Findings:
        self.last_kwargs = dict(kwargs)
        self.last_incident = incident
        if self._delay > 0:
            time.sleep(self._delay)
        return self._findings


class RaisingSpecialist(BaseSpecialist):
    def __init__(self, name: str) -> None:
        super().__init__(name=name, description=f"Failing mock {name}")

    def run_investigation(
        self, incident: Incident, start_time: datetime, end_time: datetime, **kwargs: object
    ) -> Findings:
        raise RuntimeError("Crash in specialist API client")


@pytest.mark.anyio
async def test_dispatcher_full_lifecycle_success() -> None:
    now = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    specialists = {
        "logs": MockSpecialist(
            "logs",
            Findings(
                summary="Logs found 500 error group",
                evidence=[EvidenceItem(kind="error", excerpt="Database failure", timestamp=now)],
                anomalies=["Error spike"],
                queries_run=["query_logs"],
                confidence=0.85,
                errors=[],
            ),
        ),
        "metrics": MockSpecialist(
            "metrics",
            Findings(
                summary="Metrics normal",
                evidence=[],
                anomalies=[],
                queries_run=["query_time_series"],
                confidence=0.7,
                errors=[],
            ),
        ),
        "changes": MockSpecialist(
            "changes",
            Findings(
                summary="Recent deploy",
                evidence=[
                    EvidenceItem(kind="revision", excerpt="Deployed shop-api-v2", timestamp=now)
                ],
                anomalies=["Revision deployment"],
                queries_run=["list_revisions"],
                confidence=0.9,
                errors=[],
            ),
        ),
        "resources": MockSpecialist(
            "resources",
            Findings(
                summary="Resources healthy",
                evidence=[],
                anomalies=[],
                queries_run=["describe_cloud_run"],
                confidence=0.8,
                errors=[],
            ),
        ),
    }

    config = AgentConfig(timeout_seconds=30)
    dispatcher = Dispatcher(config=config, specialists=specialists)
    req = _make_request()

    result = await dispatcher.investigate(req)

    assert isinstance(result, InvestigationResult)
    assert result.status == "completed"
    assert result.report is not None
    assert result.error is None
    assert result.incident_id == "inc-disp-1"
    assert "shop-api" in result.report.summary

    # Ensure specialists executed
    status_map = {s.specialist: s.status for s in result.report.specialists_status}
    assert status_map["logs"] == "completed"
    assert status_map["metrics"] == "completed"
    assert status_map["changes"] == "completed"
    assert status_map["resources"] == "completed"


@pytest.mark.anyio
async def test_dispatcher_error_containment() -> None:
    """When a specialist raises an unhandled exception, dispatcher contains it."""
    now = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    specialists = {
        "logs": MockSpecialist(
            "logs",
            Findings(
                summary="Logs ok",
                evidence=[EvidenceItem(kind="error", excerpt="DB timeout", timestamp=now)],
                anomalies=[],
                queries_run=["query_logs"],
                confidence=0.8,
                errors=[],
            ),
        ),
        "metrics": RaisingSpecialist("metrics"),  # Crashes
        "changes": MockSpecialist(
            "changes",
            Findings(
                summary="No changes",
                evidence=[],
                anomalies=[],
                queries_run=[],
                confidence=0.5,
                errors=[],
            ),
        ),
        "resources": MockSpecialist(
            "resources",
            Findings(
                summary="Resources ok",
                evidence=[],
                anomalies=[],
                queries_run=[],
                confidence=0.7,
                errors=[],
            ),
        ),
    }

    config = AgentConfig(timeout_seconds=30)
    dispatcher = Dispatcher(config=config, specialists=specialists)
    req = _make_request()

    result = await dispatcher.investigate(req)

    # Whole investigation does NOT fail
    assert result.status == "completed"
    assert result.report is not None
    assert result.error is None

    # But metrics status is recorded as failed
    status_map = {s.specialist: s for s in result.report.specialists_status}
    assert status_map["metrics"].status == "failed"
    assert "Crash in specialist" in (status_map["metrics"].error or "")
    assert status_map["logs"].status == "completed"


@pytest.mark.anyio
async def test_dispatcher_timeout_budget() -> None:
    """When investigation exceeds timeout, dispatcher returns failed result."""
    # Slow specialist taking 2 seconds
    specialists = {
        "logs": MockSpecialist(
            "logs",
            Findings(
                summary="Slow", evidence=[], anomalies=[], queries_run=[], confidence=0.5, errors=[]
            ),
            delay=1.0,
        ),
        "metrics": MockSpecialist(
            "metrics",
            Findings(
                summary="Normal",
                evidence=[],
                anomalies=[],
                queries_run=[],
                confidence=0.5,
                errors=[],
            ),
        ),
        "changes": MockSpecialist(
            "changes",
            Findings(
                summary="Normal",
                evidence=[],
                anomalies=[],
                queries_run=[],
                confidence=0.5,
                errors=[],
            ),
        ),
        "resources": MockSpecialist(
            "resources",
            Findings(
                summary="Normal",
                evidence=[],
                anomalies=[],
                queries_run=[],
                confidence=0.5,
                errors=[],
            ),
        ),
    }

    # Set very tight timeout of 0.1s
    config = AgentConfig(timeout_seconds=1)
    # Patch effective_timeout_seconds to 0.1s for fast test
    dispatcher = Dispatcher(config=config, specialists=specialists)
    dispatcher.config.timeout_seconds = 0  # Forces timeout immediately

    req = _make_request()
    result = await dispatcher.investigate(req)

    assert result.status == "failed"
    assert result.report is None
    assert result.error is not None
    assert "timed out" in result.error.lower()


def test_dispatcher_default_wires_gemini_llm_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    mock_llm_class = MagicMock()
    mock_instance = MagicMock()
    mock_llm_class.return_value = mock_instance

    monkeypatch.setattr("agent.app.dispatcher.is_local_mode", lambda: False)
    monkeypatch.setattr("agent.app.dispatcher.GeminiLlmClient", mock_llm_class)

    cfg = AgentConfig()
    disp = Dispatcher(config=cfg, specialists={})

    assert disp.llm_client is mock_instance
    assert disp.triage_agent.llm_client is mock_instance
    assert disp.synthesizer.llm_client is mock_instance


def test_dispatcher_gemini_init_failure_handled_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    mock_llm_class = MagicMock(
        side_effect=RuntimeError("Google ADC unavailable in test environment")
    )

    monkeypatch.setattr("agent.app.dispatcher.is_local_mode", lambda: False)
    monkeypatch.setattr("agent.app.dispatcher.GeminiLlmClient", mock_llm_class)

    cfg = AgentConfig()
    disp = Dispatcher(config=cfg, specialists={})

    assert disp.llm_client is None
    assert disp.triage_agent.llm_client is None
    assert disp.synthesizer.llm_client is None


@pytest.mark.anyio
async def test_dispatcher_backfills_service_hint_and_passes_plan_to_specialists() -> None:
    now = datetime(2026, 10, 10, 2, 0, 0, tzinfo=timezone.utc)
    mock_logs = MockSpecialist(
        "logs",
        Findings(
            summary="Logs findings",
            evidence=[
                EvidenceItem(kind="error", excerpt="503 missing PAYMENT_GATEWAY_KEY", timestamp=now)
            ],
            anomalies=["Readiness probe 503"],
            queries_run=["query_logs"],
            confidence=0.9,
            errors=[],
        ),
    )
    mock_changes = MockSpecialist(
        "changes",
        Findings(
            summary="Changes findings",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.8,
            errors=[],
        ),
    )
    mock_metrics = MockSpecialist(
        "metrics",
        Findings(
            summary="Metrics findings",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.8,
            errors=[],
        ),
    )

    specialists = {
        "logs": mock_logs,
        "changes": mock_changes,
        "metrics": mock_metrics,
    }

    cfg = AgentConfig()
    disp = Dispatcher(config=cfg, specialists=specialists)

    # Incident with service_hint=None and title containing service name
    incident = Incident(
        id="inc-backfill-1",
        source="servicenow",
        external_id="INC0010056",
        external_key="INC0010056",
        title="shop-api: Scenario 3 (Bad Release) missing PAYMENT_GATEWAY_KEY",
        description="Checkout failing with HTTP 503 on /readyz",
        severity="P1",
        service_hint=None,
        opened_at=now,
    )
    req = InvestigationRequest(
        incident=incident,
        callback_url="https://bridge-test.run.app/callback/report",
        requested_at=now,
    )

    res = await disp.investigate(req)
    assert res.status == "completed"

    # 1. Backfilling incident.service_hint from plan.affected_service
    assert incident.service_hint == "shop-api"

    # 2. Passing plan=plan to specialists in investigate
    assert "plan" in mock_logs.last_kwargs
    plan_passed = mock_logs.last_kwargs["plan"]
    assert isinstance(plan_passed, InvestigationPlan)
    assert plan_passed.affected_service == "shop-api"
    assert mock_logs.last_incident is not None
    assert mock_logs.last_incident.service_hint == "shop-api"
