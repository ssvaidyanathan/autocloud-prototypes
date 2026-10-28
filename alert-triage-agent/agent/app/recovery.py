"""Recovery work note: what is known when every alert of a ticketed group has closed.

A closed alert is not proof of a fix. With evaluation_missing_data=INACTIVE (and auto_close)
an alert also closes when its service stops receiving traffic, and the Cloud Monitoring
alert state carries no close reason. The note therefore states only that the alerts closed,
what traffic the affected Cloud Run services received around the close (measured with
run.googleapis.com/request_count, or "not checked" with the reason), and that a human must
verify the remediation.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Callable, Iterable, Optional

from .request_count import request_count_series

logger = logging.getLogger(__name__)

CLOUD_RUN_TYPE = "cloud run"

# Test hook: returns a Cloud Monitoring client (or None) for the recovery traffic check.
_metric_client_factory: Optional[Callable[[Any], Any]] = None


def set_recovery_metric_client_factory(factory: Optional[Callable[[Any], Any]]) -> None:
    global _metric_client_factory
    _metric_client_factory = factory


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def cloud_run_services(resources: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split alert resources into Cloud Run services (per TOPOLOGY.md) and the rest."""
    from topology.topology_graph import get_loader

    loader = get_loader()
    run, other = [], []
    for res in resources:
        canon = loader.canonicalize(res) or res
        comp = loader.components.get(canon)
        kind = str(getattr(comp, "type", "") or "").strip().lower()
        target = run if kind == CLOUD_RUN_TYPE else other
        if canon not in target:
            target.append(canon)
    return run, other


def _requests_and_5xx(
    client: Any, project_id: str, region: str, service: str, start: float, end: float
) -> tuple[int, int]:
    total = errors = 0
    for ts in request_count_series(client, project_id, region, service, start, end):
        labels = ts.metric.labels
        is_5xx = str(labels.get("response_code", "")).startswith("5") or labels.get("response_code_class") == "5xx"
        count = sum(int(pt.value.int64_value) for pt in ts.points)
        total += count
        if is_5xx:
            errors += count
    return total, errors


def traffic_at_close_facts(
    resources: Iterable[str], project_id: str, region: str, start: float, end: float, client: Any
) -> list[str]:
    """Measured traffic facts per affected Cloud Run service in `region` over [start, end]."""
    window = f"between {_iso(start)} and {_iso(end)}"
    try:
        run_services, other = cloud_run_services(resources)
    except Exception as exc:
        logger.warning("Topology lookup for the recovery traffic check failed: %s", exc)
        return [f"Traffic around the close was not checked: the topology lookup failed ({type(exc).__name__})."]

    facts: list[str] = []
    if run_services and client is None:
        facts.append(
            f"Traffic around the close was not checked for {', '.join(run_services)}: "
            "no Cloud Monitoring client is available."
        )
        logger.warning("Recovery traffic check skipped for %s: no Cloud Monitoring client", run_services)
        run_services = []
    for svc in run_services:
        try:
            total, errors = _requests_and_5xx(client, project_id, region, svc, start, end)
        except Exception as exc:
            logger.warning("Recovery traffic check for %s failed: %s", svc, exc)
            facts.append(f"Traffic around the close was not checked for {svc}: the query failed ({type(exc).__name__}).")
            continue
        if total == 0:
            facts.append(
                f"Cloud Monitoring recorded no requests on {svc} {window}: the alerts closed while it received "
                "no traffic (missing data), which says nothing about whether the fault is gone."
            )
        else:
            facts.append(f"{svc} received {_plural(total, 'request')} {window}, {errors} with HTTP 5xx.")
    if other:
        facts.append(f"Traffic around the close is not measured for {', '.join(other)} (not a Cloud Run service).")
    return facts


def build_recovery_note(recovered_iso: str, alert_count: int, cooling_seconds: int, facts: list[str]) -> str:
    alerts = "the alert" if alert_count == 1 else f"all {alert_count} alerts"
    parts = [
        f"Monitoring alerts closed: {alerts} of this incident closed by {recovered_iso} and none reopened "
        f"during the {cooling_seconds}s cool-down.",
        "An alert also closes when its service stops receiving traffic, so a close is not proof of a fix; "
        "the agent recorded no remediation.",
        *facts,
        "A human must verify the remediation (for example, that the faulty change was reverted or the "
        "injected fault was cleared) before resolving this incident.",
    ]
    return " ".join(parts)


def recovery_note_for_group(
    alerts: list[dict[str, Any]],
    recovered_epoch: float,
    now_epoch: float,
    cooling_seconds: int,
    settings: Any,
) -> str:
    resources = [str(a.get("resource_name") or a.get("resource") or "") for a in alerts]
    resources = [r for r in resources if r]
    start = recovered_epoch - settings.recovery_traffic_lookback_seconds
    client = None
    try:
        if _metric_client_factory is not None:
            client = _metric_client_factory(settings)
        elif not settings.is_local:
            from google.cloud import monitoring_v3

            client = monitoring_v3.MetricServiceClient()
    except Exception as exc:
        logger.warning("Cloud Monitoring client unavailable for the recovery traffic check: %s", exc)
    facts = traffic_at_close_facts(resources, settings.project_id, settings.region, start, now_epoch, client)
    return build_recovery_note(_iso(recovered_epoch), len(alerts), cooling_seconds, facts)
