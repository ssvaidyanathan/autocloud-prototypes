import asyncio
from unittest import mock

from demo.loadgen.loadgen import LoadStats, run_loadgen


def test_load_stats_recording() -> None:
    stats = LoadStats()
    stats.record(status_code=200, latency=0.010)
    stats.record(status_code=200, latency=0.020)
    stats.record(status_code=500, latency=0.050)
    stats.record(status_code=None, latency=0.100)

    summary = stats.summary()
    assert summary["total_requests"] == 4
    assert summary["status_counts"] == {200: 2, 500: 1}
    assert summary["errors"] == 1
    assert summary["latency_p50_ms"] > 0
    assert summary["throughput_rps"] > 0


def test_run_loadgen_short_duration() -> None:
    with (
        mock.patch("httpx.AsyncClient.get") as mock_get,
        mock.patch("httpx.AsyncClient.post") as mock_post,
    ):
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        mock_post.return_value = mock_resp

        stats = asyncio.run(
            run_loadgen(
                target_url="http://mock-shop-api:8080",
                concurrency=2,
                rate=50.0,
                duration=0.15,
                report_interval=0.1,
            )
        )

        summary = stats.summary()
        assert summary["total_requests"] > 0
        assert 200 in summary["status_counts"]
