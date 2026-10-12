from typing import Any, Optional

import pytest
from bridge.app.config import BridgeConfig
from bridge.app.core.auth import create_mock_oidc_token, verify_oidc_token
from bridge.tests.test_adapters import _make_dummy_request
from fastapi import HTTPException


def test_callback_auth_missing_header() -> None:
    config = BridgeConfig(
        app_env="local",
        callback_url="https://bridge.example.com/callback",
        agent_sa="agent-sa@project.iam.gserviceaccount.com",
    )
    req = _make_dummy_request({})
    with pytest.raises(HTTPException) as exc_info:
        verify_oidc_token(req, config)
    assert exc_info.value.status_code == 401
    assert "Missing Authorization header" in exc_info.value.detail


def test_callback_auth_invalid_header_format() -> None:
    config = BridgeConfig(app_env="local")
    req = _make_dummy_request({"Authorization": "Basic dXNlcjpwYXNz"})
    with pytest.raises(HTTPException) as exc_info:
        verify_oidc_token(req, config)
    assert exc_info.value.status_code == 401
    assert "expected 'Bearer <token>'" in exc_info.value.detail


def test_callback_auth_default_mock_token_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    config = BridgeConfig(
        app_env="local",
        callback_url="https://bridge.example.com/callback",
        agent_sa="agent-sa@project.iam.gserviceaccount.com",
    )
    req = _make_dummy_request({"Authorization": "Bearer mock-oidc-token-for-testing"})
    claims = verify_oidc_token(req, config)
    assert claims["mock"] is True


def test_callback_auth_jwt_valid_claims_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    aud = "https://bridge.example.com/callback"
    email = "agent-sa@project.iam.gserviceaccount.com"
    config = BridgeConfig(app_env="local", callback_url=aud, agent_sa=email)

    token = create_mock_oidc_token(audience=aud, email=email)
    req = _make_dummy_request({"Authorization": f"Bearer {token}"})

    claims = verify_oidc_token(req, config)
    assert claims["aud"] == aud
    assert claims["email"] == email


def test_callback_auth_reject_wrong_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    expected_aud = "https://bridge.example.com/callback"
    actual_aud = "https://wrong-audience.example.com"
    email = "agent-sa@project.iam.gserviceaccount.com"
    config = BridgeConfig(app_env="local", callback_url=expected_aud, agent_sa=email)

    token = create_mock_oidc_token(audience=actual_aud, email=email)
    req = _make_dummy_request({"Authorization": f"Bearer {token}"})

    with pytest.raises(HTTPException) as exc_info:
        verify_oidc_token(req, config)
    assert exc_info.value.status_code == 403
    assert "Invalid token audience" in exc_info.value.detail


def test_callback_auth_reject_wrong_service_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    aud = "https://bridge.example.com/callback"
    expected_email = "agent-sa@project.iam.gserviceaccount.com"
    wrong_email = "attacker-sa@evil.iam.gserviceaccount.com"
    config = BridgeConfig(app_env="local", callback_url=aud, agent_sa=expected_email)

    token = create_mock_oidc_token(audience=aud, email=wrong_email)
    req = _make_dummy_request({"Authorization": f"Bearer {token}"})

    with pytest.raises(HTTPException) as exc_info:
        verify_oidc_token(req, config)
    assert exc_info.value.status_code == 403
    assert "Invalid caller service account" in exc_info.value.detail


def test_callback_auth_mock_token_rejected_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "production")
    config = BridgeConfig(
        app_env="production",
        callback_url="https://bridge.example.com/callback",
        agent_sa="agent-sa@project.iam.gserviceaccount.com",
    )
    req = _make_dummy_request({"Authorization": "Bearer mock-oidc-token-for-testing"})
    with pytest.raises(HTTPException) as exc_info:
        verify_oidc_token(req, config)
    assert exc_info.value.status_code == 401
    assert "Mock OIDC token not allowed in production" in exc_info.value.detail


def test_callback_auth_invalid_signature_rejected_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "production")
    config = BridgeConfig(
        app_env="production",
        callback_url="https://bridge.example.com/callback",
        agent_sa="agent-sa@project.iam.gserviceaccount.com",
    )
    # create_mock_oidc_token produces an unsigned token with dummy signature
    token = create_mock_oidc_token(
        audience="https://bridge.example.com/callback",
        email="agent-sa@project.iam.gserviceaccount.com",
    )
    req = _make_dummy_request({"Authorization": f"Bearer {token}"})
    with pytest.raises(HTTPException) as exc_info:
        verify_oidc_token(req, config)
    assert exc_info.value.status_code == 401
    assert "OIDC token verification failed" in exc_info.value.detail


def test_callback_auth_valid_token_production_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "production")
    aud = "https://bridge.example.com/callback"
    email = "agent-sa@project.iam.gserviceaccount.com"
    config = BridgeConfig(
        app_env="production",
        callback_url=aud,
        agent_sa=email,
    )

    from google.oauth2 import id_token

    def mock_verify_oauth2_token(
        token: str, request: Any, audience: Optional[str] = None
    ) -> dict[str, Any]:
        return {"aud": aud, "email": email, "sub": "1234567890"}

    monkeypatch.setattr(id_token, "verify_oauth2_token", mock_verify_oauth2_token)

    req = _make_dummy_request({"Authorization": "Bearer valid-signed-google-id-token"})
    claims = verify_oidc_token(req, config)
    assert claims["aud"] == aud
    assert claims["email"] == email
