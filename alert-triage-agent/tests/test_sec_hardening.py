"""Security hardening from the ata-fresh-sec audit (L3, L4, L5): fail-closed startup on Cloud
Run and narrower token acceptance. Nothing here reaches a cloud API."""

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import tools.app.server as tools_server
from tools.app.config import ToolsConfig, reset_tools_config

TOOLS_URL = "https://atat-tools-123456789012.us-central1.run.app"


def _tools_cfg(**overrides: Any) -> ToolsConfig:
    values = {"PROJECT_ID": "proj-x", "NAME_PREFIX": "atat", "REGION": "us-central1",
              "PROJECT_NUMBER": "123456789012", "ENFORCE_AUTH": True, "VERIFY_OIDC": True}
    values.update(overrides)
    return ToolsConfig(**values)


@pytest.fixture
def tools_cfg():
    def use(**overrides: Any) -> ToolsConfig:
        cfg = _tools_cfg(**overrides)
        reset_tools_config(cfg)
        return cfg
    yield use
    reset_tools_config(None)


# ---------------------------------------------------------------------------
# L3: tools refuses to start on Cloud Run with auth off; caller emails compared lowercased
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("enforce,verify", [(False, False), (True, False), (False, True)])
def test_tools_refuses_cloud_run_start_without_full_auth(monkeypatch, tools_cfg, enforce, verify):
    monkeypatch.setenv("K_SERVICE", "atat-tools")
    tools_cfg(ENFORCE_AUTH=enforce, VERIFY_OIDC=verify)
    with pytest.raises(RuntimeError, match="requires ENFORCE_AUTH=true and VERIFY_OIDC=true"):
        tools_server.create_app()


def test_tools_starts_on_cloud_run_with_full_auth(monkeypatch, tools_cfg):
    monkeypatch.setenv("K_SERVICE", "atat-tools")
    tools_cfg()
    assert tools_server.create_app() is not None


def test_tools_without_k_service_may_run_with_auth_off(monkeypatch, tools_cfg):
    monkeypatch.delenv("K_SERVICE", raising=False)
    tools_cfg(ENFORCE_AUTH=False, VERIFY_OIDC=False)
    assert tools_server.create_app() is not None


def _middleware_client(monkeypatch, allowed: list[str], email: str) -> TestClient:
    from google.oauth2 import id_token

    monkeypatch.setattr(id_token, "verify_oauth2_token",
                        lambda token, request, audience=None, **kw: {"email": email, "aud": TOOLS_URL})
    app = Starlette(
        routes=[Route("/mcp", lambda request: PlainTextResponse("ok"), methods=["POST"])],
        middleware=[Middleware(tools_server.AuthMiddleware, enforce_auth=True, verify_oidc=True,
                               expected_audience=[TOOLS_URL], allowed_service_accounts=allowed)],
    )
    return TestClient(app)


def test_tools_caller_email_is_compared_case_insensitively(monkeypatch):
    client = _middleware_client(monkeypatch, ["atat-agent-sa@proj-x.iam.gserviceaccount.com"],
                                "ATAT-Agent-SA@proj-x.iam.gserviceaccount.com")
    assert client.post("/mcp", headers={"Authorization": "Bearer t"}).status_code == 200
    client = _middleware_client(monkeypatch, ["ATAT-agent-sa@proj-x.iam.gserviceaccount.com"],
                                "atat-agent-sa@proj-x.iam.gserviceaccount.com")
    assert client.post("/mcp", headers={"Authorization": "Bearer t"}).status_code == 200


def test_tools_other_caller_is_still_forbidden(monkeypatch):
    client = _middleware_client(monkeypatch, ["atat-agent-sa@proj-x.iam.gserviceaccount.com"],
                                "ata-agent-sa@proj-x.iam.gserviceaccount.com")
    assert client.post("/mcp", headers={"Authorization": "Bearer t"}).status_code == 403


# ---------------------------------------------------------------------------
# L4: IS_LOCAL=true is refused on Cloud Run (K_SERVICE set) in agent, bridge and tools
# ---------------------------------------------------------------------------

def _config_classes():
    from agent.app.config import AgentConfig
    from bridge.app.config import BridgeConfig
    return [AgentConfig, BridgeConfig, ToolsConfig]


@pytest.mark.parametrize("cls_index", [0, 1, 2], ids=["agent", "bridge", "tools"])
def test_is_local_is_refused_on_cloud_run(monkeypatch, cls_index):
    from pydantic import ValidationError

    cls = _config_classes()[cls_index]
    monkeypatch.setenv("K_SERVICE", "atat-svc")
    with pytest.raises(ValidationError, match="IS_LOCAL=true is not allowed on Cloud Run"):
        cls(NAME_PREFIX="atat", IS_LOCAL=True)
    # Off Cloud Run, local mode still works.
    monkeypatch.delenv("K_SERVICE")
    assert cls(NAME_PREFIX="atat", IS_LOCAL=True).is_local is True


@pytest.mark.parametrize("cls_index", [0, 1, 2], ids=["agent", "bridge", "tools"])
def test_non_local_config_on_cloud_run_is_unaffected(monkeypatch, cls_index):
    cls = _config_classes()[cls_index]
    monkeypatch.setenv("K_SERVICE", "atat-svc")
    cfg = cls(NAME_PREFIX="atat", PROJECT_ID="proj-x", REGION="us-central1", IS_LOCAL=False)
    assert cfg.is_local is False
