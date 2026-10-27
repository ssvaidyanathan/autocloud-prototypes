"""Stack isolation: with NAME_PREFIX=atat, no command of the scenario, preflight, result,
reset, re-pin, caps or inventory deploy scripts may name an `ata-` resource or the `/ata/`
Apigee base path (fresh-deploy audit gaps 28/37, fresh-inv Q4 I7).

Every gcloud and curl invocation is recorded by stubs (one JSON argv line per call). After
removing the stack's own `atat-` / `/atat/` names, no invocation and no line of output may
still contain `ata-` (as a name start) or `/ata/`. The dry-run and mock paths of every
scenario run, and so do the live paths of S1-S4, preflight and the inventory deploy (stubbed:
nothing leaves this machine).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.cloud_run_fake import FakeCloud, NEW_DIGEST, for_prefix, fresh_project_state, live_like_state, write_pin

ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / "scripts"
SCENARIOS = SCRIPTS / "scenarios"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))

PREFIX = "atat"
PIN_REV = f"{PREFIX}-inventory-api-00001-abc"
PIN_DIGEST = "sha256:" + "cd" * 32

# A name of the other stack: `ata-` at a name start, or the `/ata/` base path. `atat-`,
# `/atat/` and words that merely end in "ata" (e.g. "data-") are not matches.
OTHER_STACK = re.compile(r"(?<![A-Za-z0-9])ata-|/ata/")

STUB = r'''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).stem  # gcloud.py / curl.py
args = sys.argv[1:]
with open(os.environ["ISO_CALLS"], "a") as log:
    log.write(json.dumps([name] + args) + "\n")
joined = " ".join(args)

def opt(flag):
    for a in args:
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None

if name == "gcloud":
    fmt = opt("--format") or ""
    if args[:2] == ["auth", "print-identity-token"] or "print-access-token" in args:
        print("tok")
    elif args[:3] == ["run", "services", "describe"]:
        svc = args[3]
        if fmt == "value(status.url)":
            print(f"https://{svc}.run.example")
        elif fmt == "value(status.latestReadyRevisionName)":
            print(f"{svc}-00002-new")
        elif fmt.startswith("csv"):
            print(os.environ["ISO_PIN_REV"] + ",100")
        elif fmt == "json":
            print(json.dumps({"metadata": {"name": svc, "annotations": {"run.googleapis.com/maxScale": "1"}},
                              "spec": {"template": {"metadata": {"annotations": {}}, "spec": {"containers": [{"image": "x", "env": []}]}}},
                              "status": {"url": f"https://{svc}.run.example", "traffic": []}}))
    elif args[:3] == ["run", "revisions", "describe"]:
        if fmt == "value(status.imageDigest)":
            print("img@" + os.environ["ISO_PIN_DIGEST"])
        elif fmt == "value(metadata.name)":
            print(args[3])
        else:
            print("{}")
    elif args[:2] == ["builds", "submit"]:
        print(os.environ["ISO_BUILD_DIGEST"])
    elif args[:3] == ["monitoring", "policies", "list"]:
        print("[]")
    elif args[:3] == ["sql", "instances", "describe"]:
        print("RUNNABLE")
    elif args[:3] == ["artifacts", "docker", "images"]:
        ar = os.environ.get("ISO_AR", "notfound")
        if ar == "found":
            print(args[4].split("@")[-1])
        elif ar == "notfound":
            print("ERROR: (gcloud.artifacts.docker.images.describe) NOT_FOUND: image not found", file=sys.stderr)
            sys.exit(1)
        else:
            print("ERROR: (gcloud.artifacts.docker.images.describe) PERMISSION_DENIED: denied", file=sys.stderr)
            sys.exit(1)
    sys.exit(0)

# curl
out = fmt = url = None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-w", "-X", "-d", "-H", "-F", "--connect-timeout", "--max-time", "--data"):
        if a == "-o":
            out = args[i + 1]
        elif a == "-w":
            fmt = args[i + 1]
        i += 2
        continue
    if a.startswith("http"):
        url = a
    i += 1
if url and url.endswith("/items"):
    body = json.dumps([{"id": "item-105", "stock": 900}])
elif url and "/deployments" in url and "/revisions/" not in url:
    body = json.dumps({"deployments": [{"revision": "3"}]})
elif url and "action=import" in url:
    body = json.dumps({"revision": "4"})
else:
    body = json.dumps({"status": "ok"})
if out and out != "/dev/null":
    Path(out).write_text(body)
elif not out and fmt is None:
    sys.stdout.write(body)
if fmt:
    sys.stdout.write(fmt.replace("%{http_code}", "200").replace("%{time_total}", "0.010"))
'''


@pytest.fixture
def iso(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("gcloud", "curl"):
        # One stub; it tells gcloud from curl by its file name.
        (bin_dir / f"{name}.py").write_text(STUB)
        exe = bin_dir / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{bin_dir}/{name}.py" "$@"\n')
        exe.chmod(0o755)
    # No sleep in the traffic loops of this test.
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    pin = tmp_path / f"inventory-pin.{PREFIX}.env"
    write_pin(pin, PIN_REV, PIN_DIGEST)
    calls = tmp_path / "calls.jsonl"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PINNED_INVENTORY_", "ATA_", "APIGEE_")) and k not in ("MOCK_MODE", "DRY_RUN")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", NAME_PREFIX=PREFIX, PROJECT_ID="test-project",
               REGION="test-region1", ATA_INVENTORY_PIN_FILE=str(pin), ISO_CALLS=str(calls),
               ISO_PIN_REV=PIN_REV, ISO_PIN_DIGEST=PIN_DIGEST, ISO_BUILD_DIGEST=NEW_DIGEST,
               TMPDIR=str(tmp_path), RUN_PREFLIGHT="false")
    return {"env": env, "calls": calls, "tmp": tmp_path}


def run(iso, argv, timeout=120, **extra):
    env = dict(iso["env"], **extra)
    return subprocess.run([TEST_BASH, *map(str, argv)], cwd=str(iso["tmp"]), env=env, capture_output=True,
                          text=True, timeout=timeout, check=False)


def recorded(iso):
    path = iso["calls"]
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def scrub(text):
    """Local paths in this checkout are not cloud resources: demo/ata-inventory-api is the
    inventory app's source directory (a repo path, the same for every stack)."""
    return text.replace(f"{ROOT}/demo/ata-inventory-api", "<repo>/demo/<inventory app>")


