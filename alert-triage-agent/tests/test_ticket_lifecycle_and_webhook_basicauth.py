"""Unit tests for incident ticket lifecycle, resolution guarantees, and webhook basic auth security rotation."""

import base64
from datetime import datetime, timezone
from pathlib import Path
import re
import pytest
from starlette.testclient import TestClient

from agent.app.bridge_client import BridgeClient
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import ResolveTicketRequest, TicketResponse
from agent.app.contracts.timers import TimerWakeRequest
from agent.app.grouping.models import IncidentGroupStatus, OKFDocument
from agent.app.grouping.store import OKFStore
from agent.app.main import app as agent_app, set_clock_func
from agent.app.timers.scheduler import set_timer_scheduler_hook
from bridge.app.config import BridgeConfig, reset_settings
from bridge.app.core.dispatcher import TaskDispatcher
from bridge.app.main import app as bridge_app, set_dispatcher


@pytest.fixture
def temp_store(tmp_path: Path) -> Path:
    store_dir = tmp_path / "knowledge" / "incident-manager"
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir


@pytest.fixture
def agent_cfg(temp_store: Path) -> AgentConfig:
    return AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        incident_store_path=str(temp_store),
        fallback_store_path=str(temp_store),
        is_local=True,
    )


# ==============================================================================
# 1. Early Alert Close Lifecycle & Ticket Guarantee Tests
# ==============================================================================

def test_early_alert_close_does_not_prevent_ticket_creation(agent_cfg: AgentConfig, monkeypatch):
    """Test that an alert closing before the settle timer fires leaves the group active,
    and the settle wake successfully creates a ticket and initiates cooling down.
    """
    agent_app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    client = TestClient(agent_app)
    store = OKFStore(config=agent_cfg)

    simulated_time = 1000.0
    set_clock_func(lambda: simulated_time)

    scheduled_timers = []
    set_timer_scheduler_hook(lambda **kwargs: scheduled_timers.append(kwargs) or "task-timer")

    try:
        # Step 1: Open alert arrives at t=1000
        alert_open = AlertEvent(
            incident_id="0.early-close-alert",
            policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-backend-5xx",
            resource_name="ata-orders-api",
            resource_type="cloud_run_revision",
            state="open",
            started_at="2026-10-24T00:16:40Z",
        )
        triage_resp = client.post("/triage", json=alert_open.model_dump(mode="json"))
        assert triage_resp.status_code == 200
        group_id = triage_resp.json()["group_id"]

        doc = store.read(group_id)
        assert doc is not None
        assert doc.status == IncidentGroupStatus.OPEN.value
        assert doc.servicenow_sys_id is None
        assert doc.settle_deadline_epoch() == pytest.approx(1120.0)

        # Step 2: Alert closes early at t=1050 (before settle timer at 1120)
        simulated_time = 1050.0
        alert_close = AlertEvent(
            incident_id="0.early-close-alert",
            policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-backend-5xx",
            resource_name="ata-orders-api",
            resource_type="cloud_run_revision",
            state="closed",
            started_at="2026-10-24T00:16:40Z",
        )
        close_resp = client.post("/triage", json=alert_close.model_dump(mode="json"))
        assert close_resp.status_code == 200

        # Verification: Because the group was not yet ticketed, it must NOT transition to cooling_down!
        doc_after_close = store.read(group_id)
        assert doc_after_close.status in (IncidentGroupStatus.OPEN.value, IncidentGroupStatus.INVESTIGATING.value)
        assert doc_after_close.status != IncidentGroupStatus.COOLING_DOWN.value
        assert doc_after_close.all_alerts_closed() is True
        assert doc_after_close.servicenow_sys_id is None

        # Step 3: Settle timer wakes at t=1120.0
        simulated_time = 1120.0
        ticket_created = False

        def mock_create_ticket(self, bridge_url, request, config=None, http_client=None):
            nonlocal ticket_created
            ticket_created = True
            return TicketResponse(
                status="created",
                sys_id="sys_early_close_123",
                number="INC004455",
                url="https://mock.service-now.com/incident.do?sys_id=sys_early_close_123",
                group_id=request.group_id,
            )

        monkeypatch.setattr(BridgeClient, "create_ticket", mock_create_ticket)

        wake_req = TimerWakeRequest(
            group_id=group_id,
            deadline_type="settle",
            scheduled_epoch=1120,
        )
        wake_resp = client.post("/timer/wake", json=wake_req.model_dump(mode="json"))
        assert wake_resp.status_code == 200
        assert wake_resp.json()["status"] == "executed"
        assert ticket_created is True

        # Verification: Group is now ticketed and since all alerts are closed,
        # transitioned to cooling_down with cooling timer scheduled.
        doc_final = store.read(group_id)
        assert doc_final.servicenow_sys_id == "sys_early_close_123"
        assert doc_final.servicenow_number == "INC004455"
        assert doc_final.status == IncidentGroupStatus.COOLING_DOWN.value
        assert doc_final.cooling_deadline is not None
        assert doc_final.cooling_deadline_epoch() == pytest.approx(1120.0 + 180.0)

        # Check cooling timer was scheduled
        cooling_timers = [t for t in scheduled_timers if t.get("deadline_type") == "cooling"]
        assert len(cooling_timers) == 1
        assert cooling_timers[0]["group_id"] == group_id

    finally:
        set_clock_func(None)
        set_timer_scheduler_hook(None)
        agent_app.dependency_overrides.clear()


