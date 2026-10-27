"""S4 (s4_db_connection_pool.sh): pre-flight before the fault, and no leaked pool on an abort.

QA round 2, S4: the script leaked the DB connections first and only then called
select_order_item (GET /items through Apigee). With the pool exhausted that GET returned
HTTP 500, so the script aborted ("Could not find an in-stock item ...", rc=1) with no traffic
and the connections still leaked. Now the item is selected before the leak, and an EXIT trap
releases the leak (POST /admin/fault/reset) whenever the script exits with an error after it.

curl, gcloud, date and python3's Cloud SQL helper are stubbed; the scripts run under
ATA_TEST_BASH (default `bash`; set it to a bash 3.2 binary to check macOS compatibility).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_scenario_abort_cleanup import assert_finished_gracefully, install_ctrl_c_stubs, run_with_ctrl_c

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))
REAL_PYTHON = shutil.which("python3")
REAL_DATE = shutil.which("date")
ITEMS_OK = json.dumps([{"id": "item-101", "stock": 0}, {"id": "item-105", "stock": 480}])

# GET /items (no -w) prints $ATA_ITEMS_BODY; POST /admin/fault/reset prints $ATA_RESET_STATUS;
# every other call prints 200 (the -w status). Each call is logged as one line.
CURL = """#!/bin/sh
echo "curl $*" >> "$ATA_TEST_CALLS"
case "$*" in
  */admin/fault/reset*) printf '%s' "${ATA_RESET_STATUS:-200}" ;;
  *-w*) printf '200' ;;
  */ata/inventory/v1/items*) cat "$ATA_ITEMS_BODY" ;;
  *) printf '200' ;;
esac
"""
# `date +%s` fails when ATA_FAIL_EPOCH=1: an error after the leak, before any traffic.
DATE = f"""#!/bin/sh
if [ "$1" = "+%s" ] && [ "${{ATA_FAIL_EPOCH:-0}}" = "1" ]; then echo "date: stub failure" >&2; exit 3; fi
exec {REAL_DATE} "$@"
"""
# The S4 Cloud SQL holding-connection helper is never run for real.
PYTHON = f"""#!/bin/sh
case "$*" in
  *sql.connector*) echo "python3 cloud-sql helper (stub)"; exit 0 ;;
