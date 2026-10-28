#!/usr/bin/env python3
"""Latest Cloud SQL num_backends sample of one database (read-only), for reset-scenarios.sh.

Usage: db_backends.py PROJECT_ID TOKEN INSTANCE DATABASE [LOOKBACK_SECS]

Reads cloudsql.googleapis.com/database/postgresql/num_backends for
database_id="PROJECT_ID:INSTANCE" and metric label database=DATABASE over the last
LOOKBACK_SECS (default 900) and prints {"value": N, "at": "<sample time>", "epoch": E}
for the newest sample, or {"value": null} when there is none. Cloud SQL samples every
60 s and a sample becomes visible up to 210 s later, so the newest sample can be ~4 min old.

Errors follow open_alerts.py: the cause on stderr, exit 3 when retrying may help (timeout,
connection error, HTTP 429/5xx), exit 4 when it cannot (401/403/other 4xx, TLS, empty
token). A failed query is never reported as zero backends.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

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
    parse_time_to_epoch,
    read_error_body,
)

TIMESERIES_URL = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
METRIC = "cloudsql.googleapis.com/database/postgresql/num_backends"


def _rfc3339(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def fetch_latest(project_id: str, token: str, instance: str, database: str, lookback_secs: float,
                 urlopen: Callable = urllib.request.urlopen, now: float = None) -> dict:
    if not token:
        raise AlertsQueryError("empty access token")
    now = time.time() if now is None else now
    query = urllib.parse.urlencode({
        "filter": (
            f'metric.type="{METRIC}" AND resource.labels.database_id="{project_id}:{instance}" '
            f'AND metric.labels.database="{database}"'
        ),
        "interval.startTime": _rfc3339(now - lookback_secs),
        "interval.endTime": _rfc3339(now),
    })
    req = urllib.request.Request(
        f"{TIMESERIES_URL.format(project=project_id)}?{query}",
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
        raise AlertsQueryError(f"Cloud Monitoring timeSeries query failed: {exc}", retryable=not tls, tls=tls)
    except ValueError as exc:
        raise AlertsQueryError(f"Cloud Monitoring timeSeries response is not JSON: {exc}", retryable=True)

    newest = None
    for series in body.get("timeSeries") or []:
        for point in series.get("points") or []:
            at = (point.get("interval") or {}).get("endTime")
            epoch = parse_time_to_epoch(at)
            value = (point.get("value") or {}).get("int64Value")
            if epoch is None or value is None:
                continue
            if newest is None or epoch > newest["epoch"]:
                newest = {"value": int(value), "at": at, "epoch": int(epoch)}
    return newest or {"value": None}


def main(argv, urlopen: Callable = urllib.request.urlopen) -> int:
    if len(argv) < 5:
        print("usage: db_backends.py PROJECT_ID TOKEN INSTANCE DATABASE [LOOKBACK_SECS]", file=sys.stderr)
        return QUERY_REJECTED_EXIT
    project_id, token, instance, database = argv[1:5]
    lookback = float(argv[5]) if len(argv) > 5 else 900.0
    try:
        print(json.dumps(fetch_latest(project_id, token, instance, database, lookback, urlopen=urlopen)))
        return 0
    except AlertsQueryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if exc.tls:
            print(f"Hint: {CA_BUNDLE_HINT}", file=sys.stderr)
        return QUERY_FAILED_EXIT if exc.retryable else QUERY_REJECTED_EXIT


if __name__ == "__main__":
    sys.exit(main(sys.argv))
