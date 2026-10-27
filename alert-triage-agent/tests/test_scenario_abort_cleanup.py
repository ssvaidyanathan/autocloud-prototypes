"""Scenario scripts undo their fault when they abort after injecting it (S1, S2, S3, S5).

QA round 2 (s3fix report): S3 injected the inventory 500 fault and only then built the strict
SpikeArrest bundle and parsed the Apigee import response with `json.load` under `set -e`. A
non-JSON import response (or a missing bundle) aborted the run with the fault injected and no
traffic. Now:

- S3 prepares the bundle before any fault, reports a non-JSON (or revision-less) import
  response as a clear error, and an EXIT trap resets the inventory fault and redeploys the
  proxy revision that served before when the strict revision was deployed. It says what is
  STILL left when a cleanup step fails.
- S1 and S2 reset their inventory fault on an error exit after the injection.
- S5 reverts to the pinned baseline on an error exit after its faulty `services update`.

A successful run keeps its faults (the scenario needs them until reset). curl, gcloud and
date are stubbed; the scripts run under ATA_TEST_BASH (default `bash`; set it to a bash 3.2
binary to check macOS compatibility).
"""

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_s5_bad_deploy import BAD_USER, GOOD_USER, PINNED_REVISION, Env, baseline_state

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))
REAL_DATE = shutil.which("date")

# Each call is logged as one line. Status-printing calls (-w) print 200 unless configured.
CURL = """#!/bin/sh
echo "curl $*" >> "$ATA_TEST_CALLS"
case "$*" in
  */admin/fault/reset*) printf '%s' "${ATA_RESET_STATUS:-200}" ;;
  *action=import*)
    [ "${ATA_IMPORT_CURL_FAIL:-0}" = "1" ] && exit 7
    cat "$ATA_IMPORT_BODY" ;;
  *"/revisions/${ATA_PREV_REVISION:-168}/deployments"*) printf '%s' "${ATA_REDEPLOY_STATUS:-200}" ;;
  */apis/ata-orders-v1/deployments*) cat "$ATA_DEPLOYMENTS_BODY" ;;
  *-w*) printf '200' ;;
  *) printf '{}' ;;
esac
"""
# `date +%s` fails (exit 3) after $ATA_FAIL_EPOCH_AFTER successful calls: an error after the
# fault, before any traffic. Unset: never fails.
DATE = f"""#!/bin/sh
if [ "$1" = "+%s" ] && [ -n "${{ATA_FAIL_EPOCH_AFTER:-}}" ]; then
  n=$(cat "$ATA_TEST_CALLS.date" 2>/dev/null || echo 0)
  echo $((n + 1)) > "$ATA_TEST_CALLS.date"
  if [ "$n" -ge "$ATA_FAIL_EPOCH_AFTER" ]; then echo "date: stub failure" >&2; exit 3; fi
fi
exec {REAL_DATE} "$@"
"""
GCLOUD = """#!/bin/sh
echo "gcloud $*" >> "$ATA_TEST_CALLS"
case "$*" in
  "auth print-access-token"*) echo "apigee-token" ;;
  "auth print-identity-token"*) echo "id-token" ;;
  *"value(status.url)"*) echo "https://inv.example" ;;
esac
"""


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bash").symlink_to(TEST_BASH)
    for name, body in [("curl", CURL), ("date", DATE), ("gcloud", GCLOUD)]:
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    (tmp_path / "import.json").write_text(json.dumps({"name": "ata-orders-v1", "revision": "169"}))
    (tmp_path / "deployments.json").write_text(json.dumps({"deployments": [
        {"environment": "prod", "apiProxy": "ata-orders-v1", "revision": "168"}]}))
    e = {k: v for k, v in os.environ.items() if k not in ("ATA_API_KEY", "MOCK_MODE", "DRY_RUN")}
    e.update(PATH=f"{bin_dir}{os.pathsep}{e['PATH']}", ATA_TEST_CALLS=str(tmp_path / "calls"),
             ATA_IMPORT_BODY=str(tmp_path / "import.json"), ATA_DEPLOYMENTS_BODY=str(tmp_path / "deployments.json"),
             ATA_API_KEY="test-key", MOCK_MODE="false", DRY_RUN="false", RUN_PREFLIGHT="false")
    return e


