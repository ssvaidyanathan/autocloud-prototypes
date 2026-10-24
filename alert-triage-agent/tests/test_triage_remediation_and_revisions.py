"""Unit tests for triage remediation branching by resource type, dynamic revisions,
and ruled-out alternatives based on checks actually performed.
"""

import os
from unittest.mock import patch
import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.report import EvidenceItem, InvestigationReport
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.synthesizer import SynthesizerAgent
from agent.app.triage import generate_investigation_report
from bridge.app.templates.renderer import render_standalone_html


@pytest.fixture
def agent_config():
    return AgentConfig(
        project_id="test-project",
        model_name="test-gemini-model",
        incident_store_path="/tmp/test-incidents",
    )


def test_triage_remediation_apigee_zero_gcloud_run_commands(agent_config):
    """Assert Apigee alert remediation generates zero gcloud run commands (R2)."""
    apigee_alert = AlertEvent(
        incident_id="0.apigee_test_001",
        policy_name="ata-apigee-orders-5xx",
        resource_name="ata-orders-v1",
        resource_type="apigee_proxy",
        resource_labels={"proxy_name": "ata-orders-v1"},
        state="open",
        started_at="2026-10-23T04:00:00Z",
    )

    report = generate_investigation_report(apigee_alert, "grp_apigee_001", "incident-apigee.md", agent_config)

    # Must have recommended actions
    assert len(report.recommended_actions) > 0

    commands = [act.command for act in report.recommended_actions if act.command]
    # ZERO gcloud run commands for Apigee proxy
    for cmd in commands:
        assert "gcloud run" not in cmd, f"Found illegal gcloud run command for Apigee proxy: {cmd}"

    # Verify Apigee proxy command and security policy command
    assert any("gcloud apigee apiproxies describe" in cmd for cmd in commands)
    assert any("gcloud compute security-policies rules create" in cmd for cmd in commands)
    assert any("<CLIENT_IP>/32" in cmd for cmd in commands)


def test_triage_remediation_cloud_sql_commands(agent_config):
    """Assert Cloud SQL alert remediation generates SQL and gcloud sql commands (R2)."""
    sql_alert = AlertEvent(
        incident_id="0.sql_test_001",
        policy_name="ata-cloudsql-connections-critical",
        resource_name="ata-inventory-db",
        resource_type="cloudsql_database",
        resource_labels={"database_id": "test-project:ata-inventory-db"},
        state="open",
        started_at="2026-10-23T04:00:00Z",
    )

    report = generate_investigation_report(sql_alert, "grp_sql_001", "incident-sql.md", agent_config)

    commands = [act.command for act in report.recommended_actions if act.command]
    assert any("SELECT pg_terminate_backend" in cmd for cmd in commands)
    assert any("gcloud sql instances patch" in cmd for cmd in commands)


def test_triage_remediation_cloud_run_commands(agent_config):
    """Assert Cloud Run alert remediation generates gcloud run and logging commands (R2)."""
    run_alert = AlertEvent(
        incident_id="0.run_test_001",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        resource_labels={"service_name": "ata-inventory-api"},
        state="open",
        started_at="2026-10-23T04:00:00Z",
    )

    report = generate_investigation_report(run_alert, "grp_run_001", "incident-run.md", agent_config)

    commands = [act.command for act in report.recommended_actions if act.command]
    assert any("resource.type=\"cloud_run_revision\"" in cmd for cmd in commands)
    assert any("gcloud run services describe" in cmd for cmd in commands)


