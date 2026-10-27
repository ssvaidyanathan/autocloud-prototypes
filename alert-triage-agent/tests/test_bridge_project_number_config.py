"""The bridge audience URL's GCP project number comes from config (PROJECT_NUMBER), not a literal.

Covers:
- BridgeConfig reads and validates PROJECT_NUMBER.
- verify_agent_caller accepts the project-number Cloud Run URL built from config and rejects others.
- A non-local bridge without PROJECT_NUMBER fails loudly: startup raises, and requests get a 500.
- bridge/scripts/deploy.sh resolves PROJECT_NUMBER and passes it to Cloud Run.
- No 12-digit project number literal remains in bridge/app.
"""

import asyncio
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from bridge.app import auth as bridge_auth
from bridge.app import main as bridge_main
from bridge.app.config import BridgeConfig

ROOT = Path(__file__).resolve().parents[1]
PROJECT_NUMBER = "123456789012"
# BRIDGE_URL is required too (tests/test_bridge_url_config.py); set it where the bridge must be fully configured.
BRIDGE_URL = "https://ata-bridge-abcdefghij-uw.a.run.app"


def _request(token: str = "real-token") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "server": ("internal.example", 443),
        "path": "/instructions/ticket/create",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode()), (b"host", b"internal.example")],
    }
    return Request(scope)


def _cfg(**overrides) -> BridgeConfig:
    values = {"PROJECT_ID": "proj-x", "NAME_PREFIX": "ata", "REGION": "us-west1", "IS_LOCAL": False}
    values.update(overrides)
    return BridgeConfig(**values)


def _patch_token(monkeypatch, claims: dict) -> None:
    from google.oauth2 import id_token

    monkeypatch.setattr(id_token, "verify_oauth2_token", lambda token, req, *a, **k: dict(claims))


def _agent_email() -> str:
    return "ata-agent-sa@proj-x.iam.gserviceaccount.com"


# --- config -------------------------------------------------------------------------------


def test_project_number_read_from_env(monkeypatch):
    monkeypatch.setenv("PROJECT_NUMBER", PROJECT_NUMBER)
    assert BridgeConfig().project_number == PROJECT_NUMBER


def test_project_number_defaults_to_unset(monkeypatch):
    monkeypatch.delenv("PROJECT_NUMBER", raising=False)
    assert BridgeConfig().project_number is None


@pytest.mark.parametrize("bad", ["cloud-assist-fde-2", "12345678901a", " 1234", "-1"])
def test_project_number_must_be_numeric(bad):
    with pytest.raises(ValidationError):
        _cfg(PROJECT_NUMBER=bad)


def test_empty_project_number_counts_as_unset():
    assert _cfg(PROJECT_NUMBER="").project_number is None


# --- audience validation --------------------------------------------------------------------


def test_project_number_audience_built_from_config_is_accepted(monkeypatch):
    aud = f"https://ata-bridge-{PROJECT_NUMBER}.us-west1.run.app"
    _patch_token(monkeypatch, {"email": _agent_email(), "aud": aud})
    claims = asyncio.run(bridge_auth.verify_agent_caller(_request(), _cfg(PROJECT_NUMBER=PROJECT_NUMBER, BRIDGE_URL=BRIDGE_URL)))
    assert claims["aud"] == aud


def test_other_project_number_audience_is_rejected(monkeypatch):
    _patch_token(monkeypatch, {"email": _agent_email(), "aud": "https://ata-bridge-999999999999.us-west1.run.app"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bridge_auth.verify_agent_caller(_request(), _cfg(PROJECT_NUMBER=PROJECT_NUMBER, BRIDGE_URL=BRIDGE_URL)))
    assert exc.value.status_code == 403


def test_request_base_url_audience_is_rejected(monkeypatch):
    """sec L5: the Host-derived base URL is not an accepted audience; only BRIDGE_URL and the
    deterministic <prefix>-bridge-<number> URL are."""
    _patch_token(monkeypatch, {"email": _agent_email(), "aud": "https://internal.example"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bridge_auth.verify_agent_caller(_request(), _cfg(PROJECT_NUMBER=PROJECT_NUMBER, BRIDGE_URL=BRIDGE_URL)))
    assert exc.value.status_code == 403
    _patch_token(monkeypatch, {"email": _agent_email(), "aud": BRIDGE_URL})
    claims = asyncio.run(bridge_auth.verify_agent_caller(_request(), _cfg(PROJECT_NUMBER=PROJECT_NUMBER, BRIDGE_URL=BRIDGE_URL)))
    assert claims["aud"] == BRIDGE_URL


def test_missing_project_number_non_local_fails_loudly_on_request(monkeypatch, caplog):
    _patch_token(monkeypatch, {"email": _agent_email(), "aud": "https://internal.example"})
    with caplog.at_level("ERROR", logger=bridge_auth.logger.name):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(bridge_auth.verify_agent_caller(_request(), _cfg()))
    assert exc.value.status_code == 500
    assert "PROJECT_NUMBER" in exc.value.detail
    assert any("PROJECT_NUMBER" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_missing_project_number_is_fine_in_local_mode():
    claims = asyncio.run(
        bridge_auth.verify_agent_caller(_request("mock-agent-token"), _cfg(IS_LOCAL=True))
    )
    assert claims["aud"] == "mock-bridge-aud"


# --- startup ---------------------------------------------------------------------------------


def _run_lifespan(cfg: BridgeConfig, monkeypatch) -> None:
    monkeypatch.setattr(bridge_main, "get_settings", lambda: cfg)

    async def _go():
        async with bridge_main.lifespan(bridge_main.app):
            pass

    asyncio.run(_go())


def test_startup_fails_without_project_number_when_not_local(monkeypatch):
    with pytest.raises(RuntimeError, match="PROJECT_NUMBER"):
        _run_lifespan(_cfg(), monkeypatch)


def test_startup_ok_with_project_number(monkeypatch):
    _run_lifespan(_cfg(PROJECT_NUMBER=PROJECT_NUMBER, BRIDGE_URL=BRIDGE_URL), monkeypatch)


def test_startup_ok_without_project_number_in_local_mode(monkeypatch):
    _run_lifespan(_cfg(IS_LOCAL=True), monkeypatch)


# --- wiring and literal guard --------------------------------------------------------------


def test_deploy_script_resolves_and_passes_project_number():
    script = (ROOT / "bridge" / "scripts" / "deploy.sh").read_text()
    lib = (ROOT / "scripts" / "cloud-run-deploy-lib.sh").read_text()
    assert 'PROJECT_NUMBER="$(resolve_project_number)"' in script
    assert 'gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)"' in lib
    assert "PROJECT_NUMBER is required" in lib
    env_line = next(line for line in script.splitlines() if line.startswith("ENV_VARS="))
    assert "|PROJECT_NUMBER=${PROJECT_NUMBER}|" in env_line


def test_no_project_number_literal_in_bridge_app_or_deploy_script():
    paths = list((ROOT / "bridge" / "app").rglob("*.py")) + [ROOT / "bridge" / "scripts" / "deploy.sh"]
    offenders = [str(p) for p in paths if re.search(r"(?<!\d)\d{12}(?!\d)", p.read_text())]
    assert offenders == []
