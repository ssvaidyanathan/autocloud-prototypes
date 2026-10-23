#!/usr/bin/env python3
"""
ServiceNow Incident Cleanup Utility

Standalone administration utility to audit (dry-run) and safely delete
incident records from ServiceNow with cascading attachment cleanup.

Security & Operational Rules:
- MOCKS ONLY in automated agent testing; never run against production credentials.
- Credentials must never appear in logs, CLI arguments, or error output.
- Password is read via SNOW_PASSWORD env var or interactive getpass.getpass() prompt.
  No --password flag is accepted to prevent credential leakage into shell history.
- Standalone: stdlib + requests only. No Secret Manager or runtime agent dependencies.
- Touches ONLY the 'incident' table.
- Dry-run by default. Applying deletions requires exact count confirmation or --yes.
- Child attachments (sys_attachment) and journal records are removed via ServiceNow
  database cascade deletion upon incident record removal.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.auth import HTTPBasicAuth

# Standard ServiceNow Incident State Mapping
STATE_MAP: Dict[str, str] = {
    "new": "1",
    "in_progress": "2",
    "on_hold": "3",
    "resolved": "6",
    "closed": "7",
    "canceled": "8",
    "cancelled": "8",
}


class SensitiveDataFilter(logging.Filter):
    """Logging filter to mask passwords, tokens, and basic auth headers."""

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
        msg = re.sub(r"(password['\":\s=]+)[^,;\s&]+", r"\1********", msg, flags=re.IGNORECASE)
        record.msg = msg
        record.args = ()
        return True


def setup_logger(verbose: bool = False, secrets_to_mask: Optional[List[str]] = None) -> logging.Logger:
    """Configure a sanitized logger."""
    logger = logging.getLogger("servicenow_cleanup")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter("[%(levelname)s] %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    sanitizer = SensitiveDataFilter(secrets_to_mask=secrets_to_mask)
    logger.addFilter(sanitizer)
    for h in logger.handlers:
        h.addFilter(sanitizer)

    return logger


def parse_iso_datetime(value: str) -> str:
    """Parse ISO date or datetime string and convert to UTC YYYY-MM-DD HH:MM:SS format."""
    val = value.strip()
    try:
        # Check date-only YYYY-MM-DD
        if len(val) == 10 and val.count("-") == 2:
            dt = datetime.strptime(val, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d 00:00:00")
        # Replace trailing Z with UTC offset
        normalized = val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as exc:
        raise ValueError(f"Invalid date/datetime '{value}': {exc}. Expected ISO format (e.g. 2026-10-01 or 2026-10-01T12:00:00Z).")


def validate_no_injection(value: str, field_name: str) -> str:
    """Validate that user-supplied filter value does not contain caret (^) or newline characters."""
    if any(char in value for char in ("^", "\n", "\r")):
        raise ValueError(
            f"Invalid character in filter '{field_name}': values must not contain '^', newline, or carriage return."
        )
    return value


def build_encoded_query(
    older_than: Optional[int] = None,
    created_before: Optional[str] = None,
    created_after: Optional[str] = None,
    number_from: Optional[str] = None,
    number_to: Optional[str] = None,
    numbers: Optional[List[str]] = None,
    caller: Optional[str] = None,
    short_description_contains: Optional[List[str]] = None,
    state: Optional[List[str]] = None,
    priority: Optional[List[str]] = None,
    assignment_group: Optional[str] = None,
    category: Optional[str] = None,
    created_by: Optional[str] = None,
    raw_query: Optional[str] = None,
) -> str:
    """
    Construct ServiceNow encoded query string with server-side filter clauses.
    All clauses are combined with AND (^), except short_description_contains which
    uses OR (^OR) across its entries.
    """
    clauses: List[str] = []

    if older_than is not None:
        clauses.append(f"sys_created_on<javascript:gs.daysAgoStart({older_than})")

    if created_before:
        validate_no_injection(created_before, "created-before")
        formatted = parse_iso_datetime(created_before)
        clauses.append(f"sys_created_on<{formatted}")

    if created_after:
        validate_no_injection(created_after, "created-after")
        formatted = parse_iso_datetime(created_after)
        clauses.append(f"sys_created_on>{formatted}")

    if number_from and number_to:
        num_from = validate_no_injection(number_from, "number-from").strip()
        num_to = validate_no_injection(number_to, "number-to").strip()
        clauses.append(f"number>={num_from}^number<={num_to}")
    elif number_from:
        num_from = validate_no_injection(number_from, "number-from").strip()
        clauses.append(f"number>={num_from}")
    elif number_to:
        num_to = validate_no_injection(number_to, "number-to").strip()
        clauses.append(f"number<={num_to}")

    if numbers:
        cleaned_numbers = [validate_no_injection(n, "numbers").strip() for n in numbers if n.strip()]
        if cleaned_numbers:
            clauses.append(f"numberIN{','.join(cleaned_numbers)}")

    if caller:
        val = validate_no_injection(caller, "caller").strip()
        if len(val) == 32 and all(c in "0123456789abcdefABCDEF" for c in val):
            clauses.append(f"caller_id={val}")
        else:
            clauses.append(f"caller_id.user_name={val}^ORcaller_id.name={val}")

    if short_description_contains:
        terms = [validate_no_injection(t, "short-description-contains").strip() for t in short_description_contains if t.strip()]
        if terms:
            contains_clause = "^OR".join(f"short_descriptionLIKE{t}" for t in terms)
            clauses.append(contains_clause)

    if state:
        codes = []
        for s in state:
            s_val = validate_no_injection(s, "state").strip()
            s_clean = s_val.lower().replace(" ", "_").replace("-", "_")
            if s_clean in STATE_MAP:
                codes.append(STATE_MAP[s_clean])
            elif s_clean.isdigit():
                codes.append(s_clean)
            else:
                raise ValueError(
                    f"Invalid incident state '{s}'. Valid states: {', '.join(STATE_MAP.keys())} or numeric codes."
                )
        if codes:
            clauses.append(f"stateIN{','.join(codes)}")

    if priority:
        prios = []
        for p in priority:
            p_val = validate_no_injection(p, "priority").strip()
            if p_val in {"1", "2", "3", "4", "5"}:
                prios.append(p_val)
            else:
                raise ValueError(f"Invalid incident priority '{p}'. Valid priorities are 1 through 5.")
        if prios:
            clauses.append(f"priorityIN{','.join(prios)}")

    if assignment_group:
        grp = validate_no_injection(assignment_group, "assignment-group").strip()
        if len(grp) == 32 and all(c in "0123456789abcdefABCDEF" for c in grp):
            clauses.append(f"assignment_group={grp}")
        else:
            clauses.append(f"assignment_group.name={grp}")

    if category:
        cat = validate_no_injection(category, "category").strip()
        clauses.append(f"category={cat}")

    if created_by:
        usr = validate_no_injection(created_by, "created-by").strip()
        clauses.append(f"sys_created_by={usr}")

    if raw_query:
        if "\n" in raw_query or "\r" in raw_query:
            raise ValueError("Invalid character in filter 'query': raw query must not contain newline or carriage return.")
        cleaned_raw = raw_query.strip().strip("^")
        if cleaned_raw:
            clauses.append(cleaned_raw)

    return "^".join(clauses)


def fetch_incidents(
    session: requests.Session,
    instance_url: str,
    query: str,
    limit: int = 500,
    page_size: int = 100,
    short_description_regex: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch incident records from ServiceNow Table API with paging and client-side regex filtering.
    """
    base_url = instance_url.rstrip("/")
    endpoint = f"{base_url}/api/now/table/incident"

    matched_incidents: List[Dict[str, Any]] = []
    offset = 0
    regex_compiled = re.compile(short_description_regex) if short_description_regex else None

    fields = "sys_id,number,short_description,sys_created_on,state,priority,sys_created_by"

    while True:
        req_limit = min(page_size, limit - len(matched_incidents))
        if req_limit <= 0:
            break

        params = {
            "sysparm_query": query,
            "sysparm_limit": str(req_limit),
            "sysparm_offset": str(offset),
            "sysparm_fields": fields,
            "sysparm_display_value": "false",
        }

        if logger:
            logger.debug("Fetching page from %s (offset=%d, limit=%d)", endpoint, offset, req_limit)

        resp = session.get(endpoint, params=params, timeout=30)
        resp.raise_for_status()

        data = resp.json()
        results = data.get("result", [])
        if not results or not isinstance(results, list):
            break

        for record in results:
            if regex_compiled:
                desc = record.get("short_description") or ""
                if not regex_compiled.search(desc):
                    continue

            matched_incidents.append(record)
            if len(matched_incidents) >= limit:
                break

        if len(results) < req_limit or len(matched_incidents) >= limit:
            break

        offset += len(results)

    return matched_incidents[:limit]