def _run(tmp_path, env, script, *args, scripts_dir=SCRIPTS_DIR, **extra):
    e = dict(env, **extra)
    return subprocess.run([TEST_BASH, str(scripts_dir / "scenarios" / script), *args],
                          cwd=str(tmp_path), env=e, capture_output=True, text=True, timeout=60, check=False)


def _calls(tmp_path):
    log = tmp_path / "calls"
    return [c for c in log.read_text().splitlines() if c.startswith("curl ")] if log.exists() else []


def _index(calls, needle):
    return next((i for i, c in enumerate(calls) if needle in c), None)


REDEPLOY_168 = "/apis/ata-orders-v1/revisions/168/deployments?override=true"
DEPLOY_169 = "/apis/ata-orders-v1/revisions/169/deployments?override=true"


# --- S3 -----------------------------------------------------------------------------------


def test_s3_success_keeps_its_faults(tmp_path, env):
    res = _run(tmp_path, env, "s3_concurrent_disjoint.sh", "--hold", "0s")
    assert res.returncode == 0, res.stdout + res.stderr
    calls = _calls(tmp_path)
    assert _index(calls, "/admin/fault/inject") < _index(calls, DEPLOY_169)
    assert _index(calls, "/admin/fault/reset") is None
    assert _index(calls, REDEPLOY_168) is None
    assert "S3 aborted" not in res.stderr


@pytest.mark.parametrize("body, reason", [
    ("<html>502 Bad Gateway</html>", "is not JSON"),
    (json.dumps({"error": {"code": 400, "message": "bad bundle"}}), "is JSON without a revision"),
])
def test_s3_bad_import_response_is_a_clear_error_and_resets_the_fault(tmp_path, env, body, reason):
    (tmp_path / "import.json").write_text(body)
    res = _run(tmp_path, env, "s3_concurrent_disjoint.sh", "--hold", "0s")
    assert res.returncode == 1, res.stdout + res.stderr
    assert f"FATAL: the Apigee import response for ata-orders-v1 {reason}" in res.stderr
    assert body[:20] in res.stderr
    calls = _calls(tmp_path)
    inject, reset = _index(calls, "/admin/fault/inject"), _index(calls, "/admin/fault/reset")
    assert inject is not None and reset is not None and inject < reset
    assert "-X POST" in calls[reset] and "Authorization: Bearer id-token" in calls[reset]
    # The strict revision was never deployed, so the proxy is not touched.
    assert _index(calls, "/revisions/") is None
    assert "S3 aborted (exit 1) after injecting faults" in res.stderr
    assert "nothing left injected" in res.stderr
    assert "Traceback" not in res.stderr


def test_s3_failed_import_request_resets_the_fault(tmp_path, env):
    res = _run(tmp_path, env, "s3_concurrent_disjoint.sh", "--hold", "0s", ATA_IMPORT_CURL_FAIL="1")
    assert res.returncode == 1
    assert "the Apigee import request for ata-orders-v1 failed" in res.stderr
    assert _index(_calls(tmp_path), "/admin/fault/reset") is not None
    # No made-up revision is deployed (the old fallback deployed revision 98).
    assert _index(_calls(tmp_path), "/revisions/98/") is None


def test_s3_error_after_the_deploy_resets_the_fault_and_restores_the_proxy(tmp_path, env):
    res = _run(tmp_path, env, "s3_concurrent_disjoint.sh", "--hold", "1s", ATA_FAIL_EPOCH_AFTER="0")
    assert res.returncode == 3, res.stdout + res.stderr
    calls = _calls(tmp_path)
    deploy, reset, redeploy = (_index(calls, DEPLOY_169), _index(calls, "/admin/fault/reset"),
                               _index(calls, REDEPLOY_168))
    assert deploy is not None and reset is not None and redeploy is not None
    assert deploy < reset < redeploy
    assert "Redeployed ata-orders-v1 revision 168" in res.stderr
    assert "nothing left injected" in res.stderr
    assert "unbound variable" not in res.stderr


def test_s3_failed_cleanup_says_what_is_still_left(tmp_path, env):
    res = _run(tmp_path, env, "s3_concurrent_disjoint.sh", "--hold", "1s", ATA_FAIL_EPOCH_AFTER="0",
               ATA_RESET_STATUS="503", ATA_REDEPLOY_STATUS="500")
    assert res.returncode == 3
    assert "the 500 error fault is STILL INJECTED on ata-inventory-api" in res.stderr
    assert ("STILL LEFT after the S3 abort: the 500 error fault on ata-inventory-api; ata-orders-v1 deployed "
            "with the strict 1pm SpikeArrest revision 169. Run ./scripts/reset-scenarios.sh now.") in res.stderr


