"""ApigeeSpecialist Agent for Alert Triage Runtime.

Analyzes Apigee Cloud Monitoring metrics (proxy/response_count, target/latencies),
ata-apigee-access Cloud Logging records, and client access distribution.
All telemetry is anchored to the alert start time using real read-only GCP queries.
"""

from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Optional

from ..config import AgentConfig
from ..contracts.alert import AlertEvent
from ..contracts.report import EvidenceItem
from .base import BaseSpecialist, EvidenceDossier

logger = logging.getLogger(__name__)

# Apigee fault raised when the target does not answer within the proxy's target timeout.
GATEWAY_TIMEOUT_FAULT = "GatewayTimeout"
# Apigee fault raised by a SpikeArrest policy (metric fault_code ends with it, access-log fault_source equals it).
SPIKE_ARREST_FAULT = "SpikeArrestViolation"
# Access log written by the Apigee proxies' MessageLogging policy.
ACCESS_LOG_NAME = "ata-apigee-access"


TARGET_LATENCIES_METRIC = "apigee.googleapis.com/target/latencies"


def _nearest_rank(sorted_vals: list[float], q: float) -> int:
    return int(round(sorted_vals[min(int(len(sorted_vals) * q), len(sorted_vals) - 1)]))


def target_latency_summary(logged_ms: list[float], minute_means_ms: list[float]) -> Optional[dict[str, Any]]:
    """P50/P95/P99 over one population, with the population named in `basis`.

    Per-request access-log latencies are used when there are any. Otherwise the percentiles
    are over the per-minute target/latencies means, each minute counted once: a percentile of
    means, not of requests. The two are never mixed. None when neither was captured.
    """
    if logged_ms:
        vals, noun, source = logged_ms, "logged request", ACCESS_LOG_NAME
    elif minute_means_ms:
        vals, noun, source = minute_means_ms, "per-minute mean", TARGET_LATENCIES_METRIC
    else:
        return None
    ordered = sorted(vals)
    n = len(ordered)
    return {
        "p50_ms": _nearest_rank(ordered, 0.50),
        "p95_ms": _nearest_rank(ordered, 0.95),
        "p99_ms": _nearest_rank(ordered, 0.99),
        "population": f"{n} {noun}{'' if n == 1 else 's'}",
        "basis": f"percentiles of {n} {noun}{'' if n == 1 else 's'} ({source})",
    }


def access_log_name(project_id: str) -> str:
    """Full Cloud Logging log name of the Apigee access log in `project_id`."""
    return f"projects/{project_id}/logs/{ACCESS_LOG_NAME}"


