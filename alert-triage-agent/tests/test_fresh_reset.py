"""Short resets again: the reset's own cold start no longer forces the 600s alert window.

Live QA 2026-10-28 (deploy-evidence/final-qa/s1b, s2, s3 reset logs): every post-scenario
reset armed the 600s window because POST /admin/fault/reset hit a scaled-to-zero instance
(Cloud Run "Starting new instance", 3.0-3.8 s) and POST /admin/restock was the first
database call on it (~3.8 s), ~30 s apart; resets took 645-662 s. The latency policies have
a live 240s retest window, so such a burst cannot open an alert. After step 5b the reset
reads the live policies (scripts/scenarios/latency_retest.py, read-only) and releases a
window that only slow non-5xx requests armed when the burst is too short; it prints the
remaining risk. Step 5c (num_backends) now runs alongside step 6 instead of before it.
"""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from tests.test_reset_terminal_groups import done_at, gcs, out, run_step6  # noqa: F401
from tests.test_stray_alerts import BASE, RESET_SCRIPT, TEST_BASH, WINDOW_LEFT, _main_flow, _stub_env, _window_left

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "scenarios"))
_spec = importlib.util.spec_from_file_location("latency_retest", SCRIPTS_DIR / "scenarios" / "latency_retest.py")
latency_retest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(latency_retest)


def _policy(name, duration="240s", alignment="60s", threshold=2000, enabled=True, comparison="COMPARISON_GT"):
    return {
        "displayName": name, "enabled": enabled,
        "conditions": [{"displayName": "latency", "conditionThreshold": {
            "duration": duration, "comparison": comparison, "thresholdValue": threshold,
            "aggregations": [{"alignmentPeriod": alignment, "perSeriesAligner": "ALIGN_DELTA"}]}}],
    }


LIVE = [_policy("ata-inventory-latency"), _policy("ata-orders-latency"), _policy("ata-inventory-5xx", "60s")]


# --- latency_retest.decide ---------------------------------------------------------


@pytest.mark.parametrize("span", [0, 35, 59])
def test_qa_cold_start_burst_cannot_open_an_alert_with_the_live_240s_window(span):
    # QA: fault reset 01:22:55 (3.8 s) .. restock end ~01:23:30: ~35 s.
    d = latency_retest.decide(LIVE, "ata", span, 2000)
    assert d["safe"] is True, d
    assert d["hold_secs"] == 180 and d["min_duration_secs"] == 240
    assert "ata-inventory-latency (p99 > 2000 ms for 240s, 60s alignment)" in d["policies"]
    assert "ata-orders-latency (p99 > 2000 ms for 240s, 60s alignment)" in d["policies"]


@pytest.mark.parametrize("span", [60, 119, 600])
def test_a_burst_of_a_minute_or_more_is_not_assumed_safe(span):
    d = latency_retest.decide(LIVE, "ata", span, 2000)
    assert d["safe"] is False
    assert "not within its 240s retest window" in d["reason"]


def test_the_old_60s_retest_window_never_releases_the_window():
    d = latency_retest.decide([_policy("ata-inventory-latency", "60s"), _policy("ata-orders-latency", "60s")],
                              "ata", 0, 2000)
    assert d["safe"] is False


@pytest.mark.parametrize("policies, reason", [
    ([_policy("ata-inventory-latency")], "alert policy ata-orders-latency not found"),
    ([_policy("ata-inventory-latency", threshold=1500), _policy("ata-orders-latency")], "threshold 1500 ms is below"),
    ([_policy("ata-inventory-latency", duration=None), _policy("ata-orders-latency")], "cannot read duration"),
    ([_policy("ata-inventory-latency", comparison="COMPARISON_LT"), _policy("ata-orders-latency")], "comparison"),
    ([{"displayName": "ata-inventory-latency", "conditions": [{"displayName": "mql", "conditionMonitoringQueryLanguage": {}}]},
      _policy("ata-orders-latency")], "is not a threshold condition"),
    ([{"displayName": "ata-inventory-latency", "conditions": []}, _policy("ata-orders-latency")], "has no conditions"),
])
def test_anything_unexpected_in_the_policies_is_never_safe(policies, reason):
    d = latency_retest.decide(policies, "ata", 10, 2000)
    assert d["safe"] is False
    assert reason in d["reason"]


def test_prefix_is_honoured():
    assert latency_retest.decide(LIVE, "xyz", 10, 2000)["safe"] is False
    assert latency_retest.decide([_policy("xyz-inventory-latency"), _policy("xyz-orders-latency")],
                                 "xyz", 10, 2000)["safe"] is True


