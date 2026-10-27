"""Logs specialist agent investigating application and system logs."""

import re
from typing import Any, Optional
from urllib.parse import urlparse
from .base import BaseSpecialist, EvidenceDossier
from ..config import AgentConfig
from ..contracts.alert import AlertEvent

EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
IP_PATTERN = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')


def sanitize_log_text(text: str) -> str:
    """Sanitize text by removing newlines and redacting emails and IP addresses."""
    if not text:
        return ""
    cleaned = text.replace("\r", " ").replace("\n", " ").strip()
    cleaned = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", cleaned)
    cleaned = IP_PATTERN.sub("[REDACTED_IP]", cleaned)
    return re.sub(r'\s+', ' ', cleaned)


def is_audit_log(entry: Any) -> bool:
    """Detect if a log entry originates from Cloud Audit Logs."""
    log_name = getattr(entry, "log_name", "") or ""
    if "cloudaudit.googleapis.com" in log_name or "audit" in log_name.lower():
        return True

    payload = getattr(entry, "payload", None)
    if isinstance(payload, dict):
        type_str = str(payload.get("@type", ""))
        if "AuditLog" in type_str:
            return True
        if "authenticationInfo" in payload or "requestMetadata" in payload:
            return True
        if "serviceName" in payload and "methodName" in payload:
            return True
    elif isinstance(payload, str) and ("AuditLog" in payload or "cloudaudit" in payload):
        return True
    return False


def is_application_or_request_log(entry: Any) -> bool:
    """Check whether entry comes strictly from application or request logs."""
    if is_audit_log(entry):
        return False
    log_name = getattr(entry, "log_name", "") or ""
    if "requests" in log_name or "stdout" in log_name or "stderr" in log_name:
        return True
    if hasattr(entry, "http_request") and entry.http_request:
        return True
    return False


# Level prefix written by Python logging / uvicorn ("INFO:     ...", "ERROR: ...").
EMBEDDED_LEVEL_PATTERN = re.compile(r'^\s*(DEBUG|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL)\s*:\s*', re.IGNORECASE)
# Access-log request and status ('"GET /items HTTP/1.1" 200 OK').
ACCESS_LINE_PATTERN = re.compile(r'"([A-Z]+) (\S+) HTTP/[0-9.]+"\s+(\d{3})\b')

ERROR_SEVERITIES = ("ERROR", "CRITICAL", "ALERT", "EMERGENCY")


def parse_embedded_level(text: str) -> Optional[str]:
    """Return the log level a text line declares itself (e.g. 'INFO'), or None."""
    m = EMBEDDED_LEVEL_PATTERN.match(text or "")
    if not m:
        return None
    level = m.group(1).upper()
    return "WARNING" if level == "WARN" else level


def parse_access_log_line(text: str) -> Optional[tuple[str, str, int]]:
    """Return (method, path, status) from an access-log text line, or None."""
    m = ACCESS_LINE_PATTERN.search(text or "")
    if not m:
        return None
    path = m.group(2)
    try:
        path = urlparse(path).path or path
    except Exception:
        pass
    return m.group(1), path, int(m.group(3))


def format_single_line_log_excerpt(entry: Any) -> Optional[str]:
    """Format log entry as ONE clean line: timestamp, severity, status, path/message <= 200 chars.

    Returns None if entry is an audit log, does not meet severity/status threshold,
    or contains no relevant message.
    """
    if is_audit_log(entry):
        return None
    if not is_application_or_request_log(entry):
        return None

    # Severity check
    severity = getattr(entry, "severity", None) or "ERROR"
    sev_upper = str(severity).upper()

    status_code = None
    req_path = ""
    msg = ""

    if hasattr(entry, "http_request") and entry.http_request:
        status_code = entry.http_request.get("status")
        raw_url = entry.http_request.get("requestUrl", "")
        if raw_url:
            try:
                parsed = urlparse(raw_url)
                req_path = parsed.path or raw_url
            except Exception:
                req_path = raw_url

    payload = getattr(entry, "payload", None)
    if isinstance(payload, dict):
        msg = payload.get("message") or payload.get("error") or ""
        if not req_path:
            req_path = payload.get("path") or payload.get("requestPath") or ""
    elif isinstance(payload, str):
        msg = payload

    # Cloud Run stores everything written to stderr as ERROR, including uvicorn's INFO access
    # lines. The level and status a line states about itself win over the entry severity.
    embedded_level = parse_embedded_level(msg) if isinstance(msg, str) else None
    if embedded_level:
        msg = EMBEDDED_LEVEL_PATTERN.sub("", msg, count=1)
    if status_code is None and isinstance(msg, str):
        access = parse_access_log_line(msg)
        if access:
            _, access_path, status_code = access
            if not req_path:
                req_path = access_path

    status_num: Optional[int] = None
    try:
        if status_code is not None:
            status_num = int(status_code)
    except (ValueError, TypeError):
        status_num = None

    if status_num is not None:
        # A request line is an error only when it failed on the server side. This also drops
        # request entries with severity ERROR but a 2xx/4xx status (deliberate: client errors
        # and successes are not error evidence).
        if status_num < 500:
            return None
        sev_upper = "ERROR"
    elif embedded_level:
        if embedded_level not in ERROR_SEVERITIES:
            return None
        sev_upper = embedded_level
    elif sev_upper not in ERROR_SEVERITIES:
        return None
    is_status_500 = status_num is not None

    # Timestamp formatting
    ts_str = ""
    ts = getattr(entry, "timestamp", None)
    if ts:
        if hasattr(ts, "strftime"):
            ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            ts_str = str(ts)[:19] + "Z"

    clean_path = sanitize_log_text(req_path)
    clean_msg = sanitize_log_text(msg)
    status_str = f"HTTP {status_num}" if is_status_500 else ""

    parts = []
    if ts_str:
        parts.append(f"[{ts_str}]")
    parts.append(sev_upper)
    if status_str:
        parts.append(status_str)
    if clean_path:
        parts.append(clean_path)
    if clean_msg:
        parts.append(f"- {clean_msg}")

    line = " ".join(parts).strip()
    if len(line) > 200:
        line = line[:197] + "..."
    return line


class LogsSpecialist(BaseSpecialist):
    """Specialist analyzing error traces and log anomalies."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("LogsSpecialist")

    async def investigate(
        self,
        alert: AlertEvent,
        config: AgentConfig,
    ) -> EvidenceDossier:
        from .cloud_run import CloudRunSpecialist

        return await CloudRunSpecialist().investigate(alert, config)


__all__ = [
    "LogsSpecialist",
    "is_audit_log",
    "is_application_or_request_log",
    "sanitize_log_text",
    "format_single_line_log_excerpt",
    "parse_access_log_line",
    "parse_embedded_level",
]
