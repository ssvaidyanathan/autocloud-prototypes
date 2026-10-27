"""Stray latency tickets from single cold-start requests (QA round 2, INC0010327/INC0010328).

The latency policies (p99 > 2000 ms per service, 60s alignment) used a 60s retest window,
so one cold-start burst on an idle revision (one 60s window with p99 ~4.9 s at 4-12 rpm)
opened a P2 ticket ~6 min later, after the reset had finished. Live per-minute data
(Cloud Monitoring, 2026-10-27): the strays violated exactly 1 window each, the S5 deploy
cold start 2 windows (09:26 at 4 rpm, 09:27 at 47 rpm), S2 8 consecutive windows
(inventory-api 87-89 rpm, orders-api 30 rpm). A request-volume AND gate would not stop the
S5 cold start (47 rpm) and, per Cloud Monitoring, an AND policy opens one alert per met
condition (a second, request_count alert per firing). So the fix is the retest window.
"""

import re
from pathlib import Path

OBS = Path(__file__).resolve().parents[1] / "infra" / "terraform" / "observability"


def _policy_block(main_tf: str, name: str) -> str:
    start = main_tf.index(f'resource "google_monitoring_alert_policy" "{name}"')
    nxt = main_tf.find('resource "google_monitoring_alert_policy"', start + 1)
    return main_tf[start: nxt if nxt != -1 else len(main_tf)]


def _variable_block(name: str) -> str:
    text = (OBS / "variables.tf").read_text()
    start = text.index(f'variable "{name}"')
    nxt = text.find("\nvariable ", start + 1)
    return text[start: nxt if nxt != -1 else len(text)]


def _default_duration() -> int:
    m = re.search(r"default\s*=\s*(\d+)", _variable_block("latency_duration_secs"))
    assert m
    return int(m.group(1))


def test_latency_policies_take_the_retest_window_from_a_variable():
    main_tf = (OBS / "main.tf").read_text()
    for name in ("orders_latency", "inventory_latency"):
        block = _policy_block(main_tf, name)
        assert 'duration                = "${var.latency_duration_secs}s"' in block, name
        assert 'alignment_period     = "60s"' in block, name
        # One condition, OR combiner: no request_count AND gate (it would open a second alert).
        assert block.count("conditions {") == 1, name
        assert 'combiner              = "OR"' in block, name
        assert "request_count" not in block, name


def test_retest_window_default_outlasts_a_cold_start_but_not_s2():
    alignment = 60
    duration = _default_duration()
    # A cold start (one ~5 s burst) raises the p99 of at most 2 consecutive 60s windows.
    assert duration >= 3 * alignment
    # S2 held p99 > 6500 ms for 8 consecutive windows (7 min hold); the alert must still open
    # with room to spare (at least two windows more than the retest window needs).
    s2_violating_windows = 8
    assert duration // alignment + 2 <= s2_violating_windows


def test_retest_window_variable_is_validated():
    block = _variable_block("latency_duration_secs")
    assert "type        = number" in block
    assert "validation {" in block
    assert "var.latency_duration_secs >= 60" in block
    assert "var.latency_duration_secs % 60 == 0" in block


def test_other_policies_keep_their_literal_60s_retest_window():
    main_tf = (OBS / "main.tf").read_text()
    for name in ("orders_5xx", "inventory_5xx", "apigee_orders_5xx", "apigee_orders_429",
                 "cloudsql_connections", "cloudsql_cpu"):
        assert re.search(r'duration\s*=\s*"60s"', _policy_block(main_tf, name)), name
