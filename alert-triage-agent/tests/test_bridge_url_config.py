"""The bridge's accepted ID-token audiences and callers come from config, not literals.

auth.py used to accept the hash-style audience https://<prefix>-bridge-4klvlqnmpa-uw.a.run.app
(one project's Cloud Run URL) and the caller scion-fde-ops@deploy-demo-test.iam.gserviceaccount.com
(one operator account) for every deployment. Now:
- BRIDGE_URL (the bridge's own status.url, which the agent mints its tokens for) is a
  validated, required setting when not local: startup fails and requests get a 500 without it.
- Callers are ata-agent-sa, ata-bridge-sa and the comma-separated VERIFIER_SA list only.
"""

import asyncio
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from bridge.app import auth as bridge_auth
from bridge.app import main as bridge_main
from bridge.app.config import BridgeConfig

ROOT = Path(__file__).resolve().parents[1]
PROJECT_NUMBER = "123456789012"
BRIDGE_URL = "https://ata-bridge-abcdefghij-uw.a.run.app"
AGENT = "ata-agent-sa@proj-x.iam.gserviceaccount.com"


def _cfg(**overrides) -> BridgeConfig:
    values = {"PROJECT_ID": "proj-x", "NAME_PREFIX": "ata", "REGION": "us-west1", "IS_LOCAL": False,
              "PROJECT_NUMBER": PROJECT_NUMBER, "BRIDGE_URL": BRIDGE_URL}
    values.update(overrides)
    return BridgeConfig(**values)


def _request():
    from fastapi import Request

    return Request({
        "type": "http", "method": "POST", "scheme": "http", "server": ("internal.example", 80),
        "path": "/instructions/ticket/create", "root_path": "", "query_string": b"",
        "headers": [(b"authorization", b"Bearer real-token"), (b"host", b"internal.example")],
    })


def _verify(monkeypatch, cfg, claims):
    from google.oauth2 import id_token

    monkeypatch.setattr(id_token, "verify_oauth2_token", lambda token, req, *a, **k: dict(claims))
    return asyncio.run(bridge_auth.verify_agent_caller(_request(), cfg))


# --- config ---------------------------------------------------------------------------------


def test_bridge_url_read_from_env(monkeypatch):
    monkeypatch.setenv("BRIDGE_URL", BRIDGE_URL + "/")
    assert BridgeConfig().bridge_url == BRIDGE_URL


@pytest.mark.parametrize("bad", ["http://ata-bridge.a.run.app", "ata-bridge.a.run.app",
                                 "https://ata-bridge.a.run.app/instructions", "https://a b"])
def test_bridge_url_must_be_an_https_url_without_path(bad):
    with pytest.raises(ValidationError):
        _cfg(BRIDGE_URL=bad)


def test_bridge_url_is_required_when_not_local():
    assert _cfg(BRIDGE_URL="").missing_required_settings() == ["BRIDGE_URL"]
    assert _cfg(BRIDGE_URL="", PROJECT_NUMBER="").missing_required_settings() == ["PROJECT_NUMBER", "BRIDGE_URL"]
    assert _cfg(BRIDGE_URL="", IS_LOCAL=True).missing_required_settings() == []


# --- audiences ------------------------------------------------------------------------------


def test_configured_bridge_url_is_an_accepted_audience(monkeypatch):
    assert _verify(monkeypatch, _cfg(), {"email": AGENT, "aud": BRIDGE_URL})["aud"] == BRIDGE_URL


def test_former_hardcoded_hash_audience_is_rejected(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _verify(monkeypatch, _cfg(), {"email": AGENT, "aud": "https://ata-bridge-4klvlqnmpa-uw.a.run.app"})
    assert exc.value.status_code == 403


def test_project_number_audience_still_accepted(monkeypatch):
    aud = f"https://ata-bridge-{PROJECT_NUMBER}.us-west1.run.app"
    assert _verify(monkeypatch, _cfg(), {"email": AGENT, "aud": aud})["aud"] == aud


def test_missing_bridge_url_fails_loudly_on_request(monkeypatch, caplog):
    with caplog.at_level("ERROR", logger=bridge_auth.logger.name):
        with pytest.raises(HTTPException) as exc:
            _verify(monkeypatch, _cfg(BRIDGE_URL=""), {"email": AGENT, "aud": BRIDGE_URL})
    assert exc.value.status_code == 500
    assert exc.value.detail == "Bridge misconfigured: BRIDGE_URL not set"
    assert any("BRIDGE_URL" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_startup_fails_without_bridge_url_when_not_local(monkeypatch):
    monkeypatch.setattr(bridge_main, "get_settings", lambda: _cfg(BRIDGE_URL=""))

    async def _go():
        async with bridge_main.lifespan(bridge_main.app):
            pass

    with pytest.raises(RuntimeError, match="BRIDGE_URL"):
        asyncio.run(_go())


# --- callers --------------------------------------------------------------------------------


def test_former_hardcoded_operator_is_not_a_caller_by_default(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _verify(monkeypatch, _cfg(), {"email": "scion-fde-ops@deploy-demo-test.iam.gserviceaccount.com", "aud": BRIDGE_URL})
    assert exc.value.status_code == 403


def test_verifier_sa_list_grants_callers(monkeypatch):
    cfg = _cfg(VERIFIER_SA="ops@other.iam.gserviceaccount.com, qa@other.iam.gserviceaccount.com")
    for email in ("ops@other.iam.gserviceaccount.com", "qa@other.iam.gserviceaccount.com", AGENT,
                  "ata-bridge-sa@proj-x.iam.gserviceaccount.com"):
        assert _verify(monkeypatch, cfg, {"email": email, "aud": BRIDGE_URL})["email"] == email


def test_no_environment_literals_in_bridge_auth():
    text = (ROOT / "bridge" / "app" / "auth.py").read_text()
    assert not re.search(r"-[a-z0-9]{10}-[a-z]{2}\.a\.run\.app", text)
    assert "deploy-demo-test" not in text and "scion-fde-ops" not in text
