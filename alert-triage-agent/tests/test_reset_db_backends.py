"""Reset step 5c: the release of S4's leaked connections is verified on the database side.

QA round 2: POST /admin/fault/reset reached a Cloud Run instance that did not hold the leak
(the leak calls ran on another instance), so nothing was released until Cloud Run scaled the
leaking instance in. The reset now polls the Cloud SQL num_backends gauge of the inventory
database (Cloud Monitoring, read-only) until a sample taken after the fault reset is at most
ATA_RESET_DB_MAX_BACKENDS, and fails loudly, naming what remains, after
ATA_RESET_DB_RELEASE_TIMEOUT. The gauge helper (scripts/scenarios/db_backends.py) never
reports a failed query as zero backends.
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
RESET_SCRIPT = SCRIPTS_DIR / "reset-scenarios.sh"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))

sys.path.insert(0, str(SCRIPTS_DIR / "scenarios"))
_spec = importlib.util.spec_from_file_location("db_backends", SCRIPTS_DIR / "scenarios" / "db_backends.py")
db_backends = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db_backends)

FAKE_CLOCK = (
    "FAKE_NOW=1000\n"
    'date() { if [ "$*" = "+%s" ]; then echo "${FAKE_NOW}"; else command date "$@"; fi; }\n'
    'sleep() { FAKE_NOW=$((FAKE_NOW + $1)); }\n'
)
# Prints one line of $ATA_TEST_SAMPLES per call (the last repeats). A line is a JSON
# object, or "EXIT <rc> <message>" for a failed query.
FAKE_HELPER = """import sys
from pathlib import Path
p = Path(sys.argv[0]).with_name("samples")
lines = p.read_text().splitlines()
line = lines[0]
if len(lines) > 1:
    p.write_text("\\n".join(lines[1:]) + "\\n")
with open(Path(sys.argv[0]).with_name("argv"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
if line.startswith("EXIT "):
    _, rc, msg = line.split(" ", 2)
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(int(rc))
print(line)
"""


def _sample(value, epoch):
    return json.dumps({"value": value, "at": f"t{epoch}", "epoch": epoch})


def run_check(tmp_path, samples, since=1000, timeout=1200, poll=15, max_backends=5):
    helper = tmp_path / "db_backends_fake.py"
    helper.write_text(FAKE_HELPER)
    (tmp_path / "samples").write_text("\n".join(samples) + "\n")
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "set -euo pipefail\n"
        f'source "{RESET_SCRIPT}"\n'
        + FAKE_CLOCK
        + f'DB_BACKENDS_PY="{helper}"\nPOLL_INTERVAL_SECS={poll}\nDB_RELEASE_TIMEOUT_SECS={timeout}\n'
        f"DB_MAX_BACKENDS={max_backends}\nMAX_QUERY_ATTEMPTS=3\n"
        "resolve_monitoring_token() { echo tok; }\n"
        f"verify_db_backends_released {since}\n"
        'echo "CHECK_OK at ${FAKE_NOW}"\n'
    )
    env = {k: v for k, v in os.environ.items() if k not in ("ATA_API_KEY",)}
    env.update(MOCK_MODE="false", DRY_RUN="false", PROJECT_ID="proj")
    return subprocess.run([TEST_BASH, str(probe)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=60, check=False)


def test_sample_after_the_reset_at_baseline_passes_at_once(tmp_path):
    res = run_check(tmp_path, [_sample(0, 1010)])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "CHECK_OK at 1000" in res.stdout
    assert "ata-inventory-db: 0 backend(s) on database inventory at t1010 (<= 5); leaked connections released" in res.stdout
    assert (tmp_path / "argv").read_text().split() == ["proj", "tok", "ata-inventory-db", "inventory"]


def test_samples_from_before_the_reset_do_not_count(tmp_path):
    """QA round 2 shape: 22 until the leaking instance scaled in, then 0 (lagged samples)."""
    res = run_check(tmp_path, [_sample(0, 900), _sample(22, 990), _sample(22, 1030), _sample(0, 1090)])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "CHECK_OK at 1045" in res.stdout  # three polls of 15 s
    assert "num_backends (database inventory): 22 at t1030" in res.stdout


def test_leak_still_held_after_the_timeout_fails_loudly(tmp_path):
    res = run_check(tmp_path, [_sample(22, 1060)], timeout=120)
    assert res.returncode == 1
    assert "CHECK_OK" not in res.stdout
    assert ("FATAL: ata-inventory-db still has leaked connections 120s after the fault reset: num_backends on "
            "database inventory is 22 at t1060") in res.stderr
    assert "STILL LEFT: S4's leaked connections" in res.stderr
    assert "demo_max_instance_count" in res.stderr


def test_no_sample_is_not_zero(tmp_path):
    res = run_check(tmp_path, ['{"value": null}'], timeout=30)
    assert res.returncode == 1
    assert "num_backends on database inventory is not reported" in res.stderr


def test_transient_query_failures_are_retried(tmp_path):
    res = run_check(tmp_path, ["EXIT 3 HTTP 503", _sample(1, 1020)])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "num_backends query failed (attempt 1, retrying)" in res.stderr


def test_rejected_query_fails_at_once_and_says_release_is_not_verified(tmp_path):
    res = run_check(tmp_path, ["EXIT 4 Cloud Monitoring permission denied (403)"])
    assert res.returncode == 1
    assert "release of S4's leaked connections is NOT verified" in res.stderr
    assert "permission denied (403)" in res.stderr
    assert len((tmp_path / "argv").read_text().splitlines()) == 1


# --- db_backends.py ---------------------------------------------------------------------


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _series(database, points):
    return {"metric": {"labels": {"database": database}},
            "points": [{"interval": {"endTime": t}, "value": {"int64Value": str(v)}} for t, v in points]}


def test_helper_returns_the_newest_sample():
    seen = {}

    def urlopen(req, timeout=None, context=None):
        seen["url"] = req.full_url
        body = {"timeSeries": [_series("inventory", [("2026-10-27T13:40:00Z", 0), ("2026-10-27T13:39:00Z", 22)])]}
        return _Resp(json.dumps(body).encode())

    out = db_backends.fetch_latest("proj", "tok", "ata-inventory-db", "inventory", 900, urlopen=urlopen, now=1793108000)
    assert out == {"value": 0, "at": "2026-10-27T13:40:00Z", "epoch": 1793108400}
    assert "num_backends" in seen["url"] and "proj%3Aata-inventory-db" in seen["url"]
    assert "metric.labels.database%3D%22inventory%22" in seen["url"]


def test_helper_reports_no_sample_as_null_not_zero():
    out = db_backends.fetch_latest("proj", "tok", "db", "inventory", 900,
                                   urlopen=lambda *a, **k: _Resp(b"{}"), now=1793108000)
    assert out == {"value": None}


@pytest.mark.parametrize("code, rc", [(503, 3), (429, 3), (403, 4), (401, 4)])
def test_helper_http_errors_exit_non_zero(code, rc, capsys):
    def urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b"{}"))

    assert db_backends.main(["db_backends.py", "proj", "tok", "db", "inventory"], urlopen=urlopen) == rc
    assert capsys.readouterr().out == ""


def test_helper_empty_token_is_rejected(capsys):
    assert db_backends.main(["db_backends.py", "proj", "", "db", "inventory"]) == 4
