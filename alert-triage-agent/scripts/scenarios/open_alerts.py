#!/usr/bin/env python3
"""List OPEN Cloud Monitoring alerts on ata-* policies (read-only), for reset-scenarios.sh.

Usage: open_alerts.py PROJECT_ID TOKEN [RESET_START_EPOCH] [MAX_ALERT_AGE_SECS] [SERVING_REVISIONS]

Prints {"active": [...], "ignored": [...]} on stdout and exits 0. If the Alerts API
cannot be queried it prints the cause on stderr and exits non-zero; it never reports a
failed query as zero alerts. Exit QUERY_FAILED_EXIT (3) means a transient failure
(timeout, connection error, HTTP 429/5xx) worth retrying. Exit QUERY_REJECTED_EXIT (4)
means retrying cannot help (HTTP 401/403/other 4xx, TLS verification, CA bundle, empty
token).

TLS uses the same CA bundle resolution as scripts/servicenow-cleanup.py and
scripts/jira-cleanup.py: SSL_CERT_FILE or REQUESTS_CA_BUNDLE, then certifi, then
/etc/ssl/cert.pem on macOS, then Python's default trust store.
"""

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

ALERTS_URL = "https://monitoring.googleapis.com/v3/projects/{project}/alerts"
QUERY_FAILED_EXIT = 3
QUERY_REJECTED_EXIT = 4
CA_BUNDLE_HINT = "export SSL_CERT_FILE=/etc/ssl/cert.pem (macOS)"
MACOS_CA_BUNDLE = "/etc/ssl/cert.pem"