def delete_incidents(
    session: requests.Session,
    instance_url: str,
    incidents: List[Dict[str, Any]],
    max_retries: int = 3,
    initial_backoff: float = 1.0,
    backoff_factor: float = 2.0,
    logger: Optional[logging.Logger] = None,
) -> Tuple[List[str], List[Dict[str, str]]]:
    """
    Delete incident records via DELETE /api/now/table/incident/{sys_id} with retry and exponential backoff.
    Returns (successful_numbers, failures_list).
    """
    base_url = instance_url.rstrip("/")
    successful: List[str] = []
    failures: List[Dict[str, str]] = []

    for item in incidents:
        sys_id = (item.get("sys_id") or "").strip()
        number = item.get("number") or sys_id or "UNKNOWN"

        if not sys_id:
            error_msg = "Invalid record: missing or empty sys_id; skipping deletion to prevent table-level operation."
            failures.append({"number": number, "sys_id": "", "error": error_msg})
            if logger:
                logger.error("Skipping incident %s: %s", number, error_msg)
            continue

        endpoint = f"{base_url}/api/now/table/incident/{sys_id}"

        attempt = 0
        backoff = initial_backoff
        deleted = False
        last_error = ""

        while attempt <= max_retries:
            try:
                if logger:
                    logger.debug("Deleting incident %s (sys_id=%s, attempt=%d)", number, sys_id, attempt + 1)
                resp = session.delete(endpoint, timeout=30)

                # HTTP 204 No Content is standard ServiceNow Table API response; 200 OK also accepted
                if resp.status_code in (200, 204):
                    deleted = True
                    successful.append(number)
                    if logger:
                        logger.info("Deleted incident %s (%s)", number, sys_id)
                    break

                # 404 indicates record does not exist or was already removed
                if resp.status_code == 404:
                    deleted = True
                    successful.append(number)
                    if logger:
                        logger.warning("Incident %s (%s) was already removed (HTTP 404)", number, sys_id)
                    break

                # Retry on rate limiting (429) or server errors (5xx)
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
                    if attempt < max_retries:
                        if logger:
                            logger.warning(
                                "Transient error deleting %s: %s. Retrying in %.2fs...", number, last_error, backoff
                            )
                        time.sleep(backoff)
                        backoff *= backoff_factor
                        attempt += 1
                        continue

                last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
                break

            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < max_retries:
                    if logger:
                        logger.warning("Request failed deleting %s: %s. Retrying in %.2fs...", number, last_error, backoff)
                    time.sleep(backoff)
                    backoff *= backoff_factor
                    attempt += 1
                    continue
                break

        if not deleted:
            failures.append({"number": number, "sys_id": sys_id, "error": last_error})
            if logger:
                logger.error("Failed to delete incident %s (%s): %s", number, sys_id, last_error)

    return successful, failures


