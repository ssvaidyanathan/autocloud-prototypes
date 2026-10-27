"""The agent reads the change timeline from the deployed ata-tools FastMCP service over HTTP/MCP.

Before: change.py did ``from tools.app.server import get_change_timeline``. The agent image
does not ship ``tools``, so this always raised ImportError, the specialist fell back to Cloud
Run-only queries, and tickets said "Apigee proxy deployment history not queried".

Now (tools_client.py + change.py):
- one JSON-RPC ``tools/call`` POST to ``<TOOLS_URL>/mcp`` with an ID token whose audience is
  TOOLS_URL, bounded retries on transient failures;
- every failure is stated loudly ("change timeline unavailable: <reason>", ERROR log,
  Change-Induced "Not assessed"), with no Cloud Run-only fallback;
- a successful timeline puts the Apigee deployment history in every investigation.

The success and partial tests run the real ata-tools app (auth middleware included) in
process through httpx.ASGITransport; only its Cloud Run / Apigee REST reads are stubbed.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import agent.app.specialists.change as change_module
import tools.app.server as tools_server
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.main import app as agent_app
from agent.app.specialists.change import ChangeSpecialist
from agent.app.specialists.synthesizer import SynthesizerAgent
from agent.app.specialists.telemetry_reads import CHANGE_SOURCES_KEY
from agent.app.tools import tools_client
from agent.app.tools.tools_client import ToolsCallError, call_tool, fetch_change_timeline
from tools.app.config import ToolsConfig, reset_tools_config

from tests.test_group_origin_selection import (  # noqa: F401  (fixtures)
    _entry,
    _event,
    agent_cfg,
    telemetry,
)

TOOLS_URL = "https://ata-tools-123456789012.us-west1.run.app"
PROJECT = "proj-x"
AGENT_SA = f"ata-agent-sa@{PROJECT}.iam.gserviceaccount.com"
OTHER_SA = f"intruder@{PROJECT}.iam.gserviceaccount.com"
APIGEE_ORG = "org-x"
APIGEE_ENV = "env-x"

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ONSET = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
DEPLOYED = (NOW - timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
OLD = (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(iso: str) -> str:
    return str(int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000))


# --- stubbed upstream REST reads of ata-tools -----------------------------------------------

SVC = f"projects/{PROJECT}/locations/us-west1/services/ata-orders-api"
CLOUD_RUN_SERVICES = {"services": [{
    "name": SVC,
    "traffic": [{"revision": "ata-orders-api-00009-new", "percent": 100}],
    "updateTime": DEPLOYED,
}]}
CLOUD_RUN_REVISIONS = {"revisions": [
    {"name": f"{SVC}/revisions/ata-orders-api-00009-new", "createTime": DEPLOYED,
     "containers": [{"image": "img@sha256:new", "env": []}]},
    {"name": f"{SVC}/revisions/ata-orders-api-00008-old", "createTime": OLD,
     "containers": [{"image": "img@sha256:old", "env": []}]},
]}
APIGEE_DEPLOYMENTS = {"deployments": [
    {"environment": APIGEE_ENV, "apiProxy": "ata-orders-v1", "revision": "177", "deployStartTime": _ms(DEPLOYED)},
    {"environment": APIGEE_ENV, "apiProxy": "ata-inventory-v1", "revision": "149", "deployStartTime": _ms(OLD)},
    # Another stack's proxy in the same org/environment is not this stack's history.
    {"environment": APIGEE_ENV, "apiProxy": "httpbin", "revision": "1", "deployStartTime": _ms(DEPLOYED)},
]}


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _upstream_get(apigee_status: int = 200):
    calls: list[str] = []

    def get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        if url.startswith("https://apigee.googleapis.com/"):
            return _Resp(apigee_status, APIGEE_DEPLOYMENTS)
        if url.endswith("/revisions"):
            return _Resp(200, CLOUD_RUN_REVISIONS)
        return _Resp(200, CLOUD_RUN_SERVICES)

    get.calls = calls  # type: ignore[attr-defined]
    return get


def _tools_cfg(**overrides: Any) -> ToolsConfig:
    values = {
        "PROJECT_ID": PROJECT, "NAME_PREFIX": "ata", "REGION": "us-west1", "PROJECT_NUMBER": "123456789012",
        "APIGEE_ORG": APIGEE_ORG, "APIGEE_ENV": APIGEE_ENV, "ENFORCE_AUTH": True, "VERIFY_OIDC": True,
    }
    values.update(overrides)
    return ToolsConfig(**values)


def _patch_verify(monkeypatch, tokens: dict[str, dict]) -> list:
    """Stub Google ID-token verification: token -> claims; audience checked like google-auth."""
    from google.oauth2 import id_token

    seen: list = []

    def verify(token, request, audience=None, **kw):
        seen.append(audience)
        claims = tokens.get(token)
        if claims is None:
            raise ValueError("Could not verify token signature")
        allowed = audience if isinstance(audience, list) else [audience]
        if claims["aud"] not in allowed:
            raise ValueError(f"Token has wrong audience {claims['aud']}, expected one of {allowed}")
        return dict(claims)

    monkeypatch.setattr(id_token, "verify_oauth2_token", verify)
    return seen


@pytest.fixture
def tools_service(monkeypatch):
    """The real ata-tools ASGI app, auth on, config-derived audiences and callers."""
    monkeypatch.setattr(tools_server, "_get_auth_headers", lambda: {})
    state: dict[str, Any] = {"get": _upstream_get(), "audiences": []}
    monkeypatch.setattr(tools_server.requests, "get", lambda *a, **k: state["get"](*a, **k))
    state["verified_audiences"] = _patch_verify(monkeypatch, {
        "agent-token": {"email": AGENT_SA, "aud": TOOLS_URL},
    })

    def token(audience: str) -> str:
        state["audiences"].append(audience)
        return "agent-token"

    monkeypatch.setattr(tools_client, "fetch_id_token", token)

    def start(**cfg_overrides: Any):
        reset_tools_config(_tools_cfg(**cfg_overrides))
        app = tools_server.create_app()
        tools_client.set_default_transport(httpx.ASGITransport(app=app))
        return app

    state["start"] = start
    yield state
    tools_client.set_default_transport(None)
    reset_tools_config(None)


def _remote_cfg(agent_cfg: AgentConfig, **kw: Any) -> AgentConfig:
    return agent_cfg.model_copy(update={"tools_url": TOOLS_URL, "tools_retry_base_seconds": 0.0, **kw})


def _use_real_client(monkeypatch) -> None:
    # The shared `telemetry` fixture stubs the client seam; these tests exercise the real one.
    monkeypatch.setattr(change_module, "fetch_change_timeline", fetch_change_timeline)


def _orders_alert() -> AlertEvent:
    return _event(_entry("0.orders-5xx", "ata-orders-5xx", "ata-orders-api", "5xx", started_at=ONSET))


async def _investigate(app, alert, cfg):
    async with app.router.lifespan_context(app):
        return await SynthesizerAgent().execute_investigation(alert, "grp-tl", "incident-grp-tl.md", cfg)


# ---------------------------------------------------------------------------------------
# Success: Cloud Run and Apigee history from the deployed service
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_ticket_states_apigee_deployment_history_and_never_not_queried(
    monkeypatch, agent_cfg, telemetry, tools_service
):
    _use_real_client(monkeypatch)
    telemetry["degraded"] = {"ata-orders-api": "5xx"}
    telemetry["traffic"] = {"ata-orders-api": {"ata-orders-api-00008-old": [8] * 30}}
    app = tools_service["start"]()

    report = await _investigate(app, _orders_alert(), _remote_cfg(agent_cfg))

    evidence = str(report.evidence_by_area)
    desc = report.build_structured_description(project_id=PROJECT)
    # Apigee deployment history (this stack's proxies only) is in the ticket.
    assert (f"Apigee deployment history (org {APIGEE_ORG}, environment {APIGEE_ENV}, proxies ata-*): "
            f"proxy ata-orders-v1 revision 177 deployed {DEPLOYED} (12m before alert onset).") in evidence
    assert "httpbin" not in evidence
    # The Cloud Run revision from the same timeline is the implicated change.
    assert report.change_induced.startswith("Yes (revision ata-orders-api-00009-new on ata-orders-api")
    assert "not queried" not in desc and "not queried" not in evidence
    assert "change timeline unavailable" not in evidence
    # The ID token was minted for, and verified against, the URL called.
    assert tools_service["audiences"] == [TOOLS_URL]
    assert TOOLS_URL in tools_service["verified_audiences"][0]
    # ata-tools read APIGEE_ORG / APIGEE_ENV from its config.
    assert (f"https://apigee.googleapis.com/v1/organizations/{APIGEE_ORG}/environments/{APIGEE_ENV}/deployments"
            in tools_service["get"].calls)


@pytest.mark.asyncio
async def test_no_change_in_window_is_no_and_names_both_timeline_sources(monkeypatch, agent_cfg, telemetry, tools_service):
    _use_real_client(monkeypatch)
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}
    monkeypatch.setattr(tools_server.requests, "get", lambda url, **k: _Resp(200, {"services": [], "deployments": [
        {"environment": APIGEE_ENV, "apiProxy": "ata-inventory-v1", "revision": "149", "deployStartTime": _ms(OLD)},
    ]}))
    app = tools_service["start"]()
    alert = _event(_entry("0.inv", "ata-inventory-5xx", "ata-inventory-api", "5xx", started_at=ONSET))

    report = await _investigate(app, alert, _remote_cfg(agent_cfg))

    assert report.change_induced.startswith("No (no deployment or configuration change on ata-inventory-api")
    assert "ata-tools change timeline (Cloud Run revision history)" in report.change_induced
    assert "ata-tools change timeline (Apigee deployment history)" in report.change_induced
    assert (f"no proxy deployments in the 2-hour window prior to alert onset; deployed: ata-inventory-v1 "
            f"revision 149 since {OLD}") in str(report.evidence_by_area)


# ---------------------------------------------------------------------------------------
# Failures: stated loudly, never a fallback
# ---------------------------------------------------------------------------------------


def _assert_loud(report, dossier_findings: str, reason_part: str, caplog) -> None:
    assert f"change timeline unavailable: {reason_part}" in dossier_findings
    assert "Apigee proxy deployment history and Cloud Run revision history were not assessed" in dossier_findings
    assert report.change_induced.startswith(
        f"Not assessed (change detection incomplete: change timeline unavailable: {reason_part}"
    )
    assert "Change-Induced: Not assessed (" in report.build_structured_description(project_id=PROJECT)
    assert "No Cloud Run revision deployments detected" not in dossier_findings
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "change timeline unavailable" in r.getMessage()]
    assert errors and reason_part in errors[0].getMessage()


def _change_findings(report) -> str:
    return str(report.evidence_by_area.get("change", ""))


def test_tools_unreachable_is_not_assessed_with_the_reason(monkeypatch, agent_cfg, telemetry, caplog):
    _use_real_client(monkeypatch)
    telemetry["degraded"] = {"ata-orders-api": "5xx"}
    calls = []

    def unreachable(request):
        calls.append(request)
        raise httpx.ConnectError("Name or service not known")

    monkeypatch.setattr(tools_client, "fetch_id_token", lambda aud: "agent-token")
    tools_client.set_default_transport(httpx.MockTransport(unreachable))
    try:
        with caplog.at_level(logging.WARNING):
            report = asyncio.run(SynthesizerAgent().execute_investigation(
                _orders_alert(), "grp-u", "incident-grp-u.md", _remote_cfg(agent_cfg)))
    finally:
        tools_client.set_default_transport(None)

    assert len(calls) == 3  # TOOLS_RETRY_ATTEMPTS default, then it gives up
    _assert_loud(report, _change_findings(report), "ata-tools unreachable (ConnectError", caplog)


def test_tools_403_iam_missing_names_the_http_status(monkeypatch, agent_cfg, telemetry, caplog):
    _use_real_client(monkeypatch)
    telemetry["degraded"] = {"ata-orders-api": "5xx"}
    calls = []
    page = "<html><head><title>403 Forbidden</title></head><body><h1>Error: Forbidden</h1></body></html>"

    def forbidden(request):
        calls.append(request)
        return httpx.Response(403, text=page, headers={"content-type": "text/html"})

    monkeypatch.setattr(tools_client, "fetch_id_token", lambda aud: "agent-token")
    tools_client.set_default_transport(httpx.MockTransport(forbidden))
    try:
        with caplog.at_level(logging.WARNING):
            report = asyncio.run(SynthesizerAgent().execute_investigation(
                _orders_alert(), "grp-f", "incident-grp-f.md", _remote_cfg(agent_cfg)))
    finally:
        tools_client.set_default_transport(None)

    assert len(calls) == 1  # a 4xx is not retried
    _assert_loud(report, _change_findings(report), "ata-tools returned HTTP 403", caplog)
    assert "<html>" not in report.change_induced


@pytest.mark.asyncio
async def test_apigee_source_error_inside_the_timeline_is_stated_for_that_source(
    monkeypatch, agent_cfg, telemetry, tools_service, caplog
):
    _use_real_client(monkeypatch)
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}
    app = tools_service["start"](APIGEE_ENV="")  # ata-tools deployed without APIGEE_ENV
    alert = _event(_entry("0.inv", "ata-inventory-5xx", "ata-inventory-api", "5xx", started_at=ONSET))

    with caplog.at_level(logging.WARNING):
        report = await _investigate(app, alert, _remote_cfg(agent_cfg))

    change = _change_findings(report)
    reason = "Apigee deployment history source failed (APIGEE_ENV not set in ata-tools)"
    assert f"change timeline unavailable: {reason}. Apigee proxy deployment history was not assessed" in change
    assert report.change_induced.startswith(f"Not assessed (change detection incomplete: change timeline unavailable: {reason}")
    assert any(r.levelno == logging.ERROR and reason in r.getMessage() for r in caplog.records)
    # No Apigee history is stated from a failed read; the Cloud Run half is still a checked source.
    assert "Apigee Deployment History" not in str(report.evidence_by_area)
    assert "Apigee deployment history (" not in str(report.evidence_by_area)


@pytest.mark.asyncio
async def test_apigee_http_error_and_cloud_run_ok_records_only_cloud_run_as_checked(monkeypatch, agent_cfg, tools_service):
    monkeypatch.setattr(tools_server.requests, "get", _upstream_get(apigee_status=403))
    app = tools_service["start"]()
    async with app.router.lifespan_context(app):
        dossier = await ChangeSpecialist().investigate(_orders_alert(), _remote_cfg(agent_cfg))
    assert dossier.metrics[CHANGE_SOURCES_KEY][0] == "ata-tools change timeline (Cloud Run revision history)"
    assert "ata-tools change timeline (Apigee deployment history)" not in dossier.metrics[CHANGE_SOURCES_KEY]
    joined = " ".join(dossier.findings)
    assert "change timeline unavailable: Apigee deployment history source failed (Apigee deployments list returned HTTP 403)" in joined
    assert dossier.metrics["recent_revision"] == "ata-orders-api-00009-new"


def test_unconfigured_tools_url_outside_local_mode_is_loud(agent_cfg):
    cfg = agent_cfg.model_copy(update={"is_local": False, "tools_url": None})
    dossier = asyncio.run(ChangeSpecialist(metric_client=object()).investigate(_orders_alert(), cfg))
    assert "change timeline unavailable: TOOLS_URL is not set" in " ".join(dossier.findings)


# ---------------------------------------------------------------------------------------
# Client: request shape, audience, retries, malformed results
# ---------------------------------------------------------------------------------------


def _cfg(**kw: Any) -> AgentConfig:
    return AgentConfig(project_id=PROJECT, region="us-west1", is_local=False, tools_url=TOOLS_URL,
                       tools_retry_base_seconds=0.0, **kw)


def _ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def _run(handler, cfg=None, audiences=None):
    tools_client.set_default_transport(httpx.MockTransport(handler))
    try:
        return asyncio.run(fetch_change_timeline(
            cfg or _cfg(), token_fetcher=lambda aud: (audiences.append(aud) if audiences is not None else None) or "tok",
        ))
    finally:
        tools_client.set_default_transport(None)


def test_request_is_one_tools_call_to_tools_url_mcp_with_an_id_token_for_tools_url():
    seen: list[httpx.Request] = []
    audiences: list[str] = []

    def handler(request):
        seen.append(request)
        return _ok({"structuredContent": {"changes": []}, "isError": False, "content": []})

    assert _run(handler, audiences=audiences) == {"changes": []}
    req = seen[0]
    assert str(req.url) == f"{TOOLS_URL}/mcp"
    assert req.headers["authorization"] == "Bearer tok"
    assert "text/event-stream" in req.headers["accept"]
    import json
    body = json.loads(req.content)
    assert body["method"] == "tools/call"
    assert body["params"] == {"name": "get_change_timeline",
                              "arguments": {"window_hours": 2, "project_id": PROJECT, "region": "us-west1"}}
    assert audiences == [TOOLS_URL]


def test_transient_5xx_is_retried_then_succeeds():
    statuses = iter([503, 429, 200])

    def handler(request):
        code = next(statuses)
        if code != 200:
            return httpx.Response(code, text="busy")
        return _ok({"content": [{"type": "text", "text": '{"changes": [{"source": "cloud_run"}]}'}]})

    assert _run(handler)["changes"] == [{"source": "cloud_run"}]


def test_persistent_5xx_gives_up_after_the_configured_attempts():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, text="boom")

    with pytest.raises(ToolsCallError) as exc:
        _run(handler, cfg=_cfg(tools_retry_attempts=2))
    assert len(calls) == 2
    assert exc.value.reason == "ata-tools returned HTTP 500; 2 attempt(s)"


def test_timeout_is_bounded_and_reported():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ToolsCallError) as exc:
        _run(handler, cfg=_cfg(tools_timeout_seconds=5, tools_retry_attempts=1))
    assert exc.value.reason.startswith("ata-tools timed out after 5s (ReadTimeout)")


@pytest.mark.parametrize("response, reason", [
    (httpx.Response(200, text="not json"), "ata-tools returned a malformed response (not JSON)"),
    (httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Invalid params"}}),
     "ata-tools MCP error -32602: Invalid params"),
    (_ok({"isError": True, "content": [{"type": "text", "text": "Unknown tool"}]}),
     "ata-tools tool get_change_timeline failed: Unknown tool"),
    (_ok({"structuredContent": {"window_hours": 2}}),
     "ata-tools get_change_timeline returned a malformed result (no changes list)"),
    (_ok({"content": []}), "ata-tools tool get_change_timeline returned a malformed result (no JSON object)"),
])
def test_malformed_and_error_results_raise_with_a_reason(response, reason):
    with pytest.raises(ToolsCallError) as exc:
        _run(lambda request: response)
    assert exc.value.reason == reason


def test_id_token_failure_is_a_stated_reason():
    tools_client.set_default_transport(httpx.MockTransport(lambda r: _ok({"structuredContent": {"changes": []}})))
    try:
        def no_token(aud):
            raise RuntimeError("metadata server unavailable")

        with pytest.raises(ToolsCallError) as exc:
            asyncio.run(fetch_change_timeline(_cfg(), token_fetcher=no_token))
    finally:
        tools_client.set_default_transport(None)
    assert exc.value.reason.startswith(f"no ID token for {TOOLS_URL} (RuntimeError")


def test_tool_outside_the_read_only_allowlist_is_refused():
    with pytest.raises(ToolsCallError, match="not in the read-only allowlist"):
        asyncio.run(call_tool(_cfg(), "delete_service", {}, token_fetcher=lambda a: "tok"))


# ---------------------------------------------------------------------------------------
# Agent config and startup
# ---------------------------------------------------------------------------------------


def test_tools_url_must_be_https_without_a_path():
    assert AgentConfig(TOOLS_URL=f"{TOOLS_URL}/").tools_url == TOOLS_URL
    assert AgentConfig(TOOLS_URL="").tools_url is None
    for bad in ("http://ata-tools.run.app", f"{TOOLS_URL}/mcp", "ata-tools.run.app", f"{TOOLS_URL}?x=1"):
        with pytest.raises(ValidationError):
            AgentConfig(TOOLS_URL=bad)


def test_tools_timeout_is_bounded():
    assert AgentConfig().tools_timeout_seconds == 60.0
    with pytest.raises(ValidationError):
        AgentConfig(TOOLS_TIMEOUT_SECONDS=0)
    with pytest.raises(ValidationError):
        AgentConfig(TOOLS_TIMEOUT_SECONDS=301)


def test_agent_startup_fails_without_tools_url_when_not_local(tmp_path: Path):
    cfg = AgentConfig(is_local=False, incident_store_path=str(tmp_path), fallback_store_path=str(tmp_path))
    assert cfg.missing_required_settings() == ["TOOLS_URL"]
    agent_app.dependency_overrides[get_agent_settings] = lambda: cfg
    try:
        with pytest.raises(RuntimeError, match="required setting\\(s\\) not set: TOOLS_URL"):
            with TestClient(agent_app):
                pass
        ok = cfg.model_copy(update={"tools_url": TOOLS_URL})
        agent_app.dependency_overrides[get_agent_settings] = lambda: ok
        with TestClient(agent_app) as client:
            assert client.get("/health").status_code == 200
    finally:
        agent_app.dependency_overrides.pop(get_agent_settings, None)


def test_local_mode_does_not_require_tools_url():
    assert AgentConfig(is_local=True).missing_required_settings() == []


# ---------------------------------------------------------------------------------------
# ata-tools auth middleware: the agent SA's ID token is accepted, others are rejected
# ---------------------------------------------------------------------------------------


def test_tools_auth_settings_are_derived_from_config():
    cfg = _tools_cfg()
    assert cfg.effective_expected_audiences == [TOOLS_URL]
    assert cfg.effective_allowed_service_accounts == [AGENT_SA]
    hashed = "https://ata-tools-abcdef-uw.a.run.app"
    cfg = _tools_cfg(EXPECTED_AUDIENCE=f"{hashed}/", ALLOWED_SERVICE_ACCOUNTS=f"{AGENT_SA}, ops@x.iam.gserviceaccount.com")
    assert cfg.effective_expected_audiences == [hashed, TOOLS_URL]
    assert cfg.effective_allowed_service_accounts == [AGENT_SA, "ops@x.iam.gserviceaccount.com"]
    other = _tools_cfg(NAME_PREFIX="zz", PROJECT_ID="p2", REGION="europe-west4", PROJECT_NUMBER="42")
    assert other.effective_expected_audiences == ["https://zz-tools-42.europe-west4.run.app"]
    assert other.effective_allowed_service_accounts == ["zz-agent-sa@p2.iam.gserviceaccount.com"]


@pytest.mark.parametrize("token, status", [
    ("agent-token", 200),
    ("other-sa-token", 403),
    ("wrong-audience-token", 401),
    ("forged-token", 401),
    (None, 401),
])
def test_tools_middleware_accepts_the_agent_sa_and_rejects_others(monkeypatch, token, status):
    _patch_verify(monkeypatch, {
        "agent-token": {"email": AGENT_SA, "aud": TOOLS_URL},
        "other-sa-token": {"email": OTHER_SA, "aud": TOOLS_URL},
        "wrong-audience-token": {"email": AGENT_SA, "aud": "https://ata-bridge-123456789012.us-west1.run.app"},
    })
    monkeypatch.setattr(tools_server, "_get_auth_headers", lambda: {})
    monkeypatch.setattr(tools_server.requests, "get", _upstream_get())
    reset_tools_config(_tools_cfg())
    try:
        headers = {"Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with TestClient(tools_server.create_app()) as client:
            resp = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "get_change_timeline", "arguments": {"window_hours": 2}},
            })
            assert client.get("/health").status_code == 200
    finally:
        reset_tools_config(None)
    assert resp.status_code == status


def test_tools_oidc_without_an_audience_fails_closed():
    reset_tools_config(_tools_cfg(PROJECT_NUMBER=None))
    try:
        with pytest.raises(RuntimeError, match="no ID-token audience"):
            tools_server.create_app()
    finally:
        reset_tools_config(None)


# ---------------------------------------------------------------------------------------
# No in-process import path, no guessed Apigee org/environment
# ---------------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def test_agent_has_no_in_process_tools_import():
    for path in (ROOT / "agent" / "app").rglob("*.py"):
        text = path.read_text()
        assert "from tools." not in text and "import tools." not in text, path


def test_change_timeline_apigee_read_has_no_guessed_org_or_environment():
    src = (ROOT / "tools" / "app" / "server.py").read_text()
    body = src[src.index("def get_change_timeline"):src.index("@mcp.custom_route")]
    assert "environments/prod" not in body
    assert "cfg.apigee_org or proj" not in body
    assert "cfg.environment" not in body
    # Proxy names come from NAME_PREFIX: no string literal starting with a hardcoded "ata-".
    assert not re.search(r"""["']ata-""", body)


def test_failure_reason_names_this_stacks_tools_service():
    cfg = _cfg(NAME_PREFIX="zz", tools_retry_attempts=1)
    with pytest.raises(ToolsCallError) as exc:
        _run(lambda request: httpx.Response(403, text="Forbidden"), cfg=cfg)
    assert exc.value.reason == "zz-tools returned HTTP 403: Forbidden"
