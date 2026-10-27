"""Unit tests verifying instruction and timer contracts and Terraform specifications.

Validates:
1. Instruction contract schemas and timer contract schemas in agent and bridge.
2. JSON round-tripping for all models.
3. Validation failures on invalid or missing fields.
4. Schema defaults.
5. Contract parity between agent and bridge contracts (byte and schema equivalence).
6. Terraform foundation and applications configuration files for required resources and IAM bindings.
"""

from pathlib import Path
import pytest
from pydantic import ValidationError

# Agent contracts
from agent.app.contracts.report import InvestigationReport, ProbableRootCause
from agent.app.contracts.instructions import (
    CreateTicketRequest as AgentCreateTicketRequest,
    WorkNoteRequest as AgentWorkNoteRequest,
    UpdateReportRequest as AgentUpdateReportRequest,
    UpdateTicketFieldsRequest as AgentUpdateTicketFieldsRequest,
    ResolveTicketRequest as AgentResolveTicketRequest,
    TicketResponse as AgentTicketResponse,
    ActionStatusResponse as AgentActionStatusResponse,
)
from agent.app.contracts.timers import (
    TimerWakeRequest as AgentTimerWakeRequest,
    TimerWakeResponse as AgentTimerWakeResponse,
)

# Bridge contracts
from bridge.app.contracts.report import (
    InvestigationReport as BridgeInvestigationReport,
    ProbableRootCause as BridgeProbableRootCause,
)
from bridge.app.contracts.instructions import (
    CreateTicketRequest as BridgeCreateTicketRequest,
    WorkNoteRequest as BridgeWorkNoteRequest,
    UpdateReportRequest as BridgeUpdateReportRequest,
    UpdateTicketFieldsRequest as BridgeUpdateTicketFieldsRequest,
    ResolveTicketRequest as BridgeResolveTicketRequest,
    TicketResponse as BridgeTicketResponse,
    ActionStatusResponse as BridgeActionStatusResponse,
)
from bridge.app.contracts.timers import (
    TimerWakeRequest as BridgeTimerWakeRequest,
    TimerWakeResponse as BridgeTimerWakeResponse,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sample_report():
    return InvestigationReport(
        group_id="grp_test_001",
        summary="Test incident report for contract validation",
        observations="Sample observations",
        probable_root_cause=ProbableRootCause(text="Root cause found", confidence=0.92),
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api"],
        priority="P1",
    )


@pytest.fixture
def sample_bridge_report():
    return BridgeInvestigationReport(
        group_id="grp_test_001",
        summary="Test incident report for contract validation",
        observations="Sample observations",
        probable_root_cause=BridgeProbableRootCause(text="Root cause found", confidence=0.92),
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api"],
        priority="P1",
    )


# ---------------------------------------------------------------------------
# 1. Contract Parity Tests
# ---------------------------------------------------------------------------

def test_contract_files_identical():
    """Assert contract source files are byte-identical between agent and bridge."""
    agent_inst = (REPO_ROOT / "agent/app/contracts/instructions.py").read_bytes()
    bridge_inst = (REPO_ROOT / "bridge/app/contracts/instructions.py").read_bytes()
    assert agent_inst == bridge_inst, "instructions.py must be byte-identical between agent and bridge"

    agent_timers = (REPO_ROOT / "agent/app/contracts/timers.py").read_bytes()
    bridge_timers = (REPO_ROOT / "bridge/app/contracts/timers.py").read_bytes()
    assert agent_timers == bridge_timers, "timers.py must be byte-identical between agent and bridge"


def test_contract_json_schema_parity():
    """Assert Pydantic JSON schemas are identical between agent and bridge implementations."""
    model_pairs = [
        (AgentCreateTicketRequest, BridgeCreateTicketRequest),
        (AgentWorkNoteRequest, BridgeWorkNoteRequest),
        (AgentUpdateReportRequest, BridgeUpdateReportRequest),
        (AgentUpdateTicketFieldsRequest, BridgeUpdateTicketFieldsRequest),
        (AgentResolveTicketRequest, BridgeResolveTicketRequest),
        (AgentTicketResponse, BridgeTicketResponse),
        (AgentActionStatusResponse, BridgeActionStatusResponse),
        (AgentTimerWakeRequest, BridgeTimerWakeRequest),
        (AgentTimerWakeResponse, BridgeTimerWakeResponse),
    ]

    for agent_cls, bridge_cls in model_pairs:
        assert agent_cls.model_json_schema() == bridge_cls.model_json_schema(), (
            f"Schema mismatch between agent and bridge for {agent_cls.__name__}"
        )


# ---------------------------------------------------------------------------
# 2. JSON Round-tripping Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("create_cls,report_fixture_name", [
    (AgentCreateTicketRequest, "sample_report"),
    (BridgeCreateTicketRequest, "sample_bridge_report"),
])
def test_create_ticket_request_roundtrip(create_cls, report_fixture_name, request):
    report = request.getfixturevalue(report_fixture_name)
    req = create_cls(
        group_id="grp_test_001",
        report=report,
        idempotency_key="key-abc-123",
    )
    json_data = req.model_dump_json()
    restored = create_cls.model_validate_json(json_data)
    assert restored.group_id == req.group_id
    assert restored.idempotency_key == "key-abc-123"
    assert restored.report.group_id == req.report.group_id
    assert restored.report.summary == req.report.summary
    assert restored.report.probable_root_cause.text == "Root cause found"