def test_s3_unknown_previous_revision_is_reported_as_still_left(tmp_path, env):
    (tmp_path / "deployments.json").write_text("not json")
    res = _run(tmp_path, env, "s3_concurrent_disjoint.sh", "--hold", "1s", ATA_FAIL_EPOCH_AFTER="0")
    assert res.returncode == 3
    assert "Could not read the deployed ata-orders-v1 revision" in res.stderr
    assert "revision deployed before S3 is unknown; the proxy was NOT restored" in res.stderr
    assert "STILL LEFT after the S3 abort: ata-orders-v1 deployed with the strict 1pm SpikeArrest revision 169" in res.stderr
    assert _index(_calls(tmp_path), "/admin/fault/reset") is not None


def test_s3_missing_bundle_stops_before_any_fault(tmp_path, env):
    scripts = tmp_path / "pkg" / "alert-triage-agent" / "scripts"
    shutil.copytree(SCRIPTS_DIR, scripts)  # no demo/apigee bundle next to this copy
    res = _run(tmp_path, env, "s3_concurrent_disjoint.sh", "--hold", "0s", scripts_dir=scripts)
    assert res.returncode == 1
    assert "Apigee bundle" in res.stderr and "no fault injected" in res.stderr
    assert _calls(tmp_path) == []


def test_s3_dry_run_abort_makes_no_network_call(tmp_path, env):
    res = _run(tmp_path, env, "s3_concurrent_disjoint.sh", "--dry-run", "--hold", "1s", ATA_FAIL_EPOCH_AFTER="0")
    assert res.returncode == 3
    assert "[DRY-RUN] curl -s -X POST https://ata-inventory-api-4klvlqnmpa-uw.a.run.app/admin/fault/reset" in res.stderr
    assert _calls(tmp_path) == []


@pytest.mark.parametrize("mode", ["--mock", "--dry-run"])
def test_s3_mock_and_dry_run_still_complete(tmp_path, env, mode):
    res = _run(tmp_path, env, "s3_concurrent_disjoint.sh", mode, "--hold", "0s")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Simulating import and deployment of strict SpikeArrest revision" in res.stdout
    assert "NEXT STEPS" in res.stdout
    assert _index(_calls(tmp_path), "/admin/fault/reset") is None


# --- S1 / S2 ------------------------------------------------------------------------------


@pytest.mark.parametrize("script, fault", [
    ("s1_backend_5xx.sh", "the 500 error fault"),
    ("s2_backend_latency.sh", "the latency fault"),
])
def test_s1_s2_error_after_the_fault_resets_it(tmp_path, env, script, fault):
    res = _run(tmp_path, env, script, "--hold", "1s", ATA_FAIL_EPOCH_AFTER="0")
    assert res.returncode == 3, res.stdout + res.stderr
    calls = _calls(tmp_path)
    inject, reset = _index(calls, "/admin/fault/inject"), _index(calls, "/admin/fault/reset")
    assert inject is not None and reset is not None and inject < reset
    assert f"aborted (exit 3) after injecting {fault} on ata-inventory-api" in res.stderr
    assert f"Reset {fault} (POST https://inv.example/admin/fault/reset -> HTTP 200)" in res.stderr


@pytest.mark.parametrize("script, fault", [
    ("s1_backend_5xx.sh", "the 500 error fault"),
    ("s2_backend_latency.sh", "the latency fault"),
])
def test_s1_s2_failed_reset_says_the_fault_is_still_injected(tmp_path, env, script, fault):
    res = _run(tmp_path, env, script, "--hold", "1s", ATA_FAIL_EPOCH_AFTER="0", ATA_RESET_STATUS="503")
    assert res.returncode == 3
    assert f"returned HTTP 503; {fault} is STILL INJECTED on ata-inventory-api" in res.stderr
    assert "Run ./scripts/reset-scenarios.sh now." in res.stderr


@pytest.mark.parametrize("script", ["s1_backend_5xx.sh", "s2_backend_latency.sh"])
def test_s1_s2_success_keeps_the_fault(tmp_path, env, script):
    res = _run(tmp_path, env, script, "--hold", "0s")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _index(_calls(tmp_path), "/admin/fault/inject") is not None
    assert _index(_calls(tmp_path), "/admin/fault/reset") is None


