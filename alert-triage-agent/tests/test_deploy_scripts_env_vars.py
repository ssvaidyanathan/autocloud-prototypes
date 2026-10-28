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
elif args[:3] == ["run", "services", "describe"] and opt("format") == "json":
    env = [{"name": k, "value": v} for k, v in json.loads(os.environ.get("STUB_SERVICE_ENV", "{}")).items()]
    print(json.dumps({"spec": {"template": {"spec": {"containers": [{"env": env}]}}}}))
elif args[:2] == ["builds", "submit"]:
    if os.environ.get("STUB_BUILD_FAIL"):
        sys.exit(1)
    print("BUILD LOG LINE (stdout)")
elif args[:4] == ["artifacts", "docker", "images", "describe"]:
    print(os.environ.get("STUB_DIGEST", "sha256:" + "ab" * 32))
'''


NO_YES = object()


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
    (work / "topology" / "TOPOLOGY.md").write_text("# topology\n")
    for component in ("agent", "bridge", "tools"):
        shutil.copy(ROOT / component / "Dockerfile", work / component / "Dockerfile")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gcloud"
    stub.write_text(STUB_GCLOUD)
    stub.chmod(0o755)
    log = tmp_path / "gcloud.log"

    def run(component, *args, **env_overrides):
        log.write_text("")
        env = {k: v for k, v in os.environ.items()
               if k not in ("PROJECT_ID", "REGION", "APIGEE_ORG", "PROJECT_NUMBER", "VERIFIER_SA", "AUTO_RESOLVE_ON_RECOVERY", "BRIDGE_URL", "AGENT_URL",
                            "SERVICENOW_INSTANCE_URL", "GCP_PROJECT", "GCP_REGION", "NAME_PREFIX", "APIGEE_ENV", "TOOLS_URL",
                            "AGENT_IMAGE", "BRIDGE_IMAGE", "TOOLS_IMAGE", "IMAGE_TAG", "ASSUME_YES", "MODEL_NAME", "GEMINI_LOCATION",
                            "ALLOWED_SERVICE_ACCOUNTS", "ENVIRONMENT", "OWNER", "TOOLS_TIMEOUT_SECONDS",
                            "CHANGE_ONSET_WINDOW_MINUTES", "CHANGE_HEALTHY_PERIOD_MINUTES",
                            "CHANGE_HEALTHY_MIN_ACTIVE_MINUTES", "CHANGE_HEALTHY_MAX_5XX_SHARE", "CHANGE_HEALTHY_MAX_LATENCY_MS",
                            "ALLOW_CUSTOM_SERVICE_URLS")}
        env.update(
            PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
            STUB_LOG=str(log),
            STUB_PROJECT_NUMBER=PROJECT_NUMBER,
            STUB_DEPLOYED="ata-agent ata-bridge ata-tools ata-agent-canary",
            PROJECT_ID="proj-x",
            REGION="europe-west1",
            SERVICENOW_INSTANCE_URL="https://example.service-now.com",
            NAME_PREFIX="ata",
            APIGEE_ENV="eval-x",
            IMAGE_TAG="t1",
        )
        env.update(env_overrides)
        env = {k: v for k, v in env.items() if v is not None}  # None: unset
        # Default --yes; NO_YES: no arguments at all (stdin is not a terminal).
        argv = [] if args == (NO_YES,) else (list(args) if args else ["--yes"])
        res = subprocess.run([TEST_BASH, str(work / component / "scripts" / "deploy.sh"), *argv], cwd=str(work / component),
                             capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)
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
    res, calls = deploy("agent", BRIDGE_URL="https://bridge.example", AGENT_URL="https://agent.example",
                        TOOLS_URL="https://tools.example", ALLOW_CUSTOM_SERVICE_URLS="true")

    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert (env["BRIDGE_URL"], env["AGENT_URL"]) == ("https://bridge.example", "https://agent.example")
    assert not [c for c in calls if c[:3] == ["run", "services", "list"]]


@pytest.mark.parametrize("component,var,url", [
    # Left over in the shell from another stack's deploy (prefix "ata" vs this test's prefix).
    ("agent", "BRIDGE_URL", "https://atat-bridge-abc123-uw.a.run.app"),
    ("agent", "AGENT_URL", "https://atat-agent-123456789012.europe-west1.run.app"),
    ("agent", "TOOLS_URL", "https://atat-tools-abc123-uw.a.run.app"),
    ("bridge", "AGENT_URL", "https://atat-agent-abc123-uw.a.run.app"),
    ("bridge", "BRIDGE_URL", "https://atat-bridge-abc123-uw.a.run.app"),
    # The right prefix but the wrong service, and a custom domain without the override.
    ("agent", "BRIDGE_URL", "https://ata-agent-abc123-uw.a.run.app"),
    ("bridge", "AGENT_URL", "https://agent.custom.example"),
])
def test_exported_url_of_another_stack_or_service_stops_the_deploy(deploy, component, var, url):
    """sec L9: exported URLs must be ${NAME_PREFIX}-<svc>; ALLOW_CUSTOM_SERVICE_URLS=true for custom domains."""
    res, calls = deploy(component, **{var: url})
    assert res.returncode != 0
    assert f"{var}={url} is not the ata-" in res.stderr and "ALLOW_CUSTOM_SERVICE_URLS=true" in res.stderr
    assert not [c for c in calls if c[:2] == ["run", "deploy"]]


def test_own_stack_urls_pass_the_prefix_check(deploy):
    res, calls = deploy("agent", BRIDGE_URL="https://ata-bridge-abc123-uw.a.run.app",
                        AGENT_URL="https://ata-agent-123456789012.europe-west1.run.app",
                        TOOLS_URL="https://ata-tools-abc123-uw.a.run.app")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ALLOW_CUSTOM_SERVICE_URLS" not in res.stderr


def test_custom_domain_needs_the_explicit_override_and_says_so(deploy):
    res, calls = deploy("bridge", BRIDGE_URL="https://bridge.custom.example", ALLOW_CUSTOM_SERVICE_URLS="true")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "accepted because ALLOW_CUSTOM_SERVICE_URLS=true" in res.stderr


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


@pytest.mark.parametrize("component", ["agent", "bridge", "tools"])
@pytest.mark.parametrize("unset", ["PROJECT_ID", "REGION"])
def test_project_and_region_are_required(deploy, component, unset):
    res, calls = deploy(component, **{unset: None})

    assert res.returncode == 1
    assert "PROJECT_ID (or GCP_PROJECT) and REGION (or GCP_REGION) are required" in res.stderr
    assert calls == []


def test_gcp_project_and_region_aliases_still_work(deploy):
    res, calls = deploy("tools", PROJECT_ID=None, REGION=None, GCP_PROJECT="proj-y", GCP_REGION="asia-east1")

    assert res.returncode == 0, res.stdout + res.stderr
    call = _deploy_call(calls)
    assert (_opt(call, "project"), _opt(call, "region")) == ("proj-y", "asia-east1")
    assert _env(call)["APIGEE_ORG"] == "proj-y"


def test_tools_apigee_org_can_be_set(deploy):
    res, calls = deploy("tools", APIGEE_ORG="org-z")

    assert res.returncode == 0, res.stdout + res.stderr
    assert _env(_deploy_call(calls))["APIGEE_ORG"] == "org-z"


def test_agent_sets_timer_queue_location_and_oidc_sa_from_prefix_project_and_region(deploy):
    res, calls = deploy("agent", NAME_PREFIX="abc")

    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert env["LOCATION"] == "europe-west1"
    assert env["TIMER_QUEUE_NAME"] == "abc-timer-queue"
    assert env["SERVICE_ACCOUNT_EMAIL"] == "abc-agent-sa@proj-x.iam.gserviceaccount.com"


def test_bridge_ignores_tfvars_and_requires_servicenow_url_when_not_deployed(deploy, tmp_path):
    """No terraform.tfvars parse any more: the env var, else the deployed service's value."""
    tfvars = tmp_path / "ata" / "infra" / "terraform" / "foundation" / "terraform.tfvars"
    tfvars.parent.mkdir(parents=True)
    tfvars.write_text('servicenow_instance_url = "https://dev1.service-now.com"\n')

    res, calls = deploy("bridge", SERVICENOW_INSTANCE_URL=None, STUB_DEPLOYED="ata-agent")

    assert res.returncode == 1
    assert "SERVICENOW_INSTANCE_URL is required" in res.stderr
    assert not [c for c in calls if c[:2] in (["run", "deploy"], ["builds", "submit"])]
    text = (ROOT / "bridge" / "scripts" / "deploy.sh").read_text()
    assert "terraform.tfvars" not in text and "\\s" not in text


def test_bridge_keeps_the_deployed_servicenow_url_with_a_log_line(deploy):
    res, calls = deploy("bridge", SERVICENOW_INSTANCE_URL=None,
                        STUB_SERVICE_ENV=json.dumps({"SERVICENOW_INSTANCE_URL": "https://dev9.service-now.com"}))

    assert res.returncode == 0, res.stdout + res.stderr
    assert _env(_deploy_call(calls))["SERVICENOW_INSTANCE_URL"] == "https://dev9.service-now.com"
    assert "keeping the value on the deployed ata-bridge: https://dev9.service-now.com" in res.stdout


@pytest.mark.parametrize("component", ["agent", "bridge", "tools"])
def test_is_local_false_is_always_asserted(deploy, component):
    res, calls = deploy(component)

    assert res.returncode == 0, res.stdout + res.stderr
    assert _env(_deploy_call(calls))["IS_LOCAL"] == "false"


def test_bridge_verifier_sa_none_removes_it(deploy):
    res, calls = deploy("bridge", VERIFIER_SA="none")

    assert res.returncode == 0, res.stdout + res.stderr
    call = _deploy_call(calls)
    assert "VERIFIER_SA" not in _env(call)
    assert "--remove-env-vars=VERIFIER_SA" in call


def test_bridge_exported_bridge_url_is_never_replaced_after_the_deploy(deploy):
    res, calls = deploy("bridge", BRIDGE_URL="https://bridge.custom.example", ALLOW_CUSTOM_SERVICE_URLS="true")

    assert res.returncode == 0, res.stdout + res.stderr
    assert _env(_deploy_call(calls))["BRIDGE_URL"] == "https://bridge.custom.example"
    assert not [c for c in calls if c[:3] == ["run", "services", "update"]]


def test_agent_sets_tools_url_and_passes_optional_tuning_only_when_exported(deploy):
    res, calls = deploy("agent", STUB_DEPLOYED="ata-agent ata-bridge")
    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert env["TOOLS_URL"] == f"https://ata-tools-{PROJECT_NUMBER}.europe-west1.run.app"
    for name in ("MODEL_NAME", "GEMINI_LOCATION", "TOOLS_TIMEOUT_SECONDS"):
        assert name not in env

    res, calls = deploy("agent", MODEL_NAME="gemini-x", GEMINI_LOCATION="global", TOOLS_TIMEOUT_SECONDS="20")
    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert (env["MODEL_NAME"], env["GEMINI_LOCATION"], env["TOOLS_TIMEOUT_SECONDS"]) == ("gemini-x", "global", "20")
    assert env["TOOLS_URL"] == "https://ata-tools-hash1234-uw.a.run.app"


def test_agent_passes_change_attribution_tuning_only_when_exported(deploy):
    res, calls = deploy("agent", STUB_DEPLOYED="ata-agent ata-bridge")
    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert "CHANGE_ONSET_WINDOW_MINUTES" not in env
    assert "CHANGE_HEALTHY_PERIOD_MINUTES" not in env

    res, calls = deploy("agent", STUB_DEPLOYED="ata-agent ata-bridge", CHANGE_ONSET_WINDOW_MINUTES="20")
    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert env["CHANGE_ONSET_WINDOW_MINUTES"] == "20"
    assert "CHANGE_HEALTHY_PERIOD_MINUTES" not in env

    res, calls = deploy("agent", STUB_DEPLOYED="ata-agent ata-bridge",
                        CHANGE_ONSET_WINDOW_MINUTES="20", CHANGE_HEALTHY_PERIOD_MINUTES="5")
    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert (env["CHANGE_ONSET_WINDOW_MINUTES"], env["CHANGE_HEALTHY_PERIOD_MINUTES"]) == ("20", "5")
    assert "CHANGE_HEALTHY_MAX_5XX_SHARE" not in env and "CHANGE_HEALTHY_MAX_LATENCY_MS" not in env

    res, calls = deploy("agent", STUB_DEPLOYED="ata-agent ata-bridge",
                        CHANGE_HEALTHY_MAX_5XX_SHARE="0.02", CHANGE_HEALTHY_MAX_LATENCY_MS="1500")
    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert (env["CHANGE_HEALTHY_MAX_5XX_SHARE"], env["CHANGE_HEALTHY_MAX_LATENCY_MS"]) == ("0.02", "1500")
    assert "CHANGE_ONSET_WINDOW_MINUTES" not in env
    assert "CHANGE_HEALTHY_MIN_ACTIVE_MINUTES" not in env

    res, calls = deploy("agent", STUB_DEPLOYED="ata-agent ata-bridge", CHANGE_HEALTHY_MIN_ACTIVE_MINUTES="5")
    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert env["CHANGE_HEALTHY_MIN_ACTIVE_MINUTES"] == "5"
    assert "CHANGE_HEALTHY_PERIOD_MINUTES" not in env


def test_agent_no_longer_grants_invoker_bindings(deploy):
    res, calls = deploy("agent")

    assert res.returncode == 0, res.stdout + res.stderr
    assert not [c for c in calls if "add-iam-policy-binding" in c]
    assert "add-iam-policy-binding" not in (ROOT / "agent" / "scripts" / "deploy.sh").read_text()


def test_tools_sets_its_auth_env(deploy):
    res, calls = deploy("tools")

    assert res.returncode == 0, res.stdout + res.stderr
    env = _env(_deploy_call(calls))
    assert env["ENFORCE_AUTH"] == "true" and env["VERIFY_OIDC"] == "true"
    assert env["PROJECT_NUMBER"] == PROJECT_NUMBER
    assert env["APIGEE_ENV"] == "eval-x"
    # The same URL the agent resolves as TOOLS_URL (here: the deployed status.url).
    assert env["EXPECTED_AUDIENCE"] == "https://ata-tools-hash1234-uw.a.run.app"
    assert "ALLOWED_SERVICE_ACCOUNTS" not in env

    res, calls = deploy("tools", ALLOWED_SERVICE_ACCOUNTS="a@proj-x.iam.gserviceaccount.com")
    assert _env(_deploy_call(calls))["ALLOWED_SERVICE_ACCOUNTS"] == "a@proj-x.iam.gserviceaccount.com"


def test_tools_first_deploy_moves_expected_audience_to_the_status_url(deploy):
    res, calls = deploy("tools", STUB_DEPLOYED="ata-agent")

    assert res.returncode == 0, res.stdout + res.stderr
    assert _env(_deploy_call(calls))["EXPECTED_AUDIENCE"] == f"https://ata-tools-{PROJECT_NUMBER}.europe-west1.run.app"
    updates = [c for c in calls if c[:3] == ["run", "services", "update"]]
    assert len(updates) == 1 and _env(updates[0]) == {"EXPECTED_AUDIENCE": "https://ata-tools-hash1234-uw.a.run.app"}


def test_tools_requires_apigee_env(deploy):
    res, calls = deploy("tools", APIGEE_ENV=None)

    assert res.returncode == 1
    assert "APIGEE_ENV" in res.stderr
    assert calls == []


@pytest.mark.parametrize("component", ["agent", "bridge", "tools"])
def test_name_prefix_is_required(deploy, component):
    res, calls = deploy(component, NAME_PREFIX=None)

    assert res.returncode == 1
    assert "NAME_PREFIX is required" in res.stderr
    assert calls == []


@pytest.mark.parametrize("component", ["agent", "bridge", "tools"])
def test_build_uses_the_build_sa_and_deploys_by_digest_with_stack_label(deploy, component):
    res, calls = deploy(component, NAME_PREFIX="atat", STUB_DEPLOYED="atat-agent atat-bridge atat-tools")

    assert res.returncode == 0, res.stdout + res.stderr
    builds = [c for c in calls if c[:2] == ["builds", "submit"]]
    assert len(builds) == 1, calls
    b = builds[0]
    assert _opt(b, "service-account") == "projects/proj-x/serviceAccounts/atat-build-sa@proj-x.iam.gserviceaccount.com"
    assert _opt(b, "gcs-source-staging-dir") == "gs://proj-x-atat-build/source"
    assert _opt(b, "gcs-log-dir") == "gs://proj-x-atat-build/logs"
    assert _opt(b, "tag") == f"europe-west1-docker.pkg.dev/proj-x/atat-docker/atat-{component}:t1"
    call = _deploy_call(calls)
    assert _opt(call, "image") == f"europe-west1-docker.pkg.dev/proj-x/atat-docker/atat-{component}@sha256:" + "ab" * 32
    labels = dict(kv.split("=", 1) for kv in _opt(call, "labels").split(","))
    assert labels["stack"] == "atat" and labels["component"] == f"atat-{component}"
    assert labels["managed-by"] == "script"


@pytest.mark.parametrize("component", ["agent", "bridge", "tools"])
def test_prebuilt_image_skips_the_build(deploy, component):
    image = f"europe-west1-docker.pkg.dev/proj-x/ata-docker/ata-{component}@sha256:" + "cd" * 32
    res, calls = deploy(component, **{f"{component.upper()}_IMAGE": image})

    assert res.returncode == 0, res.stdout + res.stderr
    assert not [c for c in calls if c[:2] == ["builds", "submit"]]
    assert _opt(_deploy_call(calls), "image") == image


def test_prebuilt_image_of_another_repo_is_refused(deploy):
    res, calls = deploy("agent", AGENT_IMAGE="us-docker.pkg.dev/other/repo/ata-agent@sha256:" + "cd" * 32)

    assert res.returncode != 0
    assert "is not europe-west1-docker.pkg.dev/proj-x/ata-docker/ata-agent" in res.stderr
    assert not [c for c in calls if c[:2] == ["run", "deploy"]]


def test_bad_digest_from_artifact_registry_stops_the_deploy(deploy):
    res, calls = deploy("agent", STUB_DIGEST="latest")

    assert res.returncode != 0
    assert "unexpected digest" in res.stderr
    assert not [c for c in calls if c[:2] == ["run", "deploy"]]


@pytest.mark.parametrize("component", ["agent", "bridge", "tools"])
def test_without_yes_and_without_a_terminal_nothing_is_changed(deploy, component):
    res, calls = deploy(component, NO_YES)

    assert res.returncode == 1
    assert "re-run with --yes" in res.stderr
    assert not [c for c in calls if c[:2] in (["run", "deploy"], ["builds", "submit"])]
