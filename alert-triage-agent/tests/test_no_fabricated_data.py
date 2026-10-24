"""Test asserting that non-test production code contains no fabricated mock data,
placeholder service names, or hardcoded candidate alerts.
"""

import ast
import os
import re
import pytest

PROHIBITED_FABRICATED_STRINGS = [
    "ata-billing-worker",
    "Billing Anomaly Alert",
    "ata-inventory-api-00004-abc",
    "ata-orders-api-00002-xyz",
    "-00004-prod",
    "-00003-prev",
    "ci-deployer",
    "0.m182379",
    "0.m182380",
    "1638291048123",
    "1638291048456",
    "Database query timeout after 5000ms",
    "ExclusiveLock on relation 16422",
    "SpikeArrest[Spike-Arrest-1]",
    "ata-agent-00007-zzw",
    "ata-bridge-00020-p22",
    "14.2%",
    "98/100",
    "+716%",
    "+14100%",
    "198.51.100.0/24",
    "1.74s",
    "1.65s",
]

PRODUCTION_DIRS = ["agent", "bridge", "tools"]

REVISION_STRING_PATTERN = re.compile(r"\b[a-z0-9-]+-\d{5}-[a-z0-9]+\b")
REVISION_STRING_PATTERNS = [
    re.compile(r"\b[a-z0-9-]+-\d{5}-[a-z0-9]+\b"),     # full revision name e.g. ata-inventory-api-00070-6nq
    re.compile(r"-\d{5}-[a-z0-9]{3}"),                 # revision suffix pattern e.g. -\d{5}-[a-z0-9]{3}
    re.compile(r"-000\d{2}"),                          # revision index suffix e.g. -000NN, -00070
    re.compile(r"\b00070\b"),                          # pinned demo revision 00070
    re.compile(r"\b000\d{2}\b"),                       # revision sequence e.g. 000NN
]

PLACEHOLDER_TOKEN_PATTERNS = [
    re.compile(r"\bPREVIOUS_REVISION\b"),
    re.compile(r"\bPLACEHOLDER\b"),
]

METRIC_NUMERIC_PATTERNS = [
    re.compile(r"[+-]?\b\d+(?:\.\d+)?%"),                # percentages: 14.2%, 0.1%, 12.0%, +11900%, 28%
    re.compile(r"\b\d+(?:\.\d+)?ms\b"),                 # latencies: 12ms, 42ms, 2400ms, 5400ms, 2ms
    re.compile(r"\b\d+\s*(?:rpm|req/min)\b"),           # rates: 320 rpm, 284 req/min
    re.compile(r"\b\d+\.\d+s\b"),                       # static durations: 1.74s, 1.65s
    re.compile(r"\b\d+/\d+\s*(?:conns|connections)\b"), # connection ratios: 98/100, 100/100
]

# Whitelist patterns for legitimate operational thresholds and formatting tokens
WHITELISTED_THRESHOLDS = [
    re.compile(r"%\w"),                                 # strftime tokens like %Y, %m, %d
    re.compile(r"%s"),                                  # format specifier
    re.compile(r"<CLIENT_IP>/32"),                      # CIDR notation
    re.compile(r"below 80% capacity"),                  # operational verification target
    re.compile(r"nominal 0%"),                          # operational verification target
    re.compile(r"below 1%"),                            # operational verification target
    re.compile(r"width:\s*100%"),                       # CSS layout
    re.compile(r"style=.*100%"),                        # CSS layout
]


def test_no_prohibited_fabricated_strings_in_production_code():
    """Assert non-test code in agent/, bridge/, tools/ has zero prohibited fabricated strings."""
    violations = []
    for pdir in PRODUCTION_DIRS:
        for root, _, files in os.walk(pdir):
            for fname in files:
                if fname.endswith(".py"):
                    fpath = os.path.join(root, fname)
                    with open(fpath, "r", encoding="utf-8") as f:
                        for lineno, line in enumerate(f, 1):
                            # Skip pure comments
                            stripped = line.strip()
                            if stripped.startswith("#"):
                                continue
                            for prohibited in PROHIBITED_FABRICATED_STRINGS:
                                if prohibited.lower() in line.lower():
                                    violations.append(
                                        f"{fpath}:{lineno} contains prohibited string '{prohibited}': {line.strip()}"
                                    )
    assert not violations, "Found prohibited fabricated mock data in production code:\n" + "\n".join(violations)


