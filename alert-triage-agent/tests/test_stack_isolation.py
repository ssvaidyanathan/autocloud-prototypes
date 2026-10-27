"""Stack isolation (I3): an alert is in scope only for the stack whose NAME_PREFIX it carries.

Rule (contracts/stack_scope.py, same in agent, bridge, tools and scripts/scenarios/open_alerts.py):
  - the policy display name (or a resource-derived component) starts with f"{NAME_PREFIX}-", and
  - a `stack` policy user label, when present, equals NAME_PREFIX (mismatch -> WARNING + reject).
  - app=alert-triage-agent alone is not enough (every stack sets it).
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent.app.contracts.alert as agent_alert
import bridge.app.contracts.alert as bridge_alert
import tools.app.contracts.alert as tools_alert
from agent.app.contracts.stack_scope import validate_name_prefix
from bridge.app.config import BridgeConfig, reset_settings
from bridge.app.core.dispatcher import TaskDispatcher
from bridge.app.main import app as bridge_app, set_dispatcher

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("open_alerts_under_test", ROOT / "scripts/scenarios/open_alerts.py")
open_alerts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(open_alerts)


def webhook_payload(display_name: str, labels: dict | None = None, resource: str | None = None) -> dict:
    inc = {
        "incident_id": f"0.{display_name}",
        "policy_name": display_name,
        "policy": {"displayName": display_name, "name": "projects/p/alertPolicies/123"},
        "resource_name": resource or display_name.rsplit("-", 1)[0] + "-api",
    }
    if labels is not None:
        inc["policy_user_labels"] = labels
    return {"incident": inc}


def api_alert(display_name: str, labels: dict | None = None) -> dict:
    """Cloud Monitoring Alerts API shape (what open_alerts.py reads)."""
    policy = {"displayName": display_name, "name": "projects/p/alertPolicies/123"}
    if labels is not None:
        policy["userLabels"] = labels
    return {"name": f"projects/p/alerts/0.{display_name}", "policy": policy}


APP = {"app": "alert-triage-agent"}

# (prefix, policy display name, user labels, expected in scope)
CASES = [
    ("ata", "atat-inventory-5xx", APP, False),  # another stack's policy; the shared app label is not enough
    ("ata", "ata-inventory-5xx", {**APP, "stack": "atat"}, False),  # stack label names another stack
    ("ata", "ata-inventory-5xx", {**APP, "stack": "ata"}, True),
    ("ata", "ata-inventory-5xx", APP, True),  # no stack label yet (transition until the next TF apply)
    ("ata", "ata-inventory-5xx", None, True),
    ("ata", "data-inventory-5xx", APP, False),  # 'ata-' inside another word
    ("atat", "ata-inventory-5xx", APP, False),
    ("atat", "atat-inventory-5xx", {**APP, "stack": "ata"}, False),
    ("atat", "atat-inventory-5xx", {**APP, "stack": "atat"}, True),
    ("atat", "atat-inventory-5xx", APP, True),
    ("atat", "data-inventory-5xx", None, False),
]
FILTERS = {
    "agent-payload": lambda prefix, name, labels: agent_alert.is_ata_alert(webhook_payload(name, labels), prefix),
    "agent-event": lambda prefix, name, labels: agent_alert.is_ata_alert(
        agent_alert.AlertEvent.from_monitoring_payload(webhook_payload(name, labels)), prefix
    ),
    "bridge-payload": lambda prefix, name, labels: bridge_alert.is_ata_alert(webhook_payload(name, labels), prefix),
    "bridge-event": lambda prefix, name, labels: bridge_alert.is_ata_alert(
        bridge_alert.AlertEvent.from_monitoring_payload(webhook_payload(name, labels)), prefix
    ),
    "tools-payload": lambda prefix, name, labels: tools_alert.is_ata_alert_payload(webhook_payload(name, labels), prefix),
    "open_alerts.py": lambda prefix, name, labels: open_alerts.is_ata_alert(api_alert(name, labels), prefix),
}


@pytest.mark.parametrize("where", sorted(FILTERS))
@pytest.mark.parametrize("prefix, name, labels, expected", CASES)
def test_scope_rule_is_the_same_in_every_filter(where, prefix, name, labels, expected):
    assert FILTERS[where](prefix, name, labels) is expected


@pytest.mark.parametrize("where", ["agent-payload", "bridge-payload", "tools-payload"])
def test_stack_label_mismatch_is_logged_at_warning_with_both_values(where, caplog):
    caplog.set_level(logging.WARNING)
    assert FILTERS[where]("ata", "ata-inventory-5xx", {**APP, "stack": "atat"}) is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("'atat'" in m and "'ata'" in m and "stack" in m for m in warnings), warnings


def test_open_alerts_mismatch_is_reported_on_stderr(capsys):
    assert open_alerts.is_ata_alert(api_alert("ata-inventory-5xx", {"stack": "atat"}), "ata") is False
    err = capsys.readouterr().err
    assert "WARNING" in err and "'atat'" in err and "'ata'" in err


def test_resource_derived_component_is_enough_without_a_prefixed_policy_name():
    payload = webhook_payload("Generic latency", APP, resource="x")
    payload["incident"]["resource"] = {"type": "cloud_run_revision", "labels": {"service_name": "ata-orders-api"}}
    assert agent_alert.is_ata_alert(payload, "ata") is True
    assert agent_alert.is_ata_alert(payload, "atat") is False


def test_filters_use_the_configured_prefix_by_default(monkeypatch):
    from agent.app.config import AgentConfig, reset_agent_settings

    reset_agent_settings(AgentConfig(name_prefix="atat", project_id="p", region="r"))
    try:
        assert agent_alert.is_ata_alert(webhook_payload("atat-inventory-5xx", APP)) is True
        assert agent_alert.is_ata_alert(webhook_payload("ata-inventory-5xx", APP)) is False
    finally:
        reset_agent_settings(None)


def test_bridge_webhook_ignores_an_alert_labelled_for_another_stack():
    config = BridgeConfig(project_id="p", name_prefix="ata", region="r", webhook_token="tok", is_local=True)
    reset_settings(config)
    set_dispatcher(TaskDispatcher(config=config))
    try:
        with TestClient(bridge_app) as client:
            other = client.post("/webhook/monitoring?token=tok", json=webhook_payload("ata-inventory-5xx", {**APP, "stack": "atat"}))
            assert other.status_code == 200 and other.json()["status"] == "ignored"
            foreign = client.post("/webhook/monitoring?token=tok", json=webhook_payload("atat-inventory-5xx", APP))
            assert foreign.status_code == 200 and foreign.json()["status"] == "ignored"
            own = client.post("/webhook/monitoring?token=tok", json=webhook_payload("ata-inventory-5xx", {**APP, "stack": "ata"}))
            assert own.status_code == 202
    finally:
        reset_settings(None)
        set_dispatcher(None)


@pytest.mark.parametrize("bad", ["", "ATA", "a", "1ata", "at-a", "abcdefghijk", None])
def test_invalid_prefix_is_rejected(bad):
    with pytest.raises(ValueError):
        validate_name_prefix(bad)
    with pytest.raises(ValueError):
        agent_alert.is_ata_alert(webhook_payload("ata-inventory-5xx"), bad if bad is not None else 5)


def test_stack_scope_helper_is_byte_identical_in_agent_bridge_and_tools():
    agent = (ROOT / "agent/app/contracts/stack_scope.py").read_bytes()
    assert (ROOT / "bridge/app/contracts/stack_scope.py").read_bytes() == agent
    assert (ROOT / "tools/app/contracts/stack_scope.py").read_bytes() == agent


def test_scope_filter_block_is_identical_in_all_alert_contracts():
    marker = "# Resource labels that name the alerting component"
    blocks = [
        (ROOT / f"{svc}/app/contracts/alert.py").read_text().split(marker, 1)[1]
        for svc in ("agent", "bridge", "tools")
    ]
    assert blocks[0] == blocks[1] == blocks[2]
    assert (ROOT / "agent/app/contracts/alert.py").read_bytes() == (ROOT / "tools/app/contracts/alert.py").read_bytes()
