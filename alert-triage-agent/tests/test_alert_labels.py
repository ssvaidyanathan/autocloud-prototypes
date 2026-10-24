"""Unit tests for Alert Triage Agent (ATA) label scheme, alert policy user_labels,
and AlertEvent metadata ingestion.
"""

from pathlib import Path
import re
import pytest

from agent.app.contracts.alert import AlertEvent


# ==============================================================================
# 1. Alert Ingestion & Label Parsing Tests
# ==============================================================================

def test_alert_event_extracts_component_from_policy_user_labels():
    """Verify component is extracted from incident.policy_user_labels['component']."""
    payload = {
        "incident": {
            "incident_id": "inc-001",
            "policy_name": "ata-inventory-5xx",
            "state": "open",
            "started_at": 1700000000,
            "policy_user_labels": {
                "app": "alert-triage-agent",
                "component": "ata-inventory-api",
                "tier": "service",
                "signal": "5xx",
                "severity": "p1",
            },
            "resource": {
                "type": "cloud_run_revision",
                "labels": {
                    "service_name": "some-arbitrary-service",
                },
            },
        }
    }

    event = AlertEvent.from_monitoring_payload(payload)
    assert event.resource_name == "ata-inventory-api"
    assert event.policy_user_labels["component"] == "ata-inventory-api"
    assert event.policy_user_labels["signal"] == "5xx"
    assert event.labels["component"] == "ata-inventory-api"


def test_alert_event_component_label_takes_precedence_over_resource_labels():
    """Verify component in policy_user_labels overrides any resource-level labels."""
    payload = {
        "incident": {
            "incident_id": "inc-002",
            "policy_name": "ata-orders-latency",
            "state": "open",
            "started_at": 1700000000,
            "policy_user_labels": {
                "component": "ata-orders-api",
                "signal": "latency",
            },
            "resource": {
                "type": "cloud_run_revision",
                "labels": {
                    "service_name": "ata-orders-api-different-name",
                },
            },
        }
    }

    event = AlertEvent.from_monitoring_payload(payload)
    assert event.resource_name == "ata-orders-api"


def test_alert_event_falls_back_to_resource_canonicalization_without_component():
    """Verify fallback to resource label canonicalization when policy_user_labels lacks component."""
    payload = {
        "incident": {
            "incident_id": "inc-003",
            "policy_name": "ata-inventory-5xx",
            "state": "open",
            "started_at": 1700000000,
            "policy_user_labels": {
                "app": "alert-triage-agent",
                "tier": "service",
                "signal": "5xx",
            },
            "resource": {
                "type": "cloud_run_revision",
                "labels": {
                    "service_name": "ata-inventory-api",
                },
            },
        }
    }

    event = AlertEvent.from_monitoring_payload(payload)
    assert event.resource_name == "ata-inventory-api"
    assert "component" not in event.policy_user_labels


def test_alert_event_warns_and_falls_back_on_unrecognized_component(caplog):
    """Verify that an unrecognized component name logs a WARNING and falls back to resource canonicalization (never rejects)."""
    import logging
    payload = {
        "incident": {
            "incident_id": "inc-004",
            "policy_name": "ata-unknown-policy",
            "state": "open",
            "started_at": 1700000000,
            "policy_user_labels": {
                "component": "completely-invalid-nonexistent-component",
            },
            "resource": {
                "type": "cloud_run_revision",
                "labels": {
                    "service_name": "ata-inventory-api",
                },
            },
        }
    }

    with caplog.at_level(logging.WARNING):
        event = AlertEvent.from_monitoring_payload(payload)

    assert event.resource_name == "ata-inventory-api"
    assert event.policy_user_labels["component"] == "completely-invalid-nonexistent-component"
    assert any("Unknown component 'completely-invalid-nonexistent-component'" in r.message for r in caplog.records)


def test_direct_alert_event_instantiation_warns_and_falls_back_on_unrecognized_component(caplog):
    """Verify direct AlertEvent model instantiation also warns and falls back without rejecting."""
    import logging
    with caplog.at_level(logging.WARNING):
        event = AlertEvent(
            incident_id="inc-005",
            policy_name="ata-test",
            resource_name="ata-orders-api",
            policy_user_labels={"component": "bogus-component"},
        )

    assert event.resource_name == "ata-orders-api"
    assert event.policy_user_labels["component"] == "bogus-component"
    assert any("Unknown component 'bogus-component'" in r.message for r in caplog.records)


