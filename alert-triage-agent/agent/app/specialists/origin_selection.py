"""Group-wide, topology-aware origin selection.

An incident group can hold alerts on several services (for example a latency fault injected
into a backend raises latency alerts on the backend AND on every service that calls it).
The investigation must start from the service whose degradation explains the others, not
from whichever alert happened to arrive first.

Rule (deterministic, computed from the group's alerts and TOPOLOGY.md):

1. Every service with an alert in the group is *degraded* (the alert is the evidence).
2. Service S is *explained* by degraded service U when S depends on U (U is upstream of S in
   the topology, following "From depends on To" edges transitively). Gateway-enforced
   rejections (rate limiting, client auth) are raised by the gateway itself and are never
   explained by a backend.
3. Candidates are ranked: unexplained services first; then the number of other degraded
   services they explain (more is better); then the earliest alert onset; then arrival order.
   The top candidate is the origin.

The selection only orders candidates. Specialist evidence gathered afterwards can still move
the origin further upstream (e.g. to the database) through the synthesizer's existing rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Callable, Optional

from ..contracts.alert import AlertEvent
from ..correlation.scoring import classify_anomaly_category

logger = logging.getLogger(__name__)

# Anomaly categories enforced at the gateway itself: a backend degradation does not explain them.
GATEWAY_ENFORCED_CATEGORIES = frozenset({"client_429", "client_auth"})

# At most this many candidates are investigated (the origin plus one runner-up).
MAX_INVESTIGATED_CANDIDATES = 2


def _field(alert: dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = alert.get(key)
        if value:
            return str(value)
    return None


def _labels(alert: dict[str, Any]) -> dict[str, Any]:
    return alert.get("labels") or alert.get("policy_user_labels") or {}


def alert_component(alert: dict[str, Any]) -> Optional[str]:
    """Service named by a group alert entry (component label, else resource)."""
    return _field(alert, "component") or _labels(alert).get("component") or _field(
        alert, "resource_name", "resource"
    )


def alert_signal(alert: dict[str, Any]) -> str:
    return str(_field(alert, "signal") or _labels(alert).get("signal") or "")


def alert_category(alert: dict[str, Any]) -> str:
    """Anomaly category of a group alert, using the correlation engine's classifier."""
    return classify_anomaly_category(_field(alert, "policy_name", "policy") or "", "", alert_signal(alert))


@dataclass
class OriginCandidate:
    """One degraded service of the group, with the topology facts used to rank it."""

    service: str
    alerts: list[dict[str, Any]] = field(default_factory=list)
    first_arrival: int = 0
    onset: Optional[str] = None
    signals: list[str] = field(default_factory=list)
    explained_by: list[str] = field(default_factory=list)
    explains: list[str] = field(default_factory=list)
    gateway_enforced: bool = False

    @property
    def score(self) -> int:
        """Ranking score: degraded services explained, minus degraded upstream causes."""
        return len(self.explains) - len(self.explained_by)

    def summary(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "score": self.score,
            "explains": list(self.explains),
            "explained_by": list(self.explained_by),
            "onset": self.onset,
            "signals": list(self.signals),
            "alerts": len(self.alerts),
        }


@dataclass
class OriginSelection:
    """Result of group-wide origin selection."""

    origin: str
    primary_alert: dict[str, Any]
    candidates: list[OriginCandidate]
    reasons: list[str] = field(default_factory=list)

    @property
    def degraded_services(self) -> list[str]:
        return [c.service for c in self.candidates]

    @property
    def is_multi_service(self) -> bool:
        return len(self.candidates) > 1

    def candidate(self, service: str) -> Optional[OriginCandidate]:
        return next((c for c in self.candidates if c.service == service), None)

    def investigated_candidates(self) -> list[OriginCandidate]:
        return self.candidates[:MAX_INVESTIGATED_CANDIDATES]

    def explanation(self) -> str:
        """One-paragraph 'Origin selection' explanation for the report."""
        return " ".join(self.reasons)


