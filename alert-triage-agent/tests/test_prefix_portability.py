"""A second stack (NAME_PREFIX=atat) groups, scores and names things exactly like ata.

Runs the S1/S2 live alert sequences through the full replay pipeline (bridge webhook ->
dispatcher -> agent triage -> timers -> ServiceNow mock) once per prefix, and checks topology
distance, blast radius, Apigee runtime names and system parts per prefix.
"""

from __future__ import annotations

import pytest

from agent.app import config as agent_config_module
from agent.app.config import AgentConfig, reset_agent_settings
from agent.app.contracts.alert import AlertEvent, system_parts
from agent.app.correlation.scoring import classify_anomaly_category, compute_multi_factor_score
from bridge.app import config as bridge_config_module
from topology.loader import get_loader

from tests.test_scenario_grouping_replay import replay_environment, replay_live_sequence

S1 = [
    # (incident_id, policy suffix, component suffix, tier, signal, severity, started_at, offset)
    ("0.oe5l7g8jv5k8", "inventory-5xx", "inventory-api", "service", "5xx", "p1", "2026-10-23T07:52:06Z", 0.0),
    ("0.oe5l7g8jv5kd", "inventory-latency", "inventory-api", "service", "latency", "p2", "2026-10-23T07:52:06Z", 5.0),
    ("0.oe5guunhsdi3", "apigee-orders-5xx", "orders-v1", "edge", "5xx", "p1", "2026-10-23T07:54:37Z", 151.0),
]
S1_BASE = 1729669926.0
S2 = [
    ("0.oe61o2q7f702", "orders-latency", "orders-api", "service", "latency", "p2", "2026-10-23T19:52:57Z", 0.0),
    ("0.oe61pyb0j2oa", "apigee-orders-5xx", "orders-v1", "edge", "5xx", "p1", "2026-10-23T19:55:14Z", 137.0),
    ("0.oe61pyb0j2of", "inventory-latency", "inventory-api", "service", "latency", "p2", "2026-10-23T19:55:14Z", 137.0),
    ("0.oe61tbneyg5z", "inventory-latency", "inventory-api", "service", "latency", "p2", "2026-10-23T19:59:20Z", 383.0),
]
S2_BASE = 1729713177.0


@pytest.fixture
def stack(monkeypatch):
    """Switch the whole process to one NAME_PREFIX (env, agent and bridge settings, topology)."""
    saved_agent = agent_config_module._agent_settings
    saved_bridge = getattr(bridge_config_module, "_settings", None)

    def use(prefix: str) -> None:
        monkeypatch.setenv("NAME_PREFIX", prefix)
        reset_agent_settings(AgentConfig(name_prefix=prefix, project_id="test-project", region="test-region1"))
        bridge_config_module.reset_settings(None)
        get_loader(force_reload=True)

    yield use
    monkeypatch.undo()
    reset_agent_settings(saved_agent)
    bridge_config_module.reset_settings(saved_bridge)
    get_loader(force_reload=True)


def _alerts(prefix: str, spec, base: float, stack_label: bool):
    out = []
    for inc, pol, comp, tier, signal, sev, started, offset in spec:
        labels = {"app": "alert-triage-agent", "component": f"{prefix}-{comp}", "tier": tier, "signal": signal, "severity": sev}
        if stack_label:
            labels["stack"] = prefix
        out.append((
            AlertEvent(
                incident_id=inc, policy_name=f"{prefix}-{pol}", resource_name=f"{prefix}-{comp}",
                started_at=started, policy_user_labels=labels,
            ),
            base + offset,
        ))
    return out


def _grouping(prefix: str, spec, base: float, tmp_path, stack_label: bool):
    with replay_environment(tmp_path / prefix, name_prefix=prefix) as env:
        groups, tickets = replay_live_sequence(_alerts(prefix, spec, base, stack_label), env)
    strip = lambda name: name[len(prefix) + 1:] if name and name.startswith(f"{prefix}-") else name  # noqa: E731
    return {
        "groups": sorted(
            (strip(g.origin), tuple(sorted(a.get("incident_id") for a in g.alerts))) for g in groups
        ),
        "tickets": len(tickets),
    }


@pytest.mark.parametrize("scenario, spec, base", [("S1", S1, S1_BASE), ("S2", S2, S2_BASE)])
def test_atat_stack_groups_s1_s2_exactly_like_ata(stack, tmp_path, scenario, spec, base):
    stack("ata")
    ata = _grouping("ata", spec, base, tmp_path, stack_label=False)
    stack("atat")
    atat = _grouping("atat", spec, base, tmp_path, stack_label=True)

    assert ata["tickets"] == 1 and len(ata["groups"]) == 1, ata
    assert atat == ata, f"{scenario}: atat grouping {atat} differs from ata {ata}"


