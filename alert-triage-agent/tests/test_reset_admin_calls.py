"""reset-scenarios.sh: admin calls fail loudly and mint a fresh ID token per call (QA S4 reset).

QA saw step 1 ignore the HTTP status of /admin/fault/reset (always "fault states cleared")
and the step-9 preflight accept HTTP 401 after the ID token minted at start went stale.
The functions are tested by sourcing the script (which defines them without running the
reset) under ATA_TEST_BASH (default `bash`; set it to a bash 3.2 binary for macOS parity),
with curl and gcloud stubbed on PATH.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

RESET_SCRIPT = Path(__file__).parent.parent / "scripts" / "reset-scenarios.sh"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))
BASE = "https://inv.example.run.app"

# curl stub: logs "METHOD URL BEARER" and prints the status configured for the URL path
# in $PWD/statuses ("<path> <code>" lines; default 200; "<path> fail" = connection error),
# followed by a 10 ms time_total (reset-scenarios.sh asks for -w "%{http_code} %{time_total}").
FAKE_CURL = r"""#!/bin/sh
method=GET; url=""; bearer=""
while [ $# -gt 0 ]; do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    -H) case "$2" in "Authorization: Bearer "*) bearer="${2#Authorization: Bearer }" ;; esac; shift 2 ;;
    -o|-w|--connect-timeout|--max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
echo "$method $url $bearer" >> "$PWD/curl_calls"
path="/${url#*://*/}"
code=$(awk -v p="$path" '$1 == p {print $2}' "$PWD/statuses" 2>/dev/null)
[ "$code" = "fail" ] && exit 7
printf '%s 0.010' "${code:-200}"
"""

# gcloud stub: each identity token is new (token-1, token-2, ...); describe prints $PWD/<svc>.json.
FAKE_GCLOUD = r"""#!/bin/sh
case "$*" in
  "auth print-identity-token"*)
    n=$(( $(cat "$PWD/token_n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$PWD/token_n"; echo "token-$n" ;;
  "run services describe "*)
    svc="$4"; [ -f "$PWD/$svc.json" ] || exit 1; cat "$PWD/$svc.json" ;;
  *) echo "fake gcloud: unexpected $*" >&2; exit 1 ;;
esac
"""


def _run(tmp_path, body, statuses=""):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, text in (("curl", FAKE_CURL), ("gcloud", FAKE_GCLOUD)):
        (bindir / name).write_text(text)
        (bindir / name).chmod(0o755)
    (tmp_path / "statuses").write_text(statuses)
    script = tmp_path / "run.sh"
    script.write_text(f'source "{RESET_SCRIPT}"\n{body}\n')
    env = {k: v for k, v in os.environ.items() if k not in ("ATA_SERVING_REVISIONS", "ATA_MONITORING_TOKEN_SOURCE")}
    env.update(PATH=f"{bindir}{os.pathsep}{env['PATH']}", MOCK_MODE="false", DRY_RUN="false",
               PROJECT_ID="proj", REGION="us-west1", NAME_PREFIX="ata", PINNED_INVENTORY_REVISION="")
    return subprocess.run([TEST_BASH, str(script)], cwd=str(tmp_path), capture_output=True, text=True,
                          env=env, timeout=60)


def _curl_calls(tmp_path):
    f = tmp_path / "curl_calls"
    return f.read_text().splitlines() if f.exists() else []


# --- Step 1: fault reset -------------------------------------------------------


def test_fault_reset_2xx_succeeds_and_names_the_endpoint(tmp_path):
    res = _run(tmp_path, f'reset_app_faults ata-inventory-api "{BASE}" && echo RC0')
    assert "RC0" in res.stdout, res.stdout + res.stderr
    assert f"ata-inventory-api: POST {BASE}/admin/fault/reset -> HTTP 200" in res.stdout
    assert _curl_calls(tmp_path) == [f"POST {BASE}/admin/fault/reset token-1"]


@pytest.mark.parametrize("code", ["401", "403"])
def test_fault_reset_auth_failure_is_loud(tmp_path, code):
    res = _run(tmp_path, f'reset_app_faults ata-inventory-api "{BASE}" || echo "RC=$?"',
               statuses=f"/admin/fault/reset {code}\n")
    assert "RC=1" in res.stdout
    assert f"POST {BASE}/admin/fault/reset (ata-inventory-api) was rejected with HTTP {code}" in res.stderr
    assert "roles/run.invoker" in res.stderr
    assert "fault state was NOT cleared" in res.stderr
    assert "-> HTTP" not in res.stdout


@pytest.mark.parametrize("code, text", [("500", "returned HTTP 500"), ("404", "returned HTTP 404"),
                                        ("fail", "no HTTP response")])
def test_fault_reset_other_failures_are_loud(tmp_path, code, text):
    res = _run(tmp_path, f'reset_app_faults ata-orders-api "{BASE}" || echo "RC=$?"',
               statuses=f"/admin/fault/reset {code}\n")
    assert "RC=1" in res.stdout
    assert f"POST {BASE}/admin/fault/reset (ata-orders-api)" in res.stderr and text in res.stderr


def test_main_flow_exits_on_failed_fault_reset_before_claiming_success(tmp_path):
    """The step-1 call site stops the reset; it never prints 'fault states cleared'."""
    body = (
        f'INV_URL="{BASE}"; ORD_URL="{BASE}"\n'
        + RESET_SCRIPT.read_text().split("# 1. Reset Faults on Cloud Run Demo Apps", 1)[1]
        .split("# 2. Bridge State", 1)[0]
        + "echo AFTER_STEP1"
    )
    res = _run(tmp_path, body, statuses="/admin/fault/reset 401\n")
    assert res.returncode == 1
    assert "AFTER_STEP1" not in res.stdout
    assert "fault states cleared" not in res.stdout


# --- Token freshness -----------------------------------------------------------


def test_every_admin_call_mints_a_fresh_id_token(tmp_path):
    res = _run(tmp_path, f'reset_app_faults a "{BASE}"; reset_app_faults b "{BASE}"; verify_admin_endpoints "{BASE}"')
    assert res.returncode == 0, res.stderr
    tokens = [line.split()[-1] for line in _curl_calls(tmp_path)]
    assert tokens == ["token-1", "token-2", "token-3", "token-4"]


# --- Step 5b: preflight -----------------------------------------------------------


def test_preflight_accepts_only_2xx(tmp_path):
    res = _run(tmp_path, f'verify_admin_endpoints "{BASE}" && echo RC0')
    assert "RC0" in res.stdout
    assert "Verified /admin/leak-connections endpoint exists (HTTP 200)" in res.stdout
    assert "Verified /admin/fault/inject endpoint exists (HTTP 200)" in res.stdout
    assert [c.split()[0] for c in _curl_calls(tmp_path)] == ["GET", "GET"]


@pytest.mark.parametrize("code", ["401", "403"])
def test_preflight_treats_401_403_as_failure(tmp_path, code):
    res = _run(tmp_path, f'verify_admin_endpoints "{BASE}" || echo "RC=$?"',
               statuses=f"/admin/leak-connections {code}\n/admin/fault/inject {code}\n")
    assert "RC=1" in res.stdout
    assert f"GET {BASE}/admin/leak-connections was rejected with HTTP {code}" in res.stderr
    assert "2 admin fault injection endpoint check(s) failed" in res.stderr
    assert "Verified" not in res.stdout


def test_preflight_missing_and_unexpected_statuses_fail(tmp_path):
    res = _run(tmp_path, f'verify_admin_endpoints "{BASE}" || echo "RC=$?"',
               statuses="/admin/leak-connections 404\n/admin/fault/inject 502\n")
    assert "RC=1" in res.stdout
    assert "/admin/leak-connections is MISSING" in res.stderr
    assert f"GET {BASE}/admin/fault/inject returned HTTP 502" in res.stderr


# --- Step 3 logging --------------------------------------------------------------


def test_step3_logs_when_no_pg_sleep_helpers_run(tmp_path):
    res = _run(tmp_path, 'pkill() { return 1; }\nterminate_local_pg_sleep')
    assert "No local pg_sleep helper processes were running." in res.stdout
    res = _run(tmp_path, 'pkill() { return 0; }\nterminate_local_pg_sleep')
    assert "Terminated local pg_sleep helper process(es)." in res.stdout
    res = _run(tmp_path, 'pkill() { return 3; }\nterminate_local_pg_sleep')
    assert "pkill -f pg_sleep failed (exit 3)" in res.stdout + res.stderr


# --- Serving revisions (QA nit: latestReady and duplicates over-scoped) ---------


def test_serving_revisions_are_traffic_carrying_and_unique(tmp_path):
    inv = {"status": {
        "latestReadyRevisionName": "ata-inventory-api-s5-1793093161",
        "traffic": [
            {"revisionName": "ata-inventory-api-00073-9vb", "percent": 100},
            {"revisionName": "ata-inventory-api-s5-1793093161", "percent": 0, "tag": "s5"},
        ],
    }}
    orders = {"status": {"latestReadyRevisionName": "ata-orders-api-00004-rgg",
                         "traffic": [{"revisionName": "ata-orders-api-00004-rgg", "percent": 100}]}}
    (tmp_path / "ata-inventory-api.json").write_text(json.dumps(inv))
    (tmp_path / "ata-orders-api.json").write_text(json.dumps(orders))

    res = _run(tmp_path, 'PINNED_INVENTORY_REVISION=ata-inventory-api-00073-9vb\n'
                         'ATA_SERVING_REVISIONS=ata-orders-api-00004-rgg\necho "REVS=$(get_serving_revisions)"')
    assert "REVS=ata-inventory-api-00073-9vb ata-orders-api-00004-rgg\n" in res.stdout, res.stdout + res.stderr


def test_serving_revisions_warn_when_a_service_cannot_be_described(tmp_path):
    res = _run(tmp_path, 'PINNED_INVENTORY_REVISION=""\necho "REVS=$(get_serving_revisions)"')
    assert "REVS=\n" in res.stdout
    assert "Could not describe ata-inventory-api" in res.stderr