def test_no_hardcoded_revision_strings_in_production_code():
    """Assert non-test code in agent/, bridge/, tools/ has zero hardcoded revision-like strings."""
    violations = []
    for pdir in PRODUCTION_DIRS:
        for root, _, files in os.walk(pdir):
            for fname in files:
                if fname.endswith(".py"):
                    fpath = os.path.join(root, fname)
                    with open(fpath, "r", encoding="utf-8") as f:
                        for lineno, line in enumerate(f, 1):
                            stripped = line.strip()
                            if stripped.startswith("#"):
                                continue
                            for pat in REVISION_STRING_PATTERNS:
                                m = pat.search(line)
                                if m:
                                    violations.append(
                                        f"{fpath}:{lineno} contains hardcoded revision '{m.group(0)}': {line.strip()}"
                                    )
                                    break
    assert not violations, "Found hardcoded revision strings in production code:\n" + "\n".join(violations)


def test_no_placeholder_tokens_in_production_code():
    """Assert non-test code in agent/, bridge/, tools/ contains zero placeholder tokens."""
    violations = []
    for pdir in PRODUCTION_DIRS:
        for root, _, files in os.walk(pdir):
            for fname in files:
                if fname.endswith(".py"):
                    fpath = os.path.join(root, fname)
                    with open(fpath, "r", encoding="utf-8") as f:
                        for lineno, line in enumerate(f, 1):
                            stripped = line.strip()
                            if stripped.startswith("#"):
                                continue
                            for pat in PLACEHOLDER_TOKEN_PATTERNS:
                                m = pat.search(line)
                                if m:
                                    violations.append(
                                        f"{fpath}:{lineno} contains placeholder token '{m.group(0)}': {line.strip()}"
                                    )
                                    break
    assert not violations, "Found placeholder tokens in production code:\n" + "\n".join(violations)


