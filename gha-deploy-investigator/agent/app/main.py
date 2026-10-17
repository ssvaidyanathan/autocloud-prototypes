import logging
import threading
import time
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI

from .config import AgentConfig, get_settings
from .contracts.request import InvestigationRequest
from .contracts.result import InvestigationResult
from .dispatcher import InvestigationDispatcher

logger = logging.getLogger(__name__)

app = FastAPI(
    title="GitHub Actions Deployment Investigator Agent",
    description="Analytical engine diagnosing CI/CD workflow failure logs",
    version="0.1.0",
)

_dispatcher: Optional[InvestigationDispatcher] = None
_deduplicator: Optional["InvestigationDeduplicator"] = None


class InvestigationDeduplicator:
    """In-memory deduplicator for investigation requests.

    Prevents duplicate Cloud Tasks dispatches or concurrent retries from
    running parallel multi-agent LLM pipelines or causing duplicate write-backs.
    """

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._inflight: dict[str, tuple[threading.Condition, list[Any]]] = {}
        self._results: dict[str, tuple[float, InvestigationResult]] = {}

    def _cleanup_expired(self, now: float) -> None:
        expired_keys = [
            k for k, (t, _) in self._results.items()
            if now - t > self.ttl_seconds
        ]
        for k in expired_keys:
            del self._results[k]

    def deduplicate(
        self,
        key: str,
        compute_fn: Callable[[], InvestigationResult],
    ) -> InvestigationResult:
        with self._lock:
            now = time.time()
            self._cleanup_expired(now)

            # 1. Return cached result if already completed within TTL
            if key in self._results:
                logger.info("Returning cached investigation result for key=%s", key)
                return self._results[key][1]

            # 2. If already in flight, wait on the existing condition
            if key in self._inflight:
                logger.info(
                    "Investigation for %s is already in-flight; waiting for completion",
                    key,
                )
                cond, state = self._inflight[key]
                cond.wait(timeout=self.ttl_seconds)
                if state[0] is not None:
                    return state[0]
                if state[1] is not None:
                    raise state[1]
                if key in self._results:
                    return self._results[key][1]
                raise TimeoutError(f"Timed out waiting for in-flight investigation for {key}")

            # 3. First request: register as in-flight
            cond = threading.Condition(self._lock)
            state = [None, None]  # [result, exception]
            self._inflight[key] = (cond, state)

        # 4. Compute outside of lock so concurrent requests can wait or check cache
        try:
            result = compute_fn()
            with self._lock:
                now = time.time()
                self._results[key] = (now, result)
                state[0] = result
                self._inflight.pop(key, None)
                cond.notify_all()
            return result
        except Exception as exc:
            with self._lock:
                state[1] = exc
                self._inflight.pop(key, None)
                cond.notify_all()
            raise

    def clear(self) -> None:
        with self._lock:
            self._results.clear()
            self._inflight.clear()


def get_deduplicator() -> InvestigationDeduplicator:
    global _deduplicator
    if _deduplicator is None:
        _deduplicator = InvestigationDeduplicator(ttl_seconds=300.0)
    return _deduplicator


def set_deduplicator(deduplicator: Optional[InvestigationDeduplicator]) -> None:
    global _deduplicator
    _deduplicator = deduplicator


def get_dispatcher(
    settings: AgentConfig = Depends(get_settings),
) -> InvestigationDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = InvestigationDispatcher(config=settings)
    return _dispatcher


def set_dispatcher(dispatcher: Optional[InvestigationDispatcher]) -> None:
    global _dispatcher
    _dispatcher = dispatcher


@app.get("/health")
def health(settings: AgentConfig = Depends(get_settings)) -> dict[str, str]:
    return {"status": "healthy", "service": settings.service_name}


@app.post("/investigate")
def handle_investigate(
    request: InvestigationRequest,
    dispatcher: InvestigationDispatcher = Depends(get_dispatcher),
    deduplicator: InvestigationDeduplicator = Depends(get_deduplicator),
) -> InvestigationResult:
    """Investigate workflow run failure and return or deliver report."""
    key = (
        f"{request.incident.source}:{request.incident.external_id}:"
        f"{getattr(request.incident, 'github_run_attempt', 1) or 1}"
    )
    return deduplicator.deduplicate(key, lambda: dispatcher.investigate(request))
