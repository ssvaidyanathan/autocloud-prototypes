#!/usr/bin/env python3
"""
Jira Cloud Incident and Issue Cleanup Utility

Standalone administration utility to audit (dry-run) and safely delete
issues/incidents from Jira Cloud with cascading subtask deletion.

Security & Operational Rules:
- MOCKS ONLY in automated agent testing; never run against a real Jira site.
- Credentials must never appear in logs, CLI arguments, or error output.
- API token is read via JIRA_API_TOKEN env var or interactive getpass.getpass() prompt.
  No --token, --api-token, or --password flag is accepted to prevent credential leakage
  into shell history or process listings.
- Standalone: stdlib only (Python 3.9+). No Secret Manager or runtime agent dependencies.
- Dry-run by default. Applying deletions requires exact count confirmation or --yes.
- Child subtasks are removed via deleteSubtasks=true query parameter on DELETE.

Atlassian Jira Cloud REST API v3 Documentation Citations:
- Search issues using JQL:
  POST /rest/api/3/search/jql
  Citations:
  https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/#api-rest-api-3-search-jql-post
- Delete issue:
  DELETE /rest/api/3/issue/{issueIdOrKey}?deleteSubtasks=true
  Citations:
  https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-delete
"""

from __future__ import annotations

import argparse
import base64
import csv
import getpass
import io
import json
import logging
import os
import random
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