def test_no_fabricated_metrics_in_report_building_code_paths():
    """Assert report-building code paths contain no hardcoded numeric metric literals."""
    violations = []

    # 1. Inspect AST constants in agent/app/triage.py generate_investigation_report
    with open("agent/app/triage.py", "r", encoding="utf-8") as f:
        triage_tree = ast.parse(f.read(), filename="agent/app/triage.py")
    for node in ast.walk(triage_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "generate_investigation_report":
            for child in ast.walk(node):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    s = child.value
                    for pat in METRIC_NUMERIC_PATTERNS:
                        m = pat.search(s)
                        if m:
                            matched = m.group(0)
                            if any(w.search(s) for w in WHITELISTED_THRESHOLDS):
                                continue
                            violations.append(
                                f"agent/app/triage.py:generate_investigation_report line {child.lineno} contains metric literal '{matched}' in '{s}'"
                            )

    # 2. Inspect AST constants in agent/app/specialists/synthesizer.py ruled-out and metadata builders
    with open("agent/app/specialists/synthesizer.py", "r", encoding="utf-8") as f:
        synth_tree = ast.parse(f.read(), filename="agent/app/specialists/synthesizer.py")
    target_synth_funcs = {"_build_root_cause_analysis", "_build_agent_metadata"}
    for node in ast.walk(synth_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in target_synth_funcs:
            for child in ast.walk(node):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    s = child.value
                    for pat in METRIC_NUMERIC_PATTERNS:
                        m = pat.search(s)
                        if m:
                            matched = m.group(0)
                            if any(w.search(s) for w in WHITELISTED_THRESHOLDS):
                                continue
                            violations.append(
                                f"agent/app/specialists/synthesizer.py:{node.name} line {child.lineno} contains metric literal '{matched}' in '{s}'"
                            )

    # 3. Inspect template fallback values in bridge/app/templates/renderer.py
    with open("bridge/app/templates/renderer.py", "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Check for hardcoded fallback metrics or latencies in template lines
            for pat in METRIC_NUMERIC_PATTERNS:
                m = pat.search(line)
                if m:
                    matched = m.group(0)
                    if any(w.search(line) for w in WHITELISTED_THRESHOLDS):
                        continue
                    violations.append(
                        f"bridge/app/templates/renderer.py:{lineno} contains metric literal '{matched}': {line.strip()}"
                    )

    assert not violations, "Found hardcoded numeric metric literals in report-building paths:\n" + "\n".join(violations)


def test_synthesizer_origin_determination_is_not_hardcoded():
    """Assert synthesizer._determine_probable_origin does not hardcode static service returns."""
    with open("agent/app/specialists/synthesizer.py", "r", encoding="utf-8") as f:
        content = f.read()

    # Ensure no hardcoded service returns in origin determination
    tree = ast.parse(content)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_determine_probable_origin":
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and isinstance(child.value, ast.Constant):
                    val = child.value.value
                    if isinstance(val, str) and val.startswith("ata-"):
                        pytest.fail(f"synthesizer._determine_probable_origin returns hardcoded service: {val}")


def test_triage_decision_alerts_considered_and_excluded_is_clean():
    """Assert synthesizer and triage do not populate hardcoded fake candidates in alerts_considered_and_excluded."""
    for path in ["agent/app/specialists/synthesizer.py", "agent/app/triage.py"]:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "Billing Anomaly Alert" not in content
        assert "ata-billing-worker" not in content


def test_no_float_confidence_literals_in_return_statements():
    """AST guard asserting synthesizer.py has no constant float confidence literals in return statements."""
    with open("agent/app/specialists/synthesizer.py", "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename="agent/app/specialists/synthesizer.py")

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and child.value is not None:
                    items_to_check = []
                    if isinstance(child.value, ast.Constant) and isinstance(child.value.value, float):
                        items_to_check.append(child.value)
                    elif isinstance(child.value, ast.Tuple):
                        for elt in child.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, float):
                                items_to_check.append(elt)
                    for c in items_to_check:
                        violations.append(
                            f"{node.name} line {c.lineno} returns constant float literal {c.value}"
                        )

    assert not violations, "Found constant float confidence literals in return statements:\n" + "\n".join(violations)


def test_different_evidence_gives_different_calculated_confidences():
    """Assert calculated confidence formula derives different scores from different evidence factors."""
    from agent.app.specialists.synthesizer import SynthesizerAgent
    from agent.app.specialists.base import EvidenceDossier, EvidenceItem
    from agent.app.contracts.alert import AlertEvent

    synth = SynthesizerAgent()

    # Scenario A: minimal evidence, local anomaly, 0 corroborating signals, no recent change
    alert_a = AlertEvent(
        incident_id="inc-test-a",
        policy_name="Latency Threshold Exceeded",
        resource_name="ata-frontend",
        started_at="2026-10-23T10:00:00Z",
    )
    dossiers_a = [
        EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-frontend",
            findings=["Slight latency increase"],
            metrics={"error_rate_5xx": "0.1%"},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=False,
            confidence=0.85,
        )
    ]
    conf_a, factors_a, rationale_a = synth.calculate_confidence(
        dossiers=dossiers_a,
        origin="ata-frontend",
        alert=alert_a,
        blast_radius=["ata-frontend"],
        origin_change=None,
    )

    # Scenario B: rich evidence, upstream DAG origin, corroborating signals, recent deploy 8m ago
    alert_b = AlertEvent(
        incident_id="inc-test-b",
        policy_name="High Error Rate 5xx",
        resource_name="ata-orders-api",
        started_at="2026-10-23T10:00:00Z",
    )
    dossiers_b = [
        EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            findings=["High 5xx errors"],
            metrics={"error_rate_5xx": "18.5%", "container_restarts": 4},
            evidence_items=[EvidenceItem(headline="Error Rate", content="5xx at 18.5%")],
            queries_executed={},
            anomaly_detected=True,
            confidence=0.90,
        ),
        EvidenceDossier(
            specialist_name="CloudSQLSpecialist",
            target_resource="ata-inventory-db",
            findings=["Lock contention"],
            metrics={"active_lock_waits": 8, "active_connections": 20, "max_connections": 100},
            evidence_items=[EvidenceItem(headline="Lock Contention", content="8 locks waiting")],
            queries_executed={},
            anomaly_detected=True,
            confidence=0.90,
        ),
    ]
    change_b = {
        "service": "ata-inventory-api",
        "revision": "ata-inventory-api-00021-test",
        "deployed_minutes_ago": 8,
        "timestamp": "2026-10-23T09:52:00Z",
    }
    conf_b, factors_b, rationale_b = synth.calculate_confidence(
        dossiers=dossiers_b,
        origin="ata-inventory-api",
        alert=alert_b,
        blast_radius=["ata-inventory-api", "ata-orders-api"],
        origin_change=change_b,
    )

    # Confidence must reflect evidence strength dynamically
    assert conf_a != conf_b, f"Confidences should differ: {conf_a} == {conf_b}"
    assert conf_b > conf_a, f"Expected conf_b ({conf_b}) > conf_a ({conf_a})"
    assert "topology_position" in factors_a and "topology_position" in factors_b
    assert "corroborating_signals" in factors_a and "corroborating_signals" in factors_b
    assert "onset_ordering" in factors_a and "onset_ordering" in factors_b
    assert "change_proximity" in factors_a and "change_proximity" in factors_b
    assert factors_b["change_proximity"]["score"] > factors_a["change_proximity"]["score"]


def test_s5_like_fixture_names_revision_in_root_cause():
    """Assert S5-like recent deployment fixture explicitly cites revision and deploy time."""
    from agent.app.specialists.synthesizer import SynthesizerAgent
    from agent.app.specialists.base import EvidenceDossier, EvidenceItem
    from agent.app.contracts.alert import AlertEvent

    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="inc-test-s5",
        policy_name="Cloud Run High Latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T08:45:00Z",
    )
    revision_name = "ata-inventory-api-00035-deploy"
    deploy_time = "2026-10-23T08:35:00Z"
    dossiers = [
        EvidenceDossier(
            specialist_name="ChangeSpecialist",
            target_resource="ata-inventory-api",
            findings=[f"Deployment {revision_name} detected at {deploy_time}"],
            metrics={
                "recent_revision": revision_name,
                "deploy_timestamp": deploy_time,
                "minutes_since_deploy": 10,
                "all_changes": [
                    {
                        "service": "ata-inventory-api",
                        "revision": revision_name,
                        "timestamp": deploy_time,
                        "deployed_minutes_ago": 10,
                    }
                ],
            },
            evidence_items=[EvidenceItem(headline="Revision Deployment", content=f"Revision {revision_name}")],
            queries_executed={},
            anomaly_detected=True,
            confidence=0.88,
        ),
        EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            findings=["Latency spike"],
            metrics={"error_rate_5xx": "12.4%"},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=True,
            confidence=0.85,
        ),
    ]

    root_cause_text, confidence = synth._determine_root_cause(
        dossiers=dossiers,
        origin="ata-inventory-api",
        alert=alert,
        blast_radius=["ata-inventory-api"],
    )

    assert revision_name in root_cause_text
    assert deploy_time in root_cause_text
    assert "(change detected)" in root_cause_text
    assert confidence >= 0.70