def assert_isolated(iso, res):
    calls = recorded(iso)
    leaks = [c for c in calls if OTHER_STACK.search(scrub(" ".join(c)))]
    assert not leaks, f"invocations naming the ata stack: {leaks}"
    out = [line for line in (res.stdout + res.stderr).splitlines() if OTHER_STACK.search(scrub(line))]
    assert not out, f"output naming the ata stack: {out}"
    return calls


SCENARIO_SCRIPTS = ["s1_backend_5xx.sh", "s2_backend_latency.sh", "s3_concurrent_disjoint.sh",
                    "s4_db_connection_pool.sh", "s5_bad_deploy.sh"]


@pytest.mark.parametrize("mode", ["--dry-run", "--mock"])
@pytest.mark.parametrize("script", SCENARIO_SCRIPTS)
def test_scenario_dry_run_and_mock_name_only_the_stack(iso, script, mode):
    res = run(iso, [SCENARIOS / script, mode, "--hold", "0s"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert_isolated(iso, res)
    # The derived names are what the scenario talks about.
    assert f"{PREFIX}-inventory-api" in res.stdout + res.stderr


@pytest.mark.parametrize("argv", [
    [SCENARIOS / "preflight.sh"],
    [SCENARIOS / "check-result.sh", "--dry-run", "--scenario", "s1"],
    [SCRIPTS / "reset-scenarios.sh", "--mock"],
    [SCRIPTS / "reset-scenarios.sh", "--dry-run"],
])
def test_preflight_check_result_and_reset_mock_name_only_the_stack(iso, argv):
    res = run(iso, argv, MOCK_MODE="true" if "preflight" in str(argv[0]) else "false")
    assert res.returncode == 0, res.stdout + res.stderr
    assert_isolated(iso, res)


def test_check_result_expects_the_stack_names(iso):
    res = run(iso, [SCENARIOS / "check-result.sh", "--dry-run", "--scenario", "s4"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Expected Alerts:  atat-cloudsql-connections-critical" in res.stdout
    assert "Expected 'atat-inventory-db'" in res.stdout


# --- live paths, stubbed --------------------------------------------------------------

LIVE_ENV = {"ATA_API_KEY": "test-key", "APIGEE_HOST": "apigee.test", "APIGEE_ORG": "test-org",
            "APIGEE_ENV": "test-env", "MOCK_MODE": "false", "DRY_RUN": "false"}


@pytest.mark.parametrize("script", ["s1_backend_5xx.sh", "s2_backend_latency.sh", "s3_concurrent_disjoint.sh",
                                    "s4_db_connection_pool.sh"])
def test_live_scenario_invocations_name_only_the_stack(iso, script):
    res = run(iso, [SCENARIOS / script, "--hold", "1s"], **LIVE_ENV)
    assert res.returncode == 0, res.stdout + res.stderr
    calls = assert_isolated(iso, res)
    flat = [" ".join(c) for c in calls]
    # The stack's own service and Apigee base paths are what is called.
    assert any(c.startswith(f"gcloud run services describe {PREFIX}-inventory-api") for c in flat), flat
    assert any("https://apigee.test/atat/inventory/v1/items" in c for c in flat), flat
    if script == "s3_concurrent_disjoint.sh":
        assert any("organizations/test-org/apis?name=atat-orders-v1&action=import" in c for c in flat), flat
        assert any("environments/test-env/apis/atat-orders-v1/revisions/4/deployments" in c
                   and "atat-apigee-sa@test-project.iam.gserviceaccount.com" in c for c in flat), flat


def test_live_s3_imports_the_bundle_rendered_for_the_stack(iso, tmp_path):
    """The strict bundle S3 imports is the stack's own proxy and base path."""
    res = run(iso, [SCENARIOS / "s3_concurrent_disjoint.sh", "--hold", "0s"], **LIVE_ENV)
    assert res.returncode == 0, res.stdout + res.stderr
    imports = [c for c in recorded(iso) if any("action=import" in a for a in c)]
    assert len(imports) == 1
    # The zip is removed after the import; render the same template to check what S3 sends.
    out = tmp_path / "render"
    subprocess.run([TEST_BASH, str(ROOT / "demo" / "apigee" / "render-bundle.sh"), "orders-v1", PREFIX, str(out)],
                   check=True, capture_output=True, text=True)
    text = (out / "apiproxy" / "proxies" / "default.xml").read_text()
    assert "<BasePath>/atat/orders/v1</BasePath>" in text


def test_live_preflight_invocations_name_only_the_stack(iso):
    res = run(iso, [SCENARIOS / "preflight.sh"], **LIVE_ENV)
    # The stubbed cloud is not a healthy stack (no policies, no baseline): failures are fine.
    calls = assert_isolated(iso, res)
    flat = [" ".join(c) for c in calls]
    assert any("https://apigee.test/atat/orders/v1/health" in c for c in flat), flat
    assert any("organizations/test-org/environments/test-env/apis/atat-orders-v1/deployments" in c for c in flat), flat
    assert any(c.startswith("gcloud sql instances describe atat-inventory-db") for c in flat), flat
    for policy in ("atat-inventory-5xx", "atat-cloudsql-connections-critical"):
        assert f"Policy {policy} not found" in res.stdout + res.stderr


def test_live_inventory_deploy_of_a_new_stack_names_only_the_stack(iso):
    """No pin for atat: the deploy builds (build SA + bucket of atat) and prints the image."""
    env_pin = iso["tmp"] / f"inventory-pin.{PREFIX}.env"
    env_pin.unlink()
    res = run(iso, [ROOT / "demo" / "ata-inventory-api" / "scripts" / "deploy.sh"], **LIVE_ENV)
    assert res.returncode == 0, res.stdout + res.stderr
    calls = assert_isolated(iso, res)
    build = next(c for c in calls if c[1:3] == ["builds", "submit"])
    assert "--tag=test-region1-docker.pkg.dev/test-project/atat-docker/atat-inventory-api:v1.1.0" in build
    assert "--service-account=projects/test-project/serviceAccounts/atat-build-sa@test-project.iam.gserviceaccount.com" in build
    assert "--gcs-source-staging-dir=gs://test-project-atat-build/source" in build
    assert "--gcs-log-dir=gs://test-project-atat-build/logs" in build
    deploy = next(c for c in calls if c[1:3] == ["run", "deploy"])
    assert deploy[3] == "atat-inventory-api"
    assert f"--image=test-region1-docker.pkg.dev/test-project/atat-docker/atat-inventory-api@{NEW_DIGEST}" in deploy
    assert f"IMAGE=test-region1-docker.pkg.dev/test-project/atat-docker/atat-inventory-api@{NEW_DIGEST}" in res.stdout
    assert "write-pin --initial --revision atat-inventory-api-00002-new" in res.stdout


# --- re-pin / caps on the stateful fake, as stack atat ----------------------------------


def test_caps_dry_run_names_only_the_stack(tmp_path):
    cloud = FakeCloud(tmp_path, for_prefix(live_like_state(), PREFIX))
    cloud.pin_file.unlink()  # caps need no pin
    res = cloud.run([SCRIPTS / "set-demo-instance-caps.sh"], NAME_PREFIX=PREFIX,
                    ATA_INVENTORY_PIN_FILE=str(tmp_path / f"inventory-pin.{PREFIX}.env"))
    assert res.returncode == 0, res.stdout + res.stderr
    flat = [" ".join(c) for c in cloud.calls()]
    assert flat and not [c for c in flat if OTHER_STACK.search(c)], flat
    assert any("atat-orders-api" in c for c in flat)


def test_repin_status_names_only_the_stack(tmp_path):
    cloud = FakeCloud(tmp_path, for_prefix(fresh_project_state(), PREFIX))
    pin = tmp_path / f"inventory-pin.{PREFIX}.env"
    write_pin(pin, f"{PREFIX}-inventory-api-00001-abc", NEW_DIGEST)
    res = cloud.run([SCRIPTS / "repin-inventory.sh", "status"], NAME_PREFIX=PREFIX, ATA_INVENTORY_PIN_FILE=str(pin))
    flat = [" ".join(c) for c in cloud.calls() + cloud.curl_calls()]
    assert flat and not [c for c in flat if OTHER_STACK.search(c)], flat
    assert not [line for line in (res.stdout + res.stderr).splitlines() if OTHER_STACK.search(line)]


# --- the guard itself -------------------------------------------------------------------


@pytest.mark.parametrize("text, leaks", [
    ("gcloud run services describe atat-inventory-api", False),
    ("https://h/atat/orders/v1/orders", False),
    ("metadata-labels data-x", False),
    ("gcloud run services describe ata-inventory-api", True),
    ("https://h/ata/orders/v1/orders", True),
    ("https://ata-inventory-api-4klvlqnmpa-uw.a.run.app", True),
])
def test_other_stack_pattern(text, leaks):
    assert bool(OTHER_STACK.search(text)) is leaks
