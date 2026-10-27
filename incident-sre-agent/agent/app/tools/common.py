"""Common helper utilities for SRE agent tools and specialists."""

import os
import re
from datetime import datetime, timezone
from typing import Any, Optional, Set

# Standard GCP Logging Severities
VALID_SEVERITIES: Set[str] = {
    "DEBUG",
    "INFO",
    "NOTICE",
    "WARNING",
    "ERROR",
    "CRITICAL",
    "ALERT",
    "EMERGENCY",
}

# Regex for safe identifier / service name (alphanumeric, hyphens, underscores)
SAFE_SERVICE_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")

# Regex for safe Kubernetes / GCP identifier (alphanumeric, hyphens, underscores, dots)
SAFE_K8S_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

# Regex for status code or status code range (e.g. '500', '500-599')
STATUS_CODE_PATTERN = re.compile(r"^([1-5][0-9]{2})(?:-([1-5][0-9]{2}))?$")


def normalize_datetime(dt: Any) -> Optional[datetime]:
    """Normalize input (datetime, ISO string, etc.) into a UTC timezone-aware datetime.

    Returns None if dt is None or cannot be parsed.
    """
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(dt, str):
        cleaned = dt.strip()
        if not cleaned:
            return None
        cleaned = cleaned.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(cleaned)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None
    if isinstance(dt, (int, float)):
        try:
            return datetime.fromtimestamp(dt, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def validate_service(service: Optional[str]) -> Optional[str]:
    """Validate and sanitize service name to prevent injection attacks."""
    if service is None:
        return None
    cleaned = service.strip()
    if not cleaned:
        return None
    if not SAFE_SERVICE_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid service name '{service}'. "
            "Service names must only contain letters, numbers, hyphens, or underscores."
        )
    return cleaned


def validate_k8s_identifier(
    identifier: Optional[str], field_name: str = "identifier"
) -> Optional[str]:
    """Validate and sanitize a Kubernetes / GCP resource identifier to prevent filter injection."""
    if identifier is None:
        return None
    cleaned = identifier.strip()
    if not cleaned:
        return None
    if "\n" in cleaned or "\r" in cleaned or '"' in cleaned or "'" in cleaned:
        raise ValueError(
            f"Invalid {field_name} '{identifier}'. "
            "Identifiers cannot contain newlines or quote characters."
        )
    if not SAFE_K8S_IDENTIFIER_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid {field_name} '{identifier}'. "
            f"{field_name} must only contain letters, numbers, hyphens, underscores, or dots."
        )
    return cleaned


def validate_severity(severity: Optional[str]) -> Optional[str]:
    """Validate severity against standard GCP severities."""
    if severity is None:
        return None
    cleaned = severity.strip().upper()
    if not cleaned:
        return None
    if cleaned not in VALID_SEVERITIES:
        valid_list = ", ".join(sorted(VALID_SEVERITIES))
        raise ValueError(
            f"Invalid severity '{severity}'. "
            f"Severity must be one of standard GCP severities: {valid_list}."
        )
    return cleaned


def validate_status_code_range(status_code_range: Optional[str]) -> Optional[str]:
    """Validate status code range (e.g. '500', '500-599', or '5xx') and reject raw injection."""
    if status_code_range is None:
        return None
    cleaned = status_code_range.strip()
    if not cleaned:
        return None
    class_match = re.match(r"^([1-5])xx$", cleaned, re.IGNORECASE)
    if class_match:
        digit = class_match.group(1)
        cleaned = f"{digit}00-{digit}99"
    match = STATUS_CODE_PATTERN.match(cleaned)
    if not match:
        raise ValueError(
            f"Invalid status_code_range '{status_code_range}'. "
            "Expected 3-digit HTTP code (e.g. '500'), range (e.g. '500-599'), "
            "or class (e.g. '5xx')."
        )
    start_code = int(match.group(1))
    end_code = int(match.group(2)) if match.group(2) else start_code
    if start_code > end_code:
        raise ValueError(
            f"Invalid status_code_range '{status_code_range}': "
            f"start code {start_code} > end code {end_code}."
        )
    return cleaned


def sanitize_text_contains(text_contains: Optional[str]) -> Optional[str]:
    """Sanitize literal string for log search and escape quotes / backslashes."""
    if text_contains is None:
        return None
    cleaned = text_contains.strip()
    if not cleaned:
        return None
    # Reject raw filter control characters or multi-line injection
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("text_contains cannot contain newlines.")
    # Escape backslashes and double quotes for GCP log filter literal string
    escaped = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    return escaped


