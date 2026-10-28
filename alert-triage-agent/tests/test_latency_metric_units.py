"""Cloud Run latency is reported in its documented unit and labelled with the statistic used.

QA round 2, S1 (INC0010328): the RCA said "peak observed latency: 28243ms" although no request
on any ata-* service took longer than 4.6 s. ``run.googleapis.com/request_latencies`` is a
DISTRIBUTION metric whose unit is milliseconds; the Cloud Run specialist multiplied any value
between 1 and 100 by 1000 ("normalize seconds to ms"), so a 28.243 ms mean of fast HTTP 500s
became 28243 ms, crossed the 2000 ms breach threshold and also set the origin onset.

The value is a per-minute distribution *mean* (or the slowest single logged request), never a
percentile, so the dossier states which statistic it is and the RCA text uses that label.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.cloud_run import (
    LATENCY_STAT_LOGGED_REQUEST,
    LATENCY_STAT_MINUTE_MEAN,
    CloudRunSpecialist,
    latency_label,
    measured_breaches,
)
from agent.app.specialists.synthesizer import SynthesizerAgent

# S1 live: ata-inventory-5xx opened 11:58:50Z; fast 500s with a ~28 ms mean latency.
S1_ALERT_START = datetime(2026, 10, 27, 11, 58, 50, tzinfo=timezone.utc)
S1_MEAN_LATENCY_MS = 28.243


def _alert(policy: str = "ata-inventory-5xx", signal: str = "5xx") -> AlertEvent:
    return AlertEvent(
        incident_id="0.oebae2b4yuqu",
        policy_name=policy,
        resource_name="ata-inventory-api",
        state="open",
        started_at=S1_ALERT_START.isoformat(),
        resource_labels={"service_name": "ata-inventory-api"},
        policy_user_labels={"component": "ata-inventory-api", "signal": signal, "severity": "p1"},
    )


def _latency_series(means_ms: list[float]) -> MagicMock:
    """A request_latencies series with one DISTRIBUTION point per minute (value in ms)."""
    ts = MagicMock()
    points = []
    for i, mean in enumerate(means_ms):
        pt = MagicMock()
        pt.value.distribution_value.mean = mean
        pt.value.distribution_value.count = 40
        pt.interval.start_time.seconds = int((S1_ALERT_START - timedelta(minutes=5 - i)).timestamp())
        points.append(pt)
    ts.points = points
    return ts


def _request_log(latency: str) -> MagicMock:
    entry = MagicMock()
    entry.log_name = "projects/test-proj/logs/run.googleapis.com%2Frequests"
    entry.http_request = {"requestUrl": "https://ata-inventory-api/items", "status": 200, "latency": latency}
    entry.payload = None
    entry.text_payload = None
    entry.severity = "INFO"
    entry.timestamp = S1_ALERT_START - timedelta(minutes=11)
    return entry


async def _investigate(means_ms: list[float], logs: list[MagicMock] | None = None, alert: AlertEvent | None = None) -> EvidenceDossier:
    config = AgentConfig(project_id="test-proj", region="test-region", is_local=True)
    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mock_mon_cls, \
         patch("google.cloud.logging.Client") as mock_log_cls:
        def _list_ts(request):
            if "request_latencies" in getattr(request, "filter", ""):
                return [_latency_series(means_ms)] if means_ms else []
            return []
        mock_mon_cls.return_value.list_time_series.side_effect = _list_ts
        mock_log_cls.return_value.list_entries.return_value = logs or []
        return await CloudRunSpecialist().investigate(alert or _alert(), config)


@pytest.mark.asyncio
async def test_s1_millisecond_mean_is_not_multiplied_by_1000():
    dossier = await _investigate([12.0, S1_MEAN_LATENCY_MS, 20.5])

    assert dossier.metrics["latency_p95_ms"] == pytest.approx(S1_MEAN_LATENCY_MS)
    text = " ".join(dossier.findings) + " ".join(e.content for e in dossier.evidence_items)
    assert "28243" not in text
    assert not any("Elevated request latency" in f for f in dossier.findings)


@pytest.mark.parametrize("mean_ms", [1.5, 28.243, 99.9, 100.0, 450.0, 6500.0])
@pytest.mark.asyncio
async def test_distribution_mean_is_taken_as_milliseconds_unchanged(mean_ms):
    dossier = await _investigate([mean_ms])

    assert dossier.metrics["latency_p95_ms"] == pytest.approx(mean_ms)


@pytest.mark.asyncio
async def test_fast_mean_latency_does_not_set_origin_onset():
    # Without the x1000 heuristic, a 28 ms mean is below the 2000 ms onset threshold.
    dossier = await _investigate([S1_MEAN_LATENCY_MS])

    assert "metric_onset" not in dossier.metrics
    assert "earliest_anomaly_timestamp" not in dossier.metrics


@pytest.mark.asyncio
async def test_metric_latency_is_labelled_as_per_minute_mean_not_percentile():
    dossier = await _investigate([S1_MEAN_LATENCY_MS])

    assert dossier.metrics["latency_statistic"] == LATENCY_STAT_MINUTE_MEAN
    label = latency_label(dossier.metrics)
    assert "mean" in label
    assert "p95" not in label.lower() and "percentile" not in label.lower()
    evidence = " ".join(e.content for e in dossier.evidence_items)
    assert f"{label[0].upper()}{label[1:]}: 28.243ms" in evidence


@pytest.mark.asyncio
async def test_slowest_logged_request_is_labelled_as_single_request():
    # S1 stray cause: a 4.610 s cold-start request; the metric means stay at a few ms.
    dossier = await _investigate([S1_MEAN_LATENCY_MS], logs=[_request_log("4.610s")])

    assert dossier.metrics["latency_p95_ms"] == pytest.approx(4610.0)
    assert dossier.metrics["latency_statistic"] == LATENCY_STAT_LOGGED_REQUEST
    assert "request" in latency_label(dossier.metrics)


@pytest.mark.asyncio
async def test_no_latency_data_records_no_statistic():
    dossier = await _investigate([])

    assert dossier.metrics["latency_p95_ms"] == "N/A (not captured)"
    assert "latency_statistic" not in dossier.metrics


def test_breach_text_uses_the_recorded_statistic_label():
    metrics = {"latency_p95_ms": 2500.0, "latency_statistic": LATENCY_STAT_MINUTE_MEAN}

    assert measured_breaches(metrics) == [f"{LATENCY_STAT_MINUTE_MEAN} 2500ms"]


def test_label_without_recorded_statistic_keeps_generic_wording():
    assert latency_label({"latency_p95_ms": 2500.0}) == "peak observed latency"


@pytest.mark.asyncio
async def test_s1_root_cause_fact_states_mean_in_ms_with_accurate_label():
    # S1 hybrid ticket: the latency alert was investigated while fast 5xx errors were live.
    alert = _alert(policy="ata-inventory-latency", signal="latency")
    dossier = await _investigate([12.0, S1_MEAN_LATENCY_MS], alert=alert)

    fact = SynthesizerAgent()._format_dominant_signal_facts(alert, dossier)

    assert fact == f" ({LATENCY_STAT_MINUTE_MEAN}: 28ms)"
    assert "28243" not in fact