@pytest.mark.parametrize("script", ["s1_backend_5xx.sh", "s2_backend_latency.sh"])
@pytest.mark.parametrize("mode", ["--mock", "--dry-run"])
def test_s1_s2_mock_and_dry_run_still_complete(tmp_path, env, script, mode):
    res = _run(tmp_path, env, script, mode, "--hold", "0s")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _index(_calls(tmp_path), "/admin/fault/reset") is None


# --- S5 (stateful fake gcloud from test_s5_bad_deploy) -------------------------------------


def _s5_env(tmp_path, state):
    s5 = Env(tmp_path, state)
    date = s5.dir / "date"
    date.write_text(DATE)
    date.chmod(0o755)
    return s5


def _s5_run(s5, tmp_path, monkeypatch, *args, **extra):
    monkeypatch.setenv("ATA_TEST_CALLS", str(tmp_path / "datecalls"))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    return s5.run([TEST_BASH, str(SCRIPTS_DIR / "scenarios" / "s5_bad_deploy.sh"), *args])


def test_s5_error_after_the_faulty_deploy_reverts_to_the_pinned_baseline(tmp_path, monkeypatch):
    s5 = _s5_env(tmp_path, baseline_state())
    # The first `date +%s` names the S5 revision; the second (traffic start) fails.
    res = _s5_run(s5, tmp_path, monkeypatch, "--hold", "1s", ATA_FAIL_EPOCH_AFTER="1")
    assert res.returncode == 3, res.stdout + res.stderr
    assert "S5 aborted (exit 3) after deploying the faulty revision ata-inventory-api-s5-" in res.stderr
    assert s5.state["traffic"] == [[PINNED_REVISION, 100]]
    assert s5.state["env"]["DB_USER"] == GOOD_USER
    assert "S5 abort cleanup complete" in res.stderr


def test_s5_failed_routing_reverts_the_faulty_template(tmp_path, monkeypatch):
    s5 = _s5_env(tmp_path, baseline_state(fail=["update-traffic"]))
    res = _s5_run(s5, tmp_path, monkeypatch, "--hold", "0s")
    assert res.returncode == 1
    assert "could not route traffic to ata-inventory-api-s5-" in res.stderr
    assert s5.state["env"]["DB_USER"] == GOOD_USER
    assert s5.state["traffic"] == [[PINNED_REVISION, 100]]


def test_s5_failed_revert_says_what_is_still_left(tmp_path, monkeypatch):
    s5 = _s5_env(tmp_path, baseline_state(fail=["describe"]))
    res = _s5_run(s5, tmp_path, monkeypatch, "--hold", "0s")
    assert res.returncode == 1
    assert s5.state["env"]["DB_USER"] == BAD_USER
    assert "STILL LEFT after the S5 abort: ata-inventory-api is NOT on the pinned baseline" in res.stderr


def test_s5_update_failure_is_not_reverted_automatically(tmp_path, monkeypatch):
    """Before the faulty update succeeded nothing was injected; the existing advice stands."""
    s5 = _s5_env(tmp_path, baseline_state(fail=["update"]))
    res = _s5_run(s5, tmp_path, monkeypatch, "--hold", "0s")
    assert res.returncode == 1
    assert "S5 aborted" not in res.stderr
    assert "run '" in res.stderr and "--revert' to restore the baseline" in res.stderr


# --- Ctrl-C during the traffic hold (S1-S5) -----------------------------------------------
# DEMO.md: "Press Ctrl-C at any time to safely stop traffic early and view next steps".
# SIGINT goes to the whole foreground process group, so the loop's foreground child (the
# `sleep`, or the `date +%s` at the top of the loop) dies with 130 as well. Under set -e that
# used to abort the run and the EXIT trap undid the fault. Now the run finishes normally (rc 0),
# prints NEXT STEPS and keeps the fault. The stubs below hang in the chosen child and record a
# marker so the SIGINT deterministically lands there.

