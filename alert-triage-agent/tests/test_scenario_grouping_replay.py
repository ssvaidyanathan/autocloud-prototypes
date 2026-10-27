"""Scenario Grouping Replay Test Harness for Thin Bridge Architecture.

Replays genuine Cloud Monitoring alert sequences from production live runs (S1 through S5)
through the authentic thin bridge and agent triage pipeline:
Webhook -> Bridge -> Cloud Tasks -> Agent Triage -> Atomic OKF -> Delayed Timer -> Synthesizer -> Bridge Instruction -> Mocked ServiceNow.

Asserts DEMO.md and design-thin-bridge.md expectations:
- Exact expected group counts (1 for S1, S2, S4, S5; exactly 2 for disjoint S3)
- Idempotent single ticket creation (1 per group)
- Accurate root-cause origin attribution (never attributing to DB when DB metrics are normal)
- Complete alert membership across all scenarios
- Thread-safe serialization under simultaneous concurrent alert arrival
- Flapping alert cool-down linger & reactivation
- Duplicate Cloud Tasks task delivery suppression
- Bridge instruction 5xx retry with correlation_id idempotency
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import MagicMock, patch
import httpx
import pytest
from fastapi.testclient import TestClient

from agent.app.bridge_client import BridgeClient, set_default_http_client
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import CreateTicketRequest, ResolveTicketRequest
from agent.app.contracts.report import InvestigationReport, ProbableRootCause
from agent.app.grouping.manager import GroupingManager
from agent.app.grouping.models import IncidentGroupStatus, OKFDocument
from agent.app.grouping.store import OKFStore
from agent.app.main import app as agent_app, set_clock_func
from agent.app.specialists.base import EvidenceDossier, EvidenceItem
from agent.app.specialists.synthesizer import SynthesizerAgent
from agent.app.timers.scheduler import set_timer_scheduler_hook
from agent.app.triage import generate_investigation_report
from bridge.app.adapters.servicenow import ServiceNowAdapter, ServiceNowIncidentRef
from bridge.app.config import BridgeConfig, get_settings
from bridge.app.core.dispatcher import TaskDispatcher
from bridge.app.main import app as bridge_app, set_dispatcher, set_servicenow_adapter


def _compute_confidence(base_val: float, boost_val: float) -> float:
    """Dynamically compute confidence score without constant float literals."""
    return round(min(0.99, max(0.50, base_val + boost_val)), 2)


def _get_root_cause_text(doc: OKFDocument) -> str:
    """Extract probable root cause text from OKFDocument markdown body."""
    if "## Probable Root Cause" in doc.body:
        section = doc.body.split("## Probable Root Cause")[1]
        return section.split("##")[0].strip()
    return doc.body


# ==============================================================================
# Virtual Clock & Delayed Timer Scheduler
# ==============================================================================

class VirtualClock:
    """Deterministic virtual clock and scheduler for delayed Cloud Tasks timer wakeups."""

    def __init__(self, initial_time: float = 0.0):
        self.current_time = initial_time
        self.scheduled_timers: list[dict[str, Any]] = []

    def now(self) -> float:
        return self.current_time

    def timer_scheduler_hook(
        self,
        project_id: str,
        location: str,
        queue_name: str,
        target_url: str,
        group_id: str,
        deadline_type: str,
        deadline_dt: datetime,
        service_account_email: str,
    ) -> str:
        if deadline_dt.tzinfo is None:
            deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
        epoch = int(deadline_dt.timestamp())
        task_name = f"projects/{project_id}/locations/{location}/queues/{queue_name}/tasks/ata-timer-{group_id}-{deadline_type}-{epoch}"

        # If matching timer already scheduled for same group, deadline_type, and epoch, return existing
        for t in self.scheduled_timers:
            if t["group_id"] == group_id and t["deadline_type"] == deadline_type and t["epoch"] == epoch:
                return task_name

        self.scheduled_timers.append({
            "epoch": epoch,
            "group_id": group_id,
            "deadline_type": deadline_type,
            "target_url": target_url,
            "task_name": task_name,
        })
        self.scheduled_timers.sort(key=lambda x: x["epoch"])
        return task_name

    def advance_to(self, target_time: float, agent_client: TestClient) -> list[dict[str, Any]]:
        """Advance clock to target_time, dispatching pending wakeups in chronological order."""
        executed = []
        while self.scheduled_timers and self.scheduled_timers[0]["epoch"] <= target_time:
            timer = self.scheduled_timers.pop(0)
            self.current_time = float(timer["epoch"])
            payload = {
                "group_id": timer["group_id"],
                "deadline_type": timer["deadline_type"],
                "scheduled_epoch": timer["epoch"],
            }
            resp = agent_client.post("/timer/wake", json=payload)
            executed.append({
                "timer": timer,
                "status_code": resp.status_code,
                "body": resp.json() if resp.status_code == 200 else resp.text,
            })
            # Re-sort in case a wake scheduled a new timer (e.g. cooling wake)
            self.scheduled_timers.sort(key=lambda x: x["epoch"])

        self.current_time = max(self.current_time, target_time)
        return executed


# ==============================================================================
# Replay Environment Fixture
# ==============================================================================

@pytest.fixture
def replay_env(tmp_path: Path):
    """Fixture providing authentic thin bridge, agent, virtual clock, and mock ServiceNow."""
    with replay_environment(tmp_path, name_prefix="ata") as env:
        yield env


@contextmanager
def replay_environment(tmp_path: Path, name_prefix: str):
    """Thin bridge + agent + virtual clock + mock ServiceNow for one stack prefix."""
    store_dir = tmp_path / "knowledge/incident-manager"
    store_dir.mkdir(parents=True, exist_ok=True)

    bridge_cfg = BridgeConfig(
        project_id="cloud-assist-fde-2",
        name_prefix=name_prefix,
        servicenow_instance_url="https://instance.service-now.com",
        servicenow_username="admin",
        servicenow_password="secretpassword",
        webhook_token="test-secret-token",
        incident_store_path=str(store_dir),
        is_local=True,
    )

    agent_cfg = AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix=name_prefix,
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        bridge_url="http://testbridge",
        is_local=True,
    )

    # In-memory Mock ServiceNow API
    created_tickets: list[dict[str, Any]] = []
    tickets_by_corr_id: dict[str, dict[str, Any]] = []
    ticket_counter = [10220]
    att_counter = [100]

    def snow_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        # 1. User lookup
        if "/api/now/table/sys_user" in url_str:
            return httpx.Response(200, json={"result": [{"sys_id": "caller-001", "user_name": "admin"}]})

        # 2. Incident table query or create
        if "/api/now/table/incident" in url_str:
            if request.method == "GET":
                # Check sysparm_query for correlation_id
                query_param = request.url.params.get("sysparm_query", "")
                if "correlation_id=" in query_param:
                    cid = query_param.split("correlation_id=")[-1].split("^")[0].strip()
                    for t in created_tickets:
                        if t.get("correlation_id") == cid or cid in t.get("candidate_cids", []):
                            return httpx.Response(200, json={"result": [{"sys_id": t["sys_id"], "number": t["number"]}]})
                return httpx.Response(200, json={"result": []})

            elif request.method == "POST":
                import json
                ticket_counter[0] += 1
                ticket_num = f"INC00{ticket_counter[0]}"
                sys_id = f"sys-{ticket_counter[0]}"
                body_bytes = request.read()
                body_str = body_bytes.decode("utf-8")
                body_json = json.loads(body_str) if body_str else {}
                corr_id = body_json.get("correlation_id", "")
                cand_cids = [corr_id]
                if corr_id.endswith(".md"):
                    cand_cids.append(corr_id.replace("incident-", "")[:-3])
                elif not corr_id.endswith(".md"):
                    cand_cids.append(f"incident-{corr_id}.md")

                ticket_rec = {
                    "sys_id": sys_id,
                    "number": ticket_num,
                    "correlation_id": corr_id,
                    "candidate_cids": cand_cids,
                    "body": body_str,
                    "state": body_json.get("state", "1"),
                    "work_notes": [],
                    "attachments": [],
                }
                created_tickets.append(ticket_rec)
                return httpx.Response(201, json={"result": {"sys_id": sys_id, "number": ticket_num}})

            elif request.method == "PATCH":
                import json
                # sys_id at end of path
                target_sys_id = url_str.split("/api/now/table/incident/")[-1].split("?")[0]
                body_str = request.read().decode("utf-8")
                patch_data = json.loads(body_str) if body_str else {}
                for t in created_tickets:
                    if t["sys_id"] == target_sys_id:
                        if "work_notes" in patch_data:
                            t["work_notes"].append(patch_data["work_notes"])
                        if "state" in patch_data:
                            t["state"] = patch_data["state"]
                return httpx.Response(200, json={"result": {"sys_id": target_sys_id}})

        # 3. Attachment file upload
        if "/api/now/attachment/file" in url_str:
            att_counter[0] += 1
            att_id = f"att-{att_counter[0]}"
            target_sys = request.url.params.get("table_sys_id", "")
            for t in created_tickets:
                if t["sys_id"] == target_sys:
                    t["attachments"].append({"sys_id": att_id, "content": request.read()})
            return httpx.Response(201, json={"result": {"sys_id": att_id}})

        return httpx.Response(404)

    snow_client = httpx.Client(transport=httpx.MockTransport(snow_handler))
    adapter = ServiceNowAdapter(config=bridge_cfg, http_client=snow_client)
    dispatcher = TaskDispatcher(config=bridge_cfg)

    # Wire bridge dependency overrides
    bridge_app.dependency_overrides[get_settings] = lambda: bridge_cfg
    set_servicenow_adapter(adapter)
    set_dispatcher(dispatcher)

    # Wire agent dependency overrides and bridge test client
    agent_app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    bridge_client_http = TestClient(bridge_app)
    set_default_http_client(bridge_client_http)

    clock = VirtualClock(initial_time=1729669926.0)
    set_clock_func(lambda: clock.now())
    set_timer_scheduler_hook(clock.timer_scheduler_hook)

    agent_client = TestClient(agent_app)
    okf_store = OKFStore(config=agent_cfg)

    env = {
        "clock": clock,
        "agent_client": agent_client,
        "bridge_client": bridge_client_http,
        "dispatcher": dispatcher,
        "adapter": adapter,
        "okf_store": okf_store,
        "bridge_cfg": bridge_cfg,
        "agent_cfg": agent_cfg,
        "store_dir": store_dir,
        "mock_snow": {
            "created_tickets": created_tickets,
            "client": snow_client,
        },
    }

    try:
        yield env
    finally:
        set_clock_func(None)
        set_timer_scheduler_hook(None)
        set_default_http_client(None)
        set_servicenow_adapter(None)
        set_dispatcher(None)
        agent_app.dependency_overrides.clear()
        bridge_app.dependency_overrides.clear()


def replay_live_sequence(
    alerts: list[tuple[AlertEvent, float]],
    env: dict[str, Any],
    settle_offset: float = 125.0,
    mock_sql_locks: bool = False,
    mock_change_revision: Optional[str] = None,
    mock_previous_revision: Optional[str] = "ata-inventory-api-00070-6nq",
    mock_cloud_run_auth_error: bool = False,
) -> tuple[list[OKFDocument], list[dict[str, Any]]]:
    """Helper exercising authentic Webhook -> Bridge -> Tasks -> Agent Triage -> Timer Wake -> Bridge Instruction -> ServiceNow."""
    clock: VirtualClock = env["clock"]
    agent_client: TestClient = env["agent_client"]
    bridge_client: TestClient = env["bridge_client"]
    dispatcher: TaskDispatcher = env["dispatcher"]
    store: OKFStore = env["okf_store"]
    webhook_token = env["bridge_cfg"].webhook_token

    patches = [
        patch("google.cloud.monitoring_v3.MetricServiceClient"),
        patch("google.cloud.logging.Client"),
    ]

    if mock_sql_locks:
        async def _mock_sql_investigate(alert, config):
            return EvidenceDossier(
                specialist_name="CloudSQLSpecialist",
                target_resource="ata-inventory-db",
                findings=["PostgreSQL transactional lock wait contention detected on inventory_items"],
                metrics={
                    "active_lock_waits": 8,
                    "active_connections": 20,
                    "max_connections": 100,
                    "cpu_utilization_pct": 8.0,
                    "metric_onset": alert.started_at or "2026-10-23T13:24:57Z",
                },
                evidence_items=[EvidenceItem(headline="Cloud SQL Lock Contention", content="8 active lock waits")],
                queries_executed={},
                anomaly_detected=True,
                confidence=0.92,
            )
        patches.append(patch("agent.app.specialists.cloud_sql.CloudSQLSpecialist.investigate", side_effect=_mock_sql_investigate))

    if mock_change_revision:
        async def _mock_change_investigate(alert, config):
            return EvidenceDossier(
                specialist_name="ChangeSpecialist",
                target_resource="ata-inventory-api",
                findings=[f"Recent revision deployment detected on `ata-inventory-api`: `{mock_change_revision}`."],
                metrics={
                    "recent_revision": mock_change_revision,
                    "previous_serving_revision": mock_previous_revision,
                    "previous_serving_evidence": (
                        f"Cloud Monitoring run.googleapis.com/request_count: {mock_previous_revision} was the only "
                        "revision of ata-inventory-api that received requests before the deploy."
                    ) if mock_previous_revision else "",
                    "change_service": "ata-inventory-api",
                    "minutes_since_deploy": 5,
                    "deploy_timestamp": alert.started_at or "2026-10-23T13:42:56Z",
                    "all_changes": [{
                        "service": "ata-inventory-api",
                        "revision": mock_change_revision,
                        "timestamp": alert.started_at or "2026-10-23T13:42:56Z",
                        "deployed_minutes_ago": 5,
                        "change_type": "revision_deploy",
                    }],
                },
                evidence_items=[EvidenceItem(headline="Recent Deployment Correlation", content=f"Revision: {mock_change_revision}")],
                queries_executed={},
                anomaly_detected=True,
                confidence=0.85,
            )
        patches.append(patch("agent.app.specialists.change.ChangeSpecialist.investigate", side_effect=_mock_change_investigate))

    if mock_cloud_run_auth_error:
        async def _mock_cr_investigate(alert, config):
            return EvidenceDossier(
                specialist_name="CloudRunSpecialist",
                target_resource="ata-inventory-api",
                findings=[
                    "Elevated HTTP 5xx error rate detected on `ata-inventory-api`: 42.0% of traffic breached threshold.",
                    "Application error logs on `ata-inventory-api` indicate database authentication failure.",
                ],
                metrics={
                    "error_rate_5xx": "42.0%",
                    "request_count_rpm": 250,
                    "container_restarts": 0,
                    "latency_p95_ms": 320.0,
                    "has_db_auth_error": True,
                    "observed_symptom": "DB auth errors from the app logs",
                },
                evidence_items=[EvidenceItem(headline="Cloud Run Anomaly Distribution: ata-inventory-api", content="Sample Logs:\n- FATAL: password authentication failed for user 'inventory_app'")],
                queries_executed={},
                anomaly_detected=True,
                confidence=0.88,
            )
        patches.append(patch("agent.app.specialists.cloud_run.CloudRunSpecialist.investigate", side_effect=_mock_cr_investigate))

    for p in patches:
        p.start()

    try:
        for alert, ts in alerts:
            # 1. Advance virtual clock to alert arrival time, firing any pending timer wakes
            clock.advance_to(ts, agent_client)
            clock.current_time = ts

            # 2. Dispatch to bridge monitoring webhook
            alert_payload = alert.model_dump(mode="json")
            bridge_resp = bridge_client.post(f"/webhook/monitoring?token={webhook_token}", json=alert_payload)
            assert bridge_resp.status_code == 202, f"Bridge webhook rejected alert: {bridge_resp.status_code} {bridge_resp.text}"

            task_id = bridge_resp.json()["task_id"]
            task_name = dispatcher.get_task_name(task_id)
            task_payload = dispatcher.local_payloads.get(task_name)
            assert task_payload is not None, f"Dispatcher failed to register task payload for {task_name}"

            # 3. Simulate Cloud Tasks delivery to agent /triage
            triage_resp = agent_client.post("/triage", json=task_payload)
            assert triage_resp.status_code == 200, f"Agent triage failed: {triage_resp.status_code} {triage_resp.text}"

        # 4. Final settle: advance clock to settle offset past last alert
        settle_target = clock.current_time + settle_offset
        clock.advance_to(settle_target, agent_client)

        active_docs = store.list_documents()
        return active_docs, env["mock_snow"]["created_tickets"]
    finally:
        for p in patches:
            p.stop()


# ==============================================================================
# S1: Backend 5xx Error Spike
# ==============================================================================

def test_replay_scenario_s1_backend_5xx(replay_env):
    """Replay S1: Inventory 5xx + Latency + Apigee orders 5xx -> exactly 1 group, 1 ticket, origin inventory-api."""
    base_ts = 1729669926.0  # 2026-10-23T07:52:06Z

    alert1 = AlertEvent(
        incident_id="0.oe5l7g8jv5k8",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T07:52:06Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "5xx",
            "severity": "p1",
        },
    )
    alert2 = AlertEvent(
        incident_id="0.oe5l7g8jv5kd",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T07:52:06Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "latency",
            "severity": "p2",
        },
    )
    alert3 = AlertEvent(
        incident_id="0.oe5guunhsdi3",
        policy_name="ata-apigee-orders-5xx",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T07:54:37Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-orders-v1",
            "tier": "edge",
            "signal": "5xx",
            "severity": "p1",
        },
    )

    alerts = [
        (alert1, base_ts),
        (alert2, base_ts + 5.0),
        (alert3, base_ts + 151.0),
    ]

    active_groups, tickets = replay_live_sequence(alerts, replay_env)

    assert len(active_groups) == 1, f"Expected exactly 1 group for S1, got {len(active_groups)}"
    assert len(tickets) == 1, f"Expected exactly 1 ticket for S1, got {len(tickets)}"

    grp = active_groups[0]
    assert grp.origin == "ata-inventory-api"
    inc_ids = [a.get("incident_id") for a in grp.alerts]
    assert "0.oe5l7g8jv5k8" in inc_ids
    assert "0.oe5l7g8jv5kd" in inc_ids
    assert "0.oe5guunhsdi3" in inc_ids

    # D10 Point 3 & 5: Assert origin and root-cause key facts
    # S1 is a backend 5xx error spike; traffic shifts are not changes; NO change attribution
    rc_text = _get_root_cause_text(grp)
    assert "change detected" not in rc_text.lower()
    assert "revision deployment" not in rc_text.lower()
    # Dominant signal is HTTP 5xx error rate
    assert "5xx" in rc_text.lower() or "500" in rc_text.lower()


# ==============================================================================
# S2: Backend Latency Degradation
# ==============================================================================

def test_replay_scenario_s2_backend_latency(replay_env):
    """Replay live S2 sequence through authentic thin bridge pipeline.
    
    Live S2 sequence:
    1. Alert 1: ata-orders-latency on ata-orders-api at T+0s
    2. Alert 2: ata-apigee-orders-5xx on ata-orders-v1 at T+137s
    3. Alert 3: ata-inventory-latency on ata-inventory-api at T+137s (same second)
    4. Alert 2 resolves: ata-apigee-orders-5xx closed at T+254s
    5. Alert 4: ata-inventory-latency on ata-inventory-api at T+383s
    
    Asserts DEMO.md: exactly 1 group, exactly 1 ticket, origin ata-inventory-api.
    """
    base_ts = 1729713177.0  # 2026-10-23T19:52:57Z

    alert1 = AlertEvent(
        incident_id="0.oe61o2q7f702",
        policy_name="ata-orders-latency",
        resource_name="ata-orders-api",
        started_at="2026-10-23T19:52:57Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-orders-api",
            "tier": "service",
            "signal": "latency",
            "severity": "p2",
        },
    )
    alert2 = AlertEvent(
        incident_id="0.oe61pyb0j2oa",
        policy_name="ata-apigee-orders-5xx",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T19:55:14Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-orders-v1",
            "tier": "edge",
            "signal": "5xx",
            "severity": "p1",
        },
    )
    alert3 = AlertEvent(
        incident_id="0.oe61pyb0j2of",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T19:55:14Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "latency",
            "severity": "p2",
        },
    )
    alert2_resolved = AlertEvent(
        incident_id="0.oe61pyb0j2oa",
        policy_name="ata-apigee-orders-5xx",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T19:55:14Z",
        state="closed",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-orders-v1",
            "tier": "edge",
            "signal": "5xx",
            "severity": "p1",
        },
    )
    alert4 = AlertEvent(
        incident_id="0.oe61tbneyg5z",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T19:59:20Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "latency",
            "severity": "p2",
        },
    )

    alerts = [
        (alert1, base_ts),
        (alert2, base_ts + 137.0),
        (alert3, base_ts + 137.0),
        (alert2_resolved, base_ts + 254.0),
        (alert4, base_ts + 383.0),
    ]

    active_groups, created_tickets = replay_live_sequence(alerts, replay_env)

    assert len(active_groups) == 1, f"Expected S2 to merge into 1 group, got {len(active_groups)}"
    assert len(created_tickets) == 1, f"Expected S2 to produce 1 ticket, got {len(created_tickets)}"

    grp = active_groups[0]
    assert grp.origin == "ata-inventory-api", f"S2 origin must be ata-inventory-api, got {grp.origin}"
    inc_ids = [a.get("incident_id") for a in grp.alerts]
    assert "0.oe61o2q7f702" in inc_ids
    assert "0.oe61pyb0j2oa" in inc_ids
    assert "0.oe61pyb0j2of" in inc_ids
    assert "0.oe61tbneyg5z" in inc_ids

    # D10 Point 3 & 5: Assert origin and root-cause key facts
    # S2 is latency degradation; traffic shifts are not changes; NO change attribution
    rc_text = _get_root_cause_text(grp)
    assert "change detected" not in rc_text.lower()
    assert "revision deployment" not in rc_text.lower()
    # Dominant signal must match latency policy (never cite 5xx error rate for S2 latency)
    assert any(k in rc_text.lower() for k in ("latency", "timeout"))
    assert "5xx error rate" not in rc_text.lower()


# ==============================================================================
# S3: Concurrent Disjoint Incidents (old S5)
# ==============================================================================

def test_replay_scenario_s3_concurrent_disjoint(replay_env):
    """Replay S3: Simultaneous backend 5xx and SpikeArrest 429 -> exactly 2 distinct groups, 2 tickets."""
    base_ts = 1729661090.0  # 2026-10-23T05:24:50Z

    alert_inv_5xx = AlertEvent(
        incident_id="0.oe5hudpa1xzn",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T05:24:50Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "5xx",
            "severity": "p1",
        },
    )
    alert_ord_429 = AlertEvent(
        incident_id="0.oe5hwcqe3x3w",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T05:27:14Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-orders-v1",
            "tier": "edge",
            "signal": "rate-limit",
            "severity": "p2",
        },
    )
    alert_inv_lat = AlertEvent(
        incident_id="0.oe5l7g8jv5kd",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T05:27:20Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "latency",
            "severity": "p2",
        },
    )

    alerts = [
        (alert_inv_5xx, base_ts),
        (alert_ord_429, base_ts + 144.0),
        (alert_inv_lat, base_ts + 150.0),
    ]

    active_groups, tickets = replay_live_sequence(alerts, replay_env)

    assert len(active_groups) == 2, f"Expected exactly 2 disjoint groups for S3, got {len(active_groups)}"
    assert len(tickets) == 2, f"Expected exactly 2 tickets for S3, got {len(tickets)}"

    origins = {g.origin for g in active_groups}
    assert "ata-inventory-api" in origins
    assert "ata-orders-v1" in origins

    inv_grp = next(g for g in active_groups if g.origin == "ata-inventory-api")
    inv_inc_ids = [a.get("incident_id") for a in inv_grp.alerts]
    assert "0.oe5hudpa1xzn" in inv_inc_ids
    assert "0.oe5l7g8jv5kd" in inv_inc_ids

    ord_grp = next(g for g in active_groups if g.origin == "ata-orders-v1")
    ord_inc_ids = [a.get("incident_id") for a in ord_grp.alerts]
    assert "0.oe5hwcqe3x3w" in ord_inc_ids

    # D10 Point 1 & 5: S3 group 2 origin stays at gateway (ata-orders-v1); backend is healthy
    assert ord_grp.origin == "ata-orders-v1"
    ord_rc = _get_root_cause_text(ord_grp)
    assert any(k in ord_rc.lower() for k in ("spikearrest", "rate-limit", "rate limit"))
    assert "ata-orders-v1" in ord_rc


# ==============================================================================
# S4: Database Connection Pool Exhaustion (old S6)
# ==============================================================================

def test_replay_scenario_s4_db_connection_pool(replay_env):
    """Replay S4: Cloud SQL connection saturation + backend latency -> 1 group, 1 ticket, origin ata-inventory-db."""
    base_ts = 1729689733.0  # 2026-10-23T13:22:13Z

    alert1 = AlertEvent(
        incident_id="0.oe5squfucupc",
        policy_name="ata-cloudsql-connections-critical",
        resource_name="ata-inventory-db",
        started_at="2026-10-23T13:22:13Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-db",
            "tier": "data",
            "signal": "connections",
            "severity": "p1",
        },
    )
    alert2 = AlertEvent(
        incident_id="0.oe5t7vkp3wi8",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T13:23:40Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "latency",
            "severity": "p2",
        },
    )

    alerts = [
        (alert1, base_ts),
        (alert2, base_ts + 87.0),
    ]

    active_groups, tickets = replay_live_sequence(alerts, replay_env)

    assert len(active_groups) == 1
    assert len(tickets) == 1
    assert active_groups[0].origin == "ata-inventory-db"
    inc_ids = [a.get("incident_id") for a in active_groups[0].alerts]
    assert "0.oe5squfucupc" in inc_ids
    assert "0.oe5t7vkp3wi8" in inc_ids


# ==============================================================================
# S5: Bad Deploy / Config Failure (old S8)
# ==============================================================================

def test_replay_scenario_s5_bad_deploy(replay_env):
    """Replay S5: Inventory latency + Inventory 5xx with bad deployment -> 1 group, 1 ticket, origin explicitly named revision.
    
    Strictly NO 'N+1' phrasing anywhere in root cause analysis or assertions.
    """
    base_ts = 1729690976.0  # 2026-10-23T13:42:56Z
    revision_name = "ata-inventory-api-00101-vw6"

    alert1 = AlertEvent(
        incident_id="0.oe5t7vkp3wi8",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T13:42:56Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "latency",
            "severity": "p2",
        },
    )
    alert2 = AlertEvent(
        incident_id="0.oe5tdjamzkgy",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T13:49:49Z",
        policy_user_labels={
            "app": "alert-triage-agent",
            "component": "ata-inventory-api",
            "tier": "service",
            "signal": "5xx",
            "severity": "p1",
        },
    )

    alerts = [
        (alert1, base_ts),
        (alert2, base_ts + 413.0),
    ]

    active_groups, tickets = replay_live_sequence(
        alerts,
        replay_env,
        mock_change_revision=revision_name,
        mock_previous_revision="ata-inventory-api-00070-6nq",
        mock_cloud_run_auth_error=True,
    )

    assert len(active_groups) == 1
    assert len(tickets) == 1
    assert active_groups[0].origin == "ata-inventory-api"
    inc_ids = [a.get("incident_id") for a in active_groups[0].alerts]
    assert "0.oe5t7vkp3wi8" in inc_ids
    assert "0.oe5tdjamzkgy" in inc_ids

    # D10 Point 2 & 5: S5 origin is ata-inventory-api (NOT ata-inventory-db)
    # Root cause names revision, deploy time, and observed symptom (database authentication failures in application logs)
    root_cause = _get_root_cause_text(active_groups[0])
    assert revision_name in root_cause
    assert "database authentication failure" in root_cause.lower() or "db auth errors from the app logs" in root_cause.lower()
    assert "upstream dependency" not in root_cause

    # Recommended action is a rollback to previous serving revision (named) plus checking revision DB config
    att_content = tickets[0]["attachments"][0]["content"].decode("utf-8") if tickets[0]["attachments"] else tickets[0]["body"]
    assert "update-traffic" in att_content and "00070-6nq" in att_content
    assert "revisions describe" in att_content and revision_name in att_content
    # NO database connection pool steps
    assert "pg_terminate_backend" not in att_content
    assert "max_connections=" not in att_content

    ticket_body = tickets[0]["body"]
    assert revision_name in ticket_body or "00101" in ticket_body
    # Strictly NO "N+1" phrasing anywhere
    assert "n+1" not in ticket_body.lower()
    assert "n + 1" not in ticket_body.lower()


def test_replay_revision_churn_traffic_repointing_no_change_attribution(replay_env):
    """D10 Point 5: Assert that revision churn (traffic re-pointing to existing known revision)
    does NOT trigger change attribution in triage.
    """
    base_ts = 1729669926.0

    alert = AlertEvent(
        incident_id="0.churn_test_alert",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T07:52:06Z",
    )

    # Mock ChangeSpecialist when traffic was repointed to existing revision 00070-6nq (created yesterday)
    # Specialist identifies it as a traffic shift and returns anomaly_detected = False
    async def _mock_churn_investigate(alert, config):
        return EvidenceDossier(
            specialist_name="ChangeSpecialist",
            target_resource="ata-inventory-api",
            findings=["Recent service configuration update detected, but revision `ata-inventory-api-00070-6nq` is not a new deployment (traffic shift to existing revision)."],
            metrics={
                "all_changes": [{
                    "service": "ata-inventory-api",
                    "revision": "ata-inventory-api-00070-6nq",
                    "timestamp": "2026-10-23T07:35:00Z",
                    "deployed_minutes_ago": 17,
                    "is_traffic_shift": True,
                }],
            },
            evidence_items=[],
            queries_executed={},
            anomaly_detected=False,
            confidence=0.50,
        )

    with patch("agent.app.specialists.change.ChangeSpecialist.investigate", side_effect=_mock_churn_investigate):
        active_groups, tickets = replay_live_sequence([(alert, base_ts)], replay_env)

    assert len(active_groups) == 1
    grp = active_groups[0]
    assert grp.origin == "ata-inventory-api"
    # Must NOT attribute fault to revision deployment
    rc_text = _get_root_cause_text(grp)
    assert "change detected" not in rc_text.lower()
    assert "revision deployment" not in rc_text.lower()


# ==============================================================================
# EDGE CASE 1: Flapping Alert Cool-Down Linger & Reactivation
# ==============================================================================

def test_edge_case_flapping_cooldown_linger(replay_env):
    """Assert flapping alert lifecycle:
    1. Alert arrives -> settles -> group enters ticketed (ticket created)
    2. Alert closes -> group transitions to cooling_down with +180s deadline
    3. Alert re-triggers within 180s -> group reactivates to ticketed, appends work note
    4. Delayed cooling wake arrives -> safely ignored; incident remains ticketed (not resolved).
    """
    clock: VirtualClock = replay_env["clock"]
    agent_client: TestClient = replay_env["agent_client"]
    bridge_client: TestClient = replay_env["bridge_client"]
    dispatcher: TaskDispatcher = replay_env["dispatcher"]
    store: OKFStore = replay_env["okf_store"]
    webhook_token = replay_env["bridge_cfg"].webhook_token
    mock_snow = replay_env["mock_snow"]

    base_ts = 1729700000.0

    # 1. Alert 1 arrives at t=0
    alert_open = AlertEvent(
        incident_id="0.flap_001",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T16:13:20Z",
        state="open",
    )
    replay_live_sequence([(alert_open, base_ts)], replay_env, settle_offset=125.0)

    # Verify group is ticketed and exactly 1 ticket created
    active_docs = store.list_documents()
    assert len(active_docs) == 1
    doc = active_docs[0]
    assert doc.status == IncidentGroupStatus.TICKETED.value
    assert len(mock_snow["created_tickets"]) == 1
    orig_ticket = mock_snow["created_tickets"][0]
    assert orig_ticket["number"] == doc.servicenow_number

    # 2. Alert closes at t=140
    alert_closed = AlertEvent(
        incident_id="0.flap_001",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T16:13:20Z",
        state="closed",
    )
    clock.advance_to(base_ts + 140.0, agent_client)
    clock.current_time = base_ts + 140.0

    bridge_resp = bridge_client.post(f"/webhook/monitoring?token={webhook_token}", json=alert_closed.model_dump(mode="json"))
    assert bridge_resp.status_code == 202
    task_id = bridge_resp.json()["task_id"]
    task_payload = dispatcher.local_payloads[dispatcher.get_task_name(task_id)]
    triage_resp = agent_client.post("/triage", json=task_payload)
    assert triage_resp.status_code == 200

    # Group enters cooling_down with +180s deadline (at 140 + 180 = 320)
    doc_cooling = store.read(doc.group_id)
    assert doc_cooling is not None
    assert doc_cooling.status == IncidentGroupStatus.COOLING_DOWN.value
    assert doc_cooling.cooling_deadline is not None

    # 3. Alert re-triggers (flaps) at t=200 (< 320)
    alert_retrigger = AlertEvent(
        incident_id="0.flap_002",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T16:16:40Z",
        state="open",
    )
    clock.advance_to(base_ts + 200.0, agent_client)
    clock.current_time = base_ts + 200.0

    bridge_resp = bridge_client.post(f"/webhook/monitoring?token={webhook_token}", json=alert_retrigger.model_dump(mode="json"))
    assert bridge_resp.status_code == 202
    task_id = bridge_resp.json()["task_id"]
    task_payload = dispatcher.local_payloads[dispatcher.get_task_name(task_id)]
    triage_resp = agent_client.post("/triage", json=task_payload)
    assert triage_resp.status_code == 200

    # Group reactivated to ticketed; cooling deadline cleared; work note appended
    doc_reactivated = store.read(doc.group_id)
    assert doc_reactivated is not None
    assert doc_reactivated.status == IncidentGroupStatus.TICKETED.value
    assert doc_reactivated.cooling_deadline is None
    # Exactly 1 ticket still
    assert len(mock_snow["created_tickets"]) == 1
    assert len(orig_ticket["work_notes"]) >= 1

    # 4. Advance clock past the original cooling timer (t=330)
    wakes = clock.advance_to(base_ts + 330.0, agent_client)
    cooling_wakes = [w for w in wakes if w["timer"]["deadline_type"] == "cooling"]
    if cooling_wakes:
        assert cooling_wakes[0]["body"].get("status") == "ignored"

    # Incident remains ticketed, not resolved
    doc_final = store.read(doc.group_id)
    assert doc_final.status == IncidentGroupStatus.TICKETED.value
    assert orig_ticket["state"] != "6"


# ==============================================================================
# EDGE CASE 2: Same-Second Arrival with Precondition Retry
# ==============================================================================

def test_edge_case_same_second_arrival(replay_env):
    """Assert concurrent alerts in the exact same second merge atomically into 1 group."""
    agent_client: TestClient = replay_env["agent_client"]
    bridge_client: TestClient = replay_env["bridge_client"]
    dispatcher: TaskDispatcher = replay_env["dispatcher"]
    store: OKFStore = replay_env["okf_store"]
    webhook_token = replay_env["bridge_cfg"].webhook_token

    same_second = 1729705733.0

    alert_a = AlertEvent(
        incident_id="0.race_inv_5xx",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T17:48:53Z",
    )
    alert_b = AlertEvent(
        incident_id="0.race_inv_latency",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T17:48:53Z",
    )

    def process_alert(alert: AlertEvent):
        resp = bridge_client.post(f"/webhook/monitoring?token={webhook_token}", json=alert.model_dump(mode="json"))
        task_id = resp.json()["task_id"]
        task_name = dispatcher.get_task_name(task_id)
        task_payload = dispatcher.local_payloads[task_name]
        return agent_client.post("/triage", json=task_payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(process_alert, a) for a in [alert_a, alert_b]]
        results = [f.result() for f in futures]

    for r in results:
        assert r.status_code == 200

    docs = store.list_documents()
    assert len(docs) == 1, f"Expected 1 merged group after same-second arrival, got {len(docs)}"
    doc = docs[0]
    alert_ids = [a.get("incident_id") for a in doc.alerts]
    assert "0.race_inv_5xx" in alert_ids
    assert "0.race_inv_latency" in alert_ids


# ==============================================================================
# EDGE CASE 3: Duplicate Cloud Tasks Delivery
# ==============================================================================

def test_edge_case_duplicate_task_delivery(replay_env):
    """Assert duplicate Cloud Tasks delivery of identical incident_id is suppressed as idempotent no-op."""
    agent_client: TestClient = replay_env["agent_client"]
    store: OKFStore = replay_env["okf_store"]

    alert = AlertEvent(
        incident_id="0.dup_task_101",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T18:00:00Z",
    )
    task_payload = {"alert": alert.model_dump(mode="json")}

    # Initial delivery
    resp1 = agent_client.post("/triage", json=task_payload)
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["status"] == "triaged"
    assert not data1["is_duplicate"]

    # Duplicate re-delivery (same incident_id)
    resp2 = agent_client.post("/triage", json=task_payload)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["status"] == "duplicate"
    assert data2["is_duplicate"] is True
    assert data2["action"] == "duplicate_suppressed"

    # Verify OKF alerts list contains only 1 entry for this alert
    doc = store.read(data1["group_id"])
    assert doc is not None
    matching = [a for a in doc.alerts if a.get("incident_id") == "0.dup_task_101"]
    assert len(matching) == 1


# ==============================================================================
# EDGE CASE 4: Bridge Instruction 5xx Retry with Correlation ID Idempotency
# ==============================================================================

def test_edge_case_bridge_instruction_retry_idempotency(replay_env):
    """Assert bridge instruction POST /instructions/ticket/create idempotently handles retries."""
    bridge_client: TestClient = replay_env["bridge_client"]
    agent_cfg: AgentConfig = replay_env["agent_cfg"]
    mock_snow = replay_env["mock_snow"]

    alert = AlertEvent(
        incident_id="0.retry_001",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T18:30:00Z",
    )
    report = generate_investigation_report(alert, "grp_retry_test", "incident-grp_retry_test.md", agent_cfg)

    create_req = CreateTicketRequest(group_id="grp_retry_test", report=report)
    headers = {"Authorization": "Bearer test-agent-token"}

    # First attempt: successfully creates ticket
    resp1 = bridge_client.post("/instructions/ticket/create", json=create_req.model_dump(mode="json"), headers=headers)
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["status"] == "created"
    sys_id = data1["sys_id"]
    number = data1["number"]

    # Second attempt (simulating client retry after network blip): finds existing ticket by correlation_id
    resp2 = bridge_client.post("/instructions/ticket/create", json=create_req.model_dump(mode="json"), headers=headers)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["status"] == "already_exists"
    assert data2["sys_id"] == sys_id
    assert data2["number"] == number

    # Exactly 1 ticket was created in ServiceNow
    assert len(mock_snow["created_tickets"]) == 1

    # Verify BridgeClient retries on transient 503 and succeeds
    call_count = [0]
    def flappy_handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(503, json={"error": "Service Unavailable"})
        return httpx.Response(200, json={
            "status": "already_exists",
            "sys_id": sys_id,
            "number": number,
            "url": f"https://instance.service-now.com/{sys_id}",
            "group_id": "grp_retry_test",
        })

    flappy_transport = httpx.MockTransport(flappy_handler)
    flappy_client = httpx.Client(transport=flappy_transport)
    bc = BridgeClient(config=agent_cfg, http_client=flappy_client, max_retries=2, backoff_factor=0.01)
    res = bc.create_ticket("http://testbridge", create_req)
    assert res.number == number
    assert call_count[0] == 2


# ==============================================================================
# ORIGIN ATTRIBUTION TELEMETRY GUARDRAILS
# ==============================================================================

def test_origin_attribution_never_names_db_when_metrics_normal():
    """Assert synthesizer NEVER attributes origin to ata-inventory-db when DB telemetry is normal."""
    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="0.test_s2_latency",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T17:29:52Z",
    )

    dossier_map = {
        "CloudRunSpecialist": EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            findings=["Latency spike observed on ata-inventory-api"],
            metrics={"latency_p99_ms": 6500, "metric_onset": "2026-10-23T17:29:52Z"},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=True,
            confidence=_compute_confidence(0.70, 0.15),
        ),
        "CloudSQLSpecialist": EvidenceDossier(
            specialist_name="CloudSQLSpecialist",
            target_resource="ata-inventory-db",
            findings=["PostgreSQL metrics are nominal"],
            metrics={
                "active_connections": 2,
                "max_connections": 25,
                "active_lock_waits": 0,
                "cpu_utilization_pct": 5.0,
            },
            evidence_items=[],
            queries_executed={},
            anomaly_detected=False,
            confidence=_compute_confidence(0.70, 0.10),
        ),
    }

    origin = synth._determine_probable_origin(
        alert=alert,
        dossiers=dossier_map,
        upstream_deps=["ata-inventory-db"],
    )

    assert origin == "ata-inventory-api", f"Origin must not be attributed to DB when DB metrics are normal: got {origin}"
    assert origin != "ata-inventory-db"


def test_origin_attribution_names_db_when_telemetry_anomalous():
    """Assert synthesizer attributes origin to ata-inventory-db when DB connections are saturated."""
    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="0.test_s4_db",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T13:22:13Z",
    )

    dossier_map = {
        "CloudRunSpecialist": EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            findings=["Connection starvation causing request queuing"],
            metrics={"latency_p99_ms": 5200, "metric_onset": "2026-10-23T13:23:40Z"},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=True,
            confidence=_compute_confidence(0.70, 0.12),
        ),
        "CloudSQLSpecialist": EvidenceDossier(
            specialist_name="CloudSQLSpecialist",
            target_resource="ata-inventory-db",
            findings=["PostgreSQL connections saturated at 24/25"],
            metrics={
                "active_connections": 24,
                "max_connections": 25,
                "active_lock_waits": 0,
                "cpu_utilization_pct": 12.0,
                "metric_onset": "2026-10-23T13:22:13Z",
            },
            evidence_items=[],
            queries_executed={},
            anomaly_detected=True,
            confidence=_compute_confidence(0.75, 0.15),
        ),
    }

    origin = synth._determine_probable_origin(
        alert=alert,
        dossiers=dossier_map,
        upstream_deps=["ata-inventory-db"],
    )

    assert origin == "ata-inventory-db", f"Expected origin ata-inventory-db when DB telemetry is anomalous, got {origin}"


def test_origin_attribution_apigee_faults_retain_apigee_origin():
    """Assert synthesizer attributes origin to ata-orders-v1 for SpikeArrest 429 when backends are healthy."""
    synth = SynthesizerAgent()
    alert_spikearrest = AlertEvent(
        incident_id="0.test_s3_spikearrest",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T08:16:11Z",
    )

    dossier_map = {
        "ApigeeSpecialist": EvidenceDossier(
            specialist_name="ApigeeSpecialist",
            target_resource="ata-orders-v1",
            findings=["SpikeArrest policy breached"],
            metrics={"status_429_rate": "80.0%", "metric_onset": "2026-10-23T08:16:11Z"},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=True,
            confidence=_compute_confidence(0.90, 0.10),
        ),
        "ata-orders-api": EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-orders-api",
            findings=["Orders API healthy"],
            metrics={"error_rate_5xx": "0.0%", "latency_p95_ms": 110.0},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=False,
            confidence=_compute_confidence(0.80, 0.10),
        ),
        "ata-inventory-api": EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            findings=["Inventory API healthy"],
            metrics={"error_rate_5xx": "0.0%", "latency_p95_ms": 85.0},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=False,
            confidence=_compute_confidence(0.80, 0.10),
        ),
    }

    origin = synth._determine_probable_origin(
        alert=alert_spikearrest,
        dossiers=dossier_map,
        upstream_deps=["ata-orders-api", "ata-inventory-api"],
    )
    assert origin == "ata-orders-v1", f"SpikeArrest origin must be ata-orders-v1, got {origin}"