@pytest.mark.parametrize("work_note_cls", [AgentWorkNoteRequest, BridgeWorkNoteRequest])
def test_work_note_request_roundtrip(work_note_cls):
    req = work_note_cls(
        sys_id="sys_12345",
        work_note="[code]<p>Late alert correlated to incident</p>[/code]",
        group_id="grp_test_001",
    )
    json_data = req.model_dump_json()
    restored = work_note_cls.model_validate_json(json_data)
    assert restored == req


@pytest.mark.parametrize("update_cls,report_fixture_name", [
    (AgentUpdateReportRequest, "sample_report"),
    (BridgeUpdateReportRequest, "sample_bridge_report"),
])
def test_update_report_request_roundtrip(update_cls, report_fixture_name, request):
    report = request.getfixturevalue(report_fixture_name)
    req = update_cls(
        sys_id="sys_12345",
        group_id="grp_test_001",
        report=report,
    )
    json_data = req.model_dump_json()
    restored = update_cls.model_validate_json(json_data)
    assert restored.sys_id == "sys_12345"
    assert restored.group_id == "grp_test_001"
    assert restored.report.summary == report.summary


@pytest.mark.parametrize("resolve_cls", [AgentResolveTicketRequest, BridgeResolveTicketRequest])
def test_resolve_ticket_request_roundtrip(resolve_cls):
    req = resolve_cls(
        sys_id="sys_12345",
        group_id="grp_test_001",
        close_notes="Rolled back bad revision to fix crash loop.",
        close_code="Solved (Permanently)",
    )
    json_data = req.model_dump_json()
    restored = resolve_cls.model_validate_json(json_data)
    assert restored == req


@pytest.mark.parametrize("ticket_resp_cls", [AgentTicketResponse, BridgeTicketResponse])
def test_ticket_response_roundtrip(ticket_resp_cls):
    resp = ticket_resp_cls(
        status="created",
        sys_id="sys_abcdef123456",
        number="INC0010482",
        url="https://dev12345.service-now.com/nav_to.do?uri=incident.do?sys_id=sys_abcdef123456",
        group_id="grp_test_001",
    )
    json_data = resp.model_dump_json()
    restored = ticket_resp_cls.model_validate_json(json_data)
    assert restored == resp


@pytest.mark.parametrize("action_resp_cls", [AgentActionStatusResponse, BridgeActionStatusResponse])
def test_action_status_response_roundtrip(action_resp_cls):
    resp = action_resp_cls(
        status="updated",
        sys_id="sys_12345",
        group_id="grp_test_001",
        message="Work note appended successfully",
    )
    json_data = resp.model_dump_json()
    restored = action_resp_cls.model_validate_json(json_data)
    assert restored == resp


@pytest.mark.parametrize("timer_req_cls", [AgentTimerWakeRequest, BridgeTimerWakeRequest])
def test_timer_wake_request_roundtrip(timer_req_cls):
    req = timer_req_cls(
        group_id="grp_orders_latency_20261024",
        deadline_type="settle",
        scheduled_epoch=1792880000,
    )
    json_data = req.model_dump_json()
    restored = timer_req_cls.model_validate_json(json_data)
    assert restored == req


@pytest.mark.parametrize("timer_resp_cls", [AgentTimerWakeResponse, BridgeTimerWakeResponse])
def test_timer_wake_response_roundtrip(timer_resp_cls):
    resp = timer_resp_cls(
        status="ignored",
        group_id="grp_orders_latency_20261024",
        reason="stale_wake",
    )
    json_data = resp.model_dump_json()
    restored = timer_resp_cls.model_validate_json(json_data)
    assert restored == resp


# ---------------------------------------------------------------------------
# 3. Validation Failure Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("create_cls", [AgentCreateTicketRequest, BridgeCreateTicketRequest])
def test_create_ticket_request_missing_required_fields(create_cls):
    with pytest.raises(ValidationError) as exc:
        create_cls()  # missing group_id and report
    errors = exc.value.errors()
    field_names = [e["loc"][0] for e in errors]
    assert "group_id" in field_names
    assert "report" in field_names


