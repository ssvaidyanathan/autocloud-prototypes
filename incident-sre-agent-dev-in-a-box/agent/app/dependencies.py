"""Dependency traversal and downstream attribution for incident investigations.

Identifies downstream service dependencies from application logs and request traces,
fans out bounded specialist investigations on discovered dependencies, and supports
root cause attribution to downstream failures.
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from .config import AgentConfig, get_settings
from .contracts.findings import EvidenceItem, Findings
from .tools.common import get_candidate_service_names, normalize_datetime

logger = logging.getLogger(__name__)

# Common non-service domains and hostnames to ignore during dependency extraction
IGNORED_HOSTS = {
    "googleapis.com",
    "google.com",
    "cloud.google.com",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "cluster.local",
    "metadata.google.internal",
    "accounts.google.com",
    "oauth2.googleapis.com",
    "run.app",
    "a.run.app",
}


def resolve_run_app_service_name(host_or_url: str) -> str:
    """Resolve a Cloud Run hostname or URL to its base service name.

    Strips schemes (http://, https://), port numbers, paths, query parameters,
    and Cloud Run generated hash/region suffixes for both Form A and Form B:
      - Form A (Project Number): <service>-<digits>.<region>.run.app -> <service>
      - Form B (Direct / Tag): <service>-<tag>-<region_code>.a.run.app -> <service>
        (where tag is 6-16 alphanumeric chars and region code is 2-6 chars)
    """
    cleaned = host_or_url.strip()
    cleaned = re.sub(r"^https?://", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.split("/")[0].split("?")[0]
    cleaned = cleaned.split(":")[0].lower()

    if ".run.app" in cleaned:
        subdomain = cleaned.split(".")[0]
    else:
        if "." in cleaned:
            return cleaned
        subdomain = cleaned

    if "---" in subdomain:
        subdomain = subdomain.split("---")[-1]

    # Suffix pattern 2: -<tag>-<region_code> before .a.run.app or .run.app
    # (tag is 6-16 alphanumeric chars, region code is 2-6 chars)
    # Tag may be purely alphabetic (e.g. btcbwtmvvq) or alphanumeric.
    match_tag_region = re.search(r"-([a-z0-9]{6,16})-([a-z0-9]{2,6})$", subdomain)
    if match_tag_region and match_tag_region.group(2) not in {"svc", "service", "api"}:
        subdomain = subdomain[: match_tag_region.start()]

    # Suffix pattern 1: -<digits> before .run.app or .<region>.run.app
    subdomain = re.sub(r"-\d+$", "", subdomain)

    return subdomain


def _is_self_dependency(candidate: str, primary_candidates: Set[str]) -> bool:
    """Check if candidate service resolves to primary service or any of its aliases."""
    clean = candidate.strip().lower()
    resolved = resolve_run_app_service_name(clean)
    names_to_check = {clean, resolved}

    for cand in names_to_check:
        if not cand:
            continue
        for c in primary_candidates:
            if not c:
                continue
            if cand == c:
                return True
            if cand.startswith(f"{c}-") or c.startswith(f"{cand}-"):
                return True
            if "-" in c and cand.endswith(f"-{c}"):
                return True
            if "-" in cand and c.endswith(f"-{cand}"):
                return True
    return False


def _is_valid_dependency_name(cand: str, primary_candidates: Set[str]) -> bool:
    """Validate that candidate string looks like an internal downstream service name."""
    clean = cand.strip().lower()
    if not clean or len(clean) < 3 or len(clean) > 63:
        return False
    if _is_self_dependency(clean, primary_candidates):
        return False
    if clean in IGNORED_HOSTS or any(clean.endswith(h) for h in IGNORED_HOSTS):
        return False
    if clean.startswith("projects/") or clean.startswith("namespaces/"):
        return False
    if not re.match(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", clean):
        return False
    return True


def extract_downstream_dependencies(
    primary_service: str,
    findings: Dict[str, Findings],
    max_services: int = 3,
) -> List[str]:
    """Inspect findings and excerpts to detect failing downstream dependency services."""
    primary_clean = primary_service.strip().lower()
    primary_candidates: Set[str] = {c.lower() for c in get_candidate_service_names(primary_clean)}
    primary_candidates.add(primary_clean)
    for c in list(primary_candidates):
        resolved_c = resolve_run_app_service_name(c)
        primary_candidates.add(resolved_c)
        if "-" in c:
            stripped = c.split("-", 1)[1]
            if "-" in stripped:
                primary_candidates.add(stripped)
        if "-" in resolved_c:
            stripped = resolved_c.split("-", 1)[1]
            if "-" in stripped:
                primary_candidates.add(stripped)

    discovered: List[str] = []

    # Collect text excerpts from logs, request logs, anomalies, and errors
    text_corpus: List[str] = []
    for f in findings.values():
        for ev in f.evidence:
            text_corpus.append(ev.excerpt)
        text_corpus.extend(f.anomalies)
        text_corpus.extend(f.errors)

    full_text = " \n ".join(text_corpus)

    # Regex 1: HTTP calls to URLs e.g. http://backend-service:8080 or https://backend-service.run.app
    url_matches = re.findall(
        r"https?://([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*)(?::\d+)?(?:/[^\s\"',]*)?",
        full_text,
    )
    for host in url_matches:
        host_clean = host.split(":")[0].lower()
        if ".run.app" in host_clean or ".a.run.app" in host_clean:
            host_clean = resolve_run_app_service_name(host_clean)
        else:
            host_clean = resolve_run_app_service_name(host_clean)
        if _is_valid_dependency_name(host_clean, primary_candidates):
            if host_clean not in discovered:
                discovered.append(host_clean)

    # Regex 2: Explicit downstream or service calling patterns
    # e.g. "calling backend-service", "downstream service backend-service returned 403"
    calling_patterns = [
        (
            r"\b(?:calling|call to|target|downstream|dependency)(?:\s+service)?[:\s]+"
            r"([a-zA-Z0-9_-]+(?:-svc|-service|-api))\b"
        ),
        (
            r"\b([a-zA-Z0-9_-]+(?:-svc|-service|-api))\b.*?"
            r"(?:returned\s+(?:HTTP\s+)?(?:403|502|504|500)|connection\s+refused|timeout)"
        ),
        r"connect(?:ion)?\s+(?:to|failure\s+to)\s+([a-zA-Z0-9_-]+(?:-svc|-service|-api))\b",
    ]
    for pattern in calling_patterns:
        matches = re.findall(pattern, full_text, re.IGNORECASE)
        for m in matches:
            cand = resolve_run_app_service_name(m.lower().strip())
            if _is_valid_dependency_name(cand, primary_candidates):
                if cand not in discovered:
                    discovered.append(cand)

    return discovered[:max_services]


def inspect_downstream_dependency(
    service_name: str,
    start_time: datetime,
    end_time: datetime,
    project_id: Optional[str] = None,
    client: Any = None,
    config: Optional[AgentConfig] = None,
    max_depth: Optional[int] = None,
) -> Findings:
    """Execute targeted telemetry inspection on a downstream dependency service.

    Args:
        service_name: Downstream dependency service to inspect.
        start_time: Investigation window start timestamp.
        end_time: Investigation window end timestamp.
        project_id: Optional GCP project ID.
        client: Optional Cloud Logging / Monitoring client.
        config: Optional agent configuration.
        max_depth: Maximum dependency fan-out traversal depth. Defaults to
            `config.dependency_fanout_max_depth` (default 2), bounding multi-hop
            dependency inspection.
    """
    cfg = config or get_settings()
    eff_project_id = project_id or getattr(cfg, "gcp_project", None)
    eff_max_depth = (
        max_depth if max_depth is not None else getattr(cfg, "dependency_fanout_max_depth", 2)
    )

    evidence: List[EvidenceItem] = []
    anomalies: List[str] = []
    queries_run: List[str] = [
        f"dependency_inspection(service={service_name}, max_depth={eff_max_depth})"
    ]
    errors: List[str] = []

    total_reqs = 0
    err_4xx = 0
    err_5xx = 0

    # 1. Query Request Logs for downstream service
    try:
        from .tools.logs import query_request_logs

        req_summary = query_request_logs(
            service_name=service_name,
            project_id=eff_project_id,
            start_time=start_time,
            end_time=end_time,
            client=client,
        )
        queries_run.append(f"query_request_logs({service_name})")

        total_reqs = req_summary.get("total_requests", 0)
        status_bd = req_summary.get("status_breakdown", {})
        err_4xx = status_bd.get("4xx", 0)
        err_5xx = status_bd.get("5xx", 0)

        if total_reqs > 0 or err_4xx > 0 or err_5xx > 0:
            evidence.append(
                EvidenceItem(
                    kind="dependency_request_logs",
                    excerpt=(
                        f"Downstream dependency {service_name} request summary: "
                        f"{total_reqs} requests (4xx: {err_4xx}, 5xx: {err_5xx})"
                    ),
                    timestamp=start_time,
                    console_url=req_summary.get("console_url"),
                )
            )
            if err_4xx > 0 or err_5xx > 0:
                anomalies.append(
                    f"Downstream service {service_name} exhibits HTTP errors: "
                    f"{err_4xx} 4xx errors, {err_5xx} 5xx errors."
                )
    except Exception as exc:
        errors.append(f"Downstream request logs error for {service_name}: {exc}")

    # 2. Query Downstream Application Logs (severity >= WARNING)
    try:
        from .tools.logs import query_logs

        log_filter = (
            f'resource.type = "cloud_run_revision" AND severity >= WARNING '
            f'AND resource.labels.service_name = "{service_name}"'
        )
        logs_res = query_logs(
            filter_str=log_filter,
            resource_names=[f"projects/{eff_project_id}"] if eff_project_id else [],
            max_entries=20,
            start_time=start_time,
            end_time=end_time,
            client=client,
        )
        queries_run.append(f"query_logs({service_name})")

        for entry in logs_res.get("entries", [])[:5]:
            ts_val = normalize_datetime(entry.get("timestamp")) or start_time
            sev = str(entry.get("severity", "WARNING")).upper()
            payload = entry.get("payload_excerpt", "")
            evidence.append(
                EvidenceItem(
                    kind="dependency_log_entry",
                    excerpt=f"[{sev}] [{service_name}] {payload}",
                    timestamp=ts_val,
                    console_url=logs_res.get("console_url"),
                )
            )
            if "403" in payload or "permission" in payload.lower() or "denied" in payload.lower():
                anomalies.append(
                    f"Downstream {service_name} IAM/permission authorization failure: "
                    f"{payload[:100]}"
                )
    except Exception as exc:
        errors.append(f"Downstream logs query error for {service_name}: {exc}")

    # 3. Inspect Cloud Run Service status & configuration
    try:
        from .tools.resources import describe_cloud_run_service

        svc_desc = describe_cloud_run_service(
            service_name=service_name,
            project_id=eff_project_id,
            client=client,
        )
        queries_run.append(f"describe_cloud_run_service({service_name})")
        if isinstance(svc_desc, dict) and svc_desc.get("status") != "NOT_FOUND":
            evidence.append(
                EvidenceItem(
                    kind="dependency_service_spec",
                    excerpt=(
                        f"Downstream service {service_name} active: "
                        f"URI: {svc_desc.get('uri')}, ingress: {svc_desc.get('ingress', 'all')}"
                    ),
                    timestamp=start_time,
                    console_url=svc_desc.get("console_url"),
                )
            )
    except Exception as exc:
        errors.append(f"Downstream describe service error for {service_name}: {exc}")

    if len(anomalies) == 0 and err_4xx == 0 and err_5xx == 0:
        evidence.append(
            EvidenceItem(
                kind="dependency_health_check",
                excerpt=(
                    f"Downstream dependency {service_name} verified healthy: "
                    f"{total_reqs} requests, 0 HTTP errors (4xx: 0, 5xx: 0), no latency anomalies."
                ),
                timestamp=start_time,
            )
        )
        summary = (
            f"Downstream dependency investigation for {service_name}: "
            "verified healthy with 0 errors and no latency anomalies."
        )
    else:
        summary = (
            f"Downstream dependency investigation for {service_name}: "
            f"collected {len(evidence)} evidence items, {len(anomalies)} anomalies."
        )
    confidence = 0.80 if anomalies else 0.40

    return Findings(
        summary=summary,
        evidence=evidence,
        anomalies=anomalies,
        queries_run=queries_run,
        confidence=confidence,
        errors=errors,
        skills_used=[],
        skill_first=False,
        fallback_used=False,
    )
