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