# ==============================================================================
# 2. Terraform Alert Policy User Labels Verification
# ==============================================================================

def test_terraform_alert_policies_declare_required_user_labels():
    """Verify that infra/terraform/observability/main.tf configures all 8 alert policies

    with required user_labels (app, component, tier, signal, severity).
    """
    tf_file = Path(__file__).resolve().parent.parent / "infra" / "terraform" / "observability" / "main.tf"
    assert tf_file.is_file(), f"Terraform observability main.tf not found at {tf_file}"

    content = tf_file.read_text(encoding="utf-8")

    # Match all google_monitoring_alert_policy blocks
    policy_pattern = re.compile(
        r'resource\s+"google_monitoring_alert_policy"\s+"([^"]+)"\s*\{(.*?)(?=\nresource\s+"google_monitoring_alert_policy"|\Z)',
        re.DOTALL,
    )

    policies = policy_pattern.findall(content)
    assert len(policies) == 8, f"Expected 8 alert policies in main.tf, found {len(policies)}"

    required_keys = {"app", "component", "tier", "signal", "severity"}

    for name, block in policies:
        # Check user_labels block (handle closing brace preceded by newline and spaces)
        user_labels_match = re.search(r'user_labels\s*=\s*\{(.*?)\n\s*\}', block, re.DOTALL)
        assert user_labels_match is not None, f"Policy '{name}' missing user_labels block"

        labels_block = user_labels_match.group(1)
        found_keys = set()
        for line in labels_block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip().strip('"')
                v = v.strip().strip('"')
                found_keys.add(k)
                # Resolve Terraform variable ${var.name_prefix} to ata
                v_resolved = v.replace("${var.name_prefix}", "ata")
                # Verify lowercase GCP label value format
                assert re.match(r'^[a-z0-9_-]{1,63}$', v_resolved), f"Label value '{v}' for '{k}' in '{name}' violates GCP label format"

        missing = required_keys - found_keys
        assert not missing, f"Policy '{name}' is missing required user_labels: {missing}"


# ==============================================================================
# 3. Policy ID Extraction Tests
# ==============================================================================

def test_extract_numeric_policy_id_from_resource_paths_and_fields():
    """Verify numeric policy_id extraction from diverse Cloud Monitoring schema locations."""
    from bridge.app.contracts.alert import extract_numeric_policy_id as bridge_extract
    from agent.app.contracts.alert import extract_numeric_policy_id as agent_extract

    for extract in (bridge_extract, agent_extract):
        # 1. Full GCP resource path in policy_name
        p1 = {"incident": {"policy_name": "projects/my-gcp-project/alertPolicies/1234567890123"}}
        assert extract(p1) == "1234567890123"

        # 2. Condition name containing policy path
        p2 = {"incident": {"policy_name": "High Error Rate", "condition_name": "projects/proj-1/alertPolicies/9876543210/conditions/c1"}}
        assert extract(p2) == "9876543210"

        # 3. Explicit top-level or incident-level policy_id
        p3 = {"incident": {"policy_name": "High Latency", "policy_id": 5544332211}}
        assert extract(p3) == "5544332211"

        p3_str = {"policy_id": "4433221100"}
        assert extract(p3_str) == "4433221100"

        # 4. Nested policy dict with id
        p4 = {"incident": {"policy": {"id": 11223344, "name": "CPU Saturation"}}}
        assert extract(p4) == "11223344"

        # 5. Fallback to None when policy_id is absent or unparseable
        p5 = {"incident": {"policy_name": "Simple Name Without Numeric ID"}}
        assert extract(p5) is None

        p6 = {}
        assert extract(p6) is None


# ==============================================================================
# 4. Component Source Logging Tests (label vs canonicalization)
# ==============================================================================

def test_agent_logs_component_source_from_label(caplog):
    """Verify agent logs that component was mapped from policy_user_labels['component']."""
    import logging
    payload = {
        "incident": {
            "incident_id": "0.logtest01",
            "policy_name": "ata-inventory-5xx",
            "state": "open",
            "policy_user_labels": {
                "component": "ata-inventory-api",
                "signal": "5xx",
                "tier": "service",
            },
            "resource": {
                "type": "cloud_run_revision",
                "labels": {"service_name": "some-random-res"},
            },
        }
    }
    with caplog.at_level(logging.INFO):
        event = AlertEvent.from_monitoring_payload(payload)

    assert event.component == "ata-inventory-api"
    assert any(
        "Mapped alert 0.logtest01 to component 'ata-inventory-api' from policy_user_labels['component']" in r.message
        for r in caplog.records
    )