def select_origin(
    group_alerts: list[dict[str, Any]],
    *,
    canonicalize: Callable[[Optional[str]], Optional[str]],
    upstream_of: Callable[[str], list[str]],
) -> Optional[OriginSelection]:
    """Choose the origin among the services alerting in the group.

    ``upstream_of(s)`` returns every component ``s`` depends on (transitively). Returns None
    when the group holds no usable alert. Logs the decision at INFO with every candidate.
    """
    by_service: dict[str, OriginCandidate] = {}
    for idx, alert in enumerate(a for a in (group_alerts or []) if isinstance(a, dict)):
        raw = alert_component(alert)
        if not raw:
            logger.warning("Origin selection: group alert %s names no service; skipped", alert.get("incident_id"))
            continue
        service = canonicalize(raw) or raw
        cand = by_service.get(service)
        if cand is None:
            cand = OriginCandidate(service=service, first_arrival=idx)
            by_service[service] = cand
        cand.alerts.append(alert)
        sig = alert_signal(alert)
        if sig and sig not in cand.signals:
            cand.signals.append(sig)
        started = _field(alert, "started_at")
        if started and (cand.onset is None or started < cand.onset):
            cand.onset = started
        if alert_category(alert) in GATEWAY_ENFORCED_CATEGORIES:
            cand.gateway_enforced = True

    if not by_service:
        return None

    services = list(by_service)
    for service, cand in by_service.items():
        try:
            deps = set(upstream_of(service) or [])
        except Exception as exc:
            logger.error(
                "Origin selection: topology upstream lookup failed for %s (%s); treating it as unexplained",
                service,
                type(exc).__name__,
            )
            deps = set()
        if cand.gateway_enforced:
            continue
        for other in services:
            if other != service and other in deps:
                cand.explained_by.append(other)
                by_service[other].explains.append(service)

    ranked = sorted(
        by_service.values(),
        key=lambda c: (
            1 if c.explained_by else 0,
            -len(c.explains),
            c.onset or "~",
            c.first_arrival,
        ),
    )
    origin = ranked[0]
    first_arrived = min(by_service.values(), key=lambda c: c.first_arrival)
    reasons = _build_reasons(origin, ranked, first_arrived)

    selection = OriginSelection(
        origin=origin.service,
        primary_alert=origin.alerts[0],
        candidates=ranked,
        reasons=reasons,
    )
    logger.info(
        "Origin selection: origin=%s (first alert was on %s); candidates=%s",
        origin.service,
        first_arrived.service,
        [c.summary() for c in ranked],
    )
    return selection


def _build_reasons(origin: OriginCandidate, ranked: list[OriginCandidate], first: OriginCandidate) -> list[str]:
    if len(ranked) == 1:
        return [f"Single alerting service {origin.service}; origin follows the alert."]
    names = ", ".join(c.service for c in ranked)
    reasons = [f"Alerting services in the group: {names}."]
    if origin.explains:
        reasons.append(
            f"{origin.service} is upstream of {', '.join(origin.explains)} in the topology and shows its own"
            f" alert ({', '.join(origin.signals) or 'unlabelled signal'}), so its degradation explains theirs."
        )
    else:
        reasons.append(
            f"No alerting service depends on another; {origin.service} chosen by"
            f" {'earliest onset' if origin.onset else 'alert arrival order'}."
        )
    for cand in ranked[1:]:
        if cand.explained_by:
            reasons.append(
                f"{cand.service} {'/'.join(cand.signals) or 'degradation'} explained by upstream"
                f" {', '.join(cand.explained_by)}."
            )
        elif cand.gateway_enforced:
            reasons.append(f"{cand.service} rejections are enforced at the gateway; not attributed to a backend.")
        else:
            reasons.append(f"{cand.service} has no topology relationship to {origin.service}; not cross-attributed.")
    if first.service != origin.service:
        reasons.append(f"The first alert arrived on {first.service}; arrival order was not used to pick the origin.")
    return reasons


def alert_event_from_entry(entry: dict[str, Any], fallback: AlertEvent) -> AlertEvent:
    """Build the investigation AlertEvent for a group alert entry (OKF ``alerts`` item).

    Fields the entry lacks are taken from ``fallback`` (the alert the wake started from).
    The entry is what was stored at ingest, so resource labels and the condition name are only
    carried over when the entry is the fallback alert itself.
    """
    if entry.get("incident_id") == fallback.incident_id:
        return fallback
    labels = _labels(entry)
    return AlertEvent(
        incident_id=_field(entry, "incident_id") or fallback.incident_id,
        policy_name=_field(entry, "policy_name", "policy") or fallback.policy_name,
        policy_id=_field(entry, "policy_id"),
        resource_name=_field(entry, "resource_name", "resource") or alert_component(entry) or fallback.resource_name,
        resource_type=_field(entry, "resource_type") or fallback.resource_type,
        state=_field(entry, "state") or "open",
        started_at=_field(entry, "started_at") or fallback.started_at,
        url=_field(entry, "url"),
        policy_user_labels=labels,
        labels=labels,
    )
