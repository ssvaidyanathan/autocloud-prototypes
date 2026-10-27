"""Agent and tools config: no live defaults, values derived from PROJECT_ID / REGION / NAME_PREFIX.

User rule: nothing hardcoded (no project ids, regions, SA emails or 'ata' names). Missing
required settings fail loudly. CONFIGURATION.md documents the env names the code reads (gap 42).
"""

from __future__ import annotations

import re
import tempfile
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.app.config import AgentConfig
from bridge.app.config import BridgeConfig
from tools.app.config import ToolsConfig
from topology.loader import TopologyLoader, TopologyValidationError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def no_env(monkeypatch):
    for var in ("PROJECT_ID", "REGION", "NAME_PREFIX", "LOCATION", "TIMER_QUEUE_NAME", "SERVICE_ACCOUNT_EMAIL",
                "MODEL_NAME", "GEMINI_LOCATION", "APIGEE_ORG", "APIGEE_ENV", "IS_LOCAL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tempfile.mkdtemp())  # no stray .env file


# --- agent ---------------------------------------------------------------------------------

def test_agent_derives_timer_settings_from_project_region_and_prefix(no_env):
    cfg = AgentConfig(PROJECT_ID="proj-a", REGION="europe-west4", NAME_PREFIX="atat")  # prefix-ok: test input
    assert cfg.location == "europe-west4"  # prefix-ok: test input
    assert cfg.timer_queue_name == "atat-timer-queue"
    assert cfg.service_account_email == "atat-agent-sa@proj-a.iam.gserviceaccount.com"
    assert cfg.effective_incidents_bucket == "proj-a-atat-incidents"
    assert cfg.apigee_access_log_id == "atat-apigee-access"


def test_agent_explicit_timer_settings_win(no_env):
    cfg = AgentConfig(
        PROJECT_ID="p", REGION="r1", NAME_PREFIX="ata", LOCATION="l2",
        TIMER_QUEUE_NAME="q", SERVICE_ACCOUNT_EMAIL="sa@x.iam.gserviceaccount.com",
    )
    assert (cfg.location, cfg.timer_queue_name, cfg.service_account_email) == ("l2", "q", "sa@x.iam.gserviceaccount.com")


@pytest.mark.parametrize("missing", ["PROJECT_ID", "REGION"])
def test_agent_requires_project_and_region_when_not_local(no_env, missing):
    values = {"PROJECT_ID": "p", "REGION": "r1", "NAME_PREFIX": "ata"}
    values.pop(missing)
    with pytest.raises(ValidationError, match=missing):
        AgentConfig(**values)


def test_agent_local_mode_may_omit_project_and_region(no_env):
    cfg = AgentConfig(NAME_PREFIX="ata", IS_LOCAL=True)
    assert cfg.project_id is None and cfg.region is None and cfg.service_account_email is None
    with pytest.raises(ValueError, match="INCIDENTS_BUCKET or PROJECT_ID"):
        cfg.effective_incidents_bucket


def test_agent_requires_name_prefix(no_env):
    with pytest.raises(ValidationError, match="NAME_PREFIX"):
        AgentConfig(PROJECT_ID="p", REGION="r1")


@pytest.mark.parametrize("bad", ["ATA", "a", "1ab", "at-a", "abcdefghijk", ""])
def test_agent_rejects_prefixes_terraform_rejects(no_env, bad):
    with pytest.raises(ValidationError):
        AgentConfig(PROJECT_ID="p", REGION="r1", NAME_PREFIX=bad)


@pytest.mark.parametrize("good", ["ata", "atat", "ab", "a123456789"])
def test_agent_accepts_terraform_valid_prefixes(no_env, good):
    assert AgentConfig(PROJECT_ID="p", REGION="r1", NAME_PREFIX=good).name_prefix == good


def test_agent_gemini_settings_are_overridable_and_validated(no_env, monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_LOCATION", "us-central1")  # prefix-ok: test input
    cfg = AgentConfig(PROJECT_ID="p", REGION="r1", NAME_PREFIX="ata")
    assert (cfg.model_name, cfg.gemini_location) == ("gemini-2.5-flash", "us-central1")  # prefix-ok: test input
    with pytest.raises(ValidationError, match="GEMINI_LOCATION"):
        AgentConfig(PROJECT_ID="p", REGION="r1", NAME_PREFIX="ata", GEMINI_LOCATION="Mars")
    with pytest.raises(ValidationError, match="MODEL_NAME"):
        AgentConfig(PROJECT_ID="p", REGION="r1", NAME_PREFIX="ata", MODEL_NAME="bad model")


def test_agent_gemini_defaults_are_model_values_not_env_identifiers(no_env):
    cfg = AgentConfig(PROJECT_ID="p", REGION="r1", NAME_PREFIX="ata")
    assert cfg.model_name == "gemini-3.8-flash" and cfg.gemini_location == "global"


# --- tools ---------------------------------------------------------------------------------

def test_tools_requires_project_region_and_prefix(no_env):
    with pytest.raises(ValidationError, match="PROJECT_ID"):
        ToolsConfig(NAME_PREFIX="ata", REGION="r1")
    with pytest.raises(ValidationError, match="NAME_PREFIX"):
        ToolsConfig(PROJECT_ID="p", REGION="r1")
    with pytest.raises(ValidationError):
        ToolsConfig(PROJECT_ID="p", REGION="r1", NAME_PREFIX="Bad-")


def test_tools_apigee_org_and_env_are_explicit_with_no_project_fallback(no_env):
    # Integration decision (ata-fresh-integ): APIGEE_ORG and APIGEE_ENV are required for Apigee
    # reads, with no fallback to PROJECT_ID; get_change_timeline reports the missing setting.
    cfg = ToolsConfig(PROJECT_ID="p", REGION="r1", NAME_PREFIX="atat")
    assert cfg.apigee_org is None
    assert cfg.apigee_env is None
    assert cfg.apigee_access_log_id == "atat-apigee-access"
    cfg = ToolsConfig(PROJECT_ID="p", REGION="r1", NAME_PREFIX="ata", APIGEE_ORG="org2", APIGEE_ENV="test")
    assert (cfg.apigee_org, cfg.apigee_env) == ("org2", "test")


# --- bridge (integration: no project/region/prefix defaults) -------------------------------

def test_bridge_requires_project_region_and_a_valid_prefix(no_env):
    with pytest.raises(ValidationError, match="PROJECT_ID"):
        BridgeConfig(NAME_PREFIX="ata", REGION="r1")
    with pytest.raises(ValidationError, match="REGION"):
        BridgeConfig(NAME_PREFIX="ata", PROJECT_ID="p")
    with pytest.raises(ValidationError, match="NAME_PREFIX"):
        BridgeConfig(PROJECT_ID="p", REGION="r1")
    with pytest.raises(ValidationError, match="NAME_PREFIX"):
        BridgeConfig(PROJECT_ID="p", REGION="r1", NAME_PREFIX="Bad-")
    local = BridgeConfig(NAME_PREFIX="atat", IS_LOCAL="true")
    assert (local.project_id, local.region) == (None, None)


def test_bridge_derives_stack_names_from_the_configured_values(no_env):
    cfg = BridgeConfig(PROJECT_ID="proj-a", REGION="r9", NAME_PREFIX="atat")
    assert cfg.effective_tasks_queue == "atat-triage-queue"
    assert cfg.effective_tasks_location == "r9"
    assert cfg.effective_incidents_bucket == "proj-a-atat-incidents"
    assert cfg.effective_tasks_invoker_sa == "atat-bridge-sa@proj-a.iam.gserviceaccount.com"


# --- docs match the code (gap 42) ----------------------------------------------------------

def _agent_doc_rows() -> set[str]:
    text = (ROOT / "CONFIGURATION.md").read_text()
    section = text.split("### 1.2", 1)[1].split("### 1.3", 1)[0]
    return set(re.findall(r"^\| `([A-Z0-9_]+)`", section, re.M))


def test_configuration_md_documents_the_env_names_the_agent_reads():
    rows = _agent_doc_rows()
    assert {"TIMER_QUEUE_NAME", "LOCATION", "SERVICE_ACCOUNT_EMAIL", "MODEL_NAME", "GEMINI_LOCATION", "NAME_PREFIX"} <= rows
    assert not {"TIMER_QUEUE", "TIMER_QUEUE_LOCATION"} & rows, "documented names the code does not read"
    aliases = {f.alias for f in AgentConfig.model_fields.values() if f.alias}
    assert rows <= aliases, f"documented but not read by AgentConfig: {rows - aliases}"


def test_configuration_md_documents_tools_apigee_env():
    section = (ROOT / "CONFIGURATION.md").read_text().split("### 1.3", 1)[1].split("### 1.4", 1)[0]
    for name in ("APIGEE_ENV", "APIGEE_ORG", "NAME_PREFIX", "PROJECT_ID", "REGION"):
        assert f"| `{name}` |" in section


# --- topology loader per prefix ------------------------------------------------------------

def _topology(tmp_path, body: str) -> Path:
    path = tmp_path / "TOPOLOGY.md"
    path.write_text(textwrap.dedent(body))
    return path


SAMPLE = """\
    ## Components
    | Component | Type | Also known as (alert labels) |
    |---|---|---|
    | {prefix}-orders-api | Cloud Run | orders-api |
    | {prefix}-inventory-api | Cloud Run | inventory-api |
    ## Dependencies
    | From | To | How |
    |---|---|---|
    | {prefix}-orders-api | {prefix}-inventory-api | calls |
    """


def test_loader_substitutes_the_prefix(tmp_path):
    loader = TopologyLoader(_topology(tmp_path, SAMPLE), name_prefix="atat")
    assert set(loader.components) == {"atat-orders-api", "atat-inventory-api"}
    assert loader.canonicalize("orders-api") == "atat-orders-api"


def test_loader_reads_name_prefix_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NAME_PREFIX", "zz9")
    assert set(TopologyLoader(_topology(tmp_path, SAMPLE)).components) == {"zz9-orders-api", "zz9-inventory-api"}


def test_loader_fails_loudly_without_name_prefix(tmp_path, monkeypatch):
    monkeypatch.delenv("NAME_PREFIX", raising=False)
    with pytest.raises(TopologyValidationError, match="NAME_PREFIX is not set"):
        TopologyLoader(_topology(tmp_path, SAMPLE))


@pytest.mark.parametrize("bad", ["ATA", "a", "a-b"])
def test_loader_fails_loudly_on_invalid_name_prefix(tmp_path, bad):
    with pytest.raises(TopologyValidationError, match="invalid"):
        TopologyLoader(_topology(tmp_path, SAMPLE), name_prefix=bad)


def test_loader_fails_loudly_on_an_unsubstituted_placeholder(tmp_path):
    with pytest.raises(TopologyValidationError, match=r"Unsubstituted placeholder '\{stack\}'"):
        TopologyLoader(_topology(tmp_path, SAMPLE.replace("{prefix}-inventory-api | Cloud Run", "{stack}-inventory-api | Cloud Run")), name_prefix="ata")


def test_shipped_topology_is_prefix_relative():
    text = (ROOT / "topology/TOPOLOGY.md").read_text()
    assert "{prefix}-orders-api" in text and "/{prefix}/orders/v1" in text
    assert not re.search(r"(?<![A-Za-z0-9_{])ata-[a-z]", text)


# --- console links and report contract: no guessed project/region --------------------------

def test_console_links_need_a_project_and_never_guess_a_region():
    from agent.app.contracts.links import ConsoleLinkBuilder

    with pytest.raises(ValueError, match="project_id"):
        ConsoleLinkBuilder(project_id="")
    no_region = ConsoleLinkBuilder(project_id="p")
    assert no_region.build_cloud_run_service_url("atat-orders-api") == "https://console.cloud.google.com/run?project=p"
    with_region = ConsoleLinkBuilder(project_id="p", region="r9")
    assert with_region.build_cloud_run_service_url("atat-orders-api") == (
        "https://console.cloud.google.com/run/detail/r9/atat-orders-api/metrics?project=p"
    )


def test_ticket_description_needs_the_incidents_bucket():
    from agent.app.contracts.report import InvestigationReport

    report = InvestigationReport(group_id="g1", summary="s", observations="o", project_id="p", region="r9")
    with pytest.raises(TypeError):
        report.build_structured_description(project_id="p")  # no '<project>-ata-incidents' guess
    assert "gs://bkt-1/" in report.build_structured_description(project_id="p", incidents_bucket="bkt-1") or \
        "bkt-1" in report.build_structured_description(project_id="p", incidents_bucket="bkt-1")


def test_investigation_report_carries_the_configured_region(tmp_path):
    from agent.app.config import reset_agent_settings
    from agent.app.contracts.alert import AlertEvent
    from agent.app.triage import generate_investigation_report

    cfg = AgentConfig(
        PROJECT_ID="proj-r", REGION="r9", NAME_PREFIX="ata", IS_LOCAL=True,
        INCIDENT_STORE_PATH=str(tmp_path / "s"), FALLBACK_STORE_PATH=str(tmp_path / "f"),
    )
    reset_agent_settings(cfg)
    try:
        alert = AlertEvent(incident_id="0.r1", policy_name="ata-inventory-5xx", resource_name="ata-inventory-api",
                           started_at="2026-10-23T07:52:06Z", policy_user_labels={"signal": "5xx"})
        report = generate_investigation_report(alert, "grp_r", "incident-grp_r.md", cfg)
    finally:
        reset_agent_settings(None)
    assert (report.project_id, report.region) == ("proj-r", "r9")
