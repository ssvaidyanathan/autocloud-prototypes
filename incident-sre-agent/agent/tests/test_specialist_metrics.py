from datetime import datetime, timezone
from unittest.mock import MagicMock

from agent.app.contracts.incident import Incident
from agent.app.specialists.metrics import (
    METRIC_METADATA,
    MetricsSpecialist,
    _format_ms,
    _to_ms,
    canonical_metric_value,
    format_metric_value,
    get_metric_metadata,
)


def test_metric_metadata_dictionary() -> None:
    """Verify explicit canonical metric metadata mapping."""
    assert METRIC_METADATA["run.googleapis.com/request_latencies"] == {
        "unit": "ms",
        "type": "latency",
    }
    assert METRIC_METADATA["run.googleapis.com/request_count"] == {
        "unit": "/s",
        "type": "rate",
    }
    assert METRIC_METADATA["run.googleapis.com/container/memory/utilizations"] == {
        "unit": "%",
        "type": "ratio",
    }
    assert METRIC_METADATA["run.googleapis.com/container/cpu/utilizations"] == {
        "unit": "%",
        "type": "ratio",
    }

    assert get_metric_metadata("run.googleapis.com/request_latencies")["unit"] == "ms"
    assert get_metric_metadata("run.googleapis.com/request_count")["unit"] == "/s"
    assert get_metric_metadata("run.googleapis.com/container/memory/utilizations")["unit"] == "%"
    assert get_metric_metadata("run.googleapis.com/container/cpu/utilizations")["unit"] == "%"


def test_latency_never_multiplied_by_1000_regression() -> None:
    """Regression test: latency metrics already in milliseconds are never multiplied by 1,000."""
    # Under old _to_ms heuristic, <= 100 was assumed seconds, multiplying 98.7 by 1000 -> 98700.0
    val = 98.7
    canonical = canonical_metric_value(val, "run.googleapis.com/request_latencies")
    assert canonical == 98.7
    assert canonical != 98700.0
    assert _to_ms(val) == 98.7
    assert _to_ms(val) != 98700.0


def test_format_metric_value_latencies() -> None:
    """Verify latency values format strictly as ms (e.g. 98.7ms, 5200.0ms)."""
    assert format_metric_value(98.7, "run.googleapis.com/request_latencies") == "98.7ms"
    assert format_metric_value(5200.0, "run.googleapis.com/request_latencies") == "5200.0ms"
    assert _format_ms(98.7) == "98.7ms"
    assert _format_ms(5200.0) == "5200.0ms"


def test_format_metric_value_ratios() -> None:
    """Verify ratios format as percentages (0.93 -> 93.0%, 0.45 -> 45.0%)."""
    assert format_metric_value(0.93, "run.googleapis.com/container/memory/utilizations") == "93.0%"
    assert format_metric_value(0.45, "run.googleapis.com/container/cpu/utilizations") == "45.0%"
    assert format_metric_value(1.0, "run.googleapis.com/container/memory/utilizations") == "100.0%"
    assert format_metric_value(0.0, "run.googleapis.com/container/memory/utilizations") == "0.0%"


def test_format_metric_value_rates() -> None:
    """Verify rate metrics format with explicit unit /s (e.g. 0.0667 -> 0.0667/s)."""
    assert format_metric_value(0.0667, "run.googleapis.com/request_count") == "0.0667/s"
    assert format_metric_value(0.0011, "run.googleapis.com/request_count") == "0.0011/s"
    assert format_metric_value(0.0, "run.googleapis.com/request_count") == "0.0/s"