def test_change_correlation_without_revision_states_not_identified():
    """Assert recent change without known revision does not fabricate 'revision N+1'."""
    from agent.app.specialists.synthesizer import SynthesizerAgent
    from agent.app.specialists.base import EvidenceDossier
    from agent.app.contracts.alert import AlertEvent

    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="inc-test-chg-norev",
        policy_name="Cloud Run High Latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T08:45:00Z",
    )
    deploy_time = "2026-10-23T08:35:00Z"
    dossiers = [
        EvidenceDossier(
            specialist_name="ChangeSpecialist",
            target_resource="ata-inventory-api",
            findings=["Recent change detected"],
            metrics={
                "deploy_timestamp": deploy_time,
                "minutes_since_deploy": 10,
                "all_changes": [
                    {
                        "service": "ata-inventory-api",
                        "timestamp": deploy_time,
                        "deployed_minutes_ago": 10,
                    }
                ],
            },
            evidence_items=[],
            queries_executed={},
            anomaly_detected=True,
            confidence=0.85,
        ),
    ]

    root_cause_text, confidence = synth._determine_root_cause(
        dossiers=dossiers,
        origin="ata-inventory-api",
        alert=alert,
        blast_radius=["ata-inventory-api"],
    )

    assert "revision N+1" not in root_cause_text
    assert "revision not identified" in root_cause_text