esac
exec {REAL_PYTHON} "$@"
"""


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bash").symlink_to(TEST_BASH)
    for name, body in [("curl", CURL), ("date", DATE), ("python3", PYTHON), ("gcloud", "#!/bin/sh\n"),
                       ("pkill", '#!/bin/sh\necho "pkill $*" >> "$ATA_TEST_CALLS"\nexit 1\n')]:
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    items = tmp_path / "items.json"
    items.write_text(ITEMS_OK)
    e = {k: v for k, v in os.environ.items() if k not in ("ATA_API_KEY", "MOCK_MODE", "DRY_RUN")}
    e.update(PATH=f"{bin_dir}{os.pathsep}{e['PATH']}", ATA_TEST_CALLS=str(tmp_path / "calls"),
             ATA_ITEMS_BODY=str(items), ATA_API_KEY="test-key", MOCK_MODE="false", DRY_RUN="false",
             RUN_PREFLIGHT="false")
    return e


def _run(tmp_path, env, *args, **extra):
    e = dict(env, **extra)
    return subprocess.run([TEST_BASH, str(SCRIPTS_DIR / "scenarios" / "s4_db_connection_pool.sh"), *args],
                          cwd=str(tmp_path), env=e, capture_output=True, text=True, timeout=60, check=False)


def _calls(tmp_path):
    log = tmp_path / "calls"
    return log.read_text().splitlines() if log.exists() else []


def _index(calls, needle):
    return next((i for i, c in enumerate(calls) if needle in c), None)


def test_item_is_selected_before_the_leak(tmp_path, env):
    res = _run(tmp_path, env, "--hold", "1s")
    assert res.returncode == 0, res.stdout + res.stderr
    calls = _calls(tmp_path)
    items_get = next(i for i, c in enumerate(calls) if "/ata/inventory/v1/items" in c and "-w" not in c)
    leak = _index(calls, "/admin/leak-connections")
    assert leak is not None and items_get < leak
    assert any('"item_id":"item-105"' in c for c in calls)
    # A successful run keeps the leak: the scenario needs it until reset-scenarios.sh.
    assert _index(calls, "/admin/fault/reset") is None
    assert "S4 aborted" not in res.stdout + res.stderr


def test_unavailable_items_abort_before_any_fault(tmp_path, env):
    (tmp_path / "items.json").write_text("Internal Server Error")
    res = _run(tmp_path, env, "--hold", "1s")
    assert res.returncode == 1
    assert "Could not find an in-stock item" in res.stderr
    calls = _calls(tmp_path)
    assert _index(calls, "/admin/leak-connections") is None
    assert _index(calls, "/admin/fault/reset") is None


def test_error_after_the_leak_releases_it_loudly(tmp_path, env):
    res = _run(tmp_path, env, "--hold", "1s", ATA_FAIL_EPOCH="1")
    assert res.returncode == 3, res.stdout + res.stderr
    calls = _calls(tmp_path)
    leak, reset = _index(calls, "/admin/leak-connections"), _index(calls, "/admin/fault/reset")
    assert leak is not None and reset is not None and leak < reset
    assert "-X POST" in calls[reset]
    assert "S4 aborted (exit 3) after leaking DB connections" in res.stderr
    assert "Leaked connections released on the instance the reset reached" in res.stderr
    assert "./scripts/reset-scenarios.sh, which verifies the release" in res.stderr
    assert any(c.startswith("pkill -f pg_sleep") for c in calls)
    assert "unbound variable" not in res.stderr


def test_failed_release_says_connections_are_still_leaked(tmp_path, env):
    res = _run(tmp_path, env, "--hold", "1s", ATA_FAIL_EPOCH="1", ATA_RESET_STATUS="503")
    assert res.returncode == 3
    assert "returned HTTP 503; the DB connections are STILL LEAKED" in res.stderr
    assert "./scripts/reset-scenarios.sh" in res.stderr


def test_dry_run_abort_makes_no_network_call(tmp_path, env):
    res = _run(tmp_path, env, "--dry-run", "--hold", "1s", ATA_FAIL_EPOCH="1")
    assert res.returncode == 3
    assert "[DRY-RUN] curl -s -X POST" in res.stderr and "/admin/fault/reset" in res.stderr
    assert not any(c.startswith("curl ") for c in _calls(tmp_path))


@pytest.mark.parametrize("mode", ["--mock", "--dry-run"])
def test_mock_and_dry_run_still_complete(tmp_path, env, mode):
    res = _run(tmp_path, env, mode, "--hold", "0s")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "NEXT STEPS" in res.stdout
    assert _index(_calls(tmp_path), "/admin/fault/reset") is None


@pytest.mark.parametrize("at", ["sleep", "date"])
def test_ctrl_c_during_the_hold_finishes_and_keeps_the_leak(tmp_path, env, at):
    """DEMO.md: Ctrl-C stops traffic early and shows next steps (see test_scenario_abort_cleanup)."""
    install_ctrl_c_stubs(tmp_path / "bin")
    argv = [TEST_BASH, str(SCRIPTS_DIR / "scenarios" / "s4_db_connection_pool.sh"), "--hold", "5m"]
    # `date +%s` call 0 is START_EPOCH; call 1 is the first loop iteration.
    res = run_with_ctrl_c(argv, env, tmp_path, at, after=1)
    assert_finished_gracefully(res)
    calls = _calls(tmp_path)
    assert _index(calls, "/admin/leak-connections") is not None
    assert _index(calls, "/admin/fault/reset") is None