def test_atat_topology_distance_and_blast_radius_match_ata(stack):
    stack("ata")
    loader = get_loader()
    ata = (
        loader.hop_distance("ata-orders-api", "ata-inventory-api"),
        loader.blast_radius("ata-inventory-db"),
        loader.canonicalize("ata-apigee-orders-v1"),
        compute_multi_factor_score(
            alert_resource="ata-orders-api", alert_category="latency_504", alert_time=_t(6),
            group_resources=["ata-inventory-api"], group_category="latency_504", group_anchor_time=_t(0),
        ).total_score,
    )
    stack("atat")
    loader = get_loader()
    assert loader.name_prefix == "atat"
    assert set(loader.components) == {"atat-orders-v1", "atat-inventory-v1", "atat-orders-api", "atat-inventory-api", "atat-inventory-db"}
    atat = (
        loader.hop_distance("atat-orders-api", "atat-inventory-api"),
        loader.blast_radius("atat-inventory-db"),
        loader.canonicalize("atat-apigee-orders-v1"),
        compute_multi_factor_score(
            alert_resource="atat-orders-api", alert_category="latency_504", alert_time=_t(6),
            group_resources=["atat-inventory-api"], group_category="latency_504", group_anchor_time=_t(0),
        ).total_score,
    )
    assert atat[0] == ata[0] == 1
    assert atat[1] == [n.replace("ata-", "atat-", 1) for n in ata[1]]
    assert atat[2] == "atat-orders-v1" and ata[2] == "ata-orders-v1"
    assert atat[3] == ata[3]
    # An ata component is unknown to the atat stack (no cross-stack telemetry).
    assert loader.hop_distance("ata-orders-api", "atat-inventory-api") is None


def _t(minutes: int):
    from datetime import datetime, timedelta, timezone

    return datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


@pytest.mark.parametrize("prefix", ["ata", "atat"])
def test_gateway_keywords_classify_any_prefix(prefix):
    # scoring.py keywords are prefix-free substrings ("orders-v1"), so they hold for every prefix.
    assert classify_anomaly_category(f"{prefix}-orders-v1 error rate", signal="5xx") == "gateway_5xx"
    assert classify_anomaly_category(f"{prefix}-inventory-api error rate", signal="5xx") == "backend_5xx"


def test_system_parts_follow_the_prefix(stack):
    stack("atat")
    assert system_parts() == {"atat-agent", "atat-bridge", "atat-tools"}
    assert system_parts("ata") == {"ata-agent", "ata-bridge", "ata-tools"}


def test_apigee_access_log_name_follows_the_prefix(stack):
    from agent.app.specialists import apigee

    stack("atat")
    assert apigee.access_log_id() == "atat-apigee-access"
    assert apigee.access_log_name("p1") == "projects/p1/logs/atat-apigee-access"
    stack("ata")
    assert apigee.access_log_name("p1") == "projects/p1/logs/ata-apigee-access"


def test_tools_apigee_log_query_follows_the_prefix(monkeypatch):
    from unittest.mock import MagicMock, patch

    from tools.app import config as tools_config
    from tools.app.server import get_apigee_logs

    tools_config.reset_tools_config(tools_config.ToolsConfig(NAME_PREFIX="atat", PROJECT_ID="p1", REGION="r1"))
    try:
        client = MagicMock()
        client.list_entries.return_value = []
        with patch("google.cloud.logging.Client", return_value=client):
            get_apigee_logs(proxy_name="atat-orders-v1")
        sent_filter = client.list_entries.call_args.kwargs.get("filter_") or str(client.list_entries.call_args)
        assert 'logName="projects/p1/logs/atat-apigee-access"' in sent_filter
    finally:
        tools_config.reset_tools_config(None)


def test_task_ids_carry_the_stack_prefix(stack):
    from datetime import datetime, timezone

    from agent.app.timers import scheduler
    from bridge.app.config import BridgeConfig
    from bridge.app.core.dispatcher import TaskDispatcher

    stack("atat")
    captured = {}

    class FakeTasks:
        def create_task(self, request):
            captured["name"] = request["task"]["name"]
            return type("T", (), {"name": request["task"]["name"]})()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scheduler.tasks_v2, "CloudTasksClient", lambda *a, **k: FakeTasks())
        scheduler.schedule_delayed_timer(
            project_id="p", location="l", queue_name="atat-timer-queue", target_url="http://a", group_id="g1",
            deadline_type="settle", deadline_dt=datetime(2026, 1, 1, tzinfo=timezone.utc), service_account_email="sa@p",
        )
    assert captured["name"].split("/tasks/")[1].startswith("atat-timer-g1-settle-")

    dispatcher = TaskDispatcher(config=BridgeConfig(project_id="p", name_prefix="atat", is_local=True))
    assert dispatcher.get_task_id(current_time=1.0, incident_id="0.x").startswith("atat-alert-")
