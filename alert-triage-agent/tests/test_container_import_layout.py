"""Unit tests verifying container layout imports.

Simulates the runtime environment of Cloud Run containers where PYTHONPATH is set
to the container root (/app for agent, bridge, and tools). Asserts that all modules
can be imported without beyond-top-level relative imports, NameErrors, or silent failures.
"""

import importlib
from pathlib import Path
import subprocess
import sys
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_agent_container_import_layout():
    """Verify all agent modules import successfully under container layout (PYTHONPATH=/app)."""
    agent_dir = str(REPO_ROOT / "agent")
    cmd = [
        sys.executable,
        "-c",
        """
import importlib
import pkgutil
import sys

# Simulate Cloud Run container environment: PYTHONPATH=/app
sys.path = ['""" + agent_dir + """', '""" + str(REPO_ROOT) + """'] + [p for p in sys.path if 'site-packages' in p or 'lib/python' in p]

# Modules to verify in agent container layout
modules_to_test = [
    'topology.topology_graph',
    'app.config',
    'app.contracts.alert',
    'app.contracts.report',
    'app.contracts.links',
    'app.tools.shell',
    'app.duplicate_checker',
    'app.correlation.scoring',
    'app.correlation.engine',
    'app.specialists.base',
    'app.specialists.cloud_run',
    'app.specialists.apigee',
    'app.specialists.cloud_sql',
    'app.specialists.change',
    'app.specialists.synthesizer',
    'app.triage',
    'app.timers.scheduler',
    'app.grouping.models',
    'app.grouping.store',
    'app.grouping.suppression',
    'app.grouping.manager',
    'app.bridge_client',
    'app.main',
]

# Explicit verification that google.cloud.tasks_v2 imports successfully
import google.cloud.tasks_v2
assert google.cloud.tasks_v2 is not None

for mod in modules_to_test:
    try:
        importlib.import_module(mod)
    except Exception as exc:
        print(f"FAILED TO IMPORT {mod}: {exc}", file=sys.stderr)
        sys.exit(1)

# Verify get_blast_radius expands properly inside container
from topology.topology_graph import get_blast_radius, _canonicalize_node
assert _canonicalize_node('orders-v1') == 'ata-orders-v1'
assert _canonicalize_node('ata-apigee-orders-v1') == 'ata-orders-v1'
blast = get_blast_radius('ata-inventory-api')
assert len(blast) >= 3
assert 'ata-orders-api' in blast

print("ALL AGENT CONTAINER MODULES IMPORTED CLEANLY")
        """,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"Container import check failed: {proc.stderr}\n{proc.stdout}"
    assert "ALL AGENT CONTAINER MODULES IMPORTED CLEANLY" in proc.stdout


def test_bridge_container_import_layout():
    """Verify all bridge modules import successfully under container layout."""
    bridge_dir = str(REPO_ROOT / "bridge")
    cmd = [
        sys.executable,
        "-c",
        """
import importlib
import sys

sys.path = ['""" + bridge_dir + """'] + [p for p in sys.path if 'site-packages' in p or 'lib/python' in p]

modules_to_test = [
    'app.config',
    'app.contracts.alert',
    'app.contracts.report',
    'app.contracts.links',
    'app.adapters.servicenow',
    'app.templates.renderer',
    'app.core.dispatcher',
    'app.core.windowing',
    'app.main',
]

for mod in modules_to_test:
    try:
        importlib.import_module(mod)
    except Exception as exc:
        print(f"FAILED TO IMPORT {mod}: {exc}", file=sys.stderr)
        sys.exit(1)

# Verify topology package is NOT present or needed in bridge container
try:
    importlib.import_module('topology')
    print("FAILED: topology package should not be importable in bridge container", file=sys.stderr)
    sys.exit(1)
except ImportError:
    pass

# Verify parsing a real alert with component=ata-inventory-api succeeds in bridge without topology
alert_mod = importlib.import_module('app.contracts.alert')
real_payload = {
    'incident': {
        'incident_id': '0.bridge_lean_check_001',
        'policy_name': 'projects/cloud-assist-fde-2/alertPolicies/ata-inventory-5xx',
        'resource_name': 'ata-inventory-api',
        'state': 'open',
        'started_at': 1729631700,
        'policy_user_labels': {
            'app': 'alert-triage-agent',
            'component': 'ata-inventory-api',
            'tier': 'service',
            'signal': '5xx',
            'severity': 'p1',
        },
        'resource': {
            'type': 'cloud_run_revision',
            'labels': {'service_name': 'ata-inventory-api'},
        },
    }
}
event = alert_mod.AlertEvent.from_monitoring_payload(real_payload)
assert event.resource_name == 'ata-inventory-api', f"Expected ata-inventory-api, got {event.resource_name}"
assert event.policy_user_labels.get('component') == 'ata-inventory-api'

print("ALL BRIDGE CONTAINER MODULES IMPORTED CLEANLY")
        """,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"Bridge container import check failed: {proc.stderr}\n{proc.stdout}"
    assert "ALL BRIDGE CONTAINER MODULES IMPORTED CLEANLY" in proc.stdout


def test_agent_fails_loudly_without_topology():
    """Assert that agent alert contracts fail loudly with ImportError at startup when topology is missing."""
    agent_dir = str(REPO_ROOT / "agent")
    cmd = [
        sys.executable,
        "-c",
        """
import sys
# Exclude REPO_ROOT so topology package cannot be resolved
sys.path = ['""" + agent_dir + """'] + [p for p in sys.path if 'site-packages' in p or 'lib/python' in p]

try:
    import app.contracts.alert
    print("UNEXPECTED: agent contracts should have failed loudly without topology loader", file=sys.stderr)
    sys.exit(1)
except ImportError:
    print("AGENT FAILED LOUDLY WITHOUT TOPOLOGY LOADER AS EXPECTED")
    sys.exit(0)
        """,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"Agent did not fail loudly without topology: {proc.stderr}\n{proc.stdout}"
    assert "AGENT FAILED LOUDLY WITHOUT TOPOLOGY LOADER AS EXPECTED" in proc.stdout


def test_tools_fails_loudly_without_topology():
    """Assert that tools alert contracts fail loudly with ImportError at startup when topology is missing."""
    tools_dir = str(REPO_ROOT / "tools")
    cmd = [
        sys.executable,
        "-c",
        """
import sys
# Exclude REPO_ROOT so topology package cannot be resolved
sys.path = ['""" + tools_dir + """'] + [p for p in sys.path if 'site-packages' in p or 'lib/python' in p]

try:
    import app.contracts.alert
    print("UNEXPECTED: tools contracts should have failed loudly without topology loader", file=sys.stderr)
    sys.exit(1)
except ImportError:
    print("TOOLS FAILED LOUDLY WITHOUT TOPOLOGY LOADER AS EXPECTED")
    sys.exit(0)
        """,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"Tools did not fail loudly without topology: {proc.stderr}\n{proc.stdout}"
    assert "TOOLS FAILED LOUDLY WITHOUT TOPOLOGY LOADER AS EXPECTED" in proc.stdout


def test_tools_container_import_layout():
    """Verify all tools modules import successfully under container layout."""
    tools_dir = str(REPO_ROOT / "tools")
    cmd = [
        sys.executable,
        "-c",
        """
import importlib
import sys

sys.path = ['""" + tools_dir + """', '""" + str(REPO_ROOT) + """'] + [p for p in sys.path if 'site-packages' in p or 'lib/python' in p]

modules_to_test = [
    'app.config',
    'app.contracts.alert',
    'app.bundle_parser',
    'topology.topology_graph',
    'app.server',
    'app.main',
]

for mod in modules_to_test:
    try:
        importlib.import_module(mod)
    except Exception as exc:
        print(f"FAILED TO IMPORT {mod}: {exc}", file=sys.stderr)
        sys.exit(1)

print("ALL TOOLS CONTAINER MODULES IMPORTED CLEANLY")
        """,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"Tools container import check failed: {proc.stderr}\n{proc.stdout}"
    assert "ALL TOOLS CONTAINER MODULES IMPORTED CLEANLY" in proc.stdout


def test_fallback_path_marks_report_as_degraded():
    """Assert that fallback triage path explicitly labels output as degraded."""
    from agent.app.config import AgentConfig
    from agent.app.contracts.alert import AlertEvent
    from agent.app.triage import generate_investigation_report

    cfg = AgentConfig(project_id="cloud-assist-fde-2", is_local=True)
    alert = AlertEvent(
        incident_id="0.degraded_test",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        state="open",
        started_at="2026-10-23T04:00:00Z",
    )

    report = generate_investigation_report(
        alert=alert,
        group_id="grp_degraded_test",
        filename="incident-test.md",
        config=cfg,
        degraded_reason="multi-agent specialist investigation failed: NameError('re')",
    )

    assert "degraded: multi-agent specialist investigation failed" in report.summary
    assert "DEGRADED MODE" in report.observations


def test_agent_pyproject_dependencies_declared_and_importable():
    """Assert agent/pyproject.toml declares google-cloud-tasks and it is importable."""
    pyproject_path = REPO_ROOT / "agent" / "pyproject.toml"
    assert pyproject_path.exists(), "agent/pyproject.toml must exist"

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    deps = data.get("project", {}).get("dependencies", [])
    has_tasks_dep = any("google-cloud-tasks" in dep for dep in deps)
    assert has_tasks_dep, "google-cloud-tasks must be declared in agent/pyproject.toml dependencies"

    # Assert google.cloud.tasks_v2 is importable
    tasks_mod = importlib.import_module("google.cloud.tasks_v2")
    assert tasks_mod is not None


def test_schedule_delayed_timer_raises_timer_schedule_error_on_failure(caplog):
    """Assert schedule_delayed_timer logs error and raises TimerScheduleError when create_task fails."""
    import logging
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch
    from agent.app.timers.scheduler import (
        TimerScheduleError,
        schedule_delayed_timer,
        set_timer_scheduler_hook,
    )

    set_timer_scheduler_hook(None)
    mock_client = MagicMock()
    mock_client.create_task.side_effect = RuntimeError("Simulated GCP error")

    with patch("google.cloud.tasks_v2.CloudTasksClient", return_value=mock_client):
        with caplog.at_level(logging.ERROR):
            with pytest.raises(TimerScheduleError) as exc_info:
                schedule_delayed_timer(
                    project_id="cloud-assist-fde-2",
                    location="us-west1",
                    queue_name="ata-timer-queue",
                    target_url="http://agent:8000",
                    group_id="grp-fail-test",
                    deadline_type="settle",
                    deadline_dt=datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc),
                    service_account_email="sa@proj.iam.gserviceaccount.com",
                )

            assert "Failed to schedule Cloud Tasks timer for group grp-fail-test (settle)" in str(exc_info.value)
            assert "Simulated GCP error" in str(exc_info.value)
            assert any(
                "Failed to schedule Cloud Tasks timer for group grp-fail-test (settle)" in r.message
                for r in caplog.records
                if r.levelno >= logging.ERROR
            )


def test_grouping_manager_marks_group_degraded_on_timer_failure(tmp_path):
    """Assert GroupingManager marks group as degraded in OKF when schedule_delayed_timer fails."""
    from unittest.mock import patch
    from agent.app.config import AgentConfig
    from agent.app.contracts.alert import AlertEvent
    from agent.app.grouping.manager import GroupingManager
    from agent.app.grouping.models import IncidentGroupStatus
    from agent.app.grouping.store import OKFStore
    from agent.app.timers.scheduler import TimerScheduleError

    store_dir = tmp_path / "knowledge" / "incident-manager"
    store_dir.mkdir(parents=True, exist_ok=True)
    cfg = AgentConfig(
        project_id="cloud-assist-fde-2",
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        is_local=True,
    )
    store = OKFStore(config=cfg)
    manager = GroupingManager(config=cfg, store=store)

    alert = AlertEvent(
        incident_id="0.fail-timer-1",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-inventory-5xx",
        resource_name="ata-inventory-api",
        state="open",
        started_at="2026-10-24T05:00:00Z",
    )

    with patch("agent.app.grouping.manager.schedule_delayed_timer", side_effect=TimerScheduleError("Cloud Tasks quota exceeded")):
        with pytest.raises(TimerScheduleError):
            manager.ingest_alert(alert)

    # Check store - group should have been written and marked degraded
    docs = store.list_documents()
    assert len(docs) == 1
    doc = docs[0]
    assert doc.status == IncidentGroupStatus.DEGRADED.value
    assert doc.degraded is True
    assert doc.degraded_reason is not None
    assert "Cloud Tasks quota exceeded" in doc.degraded_reason