def render_table(incidents: List[Dict[str, Any]]) -> str:
    """Format incident records as a clear terminal table."""
    if not incidents:
        return "No records to display."

    headers = ["Number", "Created On (UTC)", "State", "Priority", "Short Description"]
    rows = []
    for inc in incidents:
        num = inc.get("number", "N/A")
        created = inc.get("sys_created_on", "N/A")
        state_code = str(inc.get("state", "N/A"))
        state_label = {v: k for k, v in STATE_MAP.items()}.get(state_code, state_code)
        prio = str(inc.get("priority", "N/A"))
        desc = (inc.get("short_description") or "")[:50]
        rows.append([num, created, state_label, prio, desc])

    col_widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(cell))

    separator = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
    header_line = "| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"

    lines = [separator, header_line, separator]
    for row in rows:
        row_line = "| " + " | ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(row)) + " |"
        lines.append(row_line)
    lines.append(separator)

    return "\n".join(lines)


def render_csv(incidents: List[Dict[str, Any]]) -> str:
    """Format incident records as CSV."""
    output = io.StringIO()
    fieldnames = ["sys_id", "number", "short_description", "sys_created_on", "state", "priority", "sys_created_by"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for inc in incidents:
        writer.writerow(inc)
    return output.getvalue()


def render_json(incidents: List[Dict[str, Any]]) -> str:
    """Format incident records as formatted JSON."""
    return json.dumps(incidents, indent=2)


def resolve_credentials(
    cli_url: Optional[str],
    cli_username: Optional[str],
    interactive: bool = True,
) -> Tuple[str, str, str]:
    """
    Resolve instance URL, username, and password securely.
    Password is ALWAYS resolved via env var or interactive getpass.getpass() prompt.
    """
    # 1. Instance URL
    url = cli_url or os.environ.get("SNOW_INSTANCE_URL") or os.environ.get("SERVICENOW_INSTANCE_URL")
    if not url and interactive and sys.stdin.isatty():
        try:
            url = input("ServiceNow Instance URL (e.g. https://dev12345.service-now.com): ").strip()
        except EOFError:
            url = None

    if not url:
        raise ValueError("ServiceNow Instance URL is required. Provide --instance-url or set SNOW_INSTANCE_URL.")

    url = url.strip()
    if not url.startswith("https://") and not url.startswith("http://"):
        url = f"https://{url}"

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid ServiceNow instance URL '{url}'. Must include a valid hostname.")

    # 2. Username
    username = cli_username or os.environ.get("SNOW_USERNAME") or os.environ.get("SERVICENOW_USERNAME")
    if not username and interactive and sys.stdin.isatty():
        try:
            username = input("ServiceNow Username: ").strip()
        except EOFError:
            username = None

    if not username:
        raise ValueError("ServiceNow Username is required. Provide --username or set SNOW_USERNAME.")
    username = username.strip()

    # 3. Password: read from env or getpass prompt (NEVER from CLI arguments)
    password = os.environ.get("SNOW_PASSWORD") or os.environ.get("SERVICENOW_PASSWORD")
    if not password and interactive and sys.stdin.isatty():
        try:
            password = getpass.getpass("ServiceNow Password: ")
        except EOFError:
            password = None

    if not password:
        raise ValueError(
            "ServiceNow Password is required. Set SNOW_PASSWORD env var or enter when prompted interactively. "
            "--password CLI flag is deliberately not supported to prevent secret exposure in shell history."
        )

    return url, username, password


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser with comprehensive filtering options and examples."""
    parser = argparse.ArgumentParser(
        prog="servicenow-cleanup.py",
        description="ServiceNow Incident Cleanup Utility: Safely query (dry-run) and purge incidents.",
        epilog="""
Examples:
  1. Default dry-run (incidents older than 7 days):
     python servicenow-cleanup.py --instance-url https://dev123.service-now.com --username admin

  2. Specific age and status:
     python servicenow-cleanup.py --older-than 14 --state closed,resolved

  3. Range of incident numbers with dry-run CSV export:
     python servicenow-cleanup.py --number-from INC0010000 --number-to INC0010050 --output csv

  4. Apply deletion interactively (prompts to type incident count to confirm):
     python servicenow-cleanup.py --older-than 30 --state closed --apply

  5. Apply deletion non-interactively with --yes:
     python servicenow-cleanup.py --numbers INC0010222,INC0010223 --apply --yes

Note:
  Child attachments in sys_attachment and journal field entries are purged automatically
  by ServiceNow database cascade deletion.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Core Target & Scope
    parser.add_argument(
        "--table",
        default="incident",
        help="Target table to clean up (strictly fixed to 'incident').",
    )
    parser.add_argument(
        "--instance-url",
        default=None,
        help="ServiceNow instance URL (or set SNOW_INSTANCE_URL).",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="ServiceNow username (or set SNOW_USERNAME).",
    )

    # Filtering Options (Combinable with AND)
    filter_group = parser.add_argument_group("Filter Options (At least one filter required; combinable with AND)")
    filter_group.add_argument(
        "--older-than",
        type=int,
        default=None,
        metavar="DAYS",
        help="Filter incidents created more than DAYS days ago (default 7 when no other filter is specified).",
    )
    filter_group.add_argument(
        "--created-before",
        default=None,
        metavar="ISO_TIMESTAMP",
        help="Filter incidents created before ISO date/datetime (UTC, e.g. '2026-10-01' or '2026-10-01T12:00:00Z').",
    )
    filter_group.add_argument(
        "--created-after",
        default=None,
        metavar="ISO_TIMESTAMP",
        help="Filter incidents created after ISO date/datetime (UTC, e.g. '2026-10-01').",
    )
    filter_group.add_argument(
        "--number-from",
        default=None,
        metavar="INC_START",
        help="Start of incident number range (e.g. 'INC0010000').",
    )
    filter_group.add_argument(
        "--number-to",
        default=None,
        metavar="INC_END",
        help="End of incident number range (e.g. 'INC0010500').",
    )
    filter_group.add_argument(
        "--numbers",
        default=None,
        metavar="INC1,INC2,...",
        help="Comma-separated list of explicit incident numbers.",
    )
    filter_group.add_argument(
        "--caller",
        default=None,
        metavar="NAME_OR_SYSID",
        help="Filter by caller username, full name, or 32-character sys_id.",
    )
    filter_group.add_argument(
        "--short-description-contains",
        action="append",
        default=None,
        metavar="TEXT",
        help="Filter incidents whose short_description contains TEXT (repeatable; matches any).",
    )
    filter_group.add_argument(
        "--short-description-regex",
        default=None,
        metavar="PATTERN",
        help="Client-side regular expression to filter short_description.",
    )
    filter_group.add_argument(
        "--state",
        action="append",
        default=None,
        metavar="STATE",
        help="Filter by state name (new, in_progress, on_hold, resolved, closed, canceled) or code (repeatable/comma-separated).",
    )
    filter_group.add_argument(
        "--priority",
        action="append",
        default=None,
        metavar="1-5",
        help="Filter by priority level 1 through 5 (repeatable or comma-separated).",
    )
    filter_group.add_argument(
        "--assignment-group",
        default=None,
        metavar="NAME_OR_SYSID",
        help="Filter by assignment group name or sys_id.",
    )
    filter_group.add_argument(
        "--category",
        default=None,
        metavar="CATEGORY",
        help="Filter by incident category.",
    )
    filter_group.add_argument(
        "--created-by",
        default=None,
        metavar="USER",
        help="Filter by sys_created_by user (e.g. integration service account).",
    )
    filter_group.add_argument(
        "--query",
        default=None,
        metavar="ENCODED_QUERY",
        help="Advanced raw ServiceNow encoded query (ANDed with other filters).",
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
        help="Skip interactive confirmation when --apply is set (echoes count).",
    )
    exec_group.add_argument(
        "--limit",
        type=int,
        default=500,
        metavar="N",
        help="Maximum number of incidents to process (default 500, hard cap 10000).",
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
        help="Batch size for Table API requests (default 100).",
    )
    exec_group.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging (credentials remain sanitized).",
    )

    return parser


