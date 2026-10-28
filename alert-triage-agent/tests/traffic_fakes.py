"""Fake Cloud Monitoring client returning run.googleapis.com/request_count series per revision.

Used to exercise ChangeSpecialist's traffic-history lookup (previous serving revision)
without a live project.
"""

import re
from types import SimpleNamespace
from typing import Any


def _series(revision: str, counts: list[int]) -> Any:
    points = [SimpleNamespace(value=SimpleNamespace(int64_value=c)) for c in counts]
    return SimpleNamespace(resource=SimpleNamespace(labels={"revision_name": revision}), points=points)


class FakeRequestCountClient:
    """`by_service` maps service name -> {revision name -> per-minute request counts in the window}.

    Like the real API, only series of the service named in the request filter are returned.
    """

    def __init__(self, by_service: dict[str, dict[str, list[int]]] | None = None, error: Exception | None = None):
        self.by_service = by_service if by_service is not None else {}
        self.error = error
        self.calls: list[Any] = []

    def list_time_series(self, request: Any = None, **kwargs: Any) -> list[Any]:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        m = re.search(r'resource\.labels\.service_name = "([^"]+)"', getattr(request, "filter", "") or "")
        revisions = self.by_service.get(m.group(1), {}) if m else {}
        return [_series(rev, counts) for rev, counts in revisions.items()]


def serving_dossier_metrics(revision: str, service: str, requests: int = 225) -> dict[str, Any]:
    """ChangeSpecialist metrics for a previous serving revision verified from traffic history."""
    return {
        "previous_serving_revision": revision,
        "previous_serving_evidence": (
            f"Cloud Monitoring run.googleapis.com/request_count: {revision} was the only revision of "
            f"{service} that received requests in the window before the deploy ({requests} requests)."
        ),
    }