@pytest.mark.parametrize("work_note_cls", [AgentWorkNoteRequest, BridgeWorkNoteRequest])
def test_work_note_request_missing_required_fields(work_note_cls):
    with pytest.raises(ValidationError) as exc:
        work_note_cls()  # missing sys_id and work_note
    field_names = [e["loc"][0] for e in exc.value.errors()]
    assert "sys_id" in field_names
    assert "work_note" in field_names


@pytest.mark.parametrize("update_cls", [AgentUpdateReportRequest, BridgeUpdateReportRequest])
def test_update_report_request_missing_required_fields(update_cls):
    with pytest.raises(ValidationError) as exc:
        update_cls()
    field_names = [e["loc"][0] for e in exc.value.errors()]
    assert "sys_id" in field_names
    assert "group_id" in field_names
    assert "report" in field_names


@pytest.mark.parametrize("resolve_cls", [AgentResolveTicketRequest, BridgeResolveTicketRequest])
def test_resolve_ticket_request_missing_required_fields(resolve_cls):
    with pytest.raises(ValidationError) as exc:
        resolve_cls()
    field_names = [e["loc"][0] for e in exc.value.errors()]
    assert "sys_id" in field_names
    assert "group_id" in field_names
    assert "close_notes" in field_names


@pytest.mark.parametrize("ticket_resp_cls", [AgentTicketResponse, BridgeTicketResponse])
def test_ticket_response_missing_required_fields(ticket_resp_cls):
    with pytest.raises(ValidationError) as exc:
        ticket_resp_cls(status="created")
    field_names = [e["loc"][0] for e in exc.value.errors()]
    assert "sys_id" in field_names
    assert "number" in field_names
    assert "url" in field_names
    assert "group_id" in field_names


@pytest.mark.parametrize("action_resp_cls", [AgentActionStatusResponse, BridgeActionStatusResponse])
def test_action_status_response_missing_status(action_resp_cls):
    with pytest.raises(ValidationError) as exc:
        action_resp_cls()
    field_names = [e["loc"][0] for e in exc.value.errors()]
    assert "status" in field_names


@pytest.mark.parametrize("timer_req_cls", [AgentTimerWakeRequest, BridgeTimerWakeRequest])
def test_timer_wake_request_validation(timer_req_cls):
    with pytest.raises(ValidationError) as exc:
        timer_req_cls()
    field_names = [e["loc"][0] for e in exc.value.errors()]
    assert "group_id" in field_names
    assert "deadline_type" in field_names
    assert "scheduled_epoch" in field_names

    with pytest.raises(ValidationError):
        timer_req_cls(
            group_id="grp_test",
            deadline_type="settle",
            scheduled_epoch="invalid_epoch_string",
        )


@pytest.mark.parametrize("timer_resp_cls", [AgentTimerWakeResponse, BridgeTimerWakeResponse])
def test_timer_wake_response_missing_fields(timer_resp_cls):
    with pytest.raises(ValidationError) as exc:
        timer_resp_cls()
    field_names = [e["loc"][0] for e in exc.value.errors()]
    assert "status" in field_names
    assert "group_id" in field_names


# ---------------------------------------------------------------------------
# 4. Schema Defaults Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("create_cls,report_fixture_name", [
    (AgentCreateTicketRequest, "sample_report"),
    (BridgeCreateTicketRequest, "sample_bridge_report"),
])
def test_create_ticket_request_defaults(create_cls, report_fixture_name, request):
    report = request.getfixturevalue(report_fixture_name)
    req = create_cls(group_id="grp_1", report=report)
    assert req.idempotency_key is None


@pytest.mark.parametrize("work_note_cls", [AgentWorkNoteRequest, BridgeWorkNoteRequest])
def test_work_note_request_defaults(work_note_cls):
    req = work_note_cls(sys_id="sys_1", work_note="sample note")
    assert req.group_id is None


@pytest.mark.parametrize("resolve_cls", [AgentResolveTicketRequest, BridgeResolveTicketRequest])
def test_resolve_ticket_request_defaults(resolve_cls):
    req = resolve_cls(sys_id="sys_1", group_id="grp_1", close_notes="Resolved permanently")
    assert req.close_code == "Solved (Permanently)"


@pytest.mark.parametrize("action_resp_cls", [AgentActionStatusResponse, BridgeActionStatusResponse])
def test_action_status_response_defaults(action_resp_cls):
    resp = action_resp_cls(status="success")
    assert resp.sys_id is None
    assert resp.group_id is None
    assert resp.message is None


@pytest.mark.parametrize("timer_resp_cls", [AgentTimerWakeResponse, BridgeTimerWakeResponse])
def test_timer_wake_response_defaults(timer_resp_cls):
    resp = timer_resp_cls(status="executed", group_id="grp_1")
    assert resp.reason is None


