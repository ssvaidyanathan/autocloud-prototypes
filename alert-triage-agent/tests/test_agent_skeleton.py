import yaml
import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.triage import (
    generate_investigation_report,
    write_preanalysis_lock,
)
from bridge.app.templates.renderer import render_standalone_html


@pytest.fixture
def test_alert():
    return AlertEvent(
        incident_id="0.12345678",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-backend-5xx",
        condition_name="Backend 5xx error rate high",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        resource_labels={"service_name": "ata-inventory-api"},
        state="open",
        started_at="2026-10-22T21:15:00Z",
    )


@pytest.fixture
def agent_config(tmp_path):
    store_dir = tmp_path / "knowledge" / "incident-manager"
    return AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        is_local=True,
    )


def test_preanalysis_lock_creation(test_alert, agent_config):
    file_path, group_id, filename = write_preanalysis_lock(test_alert, agent_config)

    assert file_path.exists()
    assert filename.startswith("incident-")
    assert filename.endswith(".md")
    assert "grp_" in group_id

    content = file_path.read_text(encoding="utf-8")
    assert content.startswith("---\n")

    # Split frontmatter
    parts = content.split("---")
    assert len(parts) >= 3
    frontmatter_yaml = parts[1]
    parsed_meta = yaml.safe_load(frontmatter_yaml)

    assert parsed_meta["type"] == "incident"
    assert parsed_meta["status"] == "mitigation-in-progress"
    assert parsed_meta["policy_name"] == test_alert.policy_name
    assert parsed_meta["resource_name"] == "ata-inventory-api"
    assert parsed_meta["origin"] == "ata-inventory-api"
    assert "ata-inventory-api" in parsed_meta["blast_radius"]
    assert len(parsed_meta["alerts"]) == 1
    assert parsed_meta["alerts"][0]["incident_id"] == "0.12345678"


def test_4_bullet_summary_generation(test_alert, agent_config):
    file_path, group_id, filename = write_preanalysis_lock(test_alert, agent_config)
    report = generate_investigation_report(test_alert, group_id, filename, agent_config)

    bullets = report.build_executive_summary_bullets()
    assert len(bullets) == 4
    assert bullets[0].startswith("* **Problem:**")
    assert bullets[1].startswith("* **Systems:** Origin: `ata-inventory-api`")
    assert bullets[2].startswith("* **Observations:**")
    assert bullets[3].startswith("* **Recommended Actions:**")


def test_standalone_html_report_rendering(test_alert, agent_config):
    _, group_id, filename = write_preanalysis_lock(test_alert, agent_config)
    report = generate_investigation_report(test_alert, group_id, filename, agent_config)

    html_out = render_standalone_html(report)
    assert "<!DOCTYPE html>" in html_out
    assert "<title>Alert Triage Diagnostic Report" in html_out
    assert "Executive Summary" in html_out
    assert "ata-inventory-api" in html_out
    assert "Probable Root Cause Analysis" in html_out
    assert "Technical Evidence Dossier" in html_out


def test_fastapi_endpoints(test_alert, agent_config, monkeypatch):
    """Test /health, /triage, and /investigate endpoints via FastAPI TestClient."""
    from fastapi.testclient import TestClient
    from agent.app.main import app
    from agent.app.config import get_agent_settings

    app.dependency_overrides[get_agent_settings] = lambda: agent_config

    client = TestClient(app)

    # 1. /health
    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json() == {"status": "ok"}

    # Mock bridge callback to avoid external HTTP errors
    import agent.app.triage
    monkeypatch.setattr(agent.app.triage, "send_findings_to_bridge", lambda rep, url: {"ok": True})
    import agent.app.main
    monkeypatch.setattr(agent.app.main, "send_findings_to_bridge", lambda rep, url: {"ok": True})
    from agent.app.timers.scheduler import set_timer_scheduler_hook
    set_timer_scheduler_hook(lambda **kwargs: "mock-task")

    # 2. /triage with initial alert
    from datetime import datetime, timezone
    recent_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    test_alert.started_at = recent_ts
    payload = test_alert.model_dump(mode="json")
    res_triage = client.post("/triage", json=payload)
    assert res_triage.status_code == 200
    data = res_triage.json()
    assert data["status"] == "triaged"
    assert "group_id" in data
    assert "correlation" in data

    # 3. /triage with duplicate alert (within 30m)
    res_dup = client.post("/triage", json=payload)
    assert res_dup.status_code == 200
    dup_data = res_dup.json()
    assert dup_data["status"] == "duplicate"
    assert dup_data["is_duplicate"] is True

    # 4. /investigate
    res_inv = client.post("/investigate", json=payload)
    assert res_inv.status_code == 200
    inv_data = res_inv.json()
    assert inv_data["status"] == "investigated"
    assert "report" in inv_data
    assert inv_data["report"]["origin"] in {"ata-inventory-api", "ata-inventory-db"}


