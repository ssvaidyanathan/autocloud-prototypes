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
    assert re.search(r">=\s*1\.\d+.*<\s*3", genai[0]), f"google-genai must be pinned to a compatible range: {genai[0]}"


# --- success ---------------------------------------------------------------------------------

def test_gemini_success_is_reported_as_gemini(gemini, caplog):
    fake = gemini(json.dumps({"decision": "new_group", "confidence": 0.71, "rationale": "Different failure modes."}))
    caplog.set_level(logging.WARNING)

    res = _resolve()

    assert isinstance(res, GrayZoneResolution)
    assert (res.source, res.decision, res.fallback_reason) == ("gemini", "new_group", None)
    assert fake.inits == [{"vertexai": True, "project": PROJECT, "location": LOCATION}]
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
