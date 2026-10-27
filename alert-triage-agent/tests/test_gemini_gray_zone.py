"""Gemini gray-zone correlation: works with the real google-genai SDK shape, fails loudly.

- The call uses the installed SDK's real API: genai.Client(vertexai=True, project, location)
  .models.generate_content(model, contents, config=types.GenerateContentConfig(...)).
- Success -> source "gemini". ImportError, PermissionDenied (SDK ClientError 403) and an empty
  response -> WARNING log + source "deterministic_fallback" + the reason, and the ticket text says
  "deterministic fallback (Gemini unavailable: <reason>)". Never "Gemini" when the heuristic decided.
- The agent image installs from agent/pyproject.toml (`pip install .`), which declares google-genai.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.correlation import engine as engine_mod
from agent.app.correlation.engine import (
    CandidateGroup,
    CorrelationEngine,
    GeminiGrayZoneDecision,
    GrayZoneResolution,
    resolve_gray_zone_with_gemini,
)
from agent.app.correlation.scoring import compute_multi_factor_score
from agent.app.specialists.synthesizer import correlation_observation

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 22, 21, 30, 0, tzinfo=timezone.utc)
MODEL, PROJECT, LOCATION = "gemini-test-model", "test-project", "global"


# --- system boundary: the google-genai client ------------------------------------------------

class FakeModels:
    def __init__(self, owner):
        self.owner = owner

    def generate_content(self, *, model, contents, config=None):
        self.owner.calls.append({"model": model, "contents": contents, "config": config})
        if isinstance(self.owner.result, BaseException):
            raise self.owner.result
        return type("Resp", (), {"text": self.owner.result})()


class FakeGenai:
    """Stands in for google.genai.Client; records how the agent builds and calls it."""

    def __init__(self, result):
        self.result = result
        self.inits: list[dict] = []
        self.calls: list[dict] = []

    def client_factory(self, **kwargs):
        self.inits.append(kwargs)
        client = type("Client", (), {})()
        client.models = FakeModels(self)
        return client


@pytest.fixture
def gemini(monkeypatch):
    from google import genai

    def install(result):
        fake = FakeGenai(result)
        monkeypatch.setattr(genai, "Client", fake.client_factory)
        return fake

    return install


def _gray_zone_inputs():
    """2 hops, plausible causal (15 pts): gray zone, and the heuristic picks new_group."""
    breakdown = compute_multi_factor_score(
        alert_resource="ata-inventory-v1", alert_category="client_429", alert_time=NOW + timedelta(minutes=3),
        group_resources=["ata-orders-api"], group_category="latency_504", group_anchor_time=NOW,
    )
    assert 35.0 <= breakdown.total_score < 65.0
    alert = AlertEvent(
        incident_id="0.gz1", policy_name="ata-apigee-inventory-429", resource_name="ata-inventory-v1",
        started_at=(NOW + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        policy_user_labels={"signal": "rate-limit", "component": "ata-inventory-v1"},
    )
    candidate = CandidateGroup(
        group_id="grp_orders", filename="incident-grp_orders.md", file_path=Path("incident-grp_orders.md"),
        origin="ata-orders-api", resources=["ata-orders-api"], category="latency_504", anchor_time=NOW,
    )
    return alert, candidate, breakdown


def _resolve():
    alert, candidate, breakdown = _gray_zone_inputs()
    return resolve_gray_zone_with_gemini(
        alert, candidate, breakdown, model_name=MODEL, project_id=PROJECT, location=LOCATION
    )


def _engine_decision():
    alert, candidate, _ = _gray_zone_inputs()
    engine = CorrelationEngine(
        incident_store_path="/tmp/unused", model_name=MODEL, project_id=PROJECT, gemini_location=LOCATION
    )
    return engine.evaluate_alert(alert, candidate_groups=[candidate])


def _ticket_text(decision) -> str:
    correlation = {
        "decision": decision.decision, "best_score": decision.best_score, "is_gray_zone": decision.is_gray_zone,
        "open_groups_compared": 1, "threshold_auto_new": 35.0, "threshold_auto_attach": 65.0,
        "gray_zone_source": decision.gray_zone_source,
    }
    if decision.gray_zone_fallback_reason:
        correlation["gray_zone_fallback_reason"] = decision.gray_zone_fallback_reason
    return correlation_observation(correlation)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == engine_mod.logger.name]


# --- real SDK ------------------------------------------------------------------------------

def test_real_google_genai_package_imports_and_matches_the_call_shape():
    from google import genai
    from google.genai import client as genai_client, types

    # genai.Client itself is replaced by the conftest guard; google.genai.client.Client is the real class.
    client_params = inspect.signature(genai_client.Client.__init__).parameters
    assert {"vertexai", "project", "location"} <= set(client_params)
    gen_params = inspect.signature(genai.models.Models.generate_content).parameters
    assert {"model", "contents", "config"} <= set(gen_params)
    config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=GeminiGrayZoneDecision)
    assert config.response_schema is GeminiGrayZoneDecision


def test_agent_image_installs_google_genai():
    dockerfile = (ROOT / "agent/Dockerfile").read_text()
    # The image installs the agent package from pyproject.toml, so dependencies must live there.
    assert "COPY pyproject.toml" in dockerfile
    assert re.search(r"^RUN pip install[^\n]*\s\.\s*$", dockerfile, re.M), dockerfile
    deps = tomllib.loads((ROOT / "agent/pyproject.toml").read_text())["project"]["dependencies"]
    genai = [d for d in deps if re.match(r"google-genai\b", d)]
    assert genai, deps
    # sec L7: 2.x only. GenerateContentConfig(response_schema=<pydantic>) is the 2.x API surface.
    assert re.fullmatch(r'"?google-genai>=2\.33,<3"?', genai[0].strip()), f"google-genai must be >=2.33,<3: {genai[0]}"


# --- success ---------------------------------------------------------------------------------

def test_gemini_success_is_reported_as_gemini(gemini, caplog):
    fake = gemini(json.dumps({"decision": "new_group", "confidence": 0.71, "rationale": "Different failure modes."}))
    caplog.set_level(logging.WARNING)

    res = _resolve()

    assert isinstance(res, GrayZoneResolution)
    assert (res.source, res.decision, res.fallback_reason) == ("gemini", "new_group", None)
    assert len(fake.inits) == 1
    init = dict(fake.inits[0])
    http_options = init.pop("http_options")
    assert init == {"vertexai": True, "project": PROJECT, "location": LOCATION}
    assert http_options.timeout == 20000  # GEMINI_TIMEOUT_SECONDS default, in milliseconds
    call = fake.calls[0]
    assert call["model"] == MODEL and "gray zone" in call["contents"]
    from google.genai import types

    assert isinstance(call["config"], types.GenerateContentConfig)
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].response_schema is GeminiGrayZoneDecision
    assert _warnings(caplog) == []


def test_gemini_success_flows_into_rationale_and_ticket_text(gemini):
    gemini(json.dumps({"decision": "new_group", "confidence": 0.7, "rationale": "Independent."}))
    decision = _engine_decision()
    assert decision.is_gray_zone and decision.gray_zone_source == "gemini"
    assert decision.rationale.startswith("Gray-zone Gemini resolution: Independent.")
    assert "the gray-zone review by Gemini decided a new group" in _ticket_text(decision)


# --- failures: loud, labelled, never called Gemini --------------------------------------------

def _permission_denied():
    from google.genai import errors

    return errors.ClientError(403, {"error": {"code": 403, "message": "Permission 'aiplatform.endpoints.predict' denied", "status": "PERMISSION_DENIED"}})


@pytest.mark.parametrize(
    "failure, reason_part",
    [
        ("import_error", "ModuleNotFoundError"),  # ImportError subclass: google-genai not installed
        ("permission_denied", "ClientError: 403 PERMISSION_DENIED"),
        ("empty_response", f"empty response from {MODEL}"),
    ],
)
def test_gemini_failure_is_a_warning_and_a_labelled_fallback(failure, reason_part, gemini, monkeypatch, caplog):
    if failure == "import_error":
        import google

        monkeypatch.delattr(google, "genai", raising=False)
        monkeypatch.setitem(sys.modules, "google.genai", None)
    elif failure == "permission_denied":
        gemini(_permission_denied())
    else:
        gemini("")
    caplog.set_level(logging.INFO)

    res = _resolve()
    decision = _engine_decision()

    assert res.source == "deterministic_fallback"
    assert reason_part in res.fallback_reason
    warnings = _warnings(caplog)
    assert warnings and all("deterministic fallback" in w for w in warnings)
    if failure != "empty_response":
        assert any(reason_part.split(":")[0] in w for w in warnings), warnings
    assert decision.gray_zone_source == "deterministic_fallback"
    assert decision.rationale.startswith("Gray-zone deterministic fallback (Gemini unavailable: ")
    assert "Gemini resolution" not in decision.rationale
    text = _ticket_text(decision)
    assert "the gray-zone review by deterministic fallback (Gemini unavailable: " in text
    assert reason_part in text


def test_no_project_id_falls_back_loudly_without_calling_gemini(gemini, caplog):
    fake = gemini(json.dumps({"decision": "attach", "rationale": "x"}))
    caplog.set_level(logging.WARNING)
    alert, candidate, breakdown = _gray_zone_inputs()
    res = resolve_gray_zone_with_gemini(alert, candidate, breakdown, model_name=MODEL, project_id=None, location=LOCATION)
    assert res.source == "deterministic_fallback" and res.fallback_reason == "PROJECT_ID is not set"
    assert fake.inits == [] and _warnings(caplog)


def test_invalid_gemini_answer_is_a_labelled_fallback(gemini, caplog):
    gemini(json.dumps({"decision": "maybe", "rationale": "?"}))
    caplog.set_level(logging.WARNING)
    res = _resolve()
    assert res.source == "deterministic_fallback" and "ValidationError" in res.fallback_reason
    assert _warnings(caplog)


def test_decision_log_reports_real_factor_scores(gemini, caplog):
    gemini("")
    caplog.set_level(logging.INFO)
    _engine_decision()
    entry = next(json.loads(r.getMessage().split("JSON_DECISION: ", 1)[1]) for r in caplog.records if "JSON_DECISION" in r.getMessage())
    _, _, breakdown = _gray_zone_inputs()
    assert entry["factors"]["topology_score"] == breakdown.topo_score > 0
    assert entry["factors"]["temporal_score"] == breakdown.time_score > 0
    assert entry["gray_zone_source"] == "deterministic_fallback"


# --- the group's correlation info carries the source -----------------------------------------

def test_grouping_manager_stores_gray_zone_source_in_correlation(tmp_path, gemini):
    from agent.app.grouping.manager import GroupingManager
    from agent.app.grouping.store import OKFStore
    from agent.app.timers.scheduler import set_timer_scheduler_hook

    gemini(_permission_denied())
    cfg = AgentConfig(
        project_id=PROJECT, region="test-region1", name_prefix="ata", is_local=True,
        incident_store_path=str(tmp_path / "store"), fallback_store_path=str(tmp_path / "fb"),
        model_name=MODEL, gemini_location=LOCATION,
    )
    set_timer_scheduler_hook(lambda **kw: "task")
    try:
        manager = GroupingManager(config=cfg, store=OKFStore(config=cfg), bridge_client=object())
        first = AlertEvent(
            incident_id="0.a1", policy_name="ata-orders-latency", resource_name="ata-orders-api",
            started_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), policy_user_labels={"signal": "latency", "component": "ata-orders-api"},
        )
        alert, _, _ = _gray_zone_inputs()
        manager.ingest_alert(first, current_time=NOW.timestamp())
        result = manager.ingest_alert(alert, current_time=(NOW + timedelta(minutes=3)).timestamp())
    finally:
        set_timer_scheduler_hook(None)

    assert result.action == "new_group", result
    assert result.correlation["is_gray_zone"] is True
    assert result.correlation["gray_zone_source"] == "deterministic_fallback"
    assert "ClientError: 403" in result.correlation["gray_zone_fallback_reason"]
    assert result.doc.correlation["gray_zone_source"] == "deterministic_fallback"


# --- crev R3: bounded call, never under the global ingest lock ----------------------------

class _HungModels:
    def __init__(self, release, lock_probe):
        self.release, self.lock_probe = release, lock_probe

    def generate_content(self, **kwargs):
        if self.lock_probe is not None:
            self.lock_probe.append(self.lock_probe_fn())
        self.release.wait(10)  # a Vertex response that never comes (within the test)
        return type("Resp", (), {"text": None})()


def _install_hung_client(monkeypatch, lock_probe=None):
    import threading
    from google import genai

    release = threading.Event()
    inits: list = []

    def factory(**kwargs):
        inits.append(kwargs)
        client = type("Client", (), {})()
        client.models = _HungModels(release, lock_probe)
        if lock_probe is not None:
            from agent.app.grouping import manager as manager_mod

            def free() -> bool:
                # Runs on the Gemini worker thread: the ingest lock is free iff nobody holds it.
                got = manager_mod._global_ingest_lock.acquire(blocking=False)
                if got:
                    manager_mod._global_ingest_lock.release()
                return got
            client.models.lock_probe_fn = free
        return client

    monkeypatch.setattr(genai, "Client", factory)
    return release, inits


def test_hung_gemini_times_out_within_the_bound_and_falls_back_loudly(monkeypatch, caplog):
    import time as _time

    release, inits = _install_hung_client(monkeypatch)
    caplog.set_level(logging.WARNING)
    alert, candidate, breakdown = _gray_zone_inputs()
    started = _time.monotonic()
    try:
        res = resolve_gray_zone_with_gemini(alert, candidate, breakdown, model_name=MODEL, project_id=PROJECT,
                                            location=LOCATION, timeout_seconds=0.3)
    finally:
        release.set()
    elapsed = _time.monotonic() - started
    assert elapsed < 2.0, f"not bounded: {elapsed:.2f}s"
    assert res.source == "deterministic_fallback"
    assert "GeminiTimeout" in res.fallback_reason and "0.3 s" in res.fallback_reason
    assert inits[0]["http_options"].timeout == 300
    assert any("GeminiTimeout" in w and "deterministic fallback" in w for w in _warnings(caplog))


@pytest.mark.parametrize("bad", ["0", "-1", "121", "abc"])
def test_gemini_timeout_setting_is_validated(bad):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentConfig(name_prefix="ata", is_local=True, GEMINI_TIMEOUT_SECONDS=bad)
    assert AgentConfig(name_prefix="ata", is_local=True).gemini_timeout_seconds == 20.0
    assert AgentConfig(name_prefix="ata", is_local=True, GEMINI_TIMEOUT_SECONDS="7.5").gemini_timeout_seconds == 7.5


def _manager_with_one_group(tmp_path, **cfg_kw):
    from agent.app.grouping.manager import GroupingManager
    from agent.app.grouping.store import OKFStore

    cfg = AgentConfig(
        project_id=PROJECT, region="test-region1", name_prefix="ata", is_local=True,
        incident_store_path=str(tmp_path / "store"), fallback_store_path=str(tmp_path / "fb"),
        model_name=MODEL, gemini_location=LOCATION, **cfg_kw,
    )
    manager = GroupingManager(config=cfg, store=OKFStore(config=cfg), bridge_client=object())
    first = AlertEvent(
        incident_id="0.a1", policy_name="ata-orders-latency", resource_name="ata-orders-api",
        started_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), policy_user_labels={"signal": "latency", "component": "ata-orders-api"},
    )
    manager.ingest_alert(first, current_time=NOW.timestamp())
    return manager


def test_gray_zone_gemini_call_is_made_without_the_ingest_lock_and_is_bounded(tmp_path, monkeypatch):
    import threading
    import time as _time

    from agent.app.grouping import manager as manager_mod
    from agent.app.timers.scheduler import set_timer_scheduler_hook

    set_timer_scheduler_hook(lambda **kw: "task")
    probe: list = []
    release, _ = _install_hung_client(monkeypatch, lock_probe=probe)
    try:
        manager = _manager_with_one_group(tmp_path, GEMINI_TIMEOUT_SECONDS="0.5")
        alert, _, _ = _gray_zone_inputs()
        # While the gray-zone call hangs, another thread can take the ingest lock.
        other_got_lock: list = []

        def other():
            _time.sleep(0.2)
            got = manager_mod._global_ingest_lock.acquire(timeout=0.2)
            other_got_lock.append(got)
            if got:
                manager_mod._global_ingest_lock.release()

        t = threading.Thread(target=other)
        t.start()
        started = _time.monotonic()
        result = manager.ingest_alert(alert, current_time=(NOW + timedelta(minutes=3)).timestamp())
        elapsed = _time.monotonic() - started
        t.join()
    finally:
        release.set()
        set_timer_scheduler_hook(None)

    assert probe == [True], "the Gemini call ran while the global ingest lock was held"
    assert other_got_lock == [True]
    assert elapsed < 3.0
    assert result.correlation["is_gray_zone"] is True
    assert result.correlation["gray_zone_source"] == "deterministic_fallback"
    assert "GeminiTimeout" in result.correlation["gray_zone_fallback_reason"]


def test_groups_changing_during_the_call_bounds_the_retries(tmp_path, monkeypatch):
    """If every locked pass sees a different gray-zone candidate/score, Gemini is asked at most
    _GRAY_ZONE_ATTEMPTS times and the deterministic fallback decides, with the reason."""
    import dataclasses

    from agent.app.grouping import manager as manager_mod
    from agent.app.timers.scheduler import set_timer_scheduler_hook

    set_timer_scheduler_hook(lambda **kw: "task")
    try:
        manager = _manager_with_one_group(tmp_path)
        engine = manager.correlation_engine
        real_evaluate = engine.evaluate_alert
        passes = {"n": 0}
        asked: list = []

        def shifting(alert, candidate_groups=None, gray_zone_resolver=None, **kw):
            passes["n"] += 1

            def resolver(a, cand, bd):
                return gray_zone_resolver(a, cand, dataclasses.replace(bd, total_score=bd.total_score + passes["n"] / 100))
            return real_evaluate(alert, candidate_groups=candidate_groups, gray_zone_resolver=resolver, **kw)

        monkeypatch.setattr(engine, "evaluate_alert", shifting)
        monkeypatch.setattr(engine, "resolve_gray_zone", lambda a, c, b: asked.append(b.total_score) or
                            GrayZoneResolution(decision="attach", rationale="r", source="gemini"))
        alert, _, _ = _gray_zone_inputs()
        result = manager.ingest_alert(alert, current_time=(NOW + timedelta(minutes=3)).timestamp())
    finally:
        set_timer_scheduler_hook(None)

    assert len(asked) == manager_mod._GRAY_ZONE_ATTEMPTS
    assert result.correlation["gray_zone_source"] == "deterministic_fallback"
    assert "incident groups changed while Gemini was consulted" in result.correlation["gray_zone_fallback_reason"]