def test_agent_logs_component_source_from_canonicalization(caplog):
    """Verify agent logs that component was mapped from resource-label canonicalization when label is absent."""
    import logging
    payload = {
        "incident": {
            "incident_id": "0.logtest02",
            "policy_name": "ata-inventory-5xx",
            "state": "open",
            "policy_user_labels": {
                "signal": "5xx",
                "tier": "service",
            },
            "resource": {
                "type": "cloud_run_revision",
                "labels": {"service_name": "ata-inventory-api"},
            },
        }
    }
    with caplog.at_level(logging.INFO):
        event = AlertEvent.from_monitoring_payload(payload)

    assert event.component == "ata-inventory-api"
    assert any(
        "Mapped alert 0.logtest02 to component 'ata-inventory-api' from resource-label canonicalization" in r.message
        for r in caplog.records
    )


# ==============================================================================
# 5. End-to-End Payload -> OKF Alert Entry Propagation Tests
# ==============================================================================

def test_end_to_end_payload_to_okf_entry_preserves_labels_and_policy_id(tmp_path):
    """End-to-end verification: Monitoring payload with policy_user_labels & policy_id

    flows through bridge -> agent AlertEvent -> format_okf_alert_entry / pre-analysis lock
    -> OKF frontmatter with full labels dictionary, component, signal, and policy_id.
    """
    import yaml
    from bridge.app.contracts.alert import AlertEvent as BridgeAlertEvent
    from agent.app.config import AgentConfig
    from agent.app.grouping.models import format_okf_alert_entry, OKFDocument
    from agent.app.triage import write_preanalysis_lock

    monitoring_payload = {
        "incident": {
            "incident_id": "0.e2e12345",
            "policy_name": "projects/cloud-assist-fde-2/alertPolicies/876543210987",
            "state": "open",
            "started_at": 1710000000,
            "url": "https://console.cloud.google.com/monitoring/alerting/incidents/0.e2e12345?project=cloud-assist-fde-2",
            "policy_user_labels": {
                "app": "alert-triage-agent",
                "component": "ata-inventory-api",
                "tier": "service",
                "signal": "5xx",
                "severity": "p1",
            },
            "resource": {
                "type": "cloud_run_revision",
                "labels": {
                    "service_name": "ata-inventory-api",
                    "location": "us-central1",
                },
            },
        }
    }

    # 1. Bridge extracts AlertEvent
    bridge_event = BridgeAlertEvent.from_monitoring_payload(monitoring_payload)
    assert bridge_event.policy_id == "876543210987"
    assert bridge_event.component == "ata-inventory-api"
    assert bridge_event.signal == "5xx"
    assert bridge_event.labels == {
        "app": "alert-triage-agent",
        "component": "ata-inventory-api",
        "tier": "service",
        "signal": "5xx",
        "severity": "p1",
    }

    # Bridge serializes to Cloud Tasks payload
    task_payload = bridge_event.model_dump()
    assert task_payload["labels"]["component"] == "ata-inventory-api"
    assert task_payload["policy_id"] == "876543210987"

    # 2. Agent receives and constructs agent AlertEvent
    agent_event = AlertEvent.model_validate(task_payload)
    assert agent_event.policy_id == "876543210987"
    assert agent_event.component == "ata-inventory-api"
    assert agent_event.signal == "5xx"
    assert agent_event.labels["signal"] == "5xx"

    # 3. OKF entry formatting
    entry = format_okf_alert_entry(agent_event)
    assert entry["incident_id"] == "0.e2e12345"
    assert entry["policy_id"] == "876543210987"
    assert entry["component"] == "ata-inventory-api"
    assert entry["signal"] == "5xx"
    assert entry["labels"] == {
        "app": "alert-triage-agent",
        "component": "ata-inventory-api",
        "tier": "service",
        "signal": "5xx",
        "severity": "p1",
    }

    # 4. OKFDocument model validator normalizes alerts
    doc = OKFDocument(
        group_id="grp_test_labels",
        alerts=[entry],
    )
    doc_entry = doc.alerts[0]
    assert doc_entry["component"] == "ata-inventory-api"
    assert doc_entry["signal"] == "5xx"
    assert doc_entry["policy_id"] == "876543210987"
    assert doc_entry["labels"]["severity"] == "p1"

    # 5. write_preanalysis_lock persists OKF markdown with complete labels
    config = AgentConfig(
        incident_store_path=str(tmp_path),
        project_id="cloud-assist-fde-2",
        is_local=True,
    )
    file_path, group_id, filename = write_preanalysis_lock(agent_event, config)
    assert file_path.is_file()

    file_content = file_path.read_text(encoding="utf-8")
    meta = yaml.safe_load(file_content.split("---")[1])
    saved_alerts = meta.get("alerts", [])
    assert len(saved_alerts) == 1
    saved_alert = saved_alerts[0]
    assert saved_alert["component"] == "ata-inventory-api"
    assert saved_alert["signal"] == "5xx"
    assert saved_alert["policy_id"] == "876543210987"
    assert saved_alert["labels"]["component"] == "ata-inventory-api"
    assert saved_alert["labels"]["signal"] == "5xx"
    assert saved_alert["labels"]["tier"] == "service"


