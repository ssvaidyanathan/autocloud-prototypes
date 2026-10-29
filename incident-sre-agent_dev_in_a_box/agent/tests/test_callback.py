import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from agent.app.callback import CallbackClient, _default_token_provider
from agent.app.config import AgentConfig
from agent.app.contracts.report import Report
from agent.app.contracts.result import InvestigationResult


def _make_result(status: str = "completed") -> InvestigationResult:
    if status == "completed":
        report = Report(
            summary="Investigation summary",
            impact="High impact",
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
        return InvestigationResult(
            investigation_id="inv-cb-1",
            incident_id="inc-101",
            incident_source="servicenow",
            incident_external_id="INC001",
            incident_external_key="INC001",
            status="completed",
            report=report,
        )
    return InvestigationResult(
        investigation_id="inv-cb-2",
        incident_id="inc-102",
        incident_source="jira",
        incident_external_id="OPS-1",
        incident_external_key="OPS-1",
        status="failed",
        error="Specialist crashed",
    )


def test_callback_url_validation_allowlist() -> None:
    config = AgentConfig(allowed_callback_hosts=["*.run.app", "localhost", "127.0.0.1"])
    client = CallbackClient(config=config)

    # Valid HTTPS on allowed wildcard domain
    client.validate_url("https://incident-bridge-xyz.a.run.app/callback/report")
    client.validate_url("https://my-bridge.run.app/callback")

    # Valid HTTP on localhost / 127.0.0.1
    client.validate_url("http://localhost:8000/callback")
    client.validate_url("http://127.0.0.1:8000/callback")

    # Reject HTTP for non-localhost
    with pytest.raises(ValueError) as exc1:
        client.validate_url("http://incident-bridge.run.app/callback")
    assert "HTTPS is required" in str(exc1.value)

    # Reject domain not in allowlist
    with pytest.raises(ValueError) as exc2:
        client.validate_url("https://malicious-site.com/callback")
    assert "not in allowed hosts" in str(exc2.value)


def test_callback_oidc_token_header_injection() -> None:
    mock_token_provider = MagicMock(return_value="test-oidc-jwt-token")
    mock_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"}))
    http_client = httpx.Client(transport=mock_transport)

    client = CallbackClient(
        token_provider=mock_token_provider,
        http_client=http_client,
    )
    result = _make_result("completed")

    callback_url = "https://bridge.run.app/callback/report"
    resp = client.deliver(callback_url, result)

    assert resp.status_code == 200
    mock_token_provider.assert_called_once_with(callback_url)


def test_callback_structured_logging_emission(capsys: pytest.CaptureFixture[str]) -> None:
    client = CallbackClient()
    result = _make_result("completed")

    log_entry = client.emit_structured_audit_log(result)
    assert log_entry["jsonPayload"]["type"] == "sre_report"
    assert log_entry["jsonPayload"]["investigation_id"] == "inv-cb-1"
    assert log_entry["jsonPayload"]["result"]["status"] == "completed"

    # Verify stdout JSON output for Cloud Logging capture
    captured = capsys.readouterr()
    assert "sre_report" in captured.out
    parsed_out = json.loads(captured.out.strip().split("\n")[-1])
    assert parsed_out["jsonPayload"]["type"] == "sre_report"


def test_callback_retry_on_transient_5xx_errors() -> None:
    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, json={"error": "Service Unavailable"})
        return httpx.Response(200, json={"status": "received"})

    mock_transport = httpx.MockTransport(mock_handler)
    http_client = httpx.Client(transport=mock_transport)

    client = CallbackClient(
        http_client=http_client,
        token_provider=lambda aud: "token",
        max_retries=3,
        backoff_factor=0.01,  # fast test
    )
    result = _make_result("completed")

    resp = client.deliver("https://bridge.run.app/callback", result)
    assert resp.status_code == 200
    assert attempts == 3  # 2 failures + 1 success


def test_callback_immediate_failure_on_4xx_client_error() -> None:
    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, json={"error": "Forbidden"})

    mock_transport = httpx.MockTransport(mock_handler)
    http_client = httpx.Client(transport=mock_transport)

    client = CallbackClient(
        http_client=http_client,
        token_provider=lambda aud: "token",
        max_retries=3,
        backoff_factor=0.01,
    )
    result = _make_result("completed")

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        client.deliver("https://bridge.run.app/callback", result)

    assert exc_info.value.response.status_code == 403
    # Non-retryable: must fail on attempt 1 without any retries
    assert attempts == 1


def test_default_token_provider_fallback_in_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")

    def mock_get(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("Metadata server unreachable")

    monkeypatch.setattr(httpx.Client, "get", mock_get)
    token = _default_token_provider("https://bridge.run.app/callback/report")
    assert token == "mock-oidc-token-for-testing"


def test_default_token_provider_raises_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "production")

    def mock_get(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("Metadata server unreachable")

    monkeypatch.setattr(httpx.Client, "get", mock_get)
    with pytest.raises(RuntimeError) as exc_info:
        _default_token_provider("https://bridge.run.app/callback/report")
    assert "Failed to fetch OIDC ID token" in str(exc_info.value)


def test_callback_delivery_preserves_structured_log_on_token_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "production")

    def failing_token_provider(aud: str) -> str:
        raise RuntimeError("Token provider failed")

    client = CallbackClient(token_provider=failing_token_provider)
    result = _make_result("completed")

    with pytest.raises(RuntimeError) as exc_info:
        client.deliver("https://bridge.run.app/callback", result)
    assert "Token provider failed" in str(exc_info.value)

    # Verify structured audit log was emitted BEFORE the token failure occurred
    captured = capsys.readouterr()
    assert "sre_report" in captured.out
    assert "inv-cb-1" in captured.out


def test_callback_client_timeout_defaults() -> None:
    client = CallbackClient()
    assert client.config.callback_read_timeout_seconds == 45.0
    assert client.config.callback_connect_timeout_seconds == 5.0