def test_cloudsql_lock_only_fixture_does_not_claim_connection_saturation():
    """Assert CloudSQL lock-only evidence cites lock contention and does not claim connection saturation."""
    from agent.app.specialists.synthesizer import SynthesizerAgent
    from agent.app.specialists.base import EvidenceDossier, EvidenceItem
    from agent.app.contracts.alert import AlertEvent

    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="inc-test-lock",
        policy_name="Database Query Latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T08:40:00Z",
    )
    sql_lock_only_dossier = EvidenceDossier(
        specialist_name="CloudSQLSpecialist",
        target_resource="ata-inventory-db",
        findings=["ExclusiveLock on relation catalog_items causing queries to block"],
        metrics={
            "active_lock_waits": 14,
            "active_connections": 15,
            "max_connections": 100,
            "cpu_utilization_pct": 22.0,
            "metric_onset": "2026-10-23T08:38:00Z",
        },
        evidence_items=[EvidenceItem(headline="Lock Contention", content="14 active lock waits")],
        queries_executed={},
        anomaly_detected=True,
        confidence=0.85,
    )
    dossiers = [sql_lock_only_dossier]

    root_cause_text, confidence = synth._determine_root_cause(
        dossiers=dossiers,
        origin="ata-inventory-db",
        alert=alert,
        blast_radius=["ata-inventory-db", "ata-inventory-api"],
    )

    text_lower = root_cause_text.lower()
    assert "lock" in text_lower, "Root cause must cite lock contention"
    assert "connection saturation" not in text_lower, "Must not claim connection saturation when only locks are observed"
    assert "connection pool saturation" not in text_lower
    assert "connection exhaustion" not in text_lower


def test_cloudsql_connection_only_fixture_does_not_claim_lock_contention():
    """Assert CloudSQL connection saturation evidence does not claim lock contention when no locks observed."""
    from agent.app.specialists.synthesizer import SynthesizerAgent
    from agent.app.specialists.base import EvidenceDossier, EvidenceItem
    from agent.app.contracts.alert import AlertEvent

    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="inc-test-conn",
        policy_name="Database Connection Saturation",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T08:40:00Z",
    )
    sql_conn_only_dossier = EvidenceDossier(
        specialist_name="CloudSQLSpecialist",
        target_resource="ata-inventory-db",
        findings=["Connection pool exhausted"],
        metrics={
            "active_lock_waits": 0,
            "active_connections": 96,
            "max_connections": 100,
            "cpu_utilization_pct": 35.0,
            "metric_onset": "2026-10-23T08:37:00Z",
        },
        evidence_items=[EvidenceItem(headline="Connection Saturation", content="96 of 100 active connections")],
        queries_executed={},
        anomaly_detected=True,
        confidence=0.85,
    )
    dossiers = [sql_conn_only_dossier]

    root_cause_text, confidence = synth._determine_root_cause(
        dossiers=dossiers,
        origin="ata-inventory-db",
        alert=alert,
        blast_radius=["ata-inventory-db", "ata-inventory-api"],
    )

    text_lower = root_cause_text.lower()
    assert "connection" in text_lower, "Root cause must cite connection pool saturation"
    assert "lock" not in text_lower, "Must not claim lock contention when only connection saturation observed"