@pytest.mark.parametrize("order", [0, 1])
def test_a_duplicate_policy_name_is_evaluated_too_and_the_strictest_wins(order):
    """Display names are not unique: a 60s copy of ata-inventory-latency keeps the window, in either order."""
    dupes = [_policy("ata-inventory-latency", "60s"), _policy("ata-inventory-latency")]
    if order:
        dupes.reverse()
    d = latency_retest.decide(dupes + [_policy("ata-orders-latency")], "ata", 35, 2000)
    assert d["safe"] is False, d
    assert "not within its 60s retest window" in d["reason"]
    assert "ata-inventory-latency [" in d["reason"] and "of 2 policies with this name]" in d["reason"]


def test_duplicate_policies_that_are_all_safe_still_release():
    d = latency_retest.decide([_policy("ata-inventory-latency"), _policy("ata-inventory-latency", "300s"),
                               _policy("ata-orders-latency")], "ata", 35, 2000)
    assert d["safe"] is True and d["min_duration_secs"] == 240
    assert len([p for p in d["policies"] if p.startswith("ata-inventory-latency [")]) == 2


def test_a_disabled_policy_cannot_alert():
    d = latency_retest.decide([_policy("ata-inventory-latency", enabled=False), _policy("ata-orders-latency")],
                              "ata", 10, 2000)
    assert d["safe"] is True
    assert "ata-inventory-latency (disabled)" in d["policies"]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_main_pages_through_policies_and_prints_the_verdict(capsys):
    pages = [{"alertPolicies": LIVE[:1], "nextPageToken": "p2"}, {"alertPolicies": LIVE[1:]}]
    urls = []

    def urlopen(req, timeout, context):
        urls.append(req.full_url)
        assert req.headers["Authorization"] == "Bearer tok"
        return _Resp(json.dumps(pages.pop(0)).encode())

    assert latency_retest.main(["x", "proj", "tok", "ata", "35", "2000"], urlopen=urlopen) == 0
    assert json.loads(capsys.readouterr().out)["safe"] is True
    assert len(urls) == 2 and "pageToken=p2" in urls[1] and "/projects/proj/alertPolicies" in urls[0]


@pytest.mark.parametrize("code, rc", [(403, 4), (503, 3), (429, 3)])
def test_main_http_errors_exit_non_zero_and_print_no_verdict(code, rc, capsys):
    def urlopen(req, timeout, context):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b"{}"))

    assert latency_retest.main(["x", "proj", "tok", "ata", "35", "2000"], urlopen=urlopen) == rc
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR:" in captured.err


@pytest.mark.parametrize("argv", [["x", "proj", "tok", "ata", "-1", "2000"], ["x", "proj", "tok", "ata", "a", "2000"],
                                  ["x", "proj", "", "ata", "1", "2000"], ["x"]])
def test_main_rejects_bad_arguments(argv, capsys):
    assert latency_retest.main(argv, urlopen=lambda *a, **k: _Resp(b'{"alertPolicies": []}')) == 4


# --- reset-scenarios.sh: settle_alert_window ----------------------------------------

FAKE_RETEST = """import json, sys
from pathlib import Path
here = Path(sys.argv[0]).parent
(here / "retest_argv").write_text(" ".join(sys.argv[1:]))
mode = (here / "retest_mode").read_text().strip()
if mode == "fail":
    print("ERROR: Cloud Monitoring API returned HTTP 403", file=sys.stderr)
    sys.exit(4)
if mode == "garbage":
    print("not json")
    sys.exit(0)
if mode == "nosafe":
    print(json.dumps({"reason": "x"}))
    sys.exit(0)
print(json.dumps({"safe": mode == "safe", "reason": "REASON-" + mode, "policies": ["ata-inventory-latency (p99 > 2000 ms for 240s, 60s alignment)"]}))
"""


def _settle(tmp_path, body, mode="safe", statuses="", **extra):
    (tmp_path / "retest.py").write_text(FAKE_RETEST)
    (tmp_path / "retest_mode").write_text(mode)
    script = tmp_path / "run.sh"
    script.write_text(f'set -euo pipefail\nsource "{RESET_SCRIPT}"\nLATENCY_RETEST_PY="{tmp_path / "retest.py"}"\n'
                      'resolve_monitoring_token() { echo tok; }\n'
                      f"{body}\nsettle_alert_window\n{WINDOW_LEFT}\n")
    return subprocess.run([TEST_BASH, str(script)], cwd=str(tmp_path), capture_output=True, text=True,
                          env=_stub_env(tmp_path, statuses, **extra), timeout=60)


QA_REQUESTS = (f'demo_request "POST {BASE}/admin/fault/reset" "{BASE}/admin/fault/reset"\n'
               f'demo_request "POST https://ord.example.run.app/admin/fault/reset" "https://ord.example.run.app/admin/fault/reset"\n'
               f'demo_request "POST {BASE}/admin/restock" "{BASE}/admin/restock"\n')
