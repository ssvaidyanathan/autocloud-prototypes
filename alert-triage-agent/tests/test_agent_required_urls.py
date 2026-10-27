"""crev R6: the agent's BRIDGE_URL and AGENT_URL are required when not local, like TOOLS_URL.
No silent localhost fallback outside IS_LOCAL=true; startup fails naming the missing settings."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent.app.config import AgentConfig, get_agent_settings
from agent.app.main import app as agent_app

ROOT = Path(__file__).resolve().parents[1]
TOOLS_URL = "https://ata-tools-abc-uw.a.run.app"
BRIDGE_URL = "https://ata-bridge-abc-uw.a.run.app"
AGENT_URL = "https://ata-agent-abc-uw.a.run.app"


def _cfg(tmp_path, **kw):
    return AgentConfig(is_local=False, incident_store_path=str(tmp_path), fallback_store_path=str(tmp_path), **kw)


def test_non_local_without_bridge_or_agent_url_has_no_localhost_default(monkeypatch, tmp_path):
    monkeypatch.delenv("BRIDGE_URL", raising=False)
    monkeypatch.delenv("AGENT_URL", raising=False)
    cfg = _cfg(tmp_path, TOOLS_URL=TOOLS_URL)
    assert cfg.bridge_url is None and cfg.agent_url is None
    assert cfg.missing_required_settings() == ["BRIDGE_URL", "AGENT_URL"]


@pytest.mark.parametrize("given,missing", [
    ({"AGENT_URL": AGENT_URL}, ["BRIDGE_URL"]),
    ({"BRIDGE_URL": BRIDGE_URL}, ["AGENT_URL"]),
    ({"BRIDGE_URL": "", "AGENT_URL": AGENT_URL}, ["BRIDGE_URL"]),  # empty counts as unset
])
def test_each_url_is_required(monkeypatch, tmp_path, given, missing):
    monkeypatch.delenv("BRIDGE_URL", raising=False)
    monkeypatch.delenv("AGENT_URL", raising=False)
    assert _cfg(tmp_path, TOOLS_URL=TOOLS_URL, **given).missing_required_settings() == missing


def test_startup_fails_without_bridge_url_and_succeeds_with_all(monkeypatch, tmp_path):
    monkeypatch.delenv("BRIDGE_URL", raising=False)
    cfg = _cfg(tmp_path, TOOLS_URL=TOOLS_URL, AGENT_URL=AGENT_URL)
    agent_app.dependency_overrides[get_agent_settings] = lambda: cfg
    try:
        with pytest.raises(RuntimeError, match=r"required setting\(s\) not set: BRIDGE_URL"):
            with TestClient(agent_app):
                pass
        ok = cfg.model_copy(update={"bridge_url": BRIDGE_URL})
        agent_app.dependency_overrides[get_agent_settings] = lambda: ok
        with TestClient(agent_app) as client:
            assert client.get("/health").status_code == 200
    finally:
        agent_app.dependency_overrides.pop(get_agent_settings, None)


def test_local_mode_keeps_the_dev_ports(monkeypatch, tmp_path):
    monkeypatch.delenv("BRIDGE_URL", raising=False)
    monkeypatch.delenv("AGENT_URL", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    cfg = AgentConfig(is_local=True, incident_store_path=str(tmp_path), fallback_store_path=str(tmp_path))
    assert (cfg.bridge_url, cfg.agent_url) == ("http://localhost:8080", "http://localhost:8000")
    assert cfg.missing_required_settings() == []


def test_agent_deploy_script_sets_both():
    text = (ROOT / "agent" / "scripts" / "deploy.sh").read_text()
    assert "BRIDGE_URL=${BRIDGE_URL}" in text and "AGENT_URL=${AGENT_URL}" in text