def validate_raw_filter(filter_str: Optional[str]) -> Optional[str]:
    """Validate a raw GCP filter string against injection and dangerous syntax."""
    if filter_str is None:
        return None
    cleaned = filter_str.strip()
    if not cleaned:
        return None
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("Filter string cannot contain newlines.")
    if "\x00" in cleaned:
        raise ValueError("Filter string cannot contain null bytes.")
    if ";" in cleaned:
        raise ValueError("Filter string cannot contain semicolons or chained commands.")
    if "`" in cleaned or "$(" in cleaned:
        raise ValueError("Filter string cannot contain command substitution characters.")
    # Rejection of classic boolean/tautology injection (e.g. OR 1=1, OR "1"="1", OR true)
    if re.search(r"\bOR\s+[\"']?1[\"']?\s*=\s*[\"']?1[\"']?", cleaned, re.IGNORECASE):
        raise ValueError("Filter string contains suspicious boolean injection.")
    if re.search(r"\bOR\s+true\b", cleaned, re.IGNORECASE):
        raise ValueError("Filter string contains suspicious boolean injection.")
    unescaped_double = re.sub(r'\\"', "", cleaned)
    if unescaped_double.count('"') % 2 != 0:
        raise ValueError("Filter string contains unbalanced double quotes.")
    unescaped_single = re.sub(r"\\'", "", cleaned)
    if unescaped_single.count("'") % 2 != 0:
        raise ValueError("Filter string contains unbalanced single quotes.")
    return cleaned


def clamp_lookback_minutes(lookback_minutes: Optional[int]) -> Optional[int]:
    """Clamp lookback_minutes to [1, 60] range."""
    if lookback_minutes is None:
        return None
    if not isinstance(lookback_minutes, int):
        try:
            lookback_minutes = int(lookback_minutes)
        except (ValueError, TypeError):
            raise ValueError(f"lookback_minutes must be an integer, got {lookback_minutes!r}")
    return max(1, min(60, lookback_minutes))


def build_cloud_logging_filter(
    service: Optional[str] = None,
    severity: Optional[str] = None,
    status_code_range: Optional[str] = None,
    text_contains: Optional[str] = None,
    prefix: Optional[str] = None,
) -> str:
    """Build a parameterized GCP Cloud Logging filter from validated arguments."""
    clauses = []

    valid_service = validate_service(service)
    valid_severity = validate_severity(severity)
    valid_status = validate_status_code_range(status_code_range)
    sanitized_text = sanitize_text_contains(text_contains)

    if valid_severity:
        clauses.append(f"severity >= {valid_severity}")

    if valid_service:
        candidates = get_candidate_service_names(valid_service, prefix=prefix)
        svc_subclauses = []
        for cand in candidates:
            svc_subclauses.append(f'resource.labels.service_name = "{cand}"')
            svc_subclauses.append(f'resource.labels.container_name = "{cand}"')
            svc_subclauses.append(f'textPayload =~ "{cand}"')
        svc_subclauses = list(dict.fromkeys(svc_subclauses))
        if len(svc_subclauses) == 1:
            clauses.append(svc_subclauses[0])
        elif svc_subclauses:
            clauses.append(f"({' OR '.join(svc_subclauses)})")

    if valid_status:
        match = STATUS_CODE_PATTERN.match(valid_status)
        if match:
            start_code = int(match.group(1))
            end_code = int(match.group(2)) if match.group(2) else start_code
            if start_code == end_code:
                clauses.append(f"httpRequest.status = {start_code}")
            else:
                clauses.append(
                    f"httpRequest.status >= {start_code} AND httpRequest.status <= {end_code}"
                )

    if sanitized_text:
        clauses.append(f'"{sanitized_text}"')

    return " AND ".join(clauses)


def build_cloud_monitoring_filter(
    service: Optional[str] = None,
    metric_type: str = "run.googleapis.com/request_count",
    status_code_range: Optional[str] = None,
    prefix: Optional[str] = None,
) -> str:
    """Build a parameterized GCP Cloud Monitoring filter from validated arguments."""
    clauses = [f'metric.type = "{metric_type}"']

    valid_service = validate_service(service)
    valid_status = validate_status_code_range(status_code_range)

    if valid_service:
        candidates = get_candidate_service_names(valid_service, prefix=prefix)
        if len(candidates) == 1:
            clauses.append(f'resource.labels.service_name = "{candidates[0]}"')
        elif len(candidates) > 1:
            cand_str = ", ".join(f'"{c}"' for c in candidates)
            clauses.append(f"resource.labels.service_name = one_of({cand_str})")

    if valid_status:
        match = STATUS_CODE_PATTERN.match(valid_status)
        if match:
            start_code = int(match.group(1))
            if start_code >= 500:
                clauses.append('metric.labels.response_code_class = "5xx"')
            elif start_code >= 400:
                clauses.append('metric.labels.response_code_class = "4xx"')
            elif start_code >= 200:
                clauses.append('metric.labels.response_code_class = "2xx"')

    return " AND ".join(clauses)


def get_candidate_service_names(
    service_name: Optional[str], prefix: Optional[str] = None
) -> list[str]:
    """Resolve candidate service names considering deployment naming prefixes.

    E.g. with prefix 'staging':
      - 'frontend-service' -> ['staging-frontend-service', 'frontend-service']
      - 'staging-frontend-service' -> ['staging-frontend-service', 'frontend-service']
    """
    if not service_name:
        return []
    eff_prefix = prefix or os.getenv("NAME_PREFIX")
    names = [service_name]
    if eff_prefix and not service_name.startswith(f"{eff_prefix}-"):
        names.insert(0, f"{eff_prefix}-{service_name}")
    elif eff_prefix and service_name.startswith(f"{eff_prefix}-"):
        names.append(service_name[len(eff_prefix) + 1 :])
    return list(dict.fromkeys(names))