QA_STATUSES = "inv.example.run.app/admin/fault/reset 200 3.845\nord.example.run.app/admin/fault/reset 200 3.449\n/admin/restock 200 3.812\n"


def test_qa_reset_cold_start_releases_the_window_and_states_the_remaining_risk(tmp_path):
    res = _settle(tmp_path, QA_REQUESTS, statuses=QA_STATUSES)
    assert res.returncode == 0, out(res)
    assert _window_left(res) == 0
    assert "checked against the live policies after step 5b" in res.stdout
    assert ("Alert window released: the slow request(s) of this reset "
            f"(POST {BASE}/admin/fault/reset (3845 ms); POST https://ord.example.run.app/admin/fault/reset (3449 ms); "
            f"POST {BASE}/admin/restock (3812 ms)) cannot open a latency alert: REASON-safe") in res.stdout
    assert "Remaining risk (not waited for)" in res.stdout
    assert "--thorough waits the full 600s instead" in res.stdout
    project, token, prefix, span, slow = (tmp_path / "retest_argv").read_text().split()
    assert (project, token, prefix, slow) == ("proj", "tok", "ata", "2000")
    # Start of the first slow request to the end of the last one (3.812 s rounds up to 4).
    assert 4 <= int(span) <= 15


@pytest.mark.parametrize("mode, msg", [
    ("unsafe", "Alert window kept ("),
    ("fail", "could not read the latency alert policies: ERROR: Cloud Monitoring API returned HTTP 403"),
    ("garbage", "unparseable latency_retest.py output: not json"),
    ("nosafe", 'unparseable latency_retest.py output: {"reason": "x"}'),
])
def test_window_is_kept_unless_the_live_policies_say_it_is_safe(tmp_path, mode, msg):
    res = _settle(tmp_path, QA_REQUESTS, mode=mode, statuses=QA_STATUSES)
    assert res.returncode == 0, out(res)
    assert msg in res.stdout
    if mode == "unsafe":
        assert "REASON-unsafe" in res.stdout
    assert 570 <= _window_left(res) <= 600
    assert "Alert window released" not in res.stdout


def test_no_token_keeps_the_window(tmp_path):
    res = _settle(tmp_path, QA_REQUESTS + "resolve_monitoring_token() { echo 'no adc' >&2; return 1; }\n",
                  statuses=QA_STATUSES)
    assert "Alert window kept (" in res.stdout and "no Cloud Monitoring token" in res.stdout, out(res)
    assert _window_left(res) > 0
    assert not (tmp_path / "retest_argv").exists()


@pytest.mark.parametrize("statuses, msg", [
    ("/admin/fault/reset 503 0.1\n/admin/restock 200 3.8\n", "a 5xx, failed or unmeasured request"),
    ("/admin/fault/reset fail\n/admin/restock 200 3.8\n", "a 5xx, failed or unmeasured request"),
])
def test_a_5xx_or_failed_request_keeps_the_full_window_even_with_a_safe_policy(tmp_path, statuses, msg):
    res = _settle(tmp_path, f'demo_request "POST {BASE}/admin/fault/reset" "{BASE}/admin/fault/reset"\n'
                            f'demo_request "POST {BASE}/admin/restock" "{BASE}/admin/restock"\n', statuses=statuses)
    assert msg in res.stdout, out(res)
    assert 570 <= _window_left(res) <= 600
    assert not (tmp_path / "retest_argv").exists()


def test_alert_window_from_start_keeps_the_full_window(tmp_path):
    res = _settle(tmp_path, 'extend_alert_window "$(date +%s)" "--alert-window-from-start: x"\n' + QA_REQUESTS,
                  statuses=QA_STATUSES)
    assert "Alert window kept: a 5xx, failed or unmeasured request (or --alert-window-from-start)" in res.stdout, out(res)
    assert _window_left(res) > 0


@pytest.mark.parametrize("how", ["env", "flag"])
def test_thorough_waits_the_full_window(tmp_path, how):
    if how == "env":
        res = _settle(tmp_path, QA_REQUESTS, statuses=QA_STATUSES, ATA_RESET_THOROUGH="true")
    else:
        res = _settle(tmp_path, 'THOROUGH=true\n' + QA_REQUESTS, statuses=QA_STATUSES)
    assert "--thorough: waiting the full alert window for the slow request(s)" in res.stdout, out(res)
    assert "step 6 keeps watching for alerts until then before archiving" in res.stdout
    assert 570 <= _window_left(res) <= 600
    assert not (tmp_path / "retest_argv").exists()


def test_no_slow_request_means_no_policy_query(tmp_path):
    res = _settle(tmp_path, f'demo_request "GET {BASE}/health" "{BASE}/health"\n')
    assert res.returncode == 0, out(res)
    assert _window_left(res) == 0
    assert not (tmp_path / "retest_argv").exists()


