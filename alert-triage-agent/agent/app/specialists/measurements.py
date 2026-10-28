"""What one investigation measured on one component: the single source for every report line.

The ticket's problem statement, the HTML "Operational Impact" column and the HTML
"Alternative Hypotheses" rows each state whether a component's HTTP errors were measured.
They read these helpers, so the ticket and the HTML cannot disagree (S4 INC0010362: the
ticket said the inventory proxy's "HTTP 5xx rate not measured" while the HTML said
"no HTTP 4xx/5xx responses recorded" for the same proxy).
"""

from dataclasses import dataclass
from typing import Any, Optional

from .base import EvidenceDossier
from .telemetry_reads import UNAVAILABLE_KEY

# Source name ApigeeSpecialist records when proxy/response_count cannot be read.
APIGEE_RESPONSE_COUNT_SOURCE = "Cloud Monitoring apigee.googleapis.com/proxy/response_count"
_RESPONSE_COUNT_QUERY = "apigee.googleapis.com/proxy/response_count"


def cloud_run_5xx_rate(d: EvidenceDossier) -> Optional[str]:
    """The measured Cloud Run 5xx rate of ``d`` as stated ("0.0%"), or None when not measured.

    A not-applicable dossier (Apigee proxy, Cloud SQL) and "N/A (not captured)" record no rate.
    """
    if d.specialist_name != "CloudRunSpecialist":
        return None
    rate = (d.metrics or {}).get("error_rate_5xx")
    if isinstance(rate, (int, float)) and not isinstance(rate, bool):
        return f"{rate}%"
    if isinstance(rate, str) and rate.strip().endswith("%"):
        try:
            float(rate.strip().rstrip("%"))
        except ValueError:
            return None
        return rate.strip()
    return None


def rate_pct(rate: str) -> float:
    """Percent value of a rate returned by ``cloud_run_5xx_rate``."""
    return float(rate.rstrip("%"))


@dataclass(frozen=True)
class ApigeeResponses:
    """Apigee proxy/response_count as queried: response counts per HTTP code in the window."""

    proxy: str
    codes: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.codes.values())

    def count(self, prefix: str) -> int:
        """Responses whose code starts with ``prefix`` ("5", "4", "429")."""
        return sum(n for c, n in self.codes.items() if c.startswith(prefix))

    @property
    def errors(self) -> dict[str, int]:
        """Non-zero 4xx/5xx counts per code."""
        return {c: n for c, n in sorted(self.codes.items()) if c[:1] in ("4", "5") and n > 0}

    def statement(self) -> str:
        """No-error statement: "0 HTTP 4xx/5xx responses (of N requests)" or "no traffic recorded"."""
        if not self.total:
            return "no traffic recorded"
        return f"0 HTTP 4xx/5xx responses (of {self.total} requests)"

    def no_5xx_statement(self) -> str:
        """Statement for a proxy with no 5xx: ``statement()``, or "0 HTTP 5xx responses (of N
        requests)" when it returned 4xx responses (listed by the Operational Impact column)."""
        if not self.errors:
            return self.statement()
        return f"0 HTTP 5xx responses (of {self.total} requests)"

    def five_xx_statement(self) -> Optional[str]:
        """Non-zero 5xx count as "N HTTP 5xx responses (of T requests)", else None."""
        n = self.count("5")
        return f"{n} HTTP 5xx responses (of {self.total} requests)" if n else None


def apigee_responses(d: EvidenceDossier) -> Optional[ApigeeResponses]:
    """Response counts of an ApigeeSpecialist dossier, or None when proxy/response_count was
    not queried or could not be read (the report then says "not measured")."""
    if d.specialist_name != "ApigeeSpecialist":
        return None
    m: dict[str, Any] = d.metrics or {}
    queried = any(_RESPONSE_COUNT_QUERY in q for q in (d.queries_executed or {}).get("cloud_monitoring", []))
    unavailable = any(
        isinstance(g, dict) and g.get("source") == APIGEE_RESPONSE_COUNT_SOURCE for g in m.get(UNAVAILABLE_KEY) or []
    )
    codes = m.get("response_codes")
    if not queried or unavailable or not isinstance(codes, dict):
        return None
    clean = {
        str(c): int(n) for c, n in codes.items() if isinstance(n, int) and not isinstance(n, bool) and n >= 0
    }
    return ApigeeResponses(proxy=d.target_resource, codes=clean)


def component_responses(resource: str, dossiers: list[EvidenceDossier]) -> Optional[ApigeeResponses]:
    """The Apigee response counts measured on ``resource``, if any."""
    for d in dossiers:
        if d.target_resource == resource:
            r = apigee_responses(d)
            if r is not None:
                return r
    return None


def component_5xx_rate(resource: str, dossiers: list[EvidenceDossier]) -> Optional[str]:
    """The Cloud Run 5xx rate measured on ``resource``, if any."""
    for d in dossiers:
        if d.target_resource == resource:
            r = cloud_run_5xx_rate(d)
            if r is not None:
                return r
    return None
