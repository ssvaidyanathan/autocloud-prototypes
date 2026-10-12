from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.app.callback import CallbackClient
from agent.app.contracts.report import Report
from agent.app.contracts.result import InvestigationResult
from agent.app.dispatcher import Dispatcher
from agent.app.main import app, set_callback_client, set_dispatcher
from fastapi.testclient import TestClient


def _sample_request_payload() -> dict[str, Any]:
    return {
        "incident": {
            "id": "inc-api-1",
            "source": "servicenow",
            "external_id": "sys_999",
            "external_key": "INC00999",
            "title": "Shop API failure",
            "description": "500 errors on checkout",
            "severity": "P1",
            "service_hint": "shop-api",
            "opened_at": "2026-10-09T02:00:00Z",
        },
        "callback_url": "https://bridge.run.app/callback/report",
        "requested_at": "2026-10-09T02:01:00Z",
    }


@pytest.fixture(autouse=True)
def reset_globals() -> Generator[None, None, None]:
    yield
    set_dispatcher(None)
    set_callback_client(None)


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "sre-agent"


def test_investigate_endpoint_default_synchronous() -> None:
    client = TestClient(app)
    mock_dispatcher = MagicMock(spec=Dispatcher)
    mock_callback = MagicMock(spec=CallbackClient)

    report = Report(
        summary="Done",
        impact="None",
        timeline=[],
        evidence_by_area={},
        probable_root_cause=None,
        recent_changes=[],
        recommended_actions=[],
        specialists_status=[],
        skills_used=[],
        queries_appendix={},
        metadata={},
    )
    mock_result = InvestigationResult(
        investigation_id="inv-123",
        incident_id="inc-api-1",
        incident_source="servicenow",
        incident_external_id="sys_999",
        incident_external_key="INC00999",
        status="completed",
        report=report,
    )
    mock_dispatcher.investigate = AsyncMock(return_value=mock_result)
    mock_callback.deliver_async = AsyncMock()

    set_dispatcher(mock_dispatcher)
    set_callback_client(mock_callback)

    response = client.post("/investigate", json=_sample_request_payload())

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["result_status"] == "completed"
    assert "investigation_id" in data
    assert mock_dispatcher.investigate.called
    assert mock_callback.deliver_async.called


def test_investigate_endpoint_sync_completed() -> None:
    client = TestClient(app)
    mock_dispatcher = MagicMock(spec=Dispatcher)
    mock_callback = MagicMock(spec=CallbackClient)

    report = Report(
        summary="Done",
        impact="None",
        timeline=[],
        evidence_by_area={},
        probable_root_cause=None,
        recent_changes=[],
        recommended_actions=[],
        specialists_status=[],
        skills_used=[],
        queries_appendix={},
        metadata={},
    )
    mock_result = InvestigationResult(
        investigation_id="inv-124",
        incident_id="inc-api-1",
        incident_source="servicenow",
        incident_external_id="sys_999",
        incident_external_key="INC00999",
        status="completed",
        report=report,
    )
    mock_dispatcher.investigate = AsyncMock(return_value=mock_result)
    mock_callback.deliver_async = AsyncMock()

    set_dispatcher(mock_dispatcher)
    set_callback_client(mock_callback)

    response = client.post("/investigate?sync=true", json=_sample_request_payload())

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["result_status"] == "completed"


def test_investigate_endpoint_validation_error() -> None:
    client = TestClient(app)
    # Missing required fields
    response = client.post("/investigate", json={"incident": {}})
    assert response.status_code == 422


def test_investigate_error_handling_delivers_failure_callback() -> None:
    client = TestClient(app)
    mock_dispatcher = MagicMock(spec=Dispatcher)
    mock_callback = MagicMock(spec=CallbackClient)

    # Simulate dispatcher unexpected failure
    mock_dispatcher.investigate = AsyncMock(side_effect=RuntimeError("Catastrophic error"))
    mock_callback.deliver_async = AsyncMock()

    set_dispatcher(mock_dispatcher)
    set_callback_client(mock_callback)

    response = client.post("/investigate?sync=true", json=_sample_request_payload())

    assert response.status_code == 200
    data = response.json()
    assert data["result_status"] == "failed"

    # Verify callback was delivered with failed result
    mock_callback.deliver_async.assert_called_once()
    called_args = mock_callback.deliver_async.call_args[0]
    delivered_result = called_args[1]
    assert isinstance(delivered_result, InvestigationResult)
    assert delivered_result.status == "failed"
    assert "Catastrophic error" in (delivered_result.error or "")