def test_invalid_thorough_setting_fails_before_any_step(tmp_path):
    env = _stub_env(tmp_path, ATA_RESET_THOROUGH="yes")
    res = subprocess.run([TEST_BASH, str(RESET_SCRIPT)], cwd=str(tmp_path), capture_output=True, text=True,
                         env=env, timeout=60)
    assert res.returncode != 0
    assert "Invalid ATA_RESET_THOROUGH 'yes' (expected true or false)." in out(res)
    assert "--> 1." not in res.stdout


def test_help_documents_thorough():
    res = subprocess.run([TEST_BASH, str(RESET_SCRIPT), "--help"], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "MOCK_MODE": "true"})
    assert "--thorough" in res.stdout and "ATA_RESET_THOROUGH" in res.stdout


def test_settle_runs_after_the_last_demo_request_and_before_the_window_report():
    flow = _main_flow()
    restock = flow.index('restock_inventory "${INV_URL}" || exit 1\n    fi\nelse')
    settle = flow.index("\nsettle_alert_window\n")
    report = flow.index('log_warn "    Alert window armed: step 6 watches')
    assert restock < settle < report < flow.index("\nwait_for_open_alerts\n")


# --- step 5c runs alongside step 6 --------------------------------------------------


def test_db_check_runs_in_the_background_and_is_finished_before_archiving():
    flow = _main_flow()
    bg = flow.index('verify_db_backends_released "${FAULT_RESET_EPOCH}" &\n    DB_CHECK_PID=$!')
    wait6 = flow.index("\nwait_for_open_alerts\n")
    finish = flow.index("\nfinish_db_backends_check\n")
    archive = flow.index("archive_active_incidents || exit 1")
    assert bg < wait6 < finish < archive


_WAIT_DONE = 'while kill -0 "${DB_CHECK_PID}" 2>/dev/null; do command sleep 0.05; done\n'


def test_a_failed_db_check_stops_step6_at_once(tmp_path, gcs):
    res = run_step6(tmp_path, gcs, fake_clock=True, poll=15, quiet=180,
                    pre='( echo "FATAL: db still leaking" >&2; exit 1 ) &\nDB_CHECK_PID=$!\n' + _WAIT_DONE)
    assert res.returncode == 1
    assert "FATAL: step 5c failed (above)" in res.stderr
    assert "Incident files were NOT archived." in res.stderr
    assert "STEP6_DONE" not in res.stdout


def test_a_db_check_that_fails_after_step6_still_blocks_the_archive(tmp_path, gcs):
    res = run_step6(tmp_path, gcs, pre='( command sleep 0.5; exit 1 ) &\nDB_CHECK_PID=$!\n',
                    after='finish_db_backends_check\necho ARCHIVED')
    assert res.returncode == 1, out(res)
    assert "STEP6_DONE" in res.stdout
    assert "Waiting for step 5c" in res.stdout
    assert "FATAL: step 5c failed (above)" in res.stderr
    assert "ARCHIVED" not in res.stdout


def test_a_step6_failure_stops_a_db_check_that_is_still_polling(tmp_path, gcs):
    res = run_step6(tmp_path, gcs, fake_clock=True, poll=15, timeout=30, alerts=[1],
                    pre='( command sleep 5; echo DB_CHECK_STILL_RUNNING > db_marker ) &\nDB_CHECK_PID=$!\n'
                        'echo "$DB_CHECK_PID" > db_pid\n')
    assert res.returncode == 1, out(res)
    assert "Timed out waiting for open ATA alerts to close" in out(res)
    pid = int((tmp_path / "db_pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not (tmp_path / "db_marker").exists()


def test_a_successful_db_check_overlaps_the_quiet_period(tmp_path, gcs):
    res = run_step6(tmp_path, gcs, fake_clock=True, poll=15, quiet=180,
                    pre='( exit 0 ) &\nDB_CHECK_PID=$!\n' + _WAIT_DONE,
                    after='finish_db_backends_check\necho ARCHIVED')
    assert res.returncode == 0, out(res)
    assert done_at(res) == 180
    assert "ARCHIVED" in res.stdout


# --- docs ---------------------------------------------------------------------------


def test_demo_md_states_the_new_reset_duration():
    demo = (Path(__file__).resolve().parents[1] / "DEMO.md").read_text()
    assert "--thorough" in demo
    # Measured live 2026-10-28 (no longer an estimate), with the S4 / cold-start exception.
    assert "226–234 s typical (measured live, 2026-10-28)" in demo
    assert "not yet measured" not in demo
    assert re.search(r"up to about 10–11 min after S4", demo)