def test_metrics_specialist_latency_formatting_and_shift() -> None:
    """Verify MetricsSpecialist formats baseline and current p95 latency accurately
    without heuristic inflation.
    """
    mock_client = MagicMock()
    mock_client.list_alert_policies.return_value = []
    mock_client.list_incidents.return_value = []

    # Latency: current 98.7ms vs baseline 95.0ms
    ts_curr_lat = MagicMock()
    pt_curr = MagicMock()
    pt_curr.value.double_value = 98.7
    ts_curr_lat.points = [pt_curr]

    ts_base_lat = MagicMock()
    pt_base = MagicMock()
    pt_base.value.double_value = 95.0
    ts_base_lat.points = [pt_base]

    # Error rate: zero current and baseline
    ts_zero = MagicMock()
    pt_zero = MagicMock()
    pt_zero.value.double_value = 0.0
    ts_zero.points = [pt_zero]

    # Order of queries in latency incident:
    # 1. latency curr & base, 2. error curr & base
    mock_client.list_time_series.side_effect = [
        [ts_curr_lat],
        [ts_base_lat],
        [ts_zero],
        [ts_zero],
    ]

    t1 = datetime(2026, 10, 10, 1, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 10, 2, 0, 0, tzinfo=timezone.utc)

    incident = Incident(
        id="inc-latency-1",
        source="jira",
        external_id="OPS-101",
        external_key="OPS-101",
        title="Latency check",
        description="Investigate p95 latency",
        severity="P2",
        service_hint="shop-api",
        opened_at=t1,
        labels=["latency"],
    )

    specialist = MetricsSpecialist(project_id="test-proj", client=mock_client)
    findings = specialist.run_investigation(incident=incident, start_time=t1, end_time=t2)

    lat_ev = [e for e in findings.evidence if e.kind == "latency_metric"]
    assert len(lat_ev) == 1
    excerpt = lat_ev[0].excerpt

    # Must contain 98.7ms and 95.0ms
    assert "current p95=98.7ms vs baseline p95=95.0ms" in excerpt
    # Must NOT contain 98700ms or 98707.7ms
    assert "98700" not in excerpt
    assert "98707" not in excerpt
    assert "+3.9%" in excerpt


def test_metrics_specialist_memory_investigation_percentage() -> None:
    """Verify MetricsSpecialist formats memory utilization as percentage for memory incidents."""
    mock_client = MagicMock()
    mock_client.list_alert_policies.return_value = []
    mock_client.list_incidents.return_value = []

    # Memory utilization: curr 0.93 (93.0%), base 0.15 (15.0%)
    ts_curr_mem = MagicMock()
    pt_mem = MagicMock()
    pt_mem.value.double_value = 0.93
    ts_curr_mem.points = [pt_mem]

    ts_base_mem = MagicMock()
    pt_base = MagicMock()
    pt_base.value.double_value = 0.15
    ts_base_mem.points = [pt_base]

    # Error rate: zero
    ts_zero = MagicMock()
    pt_zero = MagicMock()
    pt_zero.value.double_value = 0.0
    ts_zero.points = [pt_zero]

    # Latency: zero
    ts_lat_zero = MagicMock()
    pt_lat_zero = MagicMock()
    pt_lat_zero.value.double_value = 0.0
    ts_lat_zero.points = [pt_lat_zero]

    mock_client.list_time_series.side_effect = [
        [ts_curr_mem],
        [ts_base_mem],
        [ts_zero],
        [ts_zero],
        [ts_lat_zero],
        [ts_lat_zero],
    ]

    t1 = datetime(2026, 10, 10, 1, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 10, 2, 0, 0, tzinfo=timezone.utc)

    incident = Incident(
        id="inc-memory-1",
        source="servicenow",
        external_id="INC-MEM",
        external_key="INC-MEM",
        title="OOM container restarts and memory leak",
        description="Memory usage spiking",
        severity="P1",
        service_hint="shop-api",
        opened_at=t1,
        labels=["memory", "leak"],
    )

    specialist = MetricsSpecialist(project_id="test-proj", client=mock_client)
    findings = specialist.run_investigation(incident=incident, start_time=t1, end_time=t2)

    mem_ev = [e for e in findings.evidence if e.kind == "memory_metric"]
    assert len(mem_ev) == 1
    assert "current mean=93.0% vs baseline mean=15.0%" in mem_ev[0].excerpt
    assert any("Elevated memory utilization" in a and "93.0%" in a for a in findings.anomalies)