REAL_SLEEP = shutil.which("sleep")
CTRL_C_SLEEP = f"""#!/bin/sh
if [ "${{ATA_CTRL_C_AT:-}}" = "sleep" ]; then echo "sleep $*" >> "$ATA_CTRL_C_MARK"; exec {REAL_SLEEP} 30; fi
exec {REAL_SLEEP} "$@"
"""
# Hangs in the `date +%s` call number $ATA_CTRL_C_AFTER (0-based) when ATA_CTRL_C_AT=date.
CTRL_C_DATE = f"""#!/bin/sh
if [ "$1" = "+%s" ] && [ "${{ATA_CTRL_C_AT:-}}" = "date" ]; then
  n=$(cat "$ATA_CTRL_C_MARK.n" 2>/dev/null || echo 0)
  echo $((n + 1)) > "$ATA_CTRL_C_MARK.n"
  if [ "$n" -ge "$ATA_CTRL_C_AFTER" ]; then echo "date $*" >> "$ATA_CTRL_C_MARK"; exec {REAL_SLEEP} 30; fi
fi
exec {REAL_DATE} "$@"
"""


def install_ctrl_c_stubs(bin_dir):
    for name, body in [("sleep", CTRL_C_SLEEP), ("date", CTRL_C_DATE)]:
        stub = Path(bin_dir) / name
        stub.write_text(body)
        stub.chmod(0o755)


def run_with_ctrl_c(argv, env, cwd, at, after=0):
    """Run argv in its own process group and send SIGINT to the group (as a terminal Ctrl-C
    does) once the stubbed `at` child ("sleep" or "date") is hanging in the traffic loop."""
    mark = Path(cwd) / "ctrl-c-mark"
    e = dict(env, ATA_CTRL_C_MARK=str(mark), ATA_CTRL_C_AT=at, ATA_CTRL_C_AFTER=str(after))
    proc = subprocess.Popen(argv, cwd=str(cwd), env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        while not mark.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert mark.exists(), f"the loop never reached the stubbed {at}"
        time.sleep(0.3)  # let the stub exec the real sleep
        os.killpg(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def assert_finished_gracefully(res):
    output = res.stdout + res.stderr
    assert res.returncode == 0, output
    assert "interrupted by user (Ctrl-C). Finishing scenario..." in output
    assert "NEXT STEPS" in res.stdout
    assert "aborted (exit" not in output


@pytest.mark.parametrize("at", ["sleep", "date"])
@pytest.mark.parametrize("script", ["s1_backend_5xx.sh", "s2_backend_latency.sh", "s3_concurrent_disjoint.sh"])
def test_ctrl_c_during_the_hold_finishes_and_keeps_the_fault(tmp_path, env, script, at):
    install_ctrl_c_stubs(tmp_path / "bin")
    argv = [TEST_BASH, str(SCRIPTS_DIR / "scenarios" / script), "--hold", "5m"]
    # `date +%s` call 0 is START_EPOCH; call 1 is the first loop iteration.
    res = run_with_ctrl_c(argv, env, tmp_path, at, after=1)
    assert_finished_gracefully(res)
    calls = _calls(tmp_path)
    assert _index(calls, "/admin/fault/inject") is not None
    assert _index(calls, "/admin/fault/reset") is None
    assert _index(calls, REDEPLOY_168) is None


@pytest.mark.parametrize("at", ["sleep", "date"])
def test_s5_ctrl_c_during_the_hold_finishes_and_keeps_the_bad_deploy(tmp_path, at):
    s5 = Env(tmp_path, baseline_state())
    install_ctrl_c_stubs(s5.dir)
    e = os.environ.copy()
    for var in ("PINNED_INVENTORY_DIGEST", "PINNED_INVENTORY_REVISION", "PINNED_INVENTORY_IMAGE",
                "PROJECT_ID", "GCP_PROJECT", "REGION", "GCP_REGION", "NAME_PREFIX"):
        e.pop(var, None)
    e.update(PATH=f"{s5.dir}{os.pathsep}{e['PATH']}", MOCK_MODE="false", DRY_RUN="false", ATA_API_KEY="test-key")
    argv = [TEST_BASH, str(SCRIPTS_DIR / "scenarios" / "s5_bad_deploy.sh"), "--hold", "5m"]
    # `date +%s` call 0 names the revision, 1 is START_EPOCH, 2 is the first loop iteration.
    res = run_with_ctrl_c(argv, e, tmp_path, at, after=2)
    assert_finished_gracefully(res)
    assert s5.state["env"]["DB_USER"] == BAD_USER
    assert s5.state["traffic"] != [[PINNED_REVISION, 100]]