def test_s4_origin_attribution_by_metric_onset_and_dag_position():
    """Assert S4 origin attribution picks upstream Cloud SQL DB when metric onset precedes downstream API degradation."""
    from agent.app.specialists.synthesizer import SynthesizerAgent
    from agent.app.specialists.base import EvidenceDossier
    from agent.app.contracts.alert import AlertEvent

    synth = SynthesizerAgent()
    # Downstream inventory-api alert opened at 08:41 UTC
    alert = AlertEvent(
        incident_id="inc-test-s4",
        policy_name="Inventory API Elevated Latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T08:41:00Z",
    )

    # Cloud SQL DB connection metric onset was 08:37 UTC (precedes API alert at 08:41 UTC)
    sql_dossier = EvidenceDossier(
        specialist_name="CloudSQLSpecialist",
        target_resource="ata-inventory-db",
        findings=["Database connection saturation"],
        metrics={
            "metric_onset": "2026-10-23T08:37:00Z",
            "active_connections": 98,
            "max_connections": 100,
            "active_lock_waits": 0,
        },
        evidence_items=[],
        queries_executed={},
        anomaly_detected=True,
        confidence=0.90,
    )
    api_dossier = EvidenceDossier(
        specialist_name="CloudRunSpecialist",
        target_resource="ata-inventory-api",
        findings=["High request latency"],
        metrics={
            "metric_onset": "2026-10-23T08:41:00Z",
            "latency_p95_ms": 2800,
        },
        evidence_items=[],
        queries_executed={},
        anomaly_detected=True,
        confidence=0.85,
    )

    dossier_map = {
        "CloudSQLSpecialist": sql_dossier,
        "CloudRunSpecialist": api_dossier,
    }

    origin = synth._determine_probable_origin(
        alert=alert,
        dossiers=dossier_map,
        upstream_deps=["ata-inventory-db"],
    )

    # Origin must be attributed to upstream DB due to earlier metric onset and DAG hierarchy
    assert origin == "ata-inventory-db"


@pytest.mark.asyncio
async def test_generic_genuine_change_detection():
    """Assert generic genuine change detection evaluates creation time, diff, and traffic shift without hardcoded revisions."""
    from agent.app.specialists.change import ChangeSpecialist
    from agent.app.config import AgentConfig
    from agent.app.contracts.alert import AlertEvent
    from unittest.mock import patch

    specialist = ChangeSpecialist()
    alert = AlertEvent(
        incident_id="test-genuine-chg",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-24T12:00:00Z",
    )
    config = AgentConfig(project_id="test-proj", region="us-west1", is_local=True)

    # Case 1: Created within window (15m ago) with diff -> genuine change
    timeline_genuine = {
        "changes": [{
            "source": "cloud_run",
            "service_name": "ata-inventory-api",
            "revision_name": "ata-inventory-api-00099-new",
            "previous_revision": "ata-inventory-api-00088-old",
            "timestamp": "2026-10-24T11:45:00Z",
            "is_serving": True,
            "differs_from_previous": True,
            "action": "revision_create",
        }]
    }
    with patch("tools.app.server.get_change_timeline", return_value=timeline_genuine):
        dossier = await specialist.investigate(alert, config)
    assert dossier.anomaly_detected is True
    assert dossier.metrics.get("recent_revision") == "ata-inventory-api-00099-new"
    assert dossier.metrics.get("previous_revision") == "ata-inventory-api-00088-old"

    # Case 2: Created within window but image digest & env identical -> NOT genuine change (traffic shift)
    timeline_no_diff = {
        "changes": [{
            "source": "cloud_run",
            "service_name": "ata-inventory-api",
            "revision_name": "ata-inventory-api-00099-nodiff",
            "previous_revision": "ata-inventory-api-00088-old",
            "timestamp": "2026-10-24T11:45:00Z",
            "is_serving": True,
            "differs_from_previous": False,
            "is_traffic_shift": True,
            "action": "traffic_shift",
        }]
    }
    with patch("tools.app.server.get_change_timeline", return_value=timeline_no_diff):
        dossier = await specialist.investigate(alert, config)
    assert dossier.anomaly_detected is False

    # Case 3: Created outside window (150m ago) -> NOT genuine change
    timeline_outside_window = {
        "changes": [{
            "source": "cloud_run",
            "service_name": "ata-inventory-api",
            "revision_name": "ata-inventory-api-00050-ancient",
            "previous_revision": "ata-inventory-api-00040-older",
            "timestamp": "2026-10-24T09:30:00Z",
            "is_serving": True,
            "differs_from_previous": True,
            "action": "revision_create",
        }]
    }
    with patch("tools.app.server.get_change_timeline", return_value=timeline_outside_window):
        dossier = await specialist.investigate(alert, config)
    assert dossier.anomaly_detected is False

    # Case 4: Traffic shift to older revision -> NOT genuine change
    timeline_traffic_shift = {
        "changes": [{
            "source": "cloud_run_service",
            "service_name": "ata-inventory-api",
            "revision_name": "ata-inventory-api-00070-6nq",
            "timestamp": "2026-10-24T11:50:00Z",
            "action": "traffic_shift",
            "is_traffic_shift": True,
            "differs_from_previous": False,
        }]
    }
    with patch("tools.app.server.get_change_timeline", return_value=timeline_traffic_shift):
        dossier = await specialist.investigate(alert, config)
    assert dossier.anomaly_detected is False