def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments, guarding against accidental --password flags."""
    raw_args = argv if argv is not None else sys.argv[1:]

    # Proactively check for any variant of --password
    for arg in raw_args:
        if arg == "--password" or arg.startswith("--password="):
            raise SystemExit(
                "Error: --password flag is strictly forbidden to prevent credential exposure in shell history.\n"
                "Please use the SNOW_PASSWORD environment variable or enter it when prompted interactively."
            )

    parser = build_arg_parser()
    args = parser.parse_args(raw_args)

    # Validate table scope constraint
    if args.table != "incident":
        raise SystemExit(f"Error: Table '{args.table}' is not supported. This utility is restricted to 'incident'.")

    # Safety limits
    if args.limit < 1:
        raise SystemExit("Error: --limit must be a positive integer.")
    if args.limit > args.max_limit:
        raise SystemExit(
            f"Error: Requested --limit {args.limit} exceeds safety maximum ceiling of {args.max_limit}."
        )

    # Flatten comma-separated lists for repeated arguments
    if args.numbers:
        args.numbers = [n.strip() for n in args.numbers.split(",") if n.strip()]

    if args.state:
        flattened_states: List[str] = []
        for s in args.state:
            flattened_states.extend([part.strip() for part in s.split(",") if part.strip()])
        args.state = flattened_states

    if args.priority:
        flattened_prios: List[str] = []
        for p in args.priority:
            flattened_prios.extend([part.strip() for part in p.split(",") if part.strip()])
        args.priority = flattened_prios

    # Check whether any explicit filter was provided
    explicit_filters_present = any(
        [
            args.older_than is not None,
            args.created_before is not None,
            args.created_after is not None,
            args.number_from is not None,
            args.number_to is not None,
            bool(args.numbers),
            args.caller is not None,
            bool(args.short_description_contains),
            args.short_description_regex is not None,
            bool(args.state),
            bool(args.priority),
            args.assignment_group is not None,
            args.category is not None,
            args.created_by is not None,
            args.query is not None,
        ]
    )

    # Rule: If NO other filter is given, default --older-than to 7
    if not explicit_filters_present:
        args.older_than = 7

    return args


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI execution flow."""
    args = parse_arguments(argv)

    # Credential Resolution
    try:
        url, username, password = resolve_credentials(
            cli_url=args.instance_url,
            cli_username=args.username,
        )
    except Exception as exc:
        sys.stderr.write(f"Configuration Error: {exc}\n")
        return 1

    logger = setup_logger(verbose=args.verbose, secrets_to_mask=[password])

    # Construct server-side encoded query
    try:
        encoded_query = build_encoded_query(
            older_than=args.older_than,
            created_before=args.created_before,
            created_after=args.created_after,
            number_from=args.number_from,
            number_to=args.number_to,
            numbers=args.numbers,
            caller=args.caller,
            short_description_contains=args.short_description_contains,
            state=args.state,
            priority=args.priority,
            assignment_group=args.assignment_group,
            category=args.category,
            created_by=args.created_by,
            raw_query=args.query,
        )
    except Exception as exc:
        logger.error("Failed to construct query: %s", exc)
        return 1

    logger.debug("Constructed ServiceNow encoded query: %s", encoded_query)

    # Initialize authenticated requests session
    session = requests.Session()
    session.auth = HTTPBasicAuth(username, password)
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ServiceNow-Incident-Cleanup-Utility/1.0",
        }
    )

    # Query matching incidents
    try:
        logger.info("Querying ServiceNow incident table on %s...", url)
        incidents = fetch_incidents(
            session=session,
            instance_url=url,
            query=encoded_query,
            limit=args.limit,
            page_size=args.page_size,
            short_description_regex=args.short_description_regex,
            logger=logger,
        )
    except Exception as exc:
        logger.error("Error communicating with ServiceNow Table API: %s", exc)
        return 1

    total_count = len(incidents)

    # Dry-Run Mode (Default)
    if not args.apply:
        print(f"\n[DRY-RUN MODE] Found {total_count} matching incident(s). Zero deletions executed.")
        print("Note: Child attachments (sys_attachment) and journal records are removed via cascade deletion on apply.\n")

        if total_count == 0:
            print("No matching incidents found.")
            return 0

        if args.output == "json":
            print(render_json(incidents))
        elif args.output == "csv":
            print(render_csv(incidents))
        else:
            print(render_table(incidents))

        print(f"\nTotal matching incidents: {total_count}")
        print("To permanently delete these incidents, re-run with --apply.")
        return 0

    # Apply Mode
    if total_count == 0:
        print("\nNo matching incidents found. Nothing to delete.")
        return 0

    print(f"\n[APPLY MODE] {total_count} incident(s) matched for permanent deletion.")
    if args.output == "json":
        print(render_json(incidents))
    elif args.output == "csv":
        print(render_csv(incidents))
    else:
        print(render_table(incidents))

    # Confirmation
    if not args.yes:
        try:
            prompt_msg = f"\nType '{total_count}' to confirm permanent deletion of {total_count} incidents: "
            confirmation = input(prompt_msg).strip()
        except EOFError:
            print("\nAborted: No confirmation input received.")
            return 1

        if confirmation != str(total_count):
            print(f"\nAborted: Confirmation '{confirmation}' did not match incident count '{total_count}'.")
            print("Zero incidents were deleted.")
            return 1
    else:
        print(f"\nConfirming deletion of {total_count} incidents (--yes specified).")

    # Execute Deletions
    logger.info("Executing deletion of %d incidents...", total_count)
    successful, failures = delete_incidents(
        session=session,
        instance_url=url,
        incidents=incidents,
        logger=logger,
    )

    print("\n" + "=" * 50)
    print("ServiceNow Incident Cleanup Summary")
    print("=" * 50)
    print(f"Total targeted:       {total_count}")
    print(f"Successfully deleted: {len(successful)}")
    print(f"Failed deletions:     {len(failures)}")
    print("Note: Child attachments were removed by ServiceNow database cascade.")

    if failures:
        print("\nFailures:")
        for fail in failures:
            print(f"  - {fail['number']} ({fail['sys_id']}): {fail['error']}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
