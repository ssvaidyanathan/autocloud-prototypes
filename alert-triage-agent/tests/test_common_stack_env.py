"""scripts/scenarios/common.sh: the stack identity is required (no live defaults, audit gap
36), every name derives from NAME_PREFIX (gap 37), Apigee host/org/env come from
.${NAME_PREFIX}-apigee.env or the environment with no default (gap 29), and a service URL
that cannot be read fails loudly (no fixed fallback URL, gap 28)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))
PIN = "PIN_INVENTORY_REVISION={p}-inventory-api-00001-abc\nPIN_INVENTORY_DIGEST=sha256:" + "ab" * 32 + "\n"


@pytest.fixture
def pkg(tmp_path):
    """A copy of scripts/ under <tmp>/pkg/alert-triage-agent, so the stack files
    (.<prefix>-apigee.env, .<prefix>-api-key) are written next to it, not into the checkout."""
    root = tmp_path / "pkg" / "alert-triage-agent"
    shutil.copytree(ROOT / "scripts", root / "scripts", ignore=shutil.ignore_patterns("inventory-pin.*.env"))
    for p in ("ata", "atat"):
        (root / "scripts" / "scenarios" / f"inventory-pin.{p}.env").write_text(PIN.format(p=p))
    return root


def source(pkg, body, gcloud=None, **env_over):
    bin_dir = pkg.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    if gcloud is not None:
        stub = bin_dir / "gcloud"
        stub.write_text("#!/bin/sh\n" + gcloud + "\n")
        stub.chmod(0o755)
    script = pkg.parent / "probe.sh"
    script.write_text(f'source "{pkg}/scripts/scenarios/common.sh"\n{body}\n')
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PINNED_INVENTORY_", "ATA_", "APIGEE_", "GCP_"))
           and k not in ("PROJECT_ID", "REGION", "NAME_PREFIX", "MOCK_MODE", "DRY_RUN")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", PROJECT_ID="test-project", REGION="test-region1",
               NAME_PREFIX="atat")
    for k, v in env_over.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run([TEST_BASH, str(script)], cwd=str(pkg.parent), capture_output=True, text=True,
                          env=env, timeout=30, check=False)


@pytest.mark.parametrize("missing", [["PROJECT_ID"], ["REGION"], ["NAME_PREFIX"], ["PROJECT_ID", "REGION", "NAME_PREFIX"]])
def test_stack_identity_is_required_without_defaults(pkg, missing):
    res = source(pkg, "echo REACHED", **{k: None for k in missing})
    assert res.returncode != 0 and "REACHED" not in res.stdout
    assert "required environment not set: " + " ".join(missing) in res.stderr
    assert "no defaults" in res.stderr


def test_gcp_aliases_are_explicit_inputs(pkg):
    res = source(pkg, 'echo "$PROJECT_ID $REGION"', PROJECT_ID=None, REGION=None, GCP_PROJECT="p2", GCP_REGION="r2")
    assert (res.returncode, res.stdout) == (0, "p2 r2\n"), res.stderr


@pytest.mark.parametrize("prefix", ["Ata", "a", "abcdefghijk", "1ab", "at-a", "at_a", "ata "])
def test_invalid_name_prefix_fails(pkg, prefix):
    res = source(pkg, "echo REACHED", NAME_PREFIX=prefix)
    assert res.returncode != 0 and "must match [a-z][a-z0-9]{1,9}" in res.stderr


def test_every_name_derives_from_the_prefix(pkg):
    names = ("SVC_INVENTORY SVC_ORDERS SVC_AGENT SVC_BRIDGE SVC_TOOLS DB_INSTANCE DB_CONNECTION_NAME "
             "INVENTORY_SA_EMAIL APIGEE_SA_EMAIL ARTIFACT_REPO BUILD_SA_EMAIL BUILD_BUCKET PROXY_ORDERS "
             "PROXY_INVENTORY BASEPATH_ORDERS BASEPATH_INVENTORY TARGET_ORDERS TARGET_INVENTORY "
             "APIGEE_PRODUCT APIGEE_APP APIGEE_DEVELOPER APIGEE_ACCESS_LOG POLICY_INVENTORY_5XX "
             "POLICY_INVENTORY_LATENCY POLICY_ORDERS_5XX POLICY_ORDERS_LATENCY POLICY_APIGEE_ORDERS_5XX "
             "POLICY_APIGEE_ORDERS_429 POLICY_CLOUDSQL_CONNECTIONS POLICY_CLOUDSQL_CPU INCIDENTS_BUCKET").split()
    res = source(pkg, "\n".join(f'echo "{n}=${{{n}}}"' for n in names))
    assert res.returncode == 0, res.stderr
    got = dict(line.split("=", 1) for line in res.stdout.splitlines())
    assert got == {
        "SVC_INVENTORY": "atat-inventory-api", "SVC_ORDERS": "atat-orders-api", "SVC_AGENT": "atat-agent",
        "SVC_BRIDGE": "atat-bridge", "SVC_TOOLS": "atat-tools", "DB_INSTANCE": "atat-inventory-db",
        "DB_CONNECTION_NAME": "test-project:test-region1:atat-inventory-db",
        "INVENTORY_SA_EMAIL": "atat-inventory-sa@test-project.iam.gserviceaccount.com",
        "APIGEE_SA_EMAIL": "atat-apigee-sa@test-project.iam.gserviceaccount.com",
        "ARTIFACT_REPO": "test-region1-docker.pkg.dev/test-project/atat-docker",
        "BUILD_SA_EMAIL": "atat-build-sa@test-project.iam.gserviceaccount.com",
        "BUILD_BUCKET": "gs://test-project-atat-build",
        "PROXY_ORDERS": "atat-orders-v1", "PROXY_INVENTORY": "atat-inventory-v1",
        "BASEPATH_ORDERS": "/atat/orders/v1", "BASEPATH_INVENTORY": "/atat/inventory/v1",
        "TARGET_ORDERS": "atat-orders-target", "TARGET_INVENTORY": "atat-inventory-target",
        "APIGEE_PRODUCT": "atat-product", "APIGEE_APP": "atat-demo-app", "APIGEE_DEVELOPER": "atat-dev@example.com",
        "APIGEE_ACCESS_LOG": "atat-apigee-access",
        "POLICY_INVENTORY_5XX": "atat-inventory-5xx", "POLICY_INVENTORY_LATENCY": "atat-inventory-latency",
        "POLICY_ORDERS_5XX": "atat-orders-5xx", "POLICY_ORDERS_LATENCY": "atat-orders-latency",
        "POLICY_APIGEE_ORDERS_5XX": "atat-apigee-orders-5xx", "POLICY_APIGEE_ORDERS_429": "atat-apigee-orders-429",
        "POLICY_CLOUDSQL_CONNECTIONS": "atat-cloudsql-connections-critical",
        "POLICY_CLOUDSQL_CPU": "atat-cloudsql-cpu-high", "INCIDENTS_BUCKET": "test-project-atat-incidents",
    }


def test_policy_names_match_the_observability_terraform():
    """Every POLICY_* in common.sh is a display name declared in observability/main.tf."""
    tf = (ROOT / "infra" / "terraform" / "observability" / "main.tf").read_text()
    common = (ROOT / "scripts" / "scenarios" / "common.sh").read_text()
    import re
    suffixes = re.findall(r'^export POLICY_[A-Z0-9_]+="\$\{NAME_PREFIX\}-([a-z0-9-]+)"', common, re.M)
    assert len(suffixes) == 8
    for suffix in suffixes:
        assert f'display_name          = "${{var.name_prefix}}-{suffix}"' in tf, suffix


# --- Apigee host / org / env ------------------------------------------------------------

APIGEE = 'echo "host=${APIGEE_HOST} org=${APIGEE_ORG} env=${APIGEE_ENV}"'


def test_apigee_values_come_from_the_stack_env_file(pkg):
    (pkg / ".atat-apigee.env").write_text("APIGEE_HOST=10.0.0.1.nip.io\nAPIGEE_ORG=org-x\nAPIGEE_ENV=test\n")
    (pkg / ".ata-apigee.env").write_text("APIGEE_HOST=other\nAPIGEE_ORG=other\nAPIGEE_ENV=other\n")
    res = source(pkg, APIGEE)
    assert (res.returncode, res.stdout) == (0, "host=10.0.0.1.nip.io org=org-x env=test\n"), res.stderr


def test_exported_values_work_without_a_file(pkg):
    res = source(pkg, APIGEE, APIGEE_HOST="h", APIGEE_ORG="o", APIGEE_ENV="e")
    assert (res.returncode, res.stdout) == (0, "host=h org=o env=e\n"), res.stderr


def test_no_apigee_default(pkg):
    res = source(pkg, APIGEE)
    assert (res.returncode, res.stdout) == (0, "host= org= env=\n"), res.stderr
    assert "nip.io" not in (ROOT / "scripts" / "scenarios" / "common.sh").read_text()


def test_exported_value_that_contradicts_the_file_fails(pkg):
    (pkg / ".atat-apigee.env").write_text("APIGEE_HOST=file-host\nAPIGEE_ORG=o\nAPIGEE_ENV=e\n")
    res = source(pkg, "echo REACHED", APIGEE_HOST="exported-host")
    assert res.returncode != 0 and "REACHED" not in res.stdout
    assert "APIGEE_HOST is exported as 'exported-host' but" in res.stderr and "says 'file-host'" in res.stderr


def test_env_file_is_parsed_not_sourced(pkg, tmp_path):
    marker = tmp_path / "pwned"
    (pkg / ".atat-apigee.env").write_text(f"APIGEE_HOST=h\ntouch {marker}\n")
    res = source(pkg, "echo REACHED")
    assert res.returncode != 0 and "unexpected line" in res.stderr and not marker.exists()
    (pkg / ".atat-apigee.env").write_text("APIGEE_HOST=$(touch x)\n")
    res = source(pkg, "echo REACHED")
    assert res.returncode != 0 and "unexpected characters" in res.stderr


def test_require_apigee_host_is_loud_live_and_a_logged_placeholder_in_mock(pkg):
    res = source(pkg, "require_apigee_host && echo OK")
    assert res.returncode != 0 and "APIGEE_HOST is not set" in res.stderr
    assert "deploy-apigee-proxies.sh" in res.stderr
    res = source(pkg, 'require_apigee_host && echo "OK $APIGEE_HOST"', MOCK_MODE="true")
    assert res.stdout == "OK apigee.mock.invalid\n"
    assert "placeholder apigee.mock.invalid" in res.stderr


def test_require_apigee_mgmt_names_what_is_missing(pkg):
    res = source(pkg, "require_apigee_mgmt && echo OK", APIGEE_ORG="o")
    assert res.returncode != 0 and "Not set: APIGEE_ENV" in res.stderr
    res = source(pkg, "require_apigee_mgmt && echo OK", DRY_RUN="true")
    assert res.stdout == "OK\n"


# --- service URLs: no fallback ------------------------------------------------------------


def test_service_url_without_describe_fails_loudly(pkg):
    body = 'require_service_url atat-inventory-api https://ata-inventory-api-4klvlqnmpa-uw.a.run.app && echo "NO FAILURE"'
    res = source(pkg, body, gcloud="exit 1")
    assert res.returncode != 0 and "NO FAILURE" not in res.stdout and "4klvlqnmpa" not in res.stdout
    assert "Could not read the URL of Cloud Run service atat-inventory-api in test-project/test-region1" in res.stderr


def test_resolve_service_url_shim_is_gone(pkg):
    """The fallback-era name had no caller left after the scripts and scen merges."""
    res = source(pkg, 'type resolve_service_url >/dev/null 2>&1 && echo DEFINED || echo GONE')
    assert res.stdout == "GONE\n"
    callers = [p for p in (ROOT / "scripts").rglob("*.sh") if "resolve_service_url" in p.read_text()]
    callers += [p for top in ("agent", "bridge", "tools", "demo") for p in (ROOT / top).rglob("*.sh")
                if "resolve_service_url" in p.read_text()]
    assert callers == []


def test_service_url_is_the_described_one(pkg):
    res = source(pkg, "require_service_url atat-inventory-api", gcloud='echo "https://described.example"')
    assert (res.returncode, res.stdout) == (0, "https://described.example\n")


@pytest.mark.parametrize("mode", ["MOCK_MODE", "DRY_RUN"])
def test_mock_and_dry_run_use_a_logged_placeholder(pkg, mode):
    res = source(pkg, "require_service_url atat-inventory-api", gcloud="exit 1", **{mode: "true"})
    assert (res.returncode, res.stdout) == (0, "https://atat-inventory-api.mock.invalid\n")
    assert "placeholder URL https://atat-inventory-api.mock.invalid" in res.stderr


def test_no_fixed_service_url_or_project_in_lane_scripts():
    files = [*(ROOT / "scripts" / "scenarios").glob("*.sh"), ROOT / "scripts" / "reset-scenarios.sh",
             ROOT / "scripts" / "repin-inventory.sh", ROOT / "scripts" / "prune-revisions.sh",
             ROOT / "scripts" / "set-demo-instance-caps.sh", ROOT / "demo" / "ata-inventory-api" / "scripts" / "deploy.sh"]
    for f in files:
        text = f.read_text()
        for literal in ("4klvlqnmpa", "cloud-assist-fde-2", "us-west1", "nip.io", "/ata/", "dev377204"):
            assert literal not in text, (f, literal)