def test_rollback_action_formatting_with_known_and_unknown_revision():
    """Assert rollback action names real previous revision when known, or says 'previous serving revision could not be identified' - NEVER 'PREVIOUS_REVISION'."""
    from agent.app.specialists.synthesizer import SynthesizerAgent
    from agent.app.specialists.base import EvidenceDossier
    from agent.app.contracts.alert import AlertEvent

    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="test-rollback-alert",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-24T12:00:00Z",
    )

    # 1. When previous revision is known
    dossier_known = [
        EvidenceDossier(
            specialist_name="ChangeSpecialist",
            target_resource="ata-inventory-api",
            findings=["Recent deployment detected on `ata-inventory-api`: `ata-inventory-api-00099-bad`."],
            metrics={
                "recent_revision": "ata-inventory-api-00099-bad",
                "previous_revision": "ata-inventory-api-00088-good",
                "minutes_since_deploy": 5,
            },
            anomaly_detected=True,
        )
    ]
    actions_known = synth._synthesize_recommendations(dossier_known, "ata-inventory-api", alert)
    rollback_known = actions_known[0]
    assert "ata-inventory-api-00088-good" in rollback_known.command
    assert "ata-inventory-api-00088-good" in rollback_known.title
    assert "update-traffic" in rollback_known.command
    assert "PREVIOUS_REVISION" not in rollback_known.command
    assert "PREVIOUS_REVISION" not in rollback_known.title

    # 2. When previous revision is unknown / None / empty
    dossier_unknown = [
        EvidenceDossier(
            specialist_name="ChangeSpecialist",
            target_resource="ata-inventory-api",
            findings=["Recent deployment detected on `ata-inventory-api`: `ata-inventory-api-00099-bad`."],
            metrics={
                "recent_revision": "ata-inventory-api-00099-bad",
                "previous_revision": None,
                "minutes_since_deploy": 5,
            },
            anomaly_detected=True,
        )
    ]
    actions_unknown = synth._synthesize_recommendations(dossier_unknown, "ata-inventory-api", alert)
    rollback_unknown = actions_unknown[0]
    assert "previous serving revision could not be identified" in rollback_unknown.title or "previous serving revision could not be identified" in rollback_unknown.command
    assert "PREVIOUS_REVISION" not in rollback_unknown.command
    assert "PREVIOUS_REVISION" not in rollback_unknown.title


def test_scanner_patterns_flag_revision_literals_and_placeholders():
    """Assert revision and placeholder regex patterns properly match prohibited patterns."""
    test_lines = [
        ("if '00070' in rev:", True, False),
        ("rev = 'ata-inventory-api-00004-abc'", True, False),
        ("target = '-00070'", True, False),
        ("target = '-00012-xyz'", True, False),
        ("prev_rev = 'PREVIOUS_REVISION'", False, True),
        ("token = 'PLACEHOLDER'", False, True),
        ("normal_code = 'clean_variable'", False, False),
    ]

    for line, should_match_rev, should_match_placeholder in test_lines:
        has_rev = any(p.search(line) for p in REVISION_STRING_PATTERNS)
        has_placeholder = any(p.search(line) for p in PLACEHOLDER_TOKEN_PATTERNS)
        assert has_rev == should_match_rev, f"Failed rev match for: {line}"
        assert has_placeholder == should_match_placeholder, f"Failed placeholder match for: {line}"