def test_okf_file_update_preserves_metadata(test_alert, agent_config):
    """Verify that updating an existing OKF file merges alerts and preserves original group metadata."""
    from agent.app.specialists.synthesizer import SynthesizerAgent

    file_path, group_id, filename = write_preanalysis_lock(test_alert, agent_config)

    # Simulate ServiceNow enrichment on the existing file
    content = file_path.read_text(encoding="utf-8")
    parts = content.split("---")
    meta = yaml.safe_load(parts[1])
    meta["servicenow_sys_id"] = "sys_abc123"
    meta["servicenow_number"] = "INC009988"
    file_path.write_text(f"---\n{yaml.dump(meta, sort_keys=False).strip()}\n---{parts[2]}", encoding="utf-8")

    # Create a secondary correlated alert on a downstream resource
    second_alert = AlertEvent(
        incident_id="0.87654321",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-frontend-latency",
        condition_name="Latency breach",
        resource_name="ata-orders-api",
        resource_type="cloud_run_revision",
        resource_labels={"service_name": "ata-orders-api"},
        state="open",
        started_at="2026-10-22T21:18:00Z",
    )

    report = generate_investigation_report(second_alert, group_id, filename, agent_config)
    synthesizer = SynthesizerAgent()
    synthesizer._update_okf_file(report, second_alert, agent_config)

    # Read back and verify preserved metadata and merged alerts
    updated_content = file_path.read_text(encoding="utf-8")
    updated_parts = updated_content.split("---")
    updated_meta = yaml.safe_load(updated_parts[1])

    # Preserved original metadata
    assert updated_meta["timestamp"] == test_alert.started_at
    assert updated_meta["origin"] == "ata-inventory-api"
    assert updated_meta["servicenow_sys_id"] == "sys_abc123"
    assert updated_meta["servicenow_number"] == "INC009988"

    # Both alerts present
    assert len(updated_meta["alerts"]) == 2
    alert_ids = [a["incident_id"] for a in updated_meta["alerts"]]
    assert "0.12345678" in alert_ids
    assert "0.87654321" in alert_ids


@pytest.mark.asyncio
async def test_execute_multi_agent_investigation_no_fallback(test_alert, agent_config, monkeypatch):
    """Verify execute_multi_agent_investigation completes via SynthesizerAgent without fallback."""
    from agent.app.specialists.synthesizer import SynthesizerAgent
    import agent.app.main

    _, group_id, filename = write_preanalysis_lock(test_alert, agent_config)

    fallback_called = False
    orig_fallback = agent.app.main.generate_investigation_report

    def spy_fallback(*args, **kwargs):
        nonlocal fallback_called
        fallback_called = True
        return orig_fallback(*args, **kwargs)

    monkeypatch.setattr(agent.app.main, "generate_investigation_report", spy_fallback)

    synthesizer = SynthesizerAgent()
    report = await synthesizer.execute_investigation(test_alert, group_id, filename, agent_config)

    assert not fallback_called
    assert report.origin == "ata-inventory-api"
    assert "ata-orders-api" in report.blast_radius
    assert "ata-apigee-orders-v1" in report.blast_radius or "ata-orders-v1" in report.blast_radius
    assert not report.summary.startswith("degraded:")
    assert report.agent_run_metadata.get("duration_seconds") != "1.74s"


