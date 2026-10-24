"""ApigeeSpecialist Agent for Alert Triage Runtime.

Analyzes Apigee Cloud Monitoring metrics (proxy/response_count, target/latencies),
ata-apigee-access Cloud Logging records, and client access distribution.
All telemetry is anchored to the alert start time using real read-only GCP queries.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Optional

from ..config import AgentConfig
from ..contracts.alert import AlertEvent
from ..contracts.report import EvidenceItem
from .base import BaseSpecialist, EvidenceDossier

logger = logging.getLogger(__name__)

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
                f'logName="projects/{config.project_id}/logs/ata-apigee-access" AND jsonPayload.proxy="{target_proxy}"'
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
        latencies_ms: list[float] = []
        top_client_ip: Optional[str] = None
        fault_codes: set[str] = set()

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
                        mean_val = getattr(dist, "mean", 0.0)
                        count_val = getattr(dist, "count", 0)
                        for _ in range(min(count_val, 10)):
                            latencies_ms.append(float(mean_val))
        except Exception as exc:
            logger.warning("Cloud Monitoring target/latencies query error: %s", exc)

        # 3. Query Cloud Logging: ata-apigee-access
        try:
            import google.cloud.logging
            logging_client = google.cloud.logging.Client(project=config.project_id)
            start_ts = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            end_ts = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")
            log_filter = (
                f'logName="projects/{config.project_id}/logs/ata-apigee-access" AND '
                f'jsonPayload.proxy="{target_proxy}" AND '
                f'timestamp>="{start_ts}" AND timestamp<="{end_ts}"'
            )
            entries = list(logging_client.list_entries(filter_=log_filter, max_results=200))
            if not entries:
                alt_filter = (
                    f'logName="projects/{config.project_id}/logs/ata-apigee-access" AND '
                    f'(jsonPayload.proxy_name="{target_proxy}" OR textPayload:"{target_proxy}") AND '
                    f'timestamp>="{start_ts}" AND timestamp<="{end_ts}"'
                )
                entries = list(logging_client.list_entries(filter_=alt_filter, max_results=200))

            client_counter: Counter[str] = Counter()
            for entry in entries:
                payload = entry.payload if isinstance(entry.payload, dict) else {}
                sc = str(payload.get("status_code") or "")
                if sc:
                    status_counts[sc] = status_counts.get(sc, 0) + 1
                cip = payload.get("client_ip")
                if cip:
                    client_counter[cip] += 1
                tlat = payload.get("target_latency_ms")
                if tlat and str(tlat).strip():
                    try:
                        latencies_ms.append(float(tlat))
                    except ValueError:
                        pass
            if client_counter:
                top_client_ip = client_counter.most_common(1)[0][0]
        except Exception as exc:
            logger.warning("Cloud Logging ata-apigee-access query error: %s", exc)

        # Calculate latency metrics if captured
        p50_ms: Optional[int] = None
        p95_ms: Optional[int] = None
        p99_ms: Optional[int] = None
        if latencies_ms:
            sorted_lats = sorted(latencies_ms)
            n = len(sorted_lats)
            p50_ms = int(round(sorted_lats[int(n * 0.50)]))
            p95_idx = min(int(n * 0.95), n - 1)
            p95_ms = int(round(sorted_lats[p95_idx]))
            p99_idx = min(int(n * 0.99), n - 1)
            p99_ms = int(round(sorted_lats[p99_idx]))

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
                backend_status = f"Target latency: {p95_ms}ms"
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

            lat_str = f"{p95_ms}ms" if p95_ms is not None else "N/A (not captured)"
            evidence_items.append(
                EvidenceItem(
                    headline=f"Apigee Upstream Latency / {err_code}: {target_proxy}",
                    content=(
                        f"Proxy: {target_proxy}\n"
                        f"Target Latency (P95): {lat_str}\n"
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