class AlertsQueryError(Exception):
    """The Cloud Monitoring Alerts API could not be queried.

    retryable: the failure may clear on its own (timeout, 429, 5xx).
    tls: the failure may be a CA bundle problem, so the SSL_CERT_FILE hint applies.
    """

    def __init__(self, message: str, retryable: bool = False, tls: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.tls = tls


TOKEN_LOGIN = {"adc": "gcloud auth application-default login", "gcloud": "gcloud auth login"}


def google_error_details(body: str):
    """(ErrorInfo reason, message) from a Google API JSON error body; blanks if absent."""
    try:
        err = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        return ("ACCESS_TOKEN_TYPE_UNSUPPORTED" if "ACCESS_TOKEN_TYPE_UNSUPPORTED" in body else ""), ""
    reasons = [d.get("reason", "") for d in err.get("details") or [] if isinstance(d, dict)]
    return next((r for r in reasons if r), ""), str(err.get("message") or "")


def http_error_message(code: int, reason: str, project_id: str, body: str = "") -> str:
    source = os.environ.get("ATA_MONITORING_TOKEN_SOURCE", "adc")
    api_reason, api_message = google_error_details(body)
    detail = f" (API: {api_message[:200]})" if api_message else ""
    if code == 401 and api_reason == "ACCESS_TOKEN_TYPE_UNSUPPORTED":
        if source == "adc":
            fix = ("Re-create ADC with a user or service account credential: gcloud auth application-default login,"
                   " or use --monitoring-token-source gcloud, or skip the check with --no-wait.")
        else:
            fix = ("Use Application Default Credentials: gcloud auth application-default login, then"
                   " ATA_MONITORING_TOKEN_SOURCE=adc (or --monitoring-token-source adc), or skip the check with --no-wait.")
        return (f"Cloud Monitoring does not accept this type of access token (401 ACCESS_TOKEN_TYPE_UNSUPPORTED)"
                f" from token source '{source}'. Federated or workforce identity tokens are rejected by the"
                f" Alerts API. {fix}")
    if code == 401:
        login = TOKEN_LOGIN.get(source, "gcloud auth login")
        return (f"Cloud Monitoring rejected your access token from token source '{source}' (401). Run: {login}"
                f" (or skip the check with --no-wait){detail}")
    if code == 403:
        return f"Cloud Monitoring permission denied (403), needs roles/monitoring.viewer on {project_id}{detail}"
    return f"Cloud Monitoring Alerts API returned HTTP {code} {reason}{detail}"


def read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def resolve_ca_bundle() -> Optional[str]:
    """Return the CA bundle path to verify TLS with, or None for Python's default."""
    for env_var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        val = os.environ.get(env_var)
        if val and val.strip():
            return val.strip()

    try:
        import certifi  # type: ignore[import-untyped]
        return certifi.where()
    except ImportError:
        pass

    if sys.platform == "darwin" and os.path.exists(MACOS_CA_BUNDLE):
        return MACOS_CA_BUNDLE

    return None


def create_ssl_context() -> ssl.SSLContext:
    """SSLContext with verification enforced, using the resolved CA bundle."""
    cafile = resolve_ca_bundle()
    if cafile is not None and not os.path.isfile(cafile):
        raise AlertsQueryError(f"CA bundle file not found: {cafile} (from SSL_CERT_FILE / REQUESTS_CA_BUNDLE)", tls=True)
    try:
        return ssl.create_default_context(cafile=cafile)
    except (ssl.SSLError, OSError) as exc:
        raise AlertsQueryError(f"could not load CA bundle {cafile}: {exc}", tls=True) from exc


def is_cert_verification_error(exc: BaseException) -> bool:
    """True if exc (or anything it wraps) is a TLS certificate verification failure."""
    curr: Optional[BaseException] = exc
    seen = set()
    while curr is not None and id(curr) not in seen:
        seen.add(id(curr))
        if isinstance(curr, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(curr):
            return True
        reason = getattr(curr, "reason", None)
        if isinstance(reason, BaseException):
            if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
                return True
        curr = curr.__cause__ or curr.__context__
    return False


def parse_time_to_epoch(ts_str):
    if not ts_str:
        return None
    try:
        return float(ts_str)
    except (ValueError, TypeError):
        pass
    try:
        s = str(ts_str).rstrip("Z")
        if "." in s:
            parts = s.split(".")
            frac = (parts[1] + "000000")[:6]
            s = f"{parts[0]}.{frac}"
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def extract_revision(alert):
    res = alert.get("resource") or {}
    labels = res.get("labels") or {}
    if "revision_name" in labels:
        return labels["revision_name"]
    metric = alert.get("metric") or {}
    m_labels = metric.get("labels") or {}
    if "revision_name" in m_labels:
        return m_labels["revision_name"]
    top_labels = alert.get("labels") or {}
    for k, v in top_labels.items():
        if "revision_name" in k:
            return v
    res_str = str(alert.get("resourceName") or alert.get("resource_name") or "")
    m = re.search(r"revision_name[=\s:]+([a-zA-Z0-9_-]+)", res_str)
    if m:
        return m.group(1)
    return None


def is_ata_alert(alert) -> bool:
    """Scoping: ata-* policies (label app=alert-triage-agent)."""
    policy = alert.get("policy", {})
    disp = str(policy.get("displayName") or "").strip().lower()
    pol_name = str(policy.get("name") or "").strip().lower()
    user_labels = policy.get("userLabels") or alert.get("userLabels") or {}
    if isinstance(user_labels, dict) and "alert-triage-agent" in (user_labels.get("app"), user_labels.get("managed_by")):
        return True
    return disp.startswith("ata-") or "ata-" in pol_name or "alert-triage-agent" in disp or "alert-triage-agent" in pol_name


def fetch_open_alerts(project_id: str, token: str, urlopen: Callable = urllib.request.urlopen) -> list:
    """Return every OPEN alert in the project. Raises AlertsQueryError on any failure."""
    context = create_ssl_context()
    alerts = []
    page_token = None
    while True:
        query = {"filter": 'state="OPEN"', "pageSize": "100"}
        if page_token:
            query["pageToken"] = page_token
        url = ALERTS_URL.format(project=project_id) + "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urlopen(req, timeout=15, context=context) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or exc.code >= 500
            message = http_error_message(exc.code, exc.reason, project_id, read_error_body(exc))
            raise AlertsQueryError(message, retryable=retryable) from exc
        except Exception as exc:
            if is_cert_verification_error(exc):
                raise AlertsQueryError(f"TLS certificate verification failed: {exc}", tls=True) from exc
            # Timeouts, connection resets, DNS, truncated or non-JSON bodies.
            raise AlertsQueryError(f"Cloud Monitoring Alerts API query failed: {exc}", retryable=True, tls=True) from exc
        if not isinstance(data, dict):
            raise AlertsQueryError(f"unexpected Alerts API response: {str(data)[:200]}", retryable=True)
        alerts.extend(data.get("alerts", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return alerts


def classify(alerts, reset_start_time: float, max_alert_age_secs: float, serving_revs_set) -> dict:
    """Split ata-* alerts into active ones and ones ignored as stale or non-serving."""
    active_alerts = []
    ignored_alerts = []
    for alert in alerts:
        if not is_ata_alert(alert):
            continue
        policy = alert.get("policy", {})
        disp = str(policy.get("displayName") or "").strip()
        pol_name = str(policy.get("name") or "").strip()
        alert_id = alert.get("name", "").split("/")[-1]
        open_time_str = alert.get("openTime") or alert.get("started_at") or ""
        open_epoch = parse_time_to_epoch(open_time_str)
        rev = extract_revision(alert)

        ignore_reasons = []
        if rev and serving_revs_set and (rev not in serving_revs_set):
            ignore_reasons.append(f"Non-serving revision ({rev})")
        if open_epoch is not None and reset_start_time > 0:
            if open_epoch < reset_start_time - max_alert_age_secs:
                age_min = int((reset_start_time - open_epoch) / 60)
                max_min = int(max_alert_age_secs / 60)
                ignore_reasons.append(f"Opened > {max_min}m before reset started ({age_min}m ago)")

        entry = {
            "id": alert_id,
            "name": alert.get("name", ""),
            "policy_display": disp or pol_name,
            "policy_name": pol_name,
            "open_time": open_time_str,
            "revision": rev or "",
        }
        if ignore_reasons:
            entry["ignore_reasons"] = ignore_reasons
            ignored_alerts.append(entry)
        else:
            active_alerts.append(entry)
    return {"active": active_alerts, "ignored": ignored_alerts}


def main(argv, urlopen: Callable = urllib.request.urlopen) -> int:
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    project_id, token = argv[0], argv[1]
    reset_start_time = float(argv[2]) if len(argv) > 2 and argv[2] else 0.0
    max_alert_age_secs = float(argv[3]) if len(argv) > 3 and argv[3] else 3600.0
    serving_raw = argv[4] if len(argv) > 4 else ""
    serving_revs_set = {r for r in serving_raw.replace(",", " ").split() if r}

    if not token:
        login = TOKEN_LOGIN.get(os.environ.get("ATA_MONITORING_TOKEN_SOURCE", "adc"), "gcloud auth login")
        sys.stderr.write(f"Cloud Monitoring query failed: empty access token. Run: {login}\n")
        return QUERY_REJECTED_EXIT
    try:
        alerts = fetch_open_alerts(project_id, token, urlopen=urlopen)
    except AlertsQueryError as exc:
        sys.stderr.write(f"Cloud Monitoring query failed: {exc}\n")
        if exc.tls:
            sys.stderr.write(f"Hint: if this is a CA certificate problem, {CA_BUNDLE_HINT}\n")
        return QUERY_FAILED_EXIT if exc.retryable else QUERY_REJECTED_EXIT
    print(json.dumps(classify(alerts, reset_start_time, max_alert_age_secs, serving_revs_set)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