class SensitiveDataFilter(logging.Filter):
    """Logging filter to mask API tokens, passwords, and HTTP basic auth headers."""

    def __init__(self, secrets_to_mask: Optional[List[str]] = None) -> None:
        super().__init__()
        self.secrets_to_mask = [s for s in (secrets_to_mask or []) if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        for secret in self.secrets_to_mask:
            msg = msg.replace(secret, "********")
        msg = re.sub(r"(Basic\s+)[A-Za-z0-9+/=]+", r"\1********", msg)
        msg = re.sub(
            r"((?:token|password|secret|bearer)['\":\s=]+)[^,;\s&]+",
            r"\1********",
            msg,
            flags=re.IGNORECASE,
        )
        record.msg = msg
        record.args = ()
        return True


def setup_logger(verbose: bool = False, secrets_to_mask: Optional[List[str]] = None) -> logging.Logger:
    """Configure a sanitized logger."""
    logger = logging.getLogger("jira_cleanup")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter("[%(levelname)s] %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    sanitizer = SensitiveDataFilter(secrets_to_mask=secrets_to_mask)
    logger.addFilter(sanitizer)
    for h in logger.handlers:
        h.addFilter(sanitizer)

    return logger


def validate_no_newlines(value: str, field_name: str) -> str:
    """Validate that user-supplied value does not contain newline or carriage return characters."""
    if "\n" in value or "\r" in value:
        raise ValueError(
            f"Invalid character in filter '{field_name}': values must not contain newline or carriage return."
        )
    return value


def escape_jql_string(value: str, field_name: str = "") -> str:
    """
    Escape and quote a string value for safe inclusion in JQL.
    Rejects newlines and carriage returns. Escapes backslashes and double quotes.
    Wraps the resulting string in double quotes so injection attempts remain literals.
    """
    validate_no_newlines(value, field_name)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse_iso_datetime(value: str) -> str:
    """
    Parse ISO date or datetime string and convert to UTC YYYY-MM-DD or YYYY-MM-DD HH:mm format.
    Jira Cloud JQL expects dates in 'YYYY-MM-DD' or 'YYYY-MM-DD HH:mm' format.
    """
    validate_no_newlines(value, "datetime")
    val = value.strip()
    try:
        # Check date-only YYYY-MM-DD
        if len(val) == 10 and val.count("-") == 2:
            dt = datetime.strptime(val, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        # Replace trailing Z with UTC offset
        normalized = val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception as exc:
        raise ValueError(
            f"Invalid date/datetime '{value}': {exc}. Expected ISO format (e.g. 2026-10-01 or 2026-10-01T12:00:00Z)."
        )


def extract_project_and_num_from_key(key: str) -> Optional[Tuple[str, int]]:
    """Extract project key prefix and numeric issue number from an issue key like PROJ-123."""
    parts = key.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and parts[0]:
        return parts[0], int(parts[1])
    return None


def extract_project_from_key(key: str) -> Optional[str]:
    """Extract project key prefix from an issue key like PROJ-123."""
    parsed = extract_project_and_num_from_key(key)
    return parsed[0] if parsed else None


def build_jql_query(
    projects: Optional[List[str]] = None,
    older_than: Optional[int] = None,
    created_before: Optional[str] = None,
    created_after: Optional[str] = None,
    keys: Optional[List[str]] = None,
    key_from: Optional[str] = None,
    key_to: Optional[str] = None,
    issue_type: Optional[str] = None,
    status: Optional[List[str]] = None,
    labels: Optional[List[str]] = None,
    reporter: Optional[str] = None,
    assignee: Optional[str] = None,
    summary_contains: Optional[List[str]] = None,
    raw_jql: Optional[str] = None,
) -> str:
    """
    Construct Jira Cloud JQL query string with safe server-side filter clauses.
    All clauses are combined with logical AND. Strings are safely escaped against JQL injection.
    """
    clauses: List[str] = []

    # 1. Project filter
    if projects:
        for p in projects:
            validate_no_newlines(p, "project")
        cleaned_projects = [p.strip() for p in projects if p.strip()]
        if len(cleaned_projects) == 1:
            clauses.append(f"project = {escape_jql_string(cleaned_projects[0], 'project')}")
        elif len(cleaned_projects) > 1:
            escaped = ", ".join(escape_jql_string(p, "project") for p in cleaned_projects)
            clauses.append(f"project IN ({escaped})")

    # 2. Older than DAYS
    if older_than is not None:
        if older_than < 0:
            raise ValueError(f"Invalid --older-than value '{older_than}': must be a non-negative integer.")
        clauses.append(f'created <= "-{older_than}d"')

    # 3. Created before / after
    if created_before:
        validate_no_newlines(created_before, "created-before")
        formatted_before = parse_iso_datetime(created_before)
        clauses.append(f'created < "{formatted_before}"')

    if created_after:
        validate_no_newlines(created_after, "created-after")
        formatted_after = parse_iso_datetime(created_after)
        clauses.append(f'created > "{formatted_after}"')

    # 4. Explicit keys list
    if keys:
        for k in keys:
            validate_no_newlines(k, "keys")
        cleaned_keys = [k.strip() for k in keys if k.strip()]
        if len(cleaned_keys) == 1:
            clauses.append(f"key = {escape_jql_string(cleaned_keys[0], 'keys')}")
        elif len(cleaned_keys) > 1:
            escaped_keys = ", ".join(escape_jql_string(k, "keys") for k in cleaned_keys)
            clauses.append(f"key IN ({escaped_keys})")

    # 5. Key range (key-from / key-to)
    if key_from or key_to:
        parsed_from = None
        parsed_to = None
        if key_from:
            validate_no_newlines(key_from, "key-from")
            parsed_from = extract_project_and_num_from_key(key_from.strip())
            if not parsed_from:
                raise ValueError(
                    f"Invalid issue key format for --key-from: '{key_from}'. Expected format like 'PROJ-100'."
                )
        if key_to:
            validate_no_newlines(key_to, "key-to")
            parsed_to = extract_project_and_num_from_key(key_to.strip())
            if not parsed_to:
                raise ValueError(
                    f"Invalid issue key format for --key-to: '{key_to}'. Expected format like 'PROJ-200'."
                )

        if parsed_from and parsed_to:
            proj_from, num_from = parsed_from
            proj_to, num_to = parsed_to
            if proj_from != proj_to:
                raise ValueError(
                    f"--key-from ({key_from}) and --key-to ({key_to}) must belong to the same project."
                )
            if num_from > num_to:
                raise ValueError(
                    f"Invalid key range: --key-from ({key_from.strip()}) must be less than or equal to "
                    f"--key-to ({key_to.strip()}) in numeric order."
                )
            clauses.append(
                f"key >= {escape_jql_string(key_from.strip(), 'key-from')} AND "
                f"key <= {escape_jql_string(key_to.strip(), 'key-to')}"
            )
        elif key_from:
            clauses.append(f"key >= {escape_jql_string(key_from.strip(), 'key-from')}")
        elif key_to:
            clauses.append(f"key <= {escape_jql_string(key_to.strip(), 'key-to')}")

    # 6. Issue type
    if issue_type:
        validate_no_newlines(issue_type, "issue-type")
        clauses.append(f"issuetype = {escape_jql_string(issue_type.strip(), 'issue-type')}")

    # 7. Status (repeatable)
    if status:
        for s in status:
            validate_no_newlines(s, "status")
        cleaned_status = [s.strip() for s in status if s.strip()]
        if len(cleaned_status) == 1:
            clauses.append(f"status = {escape_jql_string(cleaned_status[0], 'status')}")
        elif len(cleaned_status) > 1:
            escaped_status = ", ".join(escape_jql_string(s, "status") for s in cleaned_status)
            clauses.append(f"status IN ({escaped_status})")

    # 8. Labels (repeatable)
    if labels:
        for l_item in labels:
            validate_no_newlines(l_item, "labels")
        cleaned_labels = [l_item.strip() for l_item in labels if l_item.strip()]
        if len(cleaned_labels) == 1:
            clauses.append(f"labels = {escape_jql_string(cleaned_labels[0], 'labels')}")
        elif len(cleaned_labels) > 1:
            escaped_labels = ", ".join(escape_jql_string(l, "labels") for l in cleaned_labels)
            clauses.append(f"labels IN ({escaped_labels})")

    # 9. Reporter / Assignee
    if reporter:
        validate_no_newlines(reporter, "reporter")
        clauses.append(f"reporter = {escape_jql_string(reporter.strip(), 'reporter')}")

    if assignee:
        validate_no_newlines(assignee, "assignee")
        clauses.append(f"assignee = {escape_jql_string(assignee.strip(), 'assignee')}")

    # 10. Summary contains (repeatable, ORed within parens)
    if summary_contains:
        for t in summary_contains:
            validate_no_newlines(t, "summary-contains")
        cleaned_terms = [t.strip() for t in summary_contains if t.strip()]
        if len(cleaned_terms) == 1:
            clauses.append(f"summary ~ {escape_jql_string(cleaned_terms[0], 'summary-contains')}")
        elif len(cleaned_terms) > 1:
            contains_clauses = " OR ".join(
                f"summary ~ {escape_jql_string(t, 'summary-contains')}" for t in cleaned_terms
            )
            clauses.append(f"({contains_clauses})")

    # 11. Raw JQL
    if raw_jql:
        validate_no_newlines(raw_jql, "jql")
        cleaned_raw = raw_jql.strip()
        if cleaned_raw:
            clauses.append(f"({cleaned_raw})")

    return " AND ".join(clauses)


class CaseInsensitiveDict(dict):
    """Case-insensitive dictionary for HTTP headers."""

    def __init__(self, data: Optional[Any] = None) -> None:
        super().__init__()
        if data:
            if hasattr(data, "items"):
                for k, v in data.items():
                    self[k] = v
            else:
                for k, v in data:
                    self[k] = v

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key.lower() if isinstance(key, str) else key, value)

    def __getitem__(self, key: Any) -> Any:
        return super().__getitem__(key.lower() if isinstance(key, str) else key)

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key.lower() if isinstance(key, str) else key)

    def get(self, key: Any, default: Any = None) -> Any:
        return super().get(key.lower() if isinstance(key, str) else key, default)


class HTTPResponse:
    """Lightweight HTTP response container mimicking requests.Response."""

    def __init__(self, status_code: int, headers: CaseInsensitiveDict, body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise HTTPStatusError(f"HTTP {self.status_code}: {self.text[:120]}", response=self)


class HTTPStatusError(Exception):
    """Exception raised by raise_for_status() on 4xx or 5xx responses."""

    def __init__(self, message: str, response: Optional[HTTPResponse] = None) -> None:
        super().__init__(message)
        self.response = response


class RequestError(Exception):
    """Exception raised for network or connection failures."""
    pass


# Backward compatibility alias
RequestException = RequestError

SSL_VERIFY_HINT = (
    "TLS certificate verification failed.\n"
    "Hints to resolve CA certificate verification issues:\n"
    "  - On macOS: run 'Install Certificates.command' for your Python version\n"
    "    (e.g., /Applications/Python 3.x/Install Certificates.command)\n"
    "  - Or install certifi: pip install certifi\n"
    "  - Or specify custom CA bundle via --ca-bundle PATH or SSL_CERT_FILE / REQUESTS_CA_BUNDLE"
)


def resolve_ca_bundle(ca_bundle: Optional[str] = None) -> Optional[str]:
    """
    Resolve CA certificate bundle path following precedence:
    1. Explicit CLI flag (--ca-bundle)
    2. Environment variables: SSL_CERT_FILE or REQUESTS_CA_BUNDLE
    3. certifi.where() if certifi is installed
    4. None (system default)
    """
    if ca_bundle and ca_bundle.strip():
        return ca_bundle.strip()

    env_bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if env_bundle and env_bundle.strip():
        return env_bundle.strip()

    try:
        import certifi  # type: ignore[import-untyped]
        return certifi.where()
    except ImportError:
        pass

    return None


def create_ssl_context(ca_bundle: Optional[str] = None) -> ssl.SSLContext:
    """Create an SSLContext with TLS verification strictly enforced."""
    cafile = resolve_ca_bundle(ca_bundle)
    return ssl.create_default_context(cafile=cafile)


def is_cert_verification_error(exc: BaseException) -> bool:
    """Determine whether an exception was caused by SSL/TLS certificate verification failure."""
    curr: Optional[BaseException] = exc
    visited = set()
    while curr is not None and id(curr) not in visited:
        visited.add(id(curr))
        if isinstance(curr, ssl.SSLCertVerificationError):
            return True
        reason = getattr(curr, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return True
        if reason and "CERTIFICATE_VERIFY_FAILED" in str(reason):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(curr):
            return True
        curr = curr.__cause__ or curr.__context__
    return False


def print_ssl_cert_hint(exc: Optional[BaseException] = None) -> None:
    """Print clear troubleshooting hint for TLS certificate verification failure."""
    if exc:
        sys.stderr.write(f"Error: {exc}\n")
    sys.stderr.write(f"{SSL_VERIFY_HINT}\n")


class HttpSession:
    """Lightweight HTTP client built on urllib.request."""

    def __init__(self, ca_bundle: Optional[str] = None) -> None:
        self.headers: Dict[str, str] = {}
        self._auth: Optional[Tuple[str, str]] = None
        self.ca_bundle = ca_bundle

    def set_basic_auth(self, username: str, password: str) -> None:
        self._auth = (username, password)
        auth_bytes = f"{username}:{password}".encode("utf-8")
        self.headers["Authorization"] = f"Basic {base64.b64encode(auth_bytes).decode('ascii')}"

    @property
    def auth(self) -> Optional[Tuple[str, str]]:
        return self._auth

    @auth.setter
    def auth(self, value: Any) -> None:
        if isinstance(value, tuple) and len(value) == 2:
            self.set_basic_auth(value[0], value[1])
        elif hasattr(value, "username") and hasattr(value, "password"):
            self.set_basic_auth(value.username, value.password)
        elif value is None:
            self._auth = None
            self.headers.pop("Authorization", None)

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, str]] = None,
        json_data: Optional[Any] = None,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
        ca_bundle: Optional[str] = None,
    ) -> HTTPResponse:
        req_url = url
        if params:
            query_str = urllib.parse.urlencode(params)
            parsed = urllib.parse.urlsplit(req_url)
            if parsed.query:
                query_str = f"{parsed.query}&{query_str}"
            req_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query_str, parsed.fragment))

        req_headers = dict(self.headers)
        if headers:
            req_headers.update(headers)

        req_body = None
        if json_data is not None:
            req_body = json.dumps(json_data).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif data is not None:
            req_body = data

        req = urllib.request.Request(req_url, data=req_body, headers=req_headers, method=method)

        # TLS verification on by default with standard CA certificates
        bundle_to_use = ca_bundle if ca_bundle is not None else self.ca_bundle
        context = create_ssl_context(bundle_to_use) if urlparse(req_url).scheme.lower() == "https" else None

        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as response:
                status_code = getattr(response, "status", getattr(response, "code", 200))
                resp_headers = CaseInsensitiveDict(dict(response.headers))
                body = response.read()
                return HTTPResponse(status_code=status_code, headers=resp_headers, body=body)
        except urllib.error.HTTPError as exc:
            # Preserve status codes for 4xx and 5xx responses
            status_code = exc.code
            resp_headers = CaseInsensitiveDict(dict(exc.headers))
            body = exc.read()
            return HTTPResponse(status_code=status_code, headers=resp_headers, body=body)
        except urllib.error.URLError as exc:
            raise RequestError(f"Network error: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RequestError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise RequestError(f"Request failed: {exc}") from exc

    def get(self, url: str, params: Optional[Dict[str, str]] = None, timeout: float = 30.0) -> HTTPResponse:
        return self.request("GET", url, params=params, timeout=timeout)

    def post(self, url: str, json: Optional[Any] = None, timeout: float = 30.0) -> HTTPResponse:
        return self.request("POST", url, json_data=json, timeout=timeout)

    def delete(self, url: str, timeout: float = 30.0) -> HTTPResponse:
        return self.request("DELETE", url, timeout=timeout)


def fetch_issues(
    session: HttpSession,
    site_url: str,
    jql: str,
    limit: int = 500,
    page_size: int = 100,
    summary_regex: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch issues from Jira Cloud REST API v3 using POST /rest/api/3/search/jql
    with nextPageToken pagination and client-side summary regex filtering.

    Atlassian endpoint:
    POST /rest/api/3/search/jql
    https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/#api-rest-api-3-search-jql-post
    """
    base_url = site_url.rstrip("/")
    endpoint = f"{base_url}/rest/api/3/search/jql"

    matched_issues: List[Dict[str, Any]] = []
    next_page_token: Optional[str] = None
    regex_compiled = re.compile(summary_regex) if summary_regex else None
    fields = ["key", "summary", "status", "issuetype", "created", "reporter", "project"]

    while True:
        req_limit = min(page_size, limit - len(matched_issues))
        if req_limit <= 0:
            break

        payload: Dict[str, Any] = {
            "jql": jql,
            "maxResults": req_limit,
            "fields": fields,
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        if logger:
            logger.debug(
                "Fetching page from %s (nextPageToken=%s, maxResults=%d)",
                endpoint,
                next_page_token,
                req_limit,
            )

        resp = session.post(endpoint, json=payload, timeout=30)
        resp.raise_for_status()

        data = resp.json()
        issues = data.get("issues", [])
        if not issues or not isinstance(issues, list):
            break

        for issue in issues:
            if regex_compiled:
                summary = (issue.get("fields") or {}).get("summary") or ""
                if not regex_compiled.search(summary):
                    continue

            matched_issues.append(issue)
            if len(matched_issues) >= limit:
                break

        next_page_token = data.get("nextPageToken")
        is_last = data.get("isLast", False)
        if is_last or not next_page_token or len(matched_issues) >= limit:
            break

    return matched_issues[:limit]


def delete_issues(
    session: HttpSession,
    site_url: str,
    issues: List[Dict[str, Any]],
    max_retries: int = 3,
    initial_backoff: float = 1.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Tuple[List[str], List[Dict[str, str]]]:
    """
    Delete issues from Jira Cloud REST API v3 using DELETE /rest/api/3/issue/{key}?deleteSubtasks=true
    with per-issue retry and backoff with jitter, honoring the HTTP 429 Retry-After header.

    Atlassian endpoint:
    DELETE /rest/api/3/issue/{issueIdOrKey}?deleteSubtasks=true
    https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-delete

    Returns (successful_keys, failures_list).
    """
    base_url = site_url.rstrip("/")
    successful: List[str] = []
    failures: List[Dict[str, str]] = []

    for item in issues:
        raw_key = item.get("key")
        key = raw_key.strip() if isinstance(raw_key, str) else ""

        # Safety check: skip records with missing or empty key
        if not key:
            error_msg = "Invalid record: missing or empty issue key; skipping deletion to prevent root endpoint call."
            failures.append({"key": "UNKNOWN", "error": error_msg})
            if logger:
                logger.error("Skipping issue with missing key: %s", error_msg)
            continue

        endpoint = f"{base_url}/rest/api/3/issue/{key}?deleteSubtasks=true"
        attempt = 0
        backoff = initial_backoff
        deleted = False
        last_error = ""

        while attempt <= max_retries:
            try:
                if logger:
                    logger.debug("Deleting issue %s (attempt=%d)", key, attempt + 1)
                resp = session.delete(endpoint, timeout=30)

                # HTTP 204 No Content is standard Jira Cloud response; 200 OK also accepted
                if resp.status_code in (200, 204):
                    deleted = True
                    successful.append(key)
                    if logger:
                        logger.info("Deleted issue %s", key)
                    break

                # HTTP 404 indicates issue does not exist or was already deleted (idempotent)
                if resp.status_code == 404:
                    deleted = True
                    successful.append(key)
                    if logger:
                        logger.warning("Issue %s was already removed (HTTP 404)", key)
                    break

                # HTTP 429 Rate limiting - honor Retry-After header with jitter
                if resp.status_code == 429:
                    retry_after_hdr = getattr(resp, "headers", {}).get("Retry-After")
                    sleep_time = backoff
                    if retry_after_hdr:
                        try:
                            sleep_time = max(0.0, float(retry_after_hdr))
                            if jitter:
                                sleep_time += random.uniform(0.0, 0.5)
                        except (ValueError, TypeError):
                            jitter_amount = random.uniform(0.0, 0.25 * backoff) if jitter else 0.0
                            sleep_time = backoff + jitter_amount
                    else:
                        jitter_amount = random.uniform(0.0, 0.25 * backoff) if jitter else 0.0
                        sleep_time = backoff + jitter_amount
                    last_error = f"HTTP 429 Too Many Requests: {resp.text[:120]}"
                    if attempt < max_retries:
                        if logger:
                            logger.warning(
                                "Rate limited deleting %s: %s. Retrying in %.2fs (with jitter)...",
                                key,
                                last_error,
                                sleep_time,
                            )
                        time.sleep(sleep_time)
                        backoff *= backoff_factor
                        attempt += 1
                        continue
                    break

                # Transient server errors (5xx)
                if resp.status_code in (500, 502, 503, 504):
                    last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
                    if attempt < max_retries:
                        jitter_amount = random.uniform(0.0, 0.25 * backoff) if jitter else 0.0
                        sleep_time = backoff + jitter_amount
                        if logger:
                            logger.warning(
                                "Transient error deleting %s: %s. Retrying in %.2fs (with jitter)...",
                                key,
                                last_error,
                                sleep_time,
                            )
                        time.sleep(sleep_time)
                        backoff *= backoff_factor
                        attempt += 1
                        continue
                    break

                # Non-transient client errors (e.g. 400, 401, 403)
                last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
                break

            except (RequestError, urllib.error.URLError, TimeoutError, OSError) as exc:
                if is_cert_verification_error(exc):
                    if logger:
                        logger.error("TLS certificate verification failed during deletion: %s", exc)
                    print_ssl_cert_hint(exc)
                    failures.append({"key": key, "error": str(exc)})
                    return successful, failures
                last_error = str(exc)
                if attempt < max_retries:
                    jitter_amount = random.uniform(0.0, 0.25 * backoff) if jitter else 0.0
                    sleep_time = backoff + jitter_amount
                    if logger:
                        logger.warning(
                            "Network error deleting %s: %s. Retrying in %.2fs (with jitter)...",
                            key,
                            last_error,
                            sleep_time,
                        )
                    time.sleep(sleep_time)
                    backoff *= backoff_factor
                    attempt += 1
                    continue
                break

        if not deleted:
            failures.append({"key": key, "error": last_error})
            if logger:
                logger.error("Failed to delete issue %s: %s", key, last_error)

    return successful, failures


def render_table(issues: List[Dict[str, Any]]) -> str:
    """Format Jira issues as a clear terminal table."""
    if not issues:
        return "No records to display."

    headers = ["Key", "Project", "Issue Type", "Status", "Created (UTC)", "Summary"]
    rows = []
    for issue in issues:
        key = issue.get("key") or "N/A"
        fields = issue.get("fields") or {}
        proj = (fields.get("project") or {}).get("key") or "N/A"
        itype = (fields.get("issuetype") or {}).get("name") or "N/A"
        status_name = (fields.get("status") or {}).get("name") or "N/A"
        created = fields.get("created") or "N/A"
        if len(created) >= 19:
            created = created[:19].replace("T", " ")
        summary = (fields.get("summary") or "")[:50]
        rows.append([key, proj, itype, status_name, created, summary])

    col_widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(str(cell)))

    separator = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
    header_line = "| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"

    lines = [separator, header_line, separator]
    for row in rows:
        row_line = "| " + " | ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(row)) + " |"
        lines.append(row_line)
    lines.append(separator)

    return "\n".join(lines)


def render_csv(issues: List[Dict[str, Any]]) -> str:
    """Format Jira issues as CSV."""
    output = io.StringIO()
    fieldnames = ["key", "project", "issue_type", "status", "created", "reporter", "summary"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for issue in issues:
        fields = issue.get("fields") or {}
        reporter = fields.get("reporter") or {}
        rep_name = (
            reporter.get("displayName")
            or reporter.get("emailAddress")
            or reporter.get("accountId")
            or ""
        )
        writer.writerow(
            {
                "key": issue.get("key", ""),
                "project": (fields.get("project") or {}).get("key", ""),
                "issue_type": (fields.get("issuetype") or {}).get("name", ""),
                "status": (fields.get("status") or {}).get("name", ""),
                "created": fields.get("created", ""),
                "reporter": rep_name,
                "summary": fields.get("summary", ""),
            }
        )
    return output.getvalue()


def render_json(issues: List[Dict[str, Any]]) -> str:
    """Format Jira issues as formatted JSON."""
    return json.dumps(issues, indent=2)


def resolve_credentials(
    cli_site_url: Optional[str],
    cli_email: Optional[str],
    interactive: bool = True,
) -> Tuple[str, str, str]:
    """
    Resolve Jira site URL, email, and API token securely.
    API token is ALWAYS resolved via JIRA_API_TOKEN env var or interactive getpass.getpass() prompt.
    CLI token flags are strictly rejected.
    """
    # 1. Site URL
    url = cli_site_url or os.environ.get("JIRA_SITE_URL")
    if not url and interactive and sys.stdin.isatty():
        try:
            url = input("Jira Site URL (e.g. https://your-site.atlassian.net): ").strip()
        except EOFError:
            url = None

    if not url:
        raise ValueError("Jira Site URL is required. Provide --site-url or set JIRA_SITE_URL.")

    url = url.strip().rstrip("/")
    if not url.startswith("https://") and not url.startswith("http://"):
        url = f"https://{url}"

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid Jira site URL '{url}'. Must include a valid hostname.")

    # 2. Email
    email = cli_email or os.environ.get("JIRA_EMAIL")
    if not email and interactive and sys.stdin.isatty():
        try:
            email = input("Jira Account Email: ").strip()
        except EOFError:
            email = None

    if not email:
        raise ValueError("Jira Account Email is required. Provide --email or set JIRA_EMAIL.")
    email = email.strip()

    # 3. API Token: read from env or getpass prompt (NEVER from CLI arguments)
    token = os.environ.get("JIRA_API_TOKEN")
    if not token and interactive and sys.stdin.isatty():
        try:
            token = getpass.getpass("Jira API Token: ")
        except EOFError:
            token = None

    if not token:
        raise ValueError(
            "Jira API Token is required. Set JIRA_API_TOKEN env var or enter when prompted interactively. "
            "--token/--api-token CLI flags are deliberately not supported to prevent secret exposure in shell history."
        )

    return url, email, token


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser with comprehensive filtering options and examples."""
    parser = argparse.ArgumentParser(
        prog="jira-cleanup.py",
        description="Jira Cloud Incident/Issue Cleanup Utility: Safely query (dry-run) and purge issues.",
        epilog="""
Examples:
  1. Default dry-run for project issues older than 14 days:
     python jira-cleanup.py --site-url https://myteam.atlassian.net --email user@example.com --project PROJ --older-than 14

  2. Specific issue keys dry-run output as CSV:
     python jira-cleanup.py --project PROJ --keys PROJ-101,PROJ-102 --output csv

  3. Key range within same project:
     python jira-cleanup.py --key-from PROJ-100 --key-to PROJ-150

  4. Interactive apply deletion (prompts to type exact issue count to confirm):
     python jira-cleanup.py --project PROJ --status Done,Closed --older-than 30 --apply

  5. Non-interactive apply deletion with --yes:
     python jira-cleanup.py --project PROJ --labels alert-triage --apply --yes

Note:
  Child subtasks are deleted automatically via deleteSubtasks=true parameter.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Core Target & Scope
    parser.add_argument(
        "--site-url",
        default=None,
        help="Jira Cloud site URL (e.g. https://<site>.atlassian.net or set JIRA_SITE_URL).",
    )
    parser.add_argument(
        "--email",
        default=None,
        help="Jira account email address (or set JIRA_EMAIL).",
    )
    parser.add_argument(
        "--ca-bundle",
        default=None,
        metavar="PATH",
        help="Path to custom CA certificate bundle file (or set SSL_CERT_FILE / REQUESTS_CA_BUNDLE).",
    )

    # Filtering Options (ANDed, at least one required)
    filter_group = parser.add_argument_group(
        "Filter Options (ANDed, at least one required; --project strongly recommended)"
    )
    filter_group.add_argument(
        "--project",
        action="append",
        default=None,
        metavar="KEY",
        help="Project key (repeatable or comma-separated; strongly recommended).",
    )
    filter_group.add_argument(
        "--older-than",
        type=int,
        default=None,
        metavar="DAYS",
        help="Filter issues created more than DAYS days ago (e.g. 14).",
    )
    filter_group.add_argument(
        "--created-before",
        default=None,
        metavar="ISO_TIMESTAMP",
        help="Filter issues created before ISO date/datetime (UTC, e.g. '2026-10-01' or '2026-10-01T12:00:00Z').",
    )
    filter_group.add_argument(
        "--created-after",
        default=None,
        metavar="ISO_TIMESTAMP",
        help="Filter issues created after ISO date/datetime (UTC, e.g. '2026-10-01').",
    )
    filter_group.add_argument(
        "--keys",
        default=None,
        metavar="KEY1,KEY2,...",
        help="Comma-separated or list of specific issue keys.",
    )
    filter_group.add_argument(
        "--key-from",
        default=None,
        metavar="KEY_START",
        help="Start of issue key range in the same project (e.g. 'PROJ-100').",
    )
    filter_group.add_argument(
        "--key-to",
        default=None,
        metavar="KEY_END",
        help="End of issue key range in the same project (e.g. 'PROJ-200').",
    )
    filter_group.add_argument(
        "--issue-type",
        default=None,
        metavar="TYPE",
        help="Filter by issue type name (e.g. 'Incident', 'Bug', 'Task').",
    )
    filter_group.add_argument(
        "--status",
        action="append",
        default=None,
        metavar="STATUS",
        help="Filter by status name (repeatable or comma-separated, e.g. 'Done,Closed').",
    )
    filter_group.add_argument(
        "--labels",
        action="append",
        default=None,
        metavar="LABEL",
        help="Filter by label (repeatable or comma-separated, e.g. 'alert-triage').",
    )
    filter_group.add_argument(
        "--reporter",
        default=None,
        metavar="USER",
        help="Filter by reporter account ID, email, or username.",
    )
    filter_group.add_argument(
        "--assignee",
        default=None,
        metavar="USER",
        help="Filter by assignee account ID, email, or username.",
    )
    filter_group.add_argument(
        "--summary-contains",
        action="append",
        default=None,
        metavar="TEXT",
        help="Filter issues whose summary contains TEXT (repeatable; matches any).",
    )
    filter_group.add_argument(
        "--summary-regex",
        default=None,
        metavar="PATTERN",
        help="Client-side regular expression to filter summary.",
    )
    filter_group.add_argument(
        "--jql",
        default=None,
        metavar="RAW_JQL",
        help="Raw JQL clause to combine with other filters using AND.",
    )

    # Execution Controls
    exec_group = parser.add_argument_group("Execution Controls")
    exec_group.add_argument(
        "--apply",
        action="store_true",
        help="Execute deletion (default is dry-run mode).",
    )
    exec_group.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip interactive confirmation when --apply is set (echoes targeted count).",
    )
    exec_group.add_argument(
        "--limit",
        type=int,
        default=500,
        metavar="N",
        help="Maximum number of issues to process (default 500, ceiling capped by --max-limit).",
    )
    exec_group.add_argument(
        "--max-limit",
        type=int,
        default=10000,
        metavar="MAX_N",
        help="Configurable safety ceiling for --limit (default 10000).",
    )
    exec_group.add_argument(
        "--output",
        choices=["table", "csv", "json"],
        default="table",
        help="Dry-run output format (default 'table').",
    )
    exec_group.add_argument(
        "--page-size",
        type=int,
        default=100,
        metavar="P",
        help="Batch size for search requests (default 100).",
    )
    exec_group.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging (credentials remain masked).",
    )

    return parser


def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments, rejecting any forbidden secret flags."""
    raw_args = argv if argv is not None else sys.argv[1:]

    # Proactively check and reject forbidden token/password flags
    for arg in raw_args:
        for forbidden in ("--token", "--api-token", "--password"):
            if arg == forbidden or arg.startswith(f"{forbidden}="):
                raise SystemExit(
                    f"Error: {forbidden} flag is strictly forbidden to prevent credential exposure in shell history.\n"
                    "Please use the JIRA_API_TOKEN environment variable or enter it when prompted interactively."
                )

    parser = build_arg_parser()
    args = parser.parse_args(raw_args)

    # Safety limits
    if args.limit < 1:
        raise SystemExit("Error: --limit must be a positive integer.")
    if args.limit > args.max_limit:
        raise SystemExit(
            f"Error: Requested --limit {args.limit} exceeds safety maximum ceiling of {args.max_limit}."
        )

    # Flatten comma-separated lists for repeated arguments
    if args.project:
        flattened_proj: List[str] = []
        for p in args.project:
            flattened_proj.extend([part.strip() for part in p.split(",") if part.strip()])
        args.project = flattened_proj

    if args.keys:
        args.keys = [k.strip() for k in args.keys.split(",") if k.strip()]

    if args.status:
        flattened_status: List[str] = []
        for s in args.status:
            flattened_status.extend([part.strip() for part in s.split(",") if part.strip()])
        args.status = flattened_status

    if args.labels:
        flattened_labels: List[str] = []
        for l_item in args.labels:
            flattened_labels.extend([part.strip() for part in l_item.split(",") if part.strip()])
        args.labels = flattened_labels

    # Check whether any explicit filter was provided
    explicit_filters_present = any(
        [
            bool(args.project),
            args.older_than is not None,
            args.created_before is not None,
            args.created_after is not None,
            bool(args.keys),
            args.key_from is not None,
            args.key_to is not None,
            args.issue_type is not None,
            bool(args.status),
            bool(args.labels),
            args.reporter is not None,
            args.assignee is not None,
            bool(args.summary_contains),
            args.summary_regex is not None,
            args.jql is not None,
        ]
    )

    if not explicit_filters_present:
        raise SystemExit(
            "Error: At least one filter must be specified to prevent accidental mass deletion.\n"
            "Available filters include: --project, --older-than, --created-before, --created-after, "
            "--keys, --key-from/--key-to, --issue-type, --status, --labels, --reporter, --assignee, "
            "--summary-contains, --summary-regex, or --jql."
        )

    return args


def _run_jira_cleanup(argv: Optional[List[str]] = None) -> int:
    """Internal execution flow for Jira cleanup."""
    args = parse_arguments(argv)

    if args.ca_bundle and not os.path.isfile(args.ca_bundle):
        sys.stderr.write(f"Configuration Error: CA bundle file not found: {args.ca_bundle}\n")
        return 1

    # Credential Resolution
    try:
        site_url, email, token = resolve_credentials(
            cli_site_url=args.site_url,
            cli_email=args.email,
        )
    except Exception as exc:
        sys.stderr.write(f"Configuration Error: {exc}\n")
        return 1

    logger = setup_logger(verbose=args.verbose, secrets_to_mask=[token])

    # Warn loudly if --project is omitted
    if not args.project:
        warning_msg = (
            "\n"
            "********************************************************************************\n"
            "WARNING: No --project filter specified!\n"
            "This query will search and potentially delete issues across ALL accessible projects\n"
            "on this Jira site. Specifying --project KEY is strongly recommended.\n"
            "********************************************************************************\n"
        )
        sys.stderr.write(warning_msg)
        logger.warning("No --project filter specified: query spans all accessible projects.")

    # Construct server-side JQL query
    try:
        jql_query = build_jql_query(
            projects=args.project,
            older_than=args.older_than,
            created_before=args.created_before,
            created_after=args.created_after,
            keys=args.keys,
            key_from=args.key_from,
            key_to=args.key_to,
            issue_type=args.issue_type,
            status=args.status,
            labels=args.labels,
            reporter=args.reporter,
            assignee=args.assignee,
            summary_contains=args.summary_contains,
            raw_jql=args.jql,
        )
    except Exception as exc:
        logger.error("Failed to construct JQL query: %s", exc)
        return 1

    logger.debug("Constructed Jira JQL query: %s", jql_query)

    # Initialize authenticated HTTP session
    session = HttpSession(ca_bundle=args.ca_bundle)
    session.set_basic_auth(email, token)
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Jira-Cloud-Cleanup-Utility/1.0",
        }
    )

    # Query matching issues
    try:
        logger.info("Querying Jira Cloud issues on %s...", site_url)
        issues = fetch_issues(
            session=session,
            site_url=site_url,
            jql=jql_query,
            limit=args.limit,
            page_size=args.page_size,
            summary_regex=args.summary_regex,
            logger=logger,
        )
    except Exception as exc:
        if is_cert_verification_error(exc):
            print_ssl_cert_hint(exc)
            return 1
        logger.error("Error communicating with Jira Cloud REST API: %s", exc)
        return 1

    total_count = len(issues)

    # Dry-Run Mode (Default)
    if not args.apply:
        print(f"\n[DRY-RUN MODE] Found {total_count} matching issue(s). Zero deletions executed.")
        print("Note: Child subtasks are removed via deleteSubtasks=true on apply.\n")

        if total_count == 0:
            print("No matching issues found.")
            return 0

        if args.output == "json":
            print(render_json(issues))
        elif args.output == "csv":
            print(render_csv(issues))
        else:
            print(render_table(issues))

        print(f"\nTotal matching issues: {total_count}")
        print("To permanently delete these issues, re-run with --apply.")
        return 0

    # Apply Mode
    if total_count == 0:
        print("\nNo matching issues found. Nothing to delete.")
        return 0

    print(f"\n[APPLY MODE] {total_count} issue(s) matched for permanent deletion.")
    if args.output == "json":
        print(render_json(issues))
    elif args.output == "csv":
        print(render_csv(issues))
    else:
        print(render_table(issues))

    # Confirmation
    if not args.yes:
        try:
            prompt_msg = f"\nType '{total_count}' to confirm permanent deletion of {total_count} issues: "
            confirmation = input(prompt_msg).strip()
        except EOFError:
            print("\nAborted: No confirmation input received.")
            return 1

        if confirmation != str(total_count):
            print(f"\nAborted: Confirmation '{confirmation}' did not match issue count '{total_count}'.")
            print("Zero issues were deleted.")
            return 1
    else:
        print(f"\nConfirming deletion of {total_count} issues (--yes specified).")

    # Execute Deletions
    logger.info("Executing deletion of %d issues...", total_count)
    successful, failures = delete_issues(
        session=session,
        site_url=site_url,
        issues=issues,
        logger=logger,
    )

    print("\n" + "=" * 50)
    print("Jira Cloud Issue Cleanup Summary")
    print("=" * 50)
    print(f"Total targeted:       {total_count}")
    print(f"Successfully deleted: {len(successful)}")
    print(f"Failed deletions:     {len(failures)}")
    print("Note: Child subtasks were removed via deleteSubtasks=true.")

    if failures:
        print("\nFailures:")
        for fail in failures:
            print(f"  - {fail['key']}: {fail['error']}")
        return 1

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI execution flow."""
    try:
        return _run_jira_cleanup(argv)
    except Exception as exc:
        if is_cert_verification_error(exc):
            print_ssl_cert_hint(exc)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