# ---------------------------------------------------------------------------
# 5. Terraform & IAM Configuration Verification Tests
# ---------------------------------------------------------------------------

def test_terraform_foundation_timer_queue_definition():
    """Verify google_cloud_tasks_queue.timer_queue is defined with correct rate limits."""
    main_tf = (REPO_ROOT / "infra/terraform/foundation/main.tf").read_text()

    assert 'resource "google_cloud_tasks_queue" "timer_queue"' in main_tf
    assert 'name     = "${var.name_prefix}-timer-queue"' in main_tf
    assert "location = var.region" in main_tf
    assert "max_concurrent_dispatches = 50" in main_tf
    assert "max_dispatches_per_second = 50" in main_tf


def test_terraform_foundation_agent_iam_roles():
    """Verify agent_sa is granted roles/cloudtasks.enqueuer and roles/iam.serviceAccountUser."""
    main_tf = (REPO_ROOT / "infra/terraform/foundation/main.tf").read_text()

    assert "roles/cloudtasks.enqueuer" in main_tf
    assert "roles/iam.serviceAccountUser" in main_tf

    # Verify agent_sa association
    assert "serviceAccount:${google_service_account.agent_sa.email}" in main_tf
    assert 'resource "google_project_iam_member" "agent_cloudtasks_enqueuer"' in main_tf
    assert 'resource "google_project_iam_member" "agent_sa_service_account_user"' in main_tf


def test_terraform_foundation_outputs():
    """Verify timer_queue_name and timer_queue_id outputs are exposed in outputs.tf."""
    outputs_tf = (REPO_ROOT / "infra/terraform/foundation/outputs.tf").read_text()

    assert 'output "timer_queue_name"' in outputs_tf
    assert "value       = google_cloud_tasks_queue.timer_queue.name" in outputs_tf

    assert 'output "timer_queue_id"' in outputs_tf
    assert "value       = google_cloud_tasks_queue.timer_queue.id" in outputs_tf


def test_terraform_applications_agent_invoker_roles():
    """Verify agent_sa is granted roles/run.invoker on ata-agent and ata-bridge."""
    apps_main_tf = (REPO_ROOT / "infra/terraform/applications/main.tf").read_text()

    assert 'data "google_service_account" "agent_sa"' in apps_main_tf
    assert 'account_id = "${var.name_prefix}-agent-sa"' in apps_main_tf

    # Invoker on ata-agent
    assert 'resource "google_cloud_run_v2_service_iam_member" "agent_invoker_agent"' in apps_main_tf
    assert 'name     = "${var.name_prefix}-agent"' in apps_main_tf

    # Invoker on ata-bridge
    assert 'resource "google_cloud_run_v2_service_iam_member" "bridge_invoker_agent"' in apps_main_tf
    assert 'name     = "${var.name_prefix}-bridge"' in apps_main_tf

    # Role and member verification
    assert "roles/run.invoker" in apps_main_tf
    assert "serviceAccount:${data.google_service_account.agent_sa.email}" in apps_main_tf


# ---------------------------------------------------------------------------
# 6. Container Import Layout Compatibility Tests
# ---------------------------------------------------------------------------

def test_contracts_import_in_agent_container_layout():
    """Verify new contracts import cleanly with PYTHONPATH set to agent root (/app)."""
    import subprocess
    import sys

    agent_dir = str(REPO_ROOT / "agent")
    code = f"""
import sys
sys.path = ['{agent_dir}'] + [p for p in sys.path if 'site-packages' in p or 'lib/python' in p]
import app.contracts.instructions as inst
import app.contracts.timers as timers
assert hasattr(inst, 'CreateTicketRequest')
assert hasattr(timers, 'TimerWakeRequest')
print('AGENT CONTAINER CONTRACTS IMPORTED')
"""
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, f"Failed agent container import: {res.stderr}"
    assert "AGENT CONTAINER CONTRACTS IMPORTED" in res.stdout


def test_contracts_import_in_bridge_container_layout():
    """Verify new contracts import cleanly with PYTHONPATH set to bridge root (/app)."""
    import subprocess
    import sys

    bridge_dir = str(REPO_ROOT / "bridge")
    code = f"""
import sys
sys.path = ['{bridge_dir}'] + [p for p in sys.path if 'site-packages' in p or 'lib/python' in p]
import app.contracts.instructions as inst
import app.contracts.timers as timers
assert hasattr(inst, 'CreateTicketRequest')
assert hasattr(timers, 'TimerWakeRequest')
print('BRIDGE CONTAINER CONTRACTS IMPORTED')
"""
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, f"Failed bridge container import: {res.stderr}"
    assert "BRIDGE CONTAINER CONTRACTS IMPORTED" in res.stdout

