"""Unit tests for Cloud Run proxy header trust and callback URL scheme derivation."""

from unittest.mock import MagicMock

import pytest
from bridge.app.config import BridgeConfig, get_settings
from bridge.app.core.tasks import TaskDispatcher
from bridge.app.main import app, set_task_dispatcher
from starlette.testclient import TestClient
from uvicorn.config import Config
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


@pytest.fixture
def mock_dispatcher() -> MagicMock:
    dispatcher = MagicMock(spec=TaskDispatcher)
    dispatcher.dispatch.return_value = MagicMock(is_duplicate=False, task_id="test-task-123")
    set_task_dispatcher(dispatcher)
    yield dispatcher
    set_task_dispatcher(None)


def test_uvicorn_config_forwarded_allow_ips_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Uvicorn Config respects FORWARDED_ALLOW_IPS=169.254.0.0/16."""
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "169.254.0.0/16")
    cfg = Config("bridge.app.main:app")
    assert cfg.proxy_headers is True
    assert cfg.forwarded_allow_ips == "169.254.0.0/16"


def test_proxy_headers_trusted_cloud_run_client_derives_https_callback(
    mock_dispatcher: MagicMock,
) -> None:
    """When a request arrives from Cloud Run's front end (169.254.0.0/16) with

    X-Forwarded-Proto: https, the bridge derives an https:// callback URL for the agent.
    """
    test_config = BridgeConfig(
        app_env="local",
        gcp_project="test-project",
        callback_url="",
        servicenow_webhook_token="test-token",
    )
    app.dependency_overrides[get_settings] = lambda: test_config

    try:
        # Wrap app with ProxyHeadersMiddleware configured for Cloud Run's front-end CIDR
        trusted_app = ProxyHeadersMiddleware(app, trusted_hosts="169.254.0.0/16")

        # Client IP in 169.254.0.0/16 simulates Google Front End proxying the connection
        client = TestClient(trusted_app, client=("169.254.8.1", 54321))
        resp = client.post(
            "/webhook/servicenow",
            json={"sys_id": "sys_trusted_01", "number": "INC0010001"},
            headers={
                "Authorization": "Bearer test-token",
                "X-Forwarded-Proto": "https",
            },
        )

        assert resp.status_code == 202
        mock_dispatcher.dispatch.assert_called_once()
        call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
        dispatched_callback = call_kwargs.get("callback_url")
        assert dispatched_callback == "https://testserver/callback/report"
    finally:
        app.dependency_overrides.clear()


def test_proxy_headers_untrusted_client_spoofed_proto_ignored(
    mock_dispatcher: MagicMock,
) -> None:
    """When a request arrives from an untrusted client IP outside 169.254.0.0/16,

    a spoofed X-Forwarded-Proto: https header is ignored and scheme remains http.
    """
    test_config = BridgeConfig(
        app_env="local",
        gcp_project="test-project",
        callback_url="",
        servicenow_webhook_token="test-token",
    )
    app.dependency_overrides[get_settings] = lambda: test_config

    try:
        trusted_app = ProxyHeadersMiddleware(app, trusted_hosts="169.254.0.0/16")

        # Public/untrusted client IP attempting to spoof X-Forwarded-Proto
        client = TestClient(trusted_app, client=("203.0.113.195", 54321))
        resp = client.post(
            "/webhook/servicenow",
            json={"sys_id": "sys_untrusted_02", "number": "INC0010002"},
            headers={
                "Authorization": "Bearer test-token",
                "X-Forwarded-Proto": "https",
            },
        )

        assert resp.status_code == 202
        mock_dispatcher.dispatch.assert_called_once()
        call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
        dispatched_callback = call_kwargs.get("callback_url")
        assert dispatched_callback == "http://testserver/callback/report"
    finally:
        app.dependency_overrides.clear()


def test_proxy_headers_explicit_callback_url_precedence(
    mock_dispatcher: MagicMock,
) -> None:
    """When settings.callback_url is explicitly configured, it takes precedence

    regardless of client IP or proxy headers.
    """
    explicit_url = "https://explicit-bridge.example.com/callback/report"
    test_config = BridgeConfig(
        app_env="local",
        gcp_project="test-project",
        callback_url=explicit_url,
        servicenow_webhook_token="test-token",
    )
    app.dependency_overrides[get_settings] = lambda: test_config

    try:
        trusted_app = ProxyHeadersMiddleware(app, trusted_hosts="169.254.0.0/16")
        client = TestClient(trusted_app, client=("169.254.1.1", 54321))
        resp = client.post(
            "/webhook/servicenow",
            json={"sys_id": "sys_explicit_03", "number": "INC0010003"},
            headers={
                "Authorization": "Bearer test-token",
                "X-Forwarded-Proto": "http",
            },
        )

        assert resp.status_code == 202
        mock_dispatcher.dispatch.assert_called_once()
        call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
        assert call_kwargs.get("callback_url") == explicit_url
    finally:
        app.dependency_overrides.clear()