def test_settle_wake_recovers_if_cooling_down_without_ticket(agent_cfg: AgentConfig, monkeypatch):
    """Test defensive recovery: if group is cooling_down but servicenow_sys_id is None,
    settle wake does NOT ignore the wake and proceeds with ticket creation.
    """
    agent_app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    client = TestClient(agent_app)
    store = OKFStore(config=agent_cfg)

    simulated_time = 1120.0
    set_clock_func(lambda: simulated_time)

    try:
        group_id = "test-legacy-cooling-unticketed"
        doc = OKFDocument(
            group_id=group_id,
            status=IncidentGroupStatus.COOLING_DOWN.value,
            settle_deadline=datetime.fromtimestamp(1120.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            cooling_deadline=datetime.fromtimestamp(1300.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            servicenow_sys_id=None,
            alerts=[{
                "incident_id": "0.legacy-alert",
                "policy_name": "ata-backend-5xx",
                "resource_name": "ata-orders-api",
                "state": "closed",
            }],
        )
        store.write(group_id, doc)

        ticket_created = False

        def mock_create_ticket(self, bridge_url, request, config=None, http_client=None):
            nonlocal ticket_created
            ticket_created = True
            return TicketResponse(
                status="created",
                sys_id="sys_recovered_456",
                number="INC007788",
                url="https://mock.service-now.com/incident.do?sys_id=sys_recovered_456",
                group_id=request.group_id,
            )

        monkeypatch.setattr(BridgeClient, "create_ticket", mock_create_ticket)

        wake_req = TimerWakeRequest(
            group_id=group_id,
            deadline_type="settle",
            scheduled_epoch=1120,
        )
        wake_resp = client.post("/timer/wake", json=wake_req.model_dump(mode="json"))
        assert wake_resp.status_code == 200
        assert wake_resp.json()["status"] == "executed"
        assert ticket_created is True

        doc_after = store.read(group_id)
        assert doc_after.servicenow_sys_id == "sys_recovered_456"
        assert doc_after.status == IncidentGroupStatus.COOLING_DOWN.value

    finally:
        set_clock_func(None)
        agent_app.dependency_overrides.clear()


# ==============================================================================
# 2. Cooling Wake Resolution Rejection Guarantees
# ==============================================================================

def test_cooling_wake_rejects_unticketed_group_without_suppression(agent_cfg: AgentConfig):
    """Test that cooling wake refuses to resolve an unticketed group without explicit suppression reason,
    marking it degraded instead.
    """
    agent_app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    client = TestClient(agent_app)
    store = OKFStore(config=agent_cfg)

    simulated_time = 1300.0
    set_clock_func(lambda: simulated_time)

    try:
        group_id = "test-unticketed-cooling-rejection"
        doc = OKFDocument(
            group_id=group_id,
            status=IncidentGroupStatus.COOLING_DOWN.value,
            cooling_deadline=datetime.fromtimestamp(1300.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            servicenow_sys_id=None,
            suppressed=False,
            suppression_reason=None,
            alerts=[{
                "incident_id": "0.alert-1",
                "state": "closed",
            }],
        )
        store.write(group_id, doc)

        wake_req = TimerWakeRequest(
            group_id=group_id,
            deadline_type="cooling",
            scheduled_epoch=1300,
        )
        wake_resp = client.post("/timer/wake", json=wake_req.model_dump(mode="json"))
        assert wake_resp.status_code == 200
        data = wake_resp.json()
        assert data["status"] == "rejected"
        assert data["reason"] == "unresolved_unticketed"

        doc_after = store.read(group_id)
        assert doc_after.status == IncidentGroupStatus.DEGRADED.value
        assert doc_after.degraded is True
        assert doc_after.degraded_reason == "Attempted resolution without ticket or suppression reason"
        assert doc_after.status != IncidentGroupStatus.RESOLVED.value

    finally:
        set_clock_func(None)
        agent_app.dependency_overrides.clear()


def test_cooling_wake_allows_resolution_for_suppressed_group(agent_cfg: AgentConfig):
    """Test that cooling wake allows resolving an unticketed group IF it has explicit suppression reason."""
    agent_app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    client = TestClient(agent_app)
    store = OKFStore(config=agent_cfg)

    simulated_time = 1300.0
    set_clock_func(lambda: simulated_time)

    try:
        group_id = "test-suppressed-cooling-resolution"
        doc = OKFDocument(
            group_id=group_id,
            status=IncidentGroupStatus.COOLING_DOWN.value,
            cooling_deadline=datetime.fromtimestamp(1300.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            servicenow_sys_id=None,
            suppressed=True,
            suppression_reason="Synthetic probe noise suppressed by policy",
            alerts=[{
                "incident_id": "0.alert-suppressed",
                "state": "closed",
            }],
        )
        store.write(group_id, doc)

        wake_req = TimerWakeRequest(
            group_id=group_id,
            deadline_type="cooling",
            scheduled_epoch=1300,
        )
        wake_resp = client.post("/timer/wake", json=wake_req.model_dump(mode="json"))
        assert wake_resp.status_code == 200
        assert wake_resp.json()["status"] == "executed"

        doc_after = store.read(group_id)
        assert doc_after.status == IncidentGroupStatus.RESOLVED.value
        assert doc_after.suppressed is True
        assert doc_after.suppression_reason == "Synthetic probe noise suppressed by policy"

    finally:
        set_clock_func(None)
        agent_app.dependency_overrides.clear()


def test_cooling_wake_resolves_ticketed_group(agent_cfg: AgentConfig, monkeypatch):
    """Test that cooling wake resolves a ticketed group and calls bridge resolve."""
    agent_app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    client = TestClient(agent_app)
    store = OKFStore(config=agent_cfg)

    simulated_time = 1300.0
    set_clock_func(lambda: simulated_time)

    resolved_ticket_sys_id = None

    def mock_resolve_ticket(self, bridge_url, request, config=None, http_client=None):
        nonlocal resolved_ticket_sys_id
        resolved_ticket_sys_id = request.sys_id

    monkeypatch.setattr(BridgeClient, "resolve_ticket", mock_resolve_ticket)

    try:
        group_id = "test-ticketed-cooling-resolution"
        doc = OKFDocument(
            group_id=group_id,
            status=IncidentGroupStatus.COOLING_DOWN.value,
            cooling_deadline=datetime.fromtimestamp(1300.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            servicenow_sys_id="sys_ticket_to_resolve",
            alerts=[{"incident_id": "0.alert-1", "state": "closed"}],
        )
        store.write(group_id, doc)

        wake_req = TimerWakeRequest(
            group_id=group_id,
            deadline_type="cooling",
            scheduled_epoch=1300,
        )
        wake_resp = client.post("/timer/wake", json=wake_req.model_dump(mode="json"))
        assert wake_resp.status_code == 200
        assert wake_resp.json()["status"] == "executed"

        assert resolved_ticket_sys_id == "sys_ticket_to_resolve"
        doc_after = store.read(group_id)
        assert doc_after.status == IncidentGroupStatus.RESOLVED.value

    finally:
        set_clock_func(None)
        agent_app.dependency_overrides.clear()


# ==============================================================================
# 3. Webhook BasicAuth Verification Tests
# ==============================================================================

@pytest.fixture
def bridge_test_config():
    config = BridgeConfig(
        project_id="test-project",
        name_prefix="ata",
        region="us-west1",
        webhook_token="secret-rotated-password",
        is_local=True,
    )
    reset_settings(config)
    yield config
    reset_settings(None)


@pytest.fixture
def bridge_client(bridge_test_config):
    dispatcher = TaskDispatcher(config=bridge_test_config)
    set_dispatcher(dispatcher)
    with TestClient(bridge_app) as test_client:
        yield test_client


def test_bridge_webhook_accepts_basic_auth(bridge_client):
    """Assert bridge webhook accepts valid Authorization: Basic header with username:password."""
    creds = "ata-monitoring:secret-rotated-password"
    b64_creds = base64.b64encode(creds.encode("utf-8")).decode("utf-8")
    headers = {"Authorization": f"Basic {b64_creds}"}

    payload = {
        "version": "1.2",
        "incident": {
            "incident_id": "0.basicauth_test",
            "resource_name": "ata-orders-api",
            "resource_type": "cloud_run_revision",
            "state": "open",
            "started_at": 1729631700,
            "policy_name": "ata-backend-5xx-error-rate",
        },
    }

    resp = bridge_client.post("/webhook/monitoring", json=payload, headers=headers)
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    assert resp.json()["incident_id"] == "0.basicauth_test"


def test_bridge_webhook_rejects_invalid_basic_auth(bridge_client):
    """Assert bridge webhook rejects invalid Authorization: Basic credentials with 401."""
    # 1. Invalid password
    wrong_pwd = base64.b64encode(b"ata-monitoring:wrong-secret").decode("utf-8")
    resp1 = bridge_client.post(
        "/webhook/monitoring",
        json={"incident": {"incident_id": "0.test", "policy_name": "test", "resource_name": "test"}},
        headers={"Authorization": f"Basic {wrong_pwd}"},
    )
    assert resp1.status_code == 401

    # 2. Invalid username format / missing colon
    bad_format = base64.b64encode(b"no-colon-here").decode("utf-8")
    resp2 = bridge_client.post(
        "/webhook/monitoring",
        json={"incident": {"incident_id": "0.test", "policy_name": "test", "resource_name": "test"}},
        headers={"Authorization": f"Basic {bad_format}"},
    )
    assert resp2.status_code == 401

    # 3. Corrupt base64
    resp3 = bridge_client.post(
        "/webhook/monitoring",
        json={"incident": {"incident_id": "0.test", "policy_name": "test", "resource_name": "test"}},
        headers={"Authorization": "Basic !!!not-base64!!!"},
    )
    assert resp3.status_code == 401


def test_bridge_webhook_preserves_backwards_compatibility(bridge_client):
    """Assert backwards compatibility with Bearer token and query param token."""
    payload = {
        "version": "1.2",
        "incident": {
            "incident_id": "0.compat_test",
            "resource_name": "ata-orders-api",
            "resource_type": "cloud_run_revision",
            "state": "open",
            "started_at": 1729631700,
            "policy_name": "ata-backend-5xx-error-rate",
        },
    }

    # Query param token
    resp_query = bridge_client.post("/webhook/monitoring?token=secret-rotated-password", json=payload)
    assert resp_query.status_code == 202

    # Bearer header
    resp_bearer = bridge_client.post(
        "/webhook/monitoring",
        json=payload,
        headers={"Authorization": "Bearer secret-rotated-password"},
    )
    assert resp_bearer.status_code == 202


# ==============================================================================
# 4. Terraform Notification Channel Configuration Verification
# ==============================================================================

def test_terraform_notification_channel_has_no_query_parameters():
    repo_root = Path(__file__).resolve().parent.parent
    tf_path = repo_root / "infra" / "terraform" / "foundation" / "main.tf"
    assert tf_path.exists(), f"Terraform file not found at {tf_path}"

    content = tf_path.read_text(encoding="utf-8")

    # Match bridge_webhook resource
    match = re.search(
        r'resource\s+"google_monitoring_notification_channel"\s+"bridge_webhook"\s+{(.*?)\n}',
        content,
        re.DOTALL,
    )
    assert match is not None, "resource google_monitoring_notification_channel.bridge_webhook not found"
    channel_block = match.group(1)

    # Assert type is webhook_basicauth
    assert 'type         = "webhook_basicauth"' in channel_block or 'type = "webhook_basicauth"' in channel_block

    # Assert url has NO query parameters
    url_match = re.search(r'url\s+=\s+"([^"]+)"', channel_block)
    assert url_match is not None, "URL not found in channel block"
    url = url_match.group(1)
    assert "?" not in url, f"URL must not contain query parameters: {url}"
    assert "token=" not in url

    # Assert username and password are configured
    assert 'username = "ata-monitoring"' in channel_block
    assert "password = random_password.webhook_token.result" in channel_block

    # Assert random_password has rotation keeper
    assert 'rotation = "v2_basicauth"' in content