def _entry_time(entry: Any) -> Optional[datetime]:
    ts = getattr(entry, "timestamp", None)
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return None


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarize_revision_change(
    revisions: dict[str, dict[str, Any]], latest_revision: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Describe the switch to the newest proxy revision seen in the access log, or None.

    ``revisions`` maps revision -> {first, last, requests, spike_arrest_faults} for one
    environment; ``latest_revision`` is the revision of the newest entry read. Only facts from
    the log are returned: which revision served before and after, when each was first and
    last seen, and the per-revision request and SpikeArrest fault counts. A request rate is
    given only for a revision that served for at least a minute. None when the newest entry
    is not on the most recently introduced revision (e.g. A -> B -> A): the switch is not the
    current state, so it is not reported as one.
    """
    timed = [(r, v) for r, v in revisions.items() if v.get("first") and v.get("last")]
    if len(timed) < 2:
        return None
    timed.sort(key=lambda rv: rv[1]["first"])
    (prev_rev, prev), (new_rev, new) = timed[-2], timed[-1]
    if latest_revision is not None and latest_revision != new_rev:
        logger.info(
            "Access log: newest entry is on revision %s, not on the last introduced revision %s; no revision switch reported",
            latest_revision,
            new_rev,
        )
        return None

    def rate(v: dict[str, Any]) -> Optional[float]:
        minutes = (v["last"] - v["first"]).total_seconds() / 60.0
        return round(v["requests"] / minutes, 1) if minutes >= 1.0 else None

    return {
        "previous_revision": prev_rev,
        "new_revision": new_rev,
        "previous_last_served_at": _iso(prev["last"]),
        "new_first_served_at": _iso(new["first"]),
        "previous_requests": prev["requests"],
        "new_requests": new["requests"],
        "previous_spike_arrest_faults": prev["spike_arrest_faults"],
        "new_spike_arrest_faults": new["spike_arrest_faults"],
        "previous_requests_per_minute": rate(prev),
        "new_requests_per_minute": rate(new),
    }

try:
    from topology.topology_graph import get_loader
except ImportError:
    try:
        from alert_triage_agent.topology.topology_graph import get_loader
    except ImportError as err:
        logger.error("Failed to import topology_graph: %s", err)
        raise


class ApigeeSpecialist(BaseSpecialist):
    """Specialist agent diagnosing Apigee ingress proxy faults via real read-only telemetry."""

    def __init__(self):
        super().__init__("ApigeeSpecialist")

    async def investigate(
        self,
        alert: AlertEvent,
        config: AgentConfig,
    ) -> EvidenceDossier:
        raw_proxy = (
            alert.resource_labels.get("proxy_name")
            or alert.resource_labels.get("apiproxy")
            or alert.resource_labels.get("proxy")
            or alert.resource_name
        )
        target_proxy = str(raw_proxy)
        loader = get_loader()
        canon = loader.canonicalize(target_proxy)
        comp = loader.components.get(canon) if canon else None
        if comp and "apigee" in comp.type.lower():
            target_proxy = canon
        else:
            callers = loader.downstream(canon) if canon else []
            proxy_caller = next(
                (c for c in callers if loader.components.get(c) and "apigee" in loader.components[c].type.lower()),
                None,
            )
            if proxy_caller:
                target_proxy = proxy_caller
            elif canon:
                target_proxy = canon
            elif target_proxy.startswith("ata-apigee-"):
                target_proxy = "ata-" + target_proxy[len("ata-apigee-"):]

        findings: list[str] = []
        evidence_items: list[EvidenceItem] = []
        metrics: dict[str, Any] = {}
        queries_executed: dict[str, list[str]] = {
            "cloud_monitoring": [
                f'fetch apigee.googleapis.com/proxy/response_count | filter resource.proxy_name == "{target_proxy}"',
                f'fetch apigee.googleapis.com/target/latencies | filter resource.proxy_name == "{target_proxy}"',
            ],
            "cloud_logging": [
                f'logName="{access_log_name(config.project_id)}" AND jsonPayload.proxy="{target_proxy}"'
            ],
        }

        is_apigee_incident = "apigee" in alert.resource_name.lower() or any(
            k in alert.policy_name.lower() for k in ["apigee", "spikearrest", "429", "proxy"]
        ) or "v1" in alert.resource_name.lower()

        now_utc = datetime.now(timezone.utc)
        # Parse alert start time to anchor query window
        try:
            alert_dt = datetime.fromisoformat(alert.started_at.replace("Z", "+00:00"))
        except Exception:
            alert_dt = now_utc

        window_start = alert_dt - timedelta(minutes=15)
        window_end = min(alert_dt + timedelta(minutes=5), now_utc)
        if window_end <= window_start:
            window_end = window_start + timedelta(minutes=15)
        elif (window_end - window_start).total_seconds() < 60:
            window_start = window_end - timedelta(minutes=5)

        # Real telemetry containers
        status_counts: dict[str, int] = {}
        # Kept apart: per-minute distribution means and per-request access-log latencies.
        minute_means_ms: list[float] = []
        logged_latencies_ms: list[float] = []
        top_client_ip: Optional[str] = None
        fault_codes: set[str] = set()
        # GatewayTimeout evidence: metric series (response_code/fault_code/fault_source labels)
        # and access-log entries (fault_source field, proxy revision).
        gt_metric_count = 0
        gt_fault_code: Optional[str] = None
        gt_fault_source: Optional[str] = None
        gt_response_code: Optional[str] = None
        gt_log_count = 0
        log_revisions: Counter[str] = Counter()
        # SpikeArrest evidence and per-revision access-log facts; org/env as Apigee reports them.
        sa_metric_count = 0
        sa_fault_code: Optional[str] = None
        sa_response_code: Optional[str] = None
        sa_log_count = 0
        revision_stats: dict[str, dict[str, Any]] = {}
        latest_entry: Optional[tuple[datetime, str]] = None
        metric_orgs: set[str] = set()
        metric_envs: set[str] = set()
        log_envs: set[str] = set()
        access_log_truncated = False

        # 1. Query Cloud Monitoring: proxy/response_count
        try:
            from google.cloud import monitoring_v3
            client = monitoring_v3.MetricServiceClient()
            interval = monitoring_v3.TimeInterval(
                start_time={"seconds": int(window_start.timestamp())},
                end_time={"seconds": int(window_end.timestamp())},
            )
            req = monitoring_v3.ListTimeSeriesRequest(
                name=f"projects/{config.project_id}",
                filter=(
                    f'metric.type = "apigee.googleapis.com/proxy/response_count" AND '
                    f'resource.labels.proxy_name = "{target_proxy}"'
                ),
                interval=interval,
            )
            for ts in client.list_time_series(request=req):
                code = str(ts.metric.labels.get("response_code") or "")
                fc = ts.metric.labels.get("fault_code")
                if fc:
                    fault_codes.add(fc)
                pts_sum = sum(
                    pt.value.int64_value
                    for pt in ts.points
                    if hasattr(pt.value, "int64_value")
                )
                if code and pts_sum > 0:
                    status_counts[code] = status_counts.get(code, 0) + pts_sum
                # Real responses carry a protobuf map (a Mapping, not a dict).
                res_labels = getattr(getattr(ts, "resource", None), "labels", None)
                if isinstance(res_labels, Mapping):
                    if res_labels.get("org"):
                        metric_orgs.add(str(res_labels["org"]))
                    if res_labels.get("env"):
                        metric_envs.add(str(res_labels["env"]))
                if fc and str(fc).endswith(SPIKE_ARREST_FAULT) and pts_sum > 0:
                    sa_metric_count += pts_sum
                    sa_fault_code = str(fc)
                    sa_response_code = code or sa_response_code
                if fc and str(fc).endswith(GATEWAY_TIMEOUT_FAULT) and pts_sum > 0:
                    gt_metric_count += pts_sum
                    gt_fault_code = str(fc)
                    gt_fault_source = ts.metric.labels.get("fault_source") or gt_fault_source
                    gt_response_code = code or gt_response_code
        except Exception as exc:
            logger.warning("Cloud Monitoring proxy/response_count query error: %s", exc)

        # 2. Query Cloud Monitoring: target/latencies
        try:
            from google.cloud import monitoring_v3
            client = monitoring_v3.MetricServiceClient()
            interval = monitoring_v3.TimeInterval(
                start_time={"seconds": int(window_start.timestamp())},
                end_time={"seconds": int(window_end.timestamp())},
            )
            req = monitoring_v3.ListTimeSeriesRequest(
                name=f"projects/{config.project_id}",
                filter=(
                    f'metric.type = "apigee.googleapis.com/target/latencies" AND '
                    f'resource.labels.proxy_name = "{target_proxy}"'
                ),
                interval=interval,
            )
            for ts in client.list_time_series(request=req):
                for pt in ts.points:
                    dist = getattr(pt.value, "distribution_value", None)
                    if dist and getattr(dist, "count", 0) > 0:
                        minute_means_ms.append(float(getattr(dist, "mean", 0.0)))
        except Exception as exc:
            logger.warning("Cloud Monitoring target/latencies query error: %s", exc)

        # 3. Query Cloud Logging: ata-apigee-access
        try:
            import google.cloud.logging
            logging_client = google.cloud.logging.Client(project=config.project_id)
            start_ts = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            end_ts = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")
            log_filter = (
                f'logName="{access_log_name(config.project_id)}" AND '
                f'jsonPayload.proxy="{target_proxy}" AND '
                f'timestamp>="{start_ts}" AND timestamp<="{end_ts}"'
            )
            log_cap = config.apigee_access_log_max_entries
            entries = list(logging_client.list_entries(
                filter_=log_filter, max_results=log_cap, order_by=google.cloud.logging.ASCENDING
            ))
            if not entries:
                alt_filter = (
                    f'logName="{access_log_name(config.project_id)}" AND '
                    f'(jsonPayload.proxy_name="{target_proxy}" OR textPayload:"{target_proxy}") AND '
                    f'timestamp>="{start_ts}" AND timestamp<="{end_ts}"'
                )
                entries = list(logging_client.list_entries(
                    filter_=alt_filter, max_results=log_cap, order_by=google.cloud.logging.ASCENDING
                ))

            if len(entries) >= log_cap:
                # Oldest-first and capped: the newest entries were not read, so per-revision
                # counts are partial and the current revision is unknown.
                access_log_truncated = True
                metrics["access_log_cap_hit"] = {"cap": log_cap, "window_start": start_ts, "window_end": end_ts}
                cap_text = (
                    f"Apigee access log for `{target_proxy}` hit the {log_cap}-entry read cap "
                    f"(APIGEE_ACCESS_LOG_MAX_ENTRIES) in [{start_ts}, {end_ts}]: entries after the cap were "
                    "not read, so no proxy revision switch was evaluated and log-derived counts are lower bounds."
                )
                findings.append(cap_text)
                evidence_items.append(EvidenceItem(headline=f"Apigee access log truncated: {target_proxy}", content=cap_text))
                logger.warning(
                    "Access log for %s hit the %d-entry cap in [%s, %s]; revision switch not evaluated, "
                    "log-derived counts are lower bounds",
                    target_proxy, log_cap, start_ts, end_ts,
                )
            client_counter: Counter[str] = Counter()
            for entry in entries:
                payload = entry.payload if isinstance(entry.payload, dict) else {}
                sc = str(payload.get("status_code") or "")
                if sc:
                    status_counts[sc] = status_counts.get(sc, 0) + 1
                cip = payload.get("client_ip")
                if cip:
                    client_counter[cip] += 1
                fault_name = str(payload.get("fault_source") or "")
                if fault_name == SPIKE_ARREST_FAULT:
                    sa_log_count += 1
                if payload.get("environment"):
                    log_envs.add(str(payload["environment"]))
                entry_rev = str(payload.get("revision") or "").strip()
                entry_ts = _entry_time(entry)
                if entry_rev and entry_ts:
                    if latest_entry is None or entry_ts >= latest_entry[0]:
                        latest_entry = (entry_ts, entry_rev)
                    st = revision_stats.setdefault(
                        entry_rev, {"first": entry_ts, "last": entry_ts, "requests": 0, "spike_arrest_faults": 0}
                    )
                    st["first"] = min(st["first"], entry_ts)
                    st["last"] = max(st["last"], entry_ts)
                    st["requests"] += 1
                    if fault_name == SPIKE_ARREST_FAULT:
                        st["spike_arrest_faults"] += 1
                if fault_name == GATEWAY_TIMEOUT_FAULT:
                    gt_log_count += 1
                    rev = str(payload.get("revision") or "").strip()
                    if rev:
                        log_revisions[rev] += 1
                tlat = payload.get("target_latency_ms")
                if tlat and str(tlat).strip():
                    try:
                        logged_latencies_ms.append(float(tlat))
                    except ValueError:
                        pass
            if client_counter:
                top_client_ip = client_counter.most_common(1)[0][0]
        except Exception as exc:
            logger.warning("Cloud Logging ata-apigee-access query error: %s", exc)

        # Calculate latency metrics if captured
        lat_summary = target_latency_summary(logged_latencies_ms, minute_means_ms)
        p50_ms: Optional[int] = lat_summary["p50_ms"] if lat_summary else None
        p95_ms: Optional[int] = lat_summary["p95_ms"] if lat_summary else None
        p99_ms: Optional[int] = lat_summary["p99_ms"] if lat_summary else None
        if lat_summary:
            # What the p50/p95/p99 values are percentiles of; every label of them names it.
            metrics["target_latency_basis"] = lat_summary["basis"]

        # Assign metrics without static numbers
        if p50_ms is not None:
            metrics["target_latency_p50"] = f"{p50_ms}ms"
        else:
            metrics["target_latency_p50"] = "N/A (not captured)"

        if p95_ms is not None:
            metrics["target_latency_p95_ms"] = p95_ms
            metrics["target_latency_p95"] = f"{p95_ms}ms"
        else:
            metrics["target_latency_p95_ms"] = "N/A (not captured)"
            metrics["target_latency_p95"] = "N/A (not captured)"

        if p99_ms is not None:
            metrics["target_latency_p99"] = f"{p99_ms}ms"
        else:
            metrics["target_latency_p99"] = "N/A (not captured)"

        metrics["top_client_ip"] = top_client_ip if top_client_ip else "<CLIENT_IP>/32"

        # Org and environment only when Apigee reported exactly one; never assumed.
        if len(metric_orgs) == 1:
            metrics["apigee_org"] = next(iter(metric_orgs))
        elif metric_orgs:
            logger.warning("Apigee proxy %s reported in several orgs %s; org not recorded", target_proxy, sorted(metric_orgs))
        envs = metric_envs | log_envs
        if len(envs) == 1:
            metrics["apigee_environment"] = next(iter(envs))
        elif envs:
            logger.warning(
                "Apigee proxy %s seen in several environments %s; environment and revision switch not recorded",
                target_proxy, sorted(envs),
            )
        lower_bound = access_log_truncated
        if sa_metric_count or sa_log_count:
            metrics["spike_arrest"] = {
                "count": sa_metric_count or sa_log_count,
                "count_source": (
                    "apigee.googleapis.com/proxy/response_count" if sa_metric_count
                    else f"{ACCESS_LOG_NAME} log entries"
                ),
                "count_is_lower_bound": bool(not sa_metric_count and lower_bound),
                "response_code": sa_response_code,
                "fault_code": sa_fault_code or SPIKE_ARREST_FAULT,
            }
        revision_change = None
        if not access_log_truncated and len(envs) <= 1:
            revision_change = summarize_revision_change(
                revision_stats, latest_entry[1] if latest_entry else None
            )
        if revision_change:
            metrics["proxy_revision_change"] = revision_change
            findings.append(
                f"Apigee proxy `{target_proxy}` switched from revision {revision_change['previous_revision']} "
                f"to {revision_change['new_revision']} (first served {revision_change['new_first_served_at']})."
            )

        # Only observed facts: counts, fault code and source as reported, the proxy revision
        # named by the access log. The proxy's timeout value is not part of this telemetry.
        if gt_metric_count or gt_log_count:
            metrics["gateway_timeout"] = {
                "proxy": target_proxy,
                "count": gt_metric_count or gt_log_count,
                "count_source": (
                    "apigee.googleapis.com/proxy/response_count" if gt_metric_count
                    else f"{ACCESS_LOG_NAME} log entries"
                ),
                "response_code": gt_response_code,
                "fault_code": gt_fault_code or GATEWAY_TIMEOUT_FAULT,
                "fault_source": gt_fault_source,
                "count_is_lower_bound": bool(not gt_metric_count and lower_bound),
                # With a truncated log the revision may not be the one serving now.
                "revision": (
                    log_revisions.most_common(1)[0][0] if log_revisions and not access_log_truncated else None
                ),
            }
            findings.append(
                f"Apigee proxy `{target_proxy}` returned {metrics['gateway_timeout']['count']} "
                f"{GATEWAY_TIMEOUT_FAULT} faults in the window [{window_start.isoformat()}, {window_end.isoformat()}]."
            )
        metrics["response_codes"] = status_counts if status_counts else {}

        # Log WARNING for genuinely absent data per Quality Gates
        if p50_ms is None or p95_ms is None:
            logger.warning(
                "Target latency on Apigee proxy %s is N/A: no latency distribution points or access log latency values captured in window [%s, %s]",
                target_proxy, window_start.isoformat(), window_end.isoformat(),
            )

        # Policy-specific findings & evidence items
        if "429" in alert.policy_name or "spikearrest" in alert.policy_name.lower():
            findings.append(f"SpikeArrest rate limit violation detected on Apigee proxy `{target_proxy}`.")
            c429 = status_counts.get("429")
            if c429 is not None and c429 > 0:
                metrics["proxy_response_code_429"] = c429
                findings.append(f"Telemetry confirms {c429} HTTP 429 rate-limited responses within alert window.")
                total_429_str = f"{c429} in window"
            else:
                metrics["proxy_response_code_429"] = "N/A (not captured)"
                findings.append("HTTP 429 response count: N/A (not captured).")
                total_429_str = "N/A (not captured)"
                logger.warning(
                    "HTTP 429 response count on %s is N/A: zero 429 responses found in window [%s, %s]",
                    target_proxy, window_start.isoformat(), window_end.isoformat(),
                )

            if top_client_ip:
                findings.append(f"Top offending client identified from access logs: `{top_client_ip}`.")
                client_disp = top_client_ip
            else:
                client_disp = "<CLIENT_IP>/32"

            if p95_ms is not None:
                backend_status = f"Target latency P95 of {lat_summary['population']}: {p95_ms}ms"
            else:
                backend_status = "Target latency: N/A (not captured)"

            evidence_items.append(
                EvidenceItem(
                    headline=f"Apigee Proxy Policy Breach: {target_proxy}",
                    content=(
                        f"Proxy: {target_proxy}\n"
                        f"Policy Fault: SpikeArrest 429\n"
                        f"Total 429 Responses: {total_429_str}\n"
                        f"Target Backend Status: {backend_status}\n"
                        f"Top Offending Client: {client_disp}"
                    ),
                )
            )
        elif "5xx" in alert.policy_name.lower() or "504" in alert.policy_name:
            findings.append(f"Gateway errors detected on Apigee proxy `{target_proxy}` originating from upstream target.")
            err_code = "504" if "504" in alert.policy_name else ("503" if "503" in status_counts else "5xx")
            err_count = status_counts.get(err_code) or status_counts.get("504") or status_counts.get("503")
            if err_count is not None and err_count > 0:
                metrics[f"proxy_response_code_{err_code}"] = err_count
                findings.append(f"Telemetry confirms {err_count} HTTP {err_code} responses within alert window.")
                err_str = f"HTTP {err_code} responses from backend service ({err_count})"
            else:
                metrics[f"proxy_response_code_{err_code}"] = "N/A (not captured)"
                findings.append(f"HTTP {err_code} response count: N/A (not captured).")
                err_str = f"HTTP {err_code}: N/A (not captured)"
                logger.warning(
                    "HTTP %s response count on %s is N/A: zero %s error points found in window [%s, %s]",
                    err_code, target_proxy, err_code, window_start.isoformat(), window_end.isoformat(),
                )

            lat_label = f"Target Latency (P95 of {lat_summary['population']})" if lat_summary else "Target Latency"
            lat_str = f"{p95_ms}ms" if p95_ms is not None else "N/A (not captured)"
            evidence_items.append(
                EvidenceItem(
                    headline=f"Apigee Upstream Latency / {err_code}: {target_proxy}",
                    content=(
                        f"Proxy: {target_proxy}\n"
                        f"{lat_label}: {lat_str}\n"
                        f"Target Errors: {err_str}"
                    ),
                )
            )
        else:
            findings.append(f"Apigee proxy `{target_proxy}` telemetry evaluated.")
            if status_counts:
                codes_summary = ", ".join(f"HTTP {c}: {cnt}" for c, cnt in sorted(status_counts.items()))
                findings.append(f"Observed response codes in window: {codes_summary}.")
                content_str = f"Access log metrics for `{target_proxy}`: {codes_summary}."
            else:
                findings.append("No anomalous error spike or policy breach detected in query window.")
                content_str = f"Access log metrics for `{target_proxy}`: N/A (not captured)."

            evidence_items.append(
                EvidenceItem(
                    headline=f"Apigee Baseline Telemetry: {target_proxy}",
                    content=content_str,
                )
            )

        return EvidenceDossier(
            specialist_name=self.name,
            target_resource=target_proxy,
            findings=findings,
            metrics=metrics,
            evidence_items=evidence_items,
            queries_executed=queries_executed,
            anomaly_detected=is_apigee_incident,
            confidence=0.89,
        )
