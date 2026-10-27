"""Telemetry reads with quota retry, and the record of telemetry that could not be read.

A specialist that cannot read a source must say so; it must never turn the failure into a
negative finding ("no deployments", "no error logs", "healthy"). ``record_unavailable``
stores the gap in the dossier metrics under ``UNAVAILABLE_KEY`` (a list of dicts), and the
synthesizer states every gap in the ticket.
"""

import asyncio
from collections.abc import Awaitable, Callable
import logging
import random
from typing import Any, Optional, TypeVar

from ..config import AgentConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Dossier metrics key: list of {"source", "target", "reason", "affects_change_detection"}.
UNAVAILABLE_KEY = "unavailable_telemetry"
# Dossier metrics key: change sources that were actually read (list of names). Without one,
# "no change found" is not a finding: the report says change attribution was not assessed.
CHANGE_SOURCES_KEY = "change_sources_checked"
QUOTA_HTTP_CODE = 429
QUOTA_GRPC_STATUS = "RESOURCE_EXHAUSTED"


def is_quota_exhausted(exc: BaseException) -> bool:
    """True for a quota/rate-limit rejection: HTTP 429 or gRPC RESOURCE_EXHAUSTED."""
    try:
        from google.api_core import exceptions as gexc

        if isinstance(exc, gexc.TooManyRequests):  # ResourceExhausted is a subclass
            return True
    except ImportError:
        pass
    code = getattr(exc, "code", None)
    if callable(code):
        try:
            code = code()
        except Exception:
            code = None
    if code == QUOTA_HTTP_CODE:
        return True
    return getattr(code, "name", None) == QUOTA_GRPC_STATUS


def failure_reason(exc: BaseException, service: str) -> str:
    """Short factual reason for a failed read; the exception text is not included."""
    if is_quota_exhausted(exc):
        return f"{service} read quota exceeded"
    return f"{service} read failed ({type(exc).__name__})"


async def call_with_quota_retry(
    fn: Callable[[], T],
    *,
    what: str,
    config: AgentConfig,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> T:
    """Run ``fn`` and retry it on quota errors with bounded full-jitter exponential backoff.

    ``fn`` must perform the whole read (e.g. ``list(client.list_entries(...))``): list
    iterators are lazy and raise while paging. Non-quota errors are raised at once; the quota
    error of the last attempt is raised to the caller, which must record the gap.
    """
    attempts = config.gcp_read_retry_attempts
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not is_quota_exhausted(exc) or attempt >= attempts:
                if is_quota_exhausted(exc):
                    logger.error("%s: quota exceeded on all %d attempts; data unavailable", what, attempts)
                raise
            cap = min(config.gcp_read_retry_max_seconds, config.gcp_read_retry_base_seconds * (2 ** (attempt - 1)))
            delay = jitter(0.0, cap)
            logger.warning(
                "%s: quota exceeded (attempt %d of %d); retrying in %.1fs", what, attempt, attempts, delay
            )
            await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def read_log_entries(
    client: Any,
    *,
    filter_: str,
    max_results: int,
    config: AgentConfig,
    what: str,
    order_by: Optional[str] = None,
) -> list[Any]:
    """One filtered Cloud Logging read with the configured page size and quota retry."""
    kwargs: dict[str, Any] = {
        "filter_": filter_,
        "max_results": max_results,
        "page_size": min(max_results, config.logging_read_page_size),
    }
    if order_by is not None:
        kwargs["order_by"] = order_by
    return await call_with_quota_retry(lambda: list(client.list_entries(**kwargs)), what=what, config=config)


def record_unavailable(
    metrics: dict[str, Any],
    *,
    source: str,
    target: str,
    reason: str,
    affects_change_detection: bool = False,
) -> dict[str, Any]:
    """Record in the dossier metrics that ``source`` for ``target`` could not be read."""
    gap = {
        "source": source,
        "target": target,
        "reason": reason,
        "affects_change_detection": affects_change_detection,
    }
    metrics.setdefault(UNAVAILABLE_KEY, []).append(gap)
    logger.warning("Telemetry unavailable: %s for %s (%s)", source, target, reason)
    return gap


def record_change_source(metrics: dict[str, Any], source: str) -> None:
    """Record in the dossier metrics that change source ``source`` was read successfully."""
    sources = metrics.setdefault(CHANGE_SOURCES_KEY, [])
    if source not in sources:
        sources.append(source)


def change_sources_checked(dossiers: list[Any]) -> list[str]:
    """Names of the change sources read by ``dossiers``, de-duplicated, in dossier order."""
    sources: list[str] = []
    for d in dossiers:
        for source in (getattr(d, "metrics", None) or {}).get(CHANGE_SOURCES_KEY, []) or []:
            if source not in sources:
                sources.append(source)
    return sources


def unavailable_telemetry(dossiers: list[Any]) -> list[dict[str, Any]]:
    """All gaps recorded by ``dossiers``, de-duplicated, in dossier order."""
    seen: set[tuple[str, str, str]] = set()
    gaps: list[dict[str, Any]] = []
    for d in dossiers:
        for gap in (getattr(d, "metrics", None) or {}).get(UNAVAILABLE_KEY, []) or []:
            key = (gap.get("source", ""), gap.get("target", ""), gap.get("reason", ""))
            if key not in seen:
                seen.add(key)
                gaps.append(gap)
    return gaps


def gap_text(gap: dict[str, Any]) -> str:
    return f"{gap['source']} for {gap['target']} unavailable ({gap['reason']})"


def change_detection_incomplete_text(gaps: list[dict[str, Any]]) -> Optional[str]:
    """'change detection incomplete: <reasons>; change attribution not assessed', or None."""
    reasons: list[str] = []
    for gap in gaps:
        if gap.get("affects_change_detection") and gap["reason"] not in reasons:
            reasons.append(gap["reason"])
    if not reasons:
        return None
    return f"change detection incomplete: {', '.join(reasons)}; change attribution not assessed"
