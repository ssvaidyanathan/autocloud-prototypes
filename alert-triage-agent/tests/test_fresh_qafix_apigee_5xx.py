"""F5: every 5xx status on the proxy counts toward its 5xx count (a passed-through 500 too)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.specialists.apigee import ApigeeSpecialist
from agent.app.specialists.synthesizer import SynthesizerAgent


@pytest.fixture
def cfg():
    return AgentConfig(project_id="p", name_prefix="ata", region="us-west1", is_local=True)


def _ts(code, n):
    ts = MagicMock()
    ts.metric.labels = {"response_code": code}
    pt = MagicMock()
    pt.value.int64_value = n
    ts.points = [pt]
    ts.resource = None
    return ts


async def _run(cfg, policy, series, entries=()):
    alert = AlertEvent(
        incident_id="0.f5", policy_name=policy, resource_name="ata-inventory-v1",
        state="open", started_at="2026-10-28T01:40:00Z",
    )
    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mon_cls, \
         patch("google.cloud.logging.Client") as log_cls:
        mon_cls.return_value.list_time_series.side_effect = (
            lambda request: list(series) if "response_count" in request.filter else []
        )
        log_cls.return_value.list_entries.return_value = list(entries)
        return await ApigeeSpecialist().investigate(alert, cfg)


@pytest.mark.asyncio
async def test_passed_through_500_counts_as_5xx(cfg):
    d = await _run(cfg, "ata-inventory-5xx", [_ts("500", 40), _ts("200", 10)])
    assert d.metrics["proxy_response_code_5xx"] == 40
    assert d.metrics["proxy_response_codes_5xx"] == {"500": 40}
    joined = " ".join(d.findings)
    assert "40 HTTP 5xx" in joined and "HTTP 500: 40" in joined


@pytest.mark.asyncio
async def test_all_5xx_codes_are_summed(cfg):
    series = [_ts("500", 5), _ts("502", 3), _ts("503", 2), _ts("504", 1), _ts("429", 7)]
    d = await _run(cfg, "ata-inventory-5xx", series)
    assert d.metrics["proxy_response_code_5xx"] == 11
    assert d.metrics["proxy_response_codes_5xx"] == {"500": 5, "502": 3, "503": 2, "504": 1}


@pytest.mark.asyncio
async def test_500_from_access_log_counts(cfg):
    e = MagicMock()
    e.payload = {"status_code": "500"}
    e.timestamp = datetime(2026, 10, 28, 1, 40, 10, tzinfo=timezone.utc)
    d = await _run(cfg, "ata-inventory-5xx", [], [e])
    assert d.metrics["proxy_response_code_5xx"] == 1


@pytest.mark.asyncio
async def test_no_5xx_stays_na(cfg):
    d = await _run(cfg, "ata-inventory-5xx", [_ts("200", 10)])
    assert d.metrics["proxy_response_code_5xx"] == "N/A (not captured)"


@pytest.mark.asyncio
async def test_504_policy_counts_only_504(cfg):
    d = await _run(cfg, "ata-orders-504", [_ts("500", 5), _ts("504", 2)])
    assert d.metrics["proxy_response_code_504"] == 2


@pytest.mark.asyncio
async def test_metrics_table_shows_5xx_count(cfg):
    d = await _run(cfg, "ata-inventory-5xx", [_ts("500", 40)])
    rows = SynthesizerAgent()._build_metrics_comparison([d], "ata-inventory-v1")
    apigee = [r for r in rows if r["layer"] == "Apigee"]
    assert apigee and apigee[0]["during_value"] == "40 req"
