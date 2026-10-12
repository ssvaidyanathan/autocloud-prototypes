import argparse
import asyncio
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [loadgen]: %(message)s",
)
logger = logging.getLogger("loadgen")


class LoadStats:
    """Collects and reports load generation telemetry."""

    def __init__(self) -> None:
        self.total_requests: int = 0
        self.status_counts: Dict[int, int] = {}
        self.errors: int = 0
        self.latencies: List[float] = []
        self.start_time: float = time.time()

    def record(self, status_code: Optional[int], latency: float) -> None:
        self.total_requests += 1
        if status_code is not None:
            self.status_counts[status_code] = self.status_counts.get(status_code, 0) + 1
        else:
            self.errors += 1
        self.latencies.append(latency)

    def summary(self) -> Dict[str, Any]:
        elapsed = max(time.time() - self.start_time, 0.001)
        throughput = round(self.total_requests / elapsed, 2)
        sorted_latencies = sorted(self.latencies) if self.latencies else [0.0]
        count = len(sorted_latencies)
        p50 = sorted_latencies[int(count * 0.50)]
        p95 = sorted_latencies[min(int(count * 0.95), count - 1)]
        p99 = sorted_latencies[min(int(count * 0.99), count - 1)]
        avg = sum(sorted_latencies) / count if count else 0.0

        return {
            "elapsed_seconds": round(elapsed, 2),
            "total_requests": self.total_requests,
            "throughput_rps": throughput,
            "status_counts": dict(self.status_counts),
            "errors": self.errors,
            "latency_p50_ms": round(p50 * 1000, 2),
            "latency_p95_ms": round(p95 * 1000, 2),
            "latency_p99_ms": round(p99 * 1000, 2),
            "latency_avg_ms": round(avg * 1000, 2),
        }

    def print_summary(self) -> None:
        s = self.summary()
        logger.info(
            "LoadGen Stats: %d reqs (%.1f rps) | Status: %s | "
            "Errors: %d | Latency: p50=%.1fms, p95=%.1fms, avg=%.1fms",
            s["total_requests"],
            s["throughput_rps"],
            s["status_counts"],
            s["errors"],
            s["latency_p50_ms"],
            s["latency_p95_ms"],
            s["latency_avg_ms"],
        )


async def _worker(
    worker_id: int,
    client: httpx.AsyncClient,
    target_url: str,
    stop_event: asyncio.Event,
    stats: LoadStats,
    request_interval: float,
) -> None:
    """Worker task sending randomized requests continuously."""
    item_ids = ["item-1", "item-2", "item-3", "item-4"]

    while not stop_event.is_set():
        choice = random.random()
        t0 = time.perf_counter()
        status_code: Optional[int] = None

        try:
            if choice < 0.50:
                # 50% /catalog
                resp = await client.get(f"{target_url}/catalog")
                status_code = resp.status_code
            elif choice < 0.80:
                # 30% /catalog/{id}
                item_id = random.choice(item_ids)
                resp = await client.get(f"{target_url}/catalog/{item_id}")
                status_code = resp.status_code
            else:
                # 20% /checkout
                item_id = random.choice(item_ids)
                resp = await client.post(
                    f"{target_url}/checkout",
                    json={"item_id": item_id, "quantity": 1, "customer_id": f"cust-{worker_id}"},
                )
                status_code = resp.status_code
        except Exception as exc:
            logger.debug("Worker %d request exception: %s", worker_id, exc)
            status_code = None
        finally:
            latency = time.perf_counter() - t0
            stats.record(status_code, latency)

        if request_interval > 0:
            await asyncio.sleep(request_interval)
        else:
            await asyncio.sleep(0.01)


async def run_loadgen(
    target_url: str,
    concurrency: int = 5,
    rate: float = 10.0,
    duration: float = 0.0,
    report_interval: float = 5.0,
) -> LoadStats:
    """Execute continuous load generation against target URL."""
    target_url = target_url.rstrip("/")
    logger.info(
        "Starting load generator: target=%s, concurrency=%d, "
        "target_rate=%.1f req/s, duration=%.1fs",
        target_url,
        concurrency,
        rate,
        duration,
    )

    stats = LoadStats()
    stop_event = asyncio.Event()

    interval_per_worker = (concurrency / rate) if rate > 0 else 0.05
    limits = httpx.Limits(
        max_keepalive_connections=concurrency * 2, max_connections=concurrency * 4
    )

    async with httpx.AsyncClient(limits=limits, timeout=15.0) as client:
        workers = [
            asyncio.create_task(
                _worker(i, client, target_url, stop_event, stats, interval_per_worker)
            )
            for i in range(concurrency)
        ]

        async def reporter() -> None:
            while not stop_event.is_set():
                await asyncio.sleep(report_interval)
                if not stop_event.is_set():
                    stats.print_summary()

        report_task = asyncio.create_task(reporter())

        try:
            if duration > 0:
                await asyncio.sleep(duration)
            else:
                # Run indefinitely until cancelled
                while True:
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            stop_event.set()
            report_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    stats.print_summary()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuous load generator for shop-api")
    parser.add_argument(
        "--target",
        default=os.getenv("SHOP_API_URL", "http://localhost:8080"),
        help="Target shop-api URL (default: http://localhost:8080 or SHOP_API_URL)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("CONCURRENCY", "5")),
        help="Number of concurrent worker tasks (default: 5)",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=float(os.getenv("RATE", "10.0")),
        help="Target overall request rate in requests/sec (default: 10.0)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=float(os.getenv("DURATION", "0.0")),
        help="Duration to run in seconds (0 for infinite, default: 0)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("REPORT_INTERVAL", "5.0")),
        help="Reporting interval in seconds (default: 5.0)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(
            run_loadgen(
                target_url=args.target,
                concurrency=args.concurrency,
                rate=args.rate,
                duration=args.duration,
                report_interval=args.interval,
            )
        )
    except KeyboardInterrupt:
        logger.info("Load generator stopped by user.")


if __name__ == "__main__":
    main()
