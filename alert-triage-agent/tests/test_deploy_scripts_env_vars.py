"""The Cloud Run deploy scripts keep existing configuration on a redeploy.

`gcloud run deploy --set-env-vars` replaces the service's whole env list, so a redeploy
without e.g. VERIFIER_SA exported dropped it from the live bridge. agent, bridge and tools
deploy.sh now use --update-env-vars (bridge: --update-secrets too). The tests check that:
- every variable the live services have is still set by the scripts (LIVE_ENV below),
- unset optional variables are left alone, not cleared,
- URLs come from the services or the deterministic Cloud Run URL, never a hardcoded host,
- a failed lookup stops the deploy.

The scripts run from a tmp copy against a stub `gcloud` (Python) that records every call.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))
PROJECT_NUMBER = "123456789012"

# Variable names on the live services (gcloud run services describe, 2026-10-27, values not
# copied). The deploy scripts must keep setting all of them; see DEPLOY.md "Environment
# variables set by the deploy scripts".
LIVE_ENV = {
    "agent": {"PROJECT_ID", "NAME_PREFIX", "REGION", "INCIDENTS_BUCKET", "INCIDENT_STORE_PATH", "BRIDGE_URL", "AGENT_URL"},
    "bridge": {"PROJECT_ID", "PROJECT_NUMBER", "NAME_PREFIX", "REGION", "INCIDENTS_BUCKET", "TASKS_QUEUE", "AGENT_URL",
               "SERVICENOW_INSTANCE_URL", "VERIFIER_SA"},
    "tools": {"PROJECT_ID", "NAME_PREFIX", "REGION", "APIGEE_ORG"},
}
LIVE_SECRETS = {"bridge": {"WEBHOOK_TOKEN", "SERVICENOW_USERNAME", "SERVICENOW_PASSWORD"}}

STUB_GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
def opt(name):
    for a in args:
        if a.startswith("--" + name + "="):
            return a.split("=", 1)[1]
    return None
deployed = os.environ.get("STUB_DEPLOYED", "").split()
if args[:3] == ["run", "services", "list"]:
    if os.environ.get("STUB_LIST_FAIL"):
        sys.stderr.write("ERROR: (gcloud.run.services.list) PERMISSION_DENIED\n")
        sys.exit(1)
    name = opt("filter").split("=", 1)[1]
    for svc in deployed:
        # The real filter also matches longer names; the script must pick the exact one.
        if svc.startswith(name):
            print(f"{svc}\thttps://{svc}-hash1234-uw.a.run.app")
elif args[:2] == ["projects", "describe"]:
    if os.environ.get("STUB_NO_PROJECT_NUMBER"):
        sys.exit(1)
    print(os.environ["STUB_PROJECT_NUMBER"])
elif args[:3] == ["run", "services", "describe"] and opt("format") == "value(status.url)":
    print(f"https://{args[3]}-hash1234-uw.a.run.app")
'''


@pytest.fixture
def deploy(tmp_path):
    assert TEST_BASH, f"ATA_TEST_BASH={os.environ.get('ATA_TEST_BASH')!r} not found"
    work = tmp_path / "ata"
    for component in ("agent", "bridge", "tools"):
        (work / component).mkdir(parents=True)
        shutil.copytree(ROOT / component / "scripts", work / component / "scripts")
    (work / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "cloud-run-deploy-lib.sh", work / "scripts" / "cloud-run-deploy-lib.sh")
    (work / "topology").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gcloud"
    stub.write_text(STUB_GCLOUD)
    stub.chmod(0o755)
    log = tmp_path / "gcloud.log"

    def run(component, **env_overrides):
        log.write_text("")
        env = {k: v for k, v in os.environ.items()
               if k not in ("PROJECT_NUMBER", "VERIFIER_SA", "AUTO_RESOLVE_ON_RECOVERY", "BRIDGE_URL", "AGENT_URL",
                            "SERVICENOW_INSTANCE_URL", "GCP_PROJECT", "GCP_REGION")}
        env.update(
            PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
            STUB_LOG=str(log),
            STUB_PROJECT_NUMBER=PROJECT_NUMBER,
            STUB_DEPLOYED="ata-agent ata-bridge ata-tools ata-agent-canary",
            PROJECT_ID="proj-x",
            REGION="europe-west1",
            SERVICENOW_INSTANCE_URL="https://example.service-now.com",
        )
        env.update(env_overrides)
        res = subprocess.run([TEST_BASH, str(work / component / "scripts" / "deploy.sh")], cwd=str(work / component),
                             capture_output=True, text=True, env=env, timeout=60)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return res, calls

    return run


def _deploy_call(calls):
    found = [c for c in calls if c[:2] == ["run", "deploy"]]
    assert len(found) == 1, calls
    return found[0]


def _opt(call, name):
    values = [a.split("=", 1)[1] for a in call if a.startswith(f"--{name}=")]
    assert len(values) <= 1, call
    return values[0] if values else None


def _env(call, name="update-env-vars"):
    raw = _opt(call, name)
    assert raw is not None, f"--{name} missing: {call}"
    delim = ","
    m = re.match(r"\^(.)\^", raw)
    if m:
        delim, raw = m.group(1), raw[3:]
    return dict(item.split("=", 1) for item in raw.split(delim))


@pytest.mark.parametrize("component", ["agent", "bridge", "tools"])
def test_redeploy_updates_env_vars_instead_of_replacing_them(deploy, component):
    res, calls = deploy(component, VERIFIER_SA="ops@proj-x.iam.gserviceaccount.com")

    assert res.returncode == 0, res.stdout + res.stderr
    call = _deploy_call(calls)
    assert _opt(call, "set-env-vars") is None
    assert _opt(call, "set-secrets") is None
    env = _env(call)
    assert LIVE_ENV[component] <= set(env), sorted(LIVE_ENV[component] - set(env))
    if component in LIVE_SECRETS:
        assert set(_env(call, "update-secrets")) == LIVE_SECRETS[component]
    # The image lives in the Artifact Registry repo of the deploy region (foundation: location = var.region).
    assert _opt(call, "image").startswith("europe-west1-docker.pkg.dev/proj-x/")


def test_no_deploy_script_replaces_env_vars_or_hardcodes_a_run_app_host():
    for component in ("agent", "bridge", "tools"):
        text = (ROOT / component / "scripts" / "deploy.sh").read_text()
        assert not re.search(r"--set-(env-vars|secrets)=", text), component
        assert not re.search(r"-[a-z0-9]{10}-[a-z]{2}\.a\.run\.app", text), component
        assert "us-west1-docker.pkg.dev" not in text, component


def test_agent_urls_come_from_the_services(deploy):
    res, calls = deploy("agent")

    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    # Exact name match: ata-agent-canary is not picked for ata-agent.
    assert env["AGENT_URL"] == "https://ata-agent-hash1234-uw.a.run.app"
    assert env["BRIDGE_URL"] == "https://ata-bridge-hash1234-uw.a.run.app"


def test_agent_uses_deterministic_bridge_url_when_bridge_is_not_deployed(deploy):
    res, calls = deploy("agent", STUB_DEPLOYED="ata-agent")

    assert res.returncode == 0, res.stdout + res.stderr
    assert _env(_deploy_call(calls))["BRIDGE_URL"] == f"https://ata-bridge-{PROJECT_NUMBER}.europe-west1.run.app"
    assert "ata-bridge is not deployed in proj-x/europe-west1 yet; using its deterministic URL" in res.stderr


def test_exported_urls_win(deploy):
    res, calls = deploy("agent", BRIDGE_URL="https://bridge.example", AGENT_URL="https://agent.example")

    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert (env["BRIDGE_URL"], env["AGENT_URL"]) == ("https://bridge.example", "https://agent.example")
    assert not [c for c in calls if c[:3] == ["run", "services", "list"]]


@pytest.mark.parametrize("component", ["agent", "bridge"])
def test_failed_service_lookup_stops_the_deploy(deploy, component):
    res, calls = deploy(component, STUB_LIST_FAIL="1")

    assert res.returncode != 0
    assert "could not list Cloud Run services in proj-x/europe-west1" in res.stderr
    assert not [c for c in calls if c[:2] == ["run", "deploy"]]


def test_agent_missing_project_number_for_deterministic_url_stops_the_deploy(deploy):
    res, calls = deploy("agent", STUB_DEPLOYED="ata-agent", STUB_NO_PROJECT_NUMBER="1")

    assert res.returncode != 0
    assert "PROJECT_NUMBER is required" in res.stderr
    assert not [c for c in calls if c[:2] == ["run", "deploy"]]


def test_agent_auto_resolve_is_left_alone_unless_set(deploy):
    res, calls = deploy("agent")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "AUTO_RESOLVE_ON_RECOVERY" not in _env(_deploy_call(calls))

    res, calls = deploy("agent", AUTO_RESOLVE_ON_RECOVERY="true")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _env(_deploy_call(calls))["AUTO_RESOLVE_ON_RECOVERY"] == "true"


def test_agent_rejects_invalid_auto_resolve(deploy):
    res, calls = deploy("agent", AUTO_RESOLVE_ON_RECOVERY="yes")

    assert res.returncode == 1
    assert "AUTO_RESOLVE_ON_RECOVERY must be 'true' or 'false'" in res.stderr
    assert calls == []


def test_bridge_keeps_verifier_sa_unless_exported(deploy):
    res, calls = deploy("bridge")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "VERIFIER_SA" not in _env(_deploy_call(calls))


def test_bridge_passes_a_comma_separated_verifier_sa_intact(deploy):
    sas = "ops@proj-x.iam.gserviceaccount.com,qa@proj-x.iam.gserviceaccount.com"
    res, calls = deploy("bridge", VERIFIER_SA=sas)

    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert env["VERIFIER_SA"] == sas
    assert env["PROJECT_NUMBER"] == PROJECT_NUMBER


def test_bridge_redeploy_sets_its_own_status_url_as_bridge_url(deploy):
    res, calls = deploy("bridge")

    assert res.returncode == 0, res.stdout + res.stderr
    assert _env(_deploy_call(calls))["BRIDGE_URL"] == "https://ata-bridge-hash1234-uw.a.run.app"
    assert not [c for c in calls if c[:3] == ["run", "services", "update"]]


def test_bridge_first_deploy_sets_bridge_url_after_the_deploy(deploy):
    res, calls = deploy("bridge", STUB_DEPLOYED="ata-agent")

    assert res.returncode == 0, res.stdout + res.stderr
    assert _env(_deploy_call(calls))["BRIDGE_URL"] == f"https://ata-bridge-{PROJECT_NUMBER}.europe-west1.run.app"
    updates = [c for c in calls if c[:3] == ["run", "services", "update"]]
    assert len(updates) == 1, calls
    assert _env(updates[0]) == {"BRIDGE_URL": "https://ata-bridge-hash1234-uw.a.run.app"}
    assert calls.index(updates[0]) > calls.index(_deploy_call(calls))