# ==============================================================================
# 6. HTML Report Table and Work Notes Display Component and Signal
# ==============================================================================

def test_html_report_table_displays_component_and_signal():
    """Verify HTML report includes Component and Signal column headers and data cells."""
    from bridge.app.templates.renderer import render_standalone_html
    from agent.app.contracts.report import InvestigationReport, ProbableRootCause

    report = InvestigationReport(
        group_id="grp_table_labels",
        filename="incident-test.md",
        summary="5xx spike on inventory service",
        observations="Error rate breached 5% threshold",
        probable_root_cause=ProbableRootCause(text="Inventory DB pool leak", confidence=0.92),
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api"],
        priority="P1",
        project_id="cloud-assist-fde-2",
        alerts=[
            {
                "incident_id": "0.rep123",
                "policy_name": "ata-inventory-5xx",
                "policy_id": "123456789",
                "resource_name": "ata-inventory-api",
                "component": "ata-inventory-api",
                "signal": "5xx",
                "state": "open",
                "labels": {"component": "ata-inventory-api", "signal": "5xx", "tier": "service"},
            }
        ],
    )

    html = render_standalone_html(report)
    assert '<th style="padding: 8px 12px; border: 1px solid #d0d7de;">Component</th>' in html
    assert '<th style="padding: 8px 12px; border: 1px solid #d0d7de;">Signal</th>' in html
    assert "<code>ata-inventory-api</code>" in html
    assert "<code>5xx</code>" in html


def test_work_notes_display_component_and_signal():
    """Verify late alert work notes and executive summary work notes display Component and Signal."""
    from agent.app.grouping.suppression import format_late_alert_work_note
    from agent.app.contracts.report import InvestigationReport, ProbableRootCause

    alert = AlertEvent(
        incident_id="0.wn123",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        policy_id="88776655",
        policy_user_labels={"component": "ata-inventory-api", "signal": "5xx"},
    )

    # 1. Late alert work note
    note_html = format_late_alert_work_note(alert, blast_radius=["ata-inventory-api"], project_id="cloud-assist-fde-2", as_html=True)
    assert "Component: <code>ata-inventory-api</code>" in note_html
    assert "Signal: <code>5xx</code>" in note_html

    note_plain = format_late_alert_work_note(alert, blast_radius=["ata-inventory-api"], project_id="cloud-assist-fde-2", as_html=False)
    assert "Component: ata-inventory-api" in note_plain
    assert "Signal: 5xx" in note_plain

    # 2. Executive summary HTML work note
    report = InvestigationReport(
        group_id="grp_exec_summary",
        filename="incident-exec.md",
        summary="5xx spike on inventory service",
        observations="Error rate breached threshold",
        probable_root_cause=ProbableRootCause(text="DB pool leak", confidence=0.92),
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api"],
        priority="P1",
        alerts=[
            {
                "incident_id": "0.wn123",
                "policy_name": "ata-inventory-5xx",
                "component": "ata-inventory-api",
                "signal": "5xx",
            }
        ],
    )
    exec_note = report.build_executive_summary_html()
    assert "Component: <code>ata-inventory-api</code>" in exec_note
    assert "Signal: <code>5xx</code>" in exec_note