def test_dynamic_revisions_and_duration_defaults_to_unspecified(agent_config):
    """Assert revisions and metadata default to 'Unspecified' when env vars are unset (R3)."""
    with patch.dict(os.environ, {}, clear=True):
        alert = AlertEvent(
            incident_id="0.meta_test_001",
            policy_name="ata-inventory-5xx",
            resource_name="ata-inventory-api",
            state="open",
            started_at="2026-10-23T04:00:00Z",
        )
        report = generate_investigation_report(alert, "grp_meta_001", "incident-meta.md", agent_config)

        # Revisions must default to Unspecified
        assert report.agent_run_metadata.get("agent_revision") == "Unspecified"
        assert report.agent_run_metadata.get("bridge_revision") == "Unspecified"
        assert report.agent_run_metadata.get("model") == "test-gemini-model"

        # Duration is measured with monotonic clock and formatted like '0.00s'
        duration_str = report.agent_run_metadata.get("duration_seconds", "")
        assert duration_str.endswith("s")
        assert duration_str != "1.65s"
        assert duration_str != "1.74s"

        # Synthesizer direct builder check
        synthesizer = SynthesizerAgent()
        meta = synthesizer._build_agent_metadata(agent_config)
        assert meta["agent_revision"] == "Unspecified"
        assert meta["bridge_revision"] == "Unspecified"
        assert meta["duration_seconds"] == "Unspecified"

        # Renderer fallback check
        html = render_standalone_html(report)
        assert "ata-agent-00007-zzw" not in html
        assert "ata-bridge-00020-p22" not in html
        assert "1.74s" not in html


def test_dynamic_revisions_from_env_variables(agent_config):
    """Assert revisions are dynamically read from K_REVISION and BRIDGE_REVISION (R3)."""
    env_vars = {
        "K_REVISION": "ata-agent-custom-rev",
        "BRIDGE_REVISION": "ata-bridge-custom-rev",
    }
    with patch.dict(os.environ, env_vars, clear=True):
        alert = AlertEvent(
            incident_id="0.meta_test_002",
            policy_name="ata-inventory-5xx",
            resource_name="ata-inventory-api",
            state="open",
            started_at="2026-10-23T04:00:00Z",
        )
        report = generate_investigation_report(alert, "grp_meta_002", "incident-meta.md", agent_config)

        assert report.agent_run_metadata.get("agent_revision") == "ata-agent-custom-rev"
        assert report.agent_run_metadata.get("bridge_revision") == "ata-bridge-custom-rev"

        html = render_standalone_html(report)
        assert "ata-agent-custom-rev" in html
        assert "ata-bridge-custom-rev" in html


def test_ruled_out_alternatives_only_from_performed_checks(agent_config):
    """Assert alternatives_ruled_out in synthesizer only cites checks actually performed (N2)."""
    synthesizer = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="0.synth_test_001",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        state="open",
        started_at="2026-10-23T04:00:00Z",
    )

    # Scenario: Cloud SQL checked and nominal (active_connections = 12)
    sql_dossier = EvidenceDossier(
        specialist_name="CloudSQLSpecialist",
        target_resource="ata-inventory-db",
        queries_executed={"cloud_monitoring": ["fetch cloudsql_database"]},
        metrics={"active_connections": 12},
        anomaly_detected=False,
    )

    # Cloud Run checked with anomaly
    cr_dossier = EvidenceDossier(
        specialist_name="CloudRunSpecialist",
        target_resource="ata-inventory-api",
        queries_executed={"cloud_monitoring": ["fetch cloud_run_revision"]},
        metrics={"container_restarts": 0},
        anomaly_detected=True,
    )

    dossiers = [sql_dossier, cr_dossier]
    rca = synthesizer._build_root_cause_analysis(
        root_cause_text="Test hypothesis",
        confidence=0.91,
        dossiers=dossiers,
        origin="ata-inventory-api",
        alert=alert,
    )

    alts = rca.get("alternatives_ruled_out", [])
    assert len(alts) > 0

    # Every alternative must be tied to a check performed
    for alt in alts:
        reason = alt["reason"]
        # Must not contain fabricated static values
        assert "under 2ms" not in reason
        assert "steady at 28%" not in reason

    # Must include database connection check citing real queried value
    db_alt = next((a for a in alts if "Database" in a["alternative"]), None)
    assert db_alt is not None
    assert "12 active connections" in db_alt["reason"]

    # Must include container crash loop check citing 0 restarts
    crash_alt = next((a for a in alts if "Crash" in a["alternative"]), None)
    assert crash_alt is not None
    assert "0 restarts" in crash_alt["reason"]

    # VPC latency was NOT queried, so VPC egress partition must NOT be ruled out
    vpc_alt = next((a for a in alts if "VPC" in a["alternative"]), None)
    assert vpc_alt is None
