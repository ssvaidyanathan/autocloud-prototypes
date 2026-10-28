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
# Every count statement names its source: the metric and the access log count the same requests
# separately and are never added together.
METRIC_NAME = "proxy/response_count"
METRIC_SOURCE = f"the {METRIC_NAME} metric"
# Dossier metric key of the access-log 5xx fallback (set only when the metric has no such responses).
ACCESS_LOG_5XX_KEY = "access_log_5xx"


def is_5xx_code(code: str) -> bool:
    """A 5xx response code ("500".."599" or the "5xx" class), for the metric and the access log alike."""
    return len(code) == 3 and code.startswith("5") and (code[1:].isdigit() or code == "5xx")


def access_log_clause(code: str, n: int) -> str:
    """'access log shows N <code> responses (log-based, may be partial)'."""
    return f"access log shows {n} {code} {'response' if n == 1 else 'responses'} (log-based, may be partial)"


def response_count_unavailable(metrics: dict[str, Any]) -> bool:
    """True when ApigeeSpecialist recorded proxy/response_count as unreadable."""
    return any(
        isinstance(g, dict) and g.get("source") == APIGEE_RESPONSE_COUNT_SOURCE
        for g in metrics.get(UNAVAILABLE_KEY) or []
    )


def access_log_5xx_statement(
    code: str, n: int, *, metric_unavailable: bool = False, code_prefix: bool = True
) -> str:
    """The fallback line: "HTTP 5xx: not in proxy/response_count; access log shows 3 5xx responses (...)".

    "proxy/response_count unavailable" instead of "not in proxy/response_count" when the metric
    could not be read. ``code_prefix=False`` drops the leading "HTTP 5xx: " for text that already
    names the code (the problem line's "Elevated HTTP 5xx error rate (...)", fresh-rev-n7 N-a)."""
    metric = f"{METRIC_NAME} unavailable" if metric_unavailable else f"not in {METRIC_NAME}"
    prefix = f"HTTP {code}: " if code_prefix else ""
    return f"{prefix}{metric}; {access_log_clause(code, n)}"


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
    # Access-log fallback: responses with ``log_code`` the log shows while the metric has none.
    log_5xx: int = 0
    log_code: str = "5xx"

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

    @property
    def log_statement(self) -> str:
        """"access log shows N 5xx responses (log-based, may be partial)", or "" without a fallback."""
        return access_log_clause(self.log_code, self.log_5xx) if self.log_5xx else ""

    @property
    def log_suffix(self) -> str:
        """", while the access log shows N 5xx responses (...)", or ""."""
        return f", while the {self.log_statement}" if self.log_5xx else ""

    def statement(self) -> str:
        """No-error statement, naming the metric: "0 HTTP 4xx/5xx responses in the
        proxy/response_count metric (of N requests)" or "no responses in the proxy/response_count
        metric", plus the access-log 5xx count when the log shows some."""
        if not self.total:
            return f"no responses in {METRIC_SOURCE}{self.log_suffix}"
        return f"0 HTTP 4xx/5xx responses in {METRIC_SOURCE} (of {self.total} requests){self.log_suffix}"

    def no_5xx_statement(self) -> str:
        """Statement for a proxy with no 5xx in the metric: ``statement()``, or "0 HTTP 5xx
        responses in the proxy/response_count metric (of N requests)" when it returned 4xx
        responses (listed by the Operational Impact column)."""
        if not self.errors:
            return self.statement()
        return f"0 HTTP 5xx responses in {METRIC_SOURCE} (of {self.total} requests){self.log_suffix}"

    def five_xx_statement(self) -> Optional[str]:
        """Non-zero 5xx count as "N HTTP 5xx responses in the proxy/response_count metric (of T
        requests)", else None."""
        n = self.count("5")
        if not n:
            return None
        return f"{n} HTTP 5xx responses in {METRIC_SOURCE} (of {self.total} requests){self.log_suffix}"


def apigee_responses(d: EvidenceDossier) -> Optional[ApigeeResponses]:
    """Response counts of an ApigeeSpecialist dossier, or None when proxy/response_count was
    not queried or could not be read (the report then says "not measured").

    ``metrics["response_codes"]`` holds the proxy/response_count metric counts only; access-log
    status codes are a separate count of the same requests (``access_log_status_codes``) and are
    never added in, so N in "of N requests" is the metric total. The access-log 5xx fallback
    (``metrics["access_log_5xx"]``) is carried alongside, so every statement on the proxy states it."""
    if d.specialist_name != "ApigeeSpecialist":
        return None
    m: dict[str, Any] = d.metrics or {}
    queried = any(_RESPONSE_COUNT_QUERY in q for q in (d.queries_executed or {}).get("cloud_monitoring", []))
    codes = m.get("response_codes")
    if not queried or response_count_unavailable(m) or not isinstance(codes, dict):
        return None
    clean = {
        str(c): int(n) for c, n in codes.items() if isinstance(n, int) and not isinstance(n, bool) and n >= 0
    }
    log = m.get(ACCESS_LOG_5XX_KEY)
    log_n = log.get("count") if isinstance(log, dict) else None
    if isinstance(log_n, int) and not isinstance(log_n, bool) and log_n > 0:
        return ApigeeResponses(
            proxy=d.target_resource, codes=clean, log_5xx=log_n, log_code=str(log.get("code") or "5xx"),
        )
    return ApigeeResponses(proxy=d.target_resource, codes=clean)


def access_log_only_statement(d: EvidenceDossier, *, code_prefix: bool = True) -> Optional[str]:
    """The access-log 5xx count of an ApigeeSpecialist dossier whose proxy/response_count could
    not be used (``apigee_responses`` is None): "HTTP 5xx: proxy/response_count unavailable;
    access log shows 3 5xx responses (log-based, may be partial)". None otherwise.

    The report states this instead of "not measured" (fresh-rev-n6 O1): the HTML metrics row
    shows the same log count for the proxy. ``code_prefix`` as in ``access_log_5xx_statement``."""
    if d.specialist_name != "ApigeeSpecialist" or apigee_responses(d) is not None:
        return None
    log = (d.metrics or {}).get(ACCESS_LOG_5XX_KEY)
    log_n = log.get("count") if isinstance(log, dict) else None
    if not isinstance(log_n, int) or isinstance(log_n, bool) or log_n <= 0:
        return None
    return access_log_5xx_statement(
        str(log.get("code") or "5xx"), log_n, metric_unavailable=True, code_prefix=code_prefix
    )


def component_access_log_only(
    resource: str, dossiers: list[EvidenceDossier], *, code_prefix: bool = True
) -> Optional[str]:
    """``access_log_only_statement`` of the first dossier on ``resource`` that has one."""
    for d in dossiers:
        if d.target_resource == resource:
            s = access_log_only_statement(d, code_prefix=code_prefix)
            if s is not None:
                return s
    return None


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
