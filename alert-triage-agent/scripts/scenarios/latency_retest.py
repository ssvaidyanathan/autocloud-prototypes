#!/usr/bin/env python3
"""Can the reset's own slow requests open a latency alert? (read-only), for reset-scenarios.sh.

Usage: latency_retest.py PROJECT_ID TOKEN NAME_PREFIX SPAN_SECS SLOW_REQUEST_MS

Reads the live <prefix>-inventory-latency and <prefix>-orders-latency alert policies
(Cloud Monitoring alertPolicies.list, read-only) and decides whether a burst of slow,
non-5xx requests lasting SPAN_SECS (first slow request start to last slow request end) can
keep any of their threshold conditions true for the condition's retest window (duration).

Model (live data, 2026-10-27, see ata-stray): a slow request raises the p99 of the
alignment windows it falls into, and k adjacent violating windows keep the condition true
for just under (k + 1) * alignment. A burst of S seconds touches at most S // A + 2
windows, so it holds the condition for under (S // A + 3) * A. The burst cannot open an
alert when that, plus one alignment period of margin (the margin ata-stray chose 240s for),
is at most the retest window of every enabled condition. With the Terraform defaults
(240s retest, 60s alignment) a burst shorter than 60 s holds the condition for under
180 s (+60 s margin = 240 s): safe; a burst of 60 s or more is not. A missing policy, a non-threshold condition, a
threshold below SLOW_REQUEST_MS or an unreadable field is never assumed safe.

Prints {"safe": bool, "reason": str, "hold_secs": int|null, "min_duration_secs": int|null,
"policies": [...]}. Errors follow open_alerts.py: the cause on stderr, exit 3 when
retrying may help, exit 4 when it cannot. A failed query is never reported as safe.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # open_alerts.py, also under python3 -I
from open_alerts import (  # noqa: E402
    CA_BUNDLE_HINT,
    QUERY_FAILED_EXIT,
    QUERY_REJECTED_EXIT,
    AlertsQueryError,
    create_ssl_context,
    google_error_details,
    http_error_message,
    is_cert_verification_error,
    read_error_body,
)

POLICIES_URL = "https://monitoring.googleapis.com/v3/projects/{project}/alertPolicies"
SERVICES = ("inventory", "orders")


def _secs(value) -> Optional[int]:
    """'240s' / '240.0s' -> 240; anything else -> None."""
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    try:
        secs = float(value[:-1])
    except ValueError:
        return None
    return int(secs) if secs >= 0 else None


def fetch_policies(project_id: str, token: str, urlopen: Callable = urllib.request.urlopen) -> list:
    if not token:
        raise AlertsQueryError("empty access token")
    policies, page_token = [], ""
    while True:
        params = {"pageSize": "200"}
        if page_token:
            params["pageToken"] = page_token
        req = urllib.request.Request(
            f"{POLICIES_URL.format(project=project_id)}?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urlopen(req, timeout=30, context=create_ssl_context()) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = read_error_body(exc)
            reason, _ = google_error_details(text)
            raise AlertsQueryError(http_error_message(exc.code, reason, project_id, text),
                                   retryable=exc.code == 429 or exc.code >= 500)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            tls = is_cert_verification_error(exc)
            raise AlertsQueryError(f"Cloud Monitoring alertPolicies query failed: {exc}", retryable=not tls, tls=tls)
        except ValueError as exc:
            raise AlertsQueryError(f"Cloud Monitoring alertPolicies response is not JSON: {exc}", retryable=True)
        policies.extend(body.get("alertPolicies") or [])
        page_token = body.get("nextPageToken") or ""
        if not page_token:
            return policies


def decide(policies: list, name_prefix: str, span_secs: int, slow_request_ms: float) -> dict:
    result = {"safe": False, "reason": "", "hold_secs": None, "min_duration_secs": None, "policies": []}
    by_name = {p.get("displayName"): p for p in policies}
    hold_max, min_duration = 0, None
    for svc in SERVICES:
        name = f"{name_prefix}-{svc}-latency"
        policy = by_name.get(name)
        if policy is None:
            result["reason"] = f"alert policy {name} not found, so its retest window is unknown"
            return result
        if policy.get("enabled") is False:
            result["policies"].append(f"{name} (disabled)")
            continue
        conditions = policy.get("conditions") or []
        if not conditions:
            result["reason"] = f"{name} has no conditions"
            return result
        for cond in conditions:
            thr = cond.get("conditionThreshold")
            if not thr:
                result["reason"] = f"{name} condition '{cond.get('displayName')}' is not a threshold condition"
                return result
            duration = _secs(thr.get("duration", "0s"))
            aggs = thr.get("aggregations") or [{}]
            alignment = _secs(aggs[0].get("alignmentPeriod"))
            threshold = thr.get("thresholdValue")
            if duration is None or not alignment or threshold is None:
                result["reason"] = (f"{name}: cannot read duration/alignmentPeriod/thresholdValue "
                                    f"({thr.get('duration')!r}, {aggs[0].get('alignmentPeriod')!r}, {threshold!r})")
                return result
            if thr.get("comparison") != "COMPARISON_GT":
                result["reason"] = f"{name}: comparison {thr.get('comparison')} (expected COMPARISON_GT)"
                return result
            if float(threshold) < float(slow_request_ms):
                result["reason"] = (f"{name}: threshold {threshold:g} ms is below ATA_RESET_SLOW_REQUEST_MS "
                                    f"({slow_request_ms:g} ms), so requests the reset did not count as slow can violate it")
                return result
            hold = (span_secs // alignment + 3) * alignment
            hold_max = max(hold_max, hold)
            if min_duration is None or duration < min_duration:
                min_duration = duration
            result["policies"].append(f"{name} (p99 > {threshold:g} ms for {duration}s, {alignment}s alignment)")
            if hold + alignment > duration:
                result.update(hold_secs=hold, min_duration_secs=duration, reason=(
                    f"slow requests spanning {span_secs}s can keep {name} true for up to ~{hold}s, "
                    f"which with a {alignment}s margin is not within its {duration}s retest window"))
                return result
    result.update(safe=True, hold_secs=hold_max or None, min_duration_secs=min_duration, reason=(
        f"slow requests spanning {span_secs}s keep a latency condition true for under {hold_max}s, "
        f"which with one alignment period of margin is within the shortest retest window ({min_duration}s)"
        if min_duration is not None
        else "both latency policies are disabled"))
    return result


def main(argv, urlopen: Callable = urllib.request.urlopen) -> int:
    if len(argv) != 6:
        print("usage: latency_retest.py PROJECT_ID TOKEN NAME_PREFIX SPAN_SECS SLOW_REQUEST_MS", file=sys.stderr)
        return QUERY_REJECTED_EXIT
    project_id, token, prefix = argv[1:4]
    try:
        span, slow_ms = int(argv[4]), float(argv[5])
    except ValueError:
        print(f"ERROR: invalid SPAN_SECS/SLOW_REQUEST_MS {argv[4]!r} {argv[5]!r}", file=sys.stderr)
        return QUERY_REJECTED_EXIT
    if span < 0:
        print(f"ERROR: negative SPAN_SECS {span}", file=sys.stderr)
        return QUERY_REJECTED_EXIT
    try:
        print(json.dumps(decide(fetch_policies(project_id, token, urlopen=urlopen), prefix, span, slow_ms)))
        return 0
    except AlertsQueryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if exc.tls:
            print(f"Hint: {CA_BUNDLE_HINT}", file=sys.stderr)
        return QUERY_FAILED_EXIT if exc.retryable else QUERY_REJECTED_EXIT


if __name__ == "__main__":
    sys.exit(main(sys.argv))
