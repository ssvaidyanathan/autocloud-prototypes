"""Duplicate Alert Checker and Stale Lock Handler for Alert Triage Agent.

Constructs and executes the verbatim multi-grep pipeline:
  grep -FlZ 'policy_name: "<policy_name>"' /knowledge/incident-manager/*.md 2>/dev/null \
    | xargs -r0 grep -FlZ 'resource_name: "<resource_name>"' \
    | xargs -r0 grep -l 'status: mitigation-in-progress'

Evaluates timestamp in matching records:
- If <= 30 minutes: ACTIVE_DUPLICATE_PROCESSING (is_duplicate: true)
- If > 30 minutes: treats as stale lock from crashed session, logs warning, ignores
- If no match: is_duplicate: false
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
from typing import Any, Optional
import yaml

from .grouping.models import format_okf_alert_entry
from .tools.shell import DEFAULT_INCIDENT_STORE, execute_incident_command

logger = logging.getLogger(__name__)

STALE_LOCK_THRESHOLD_SECONDS = 30 * 60  # 30 minutes


@dataclass
class DuplicateCheckResult:
    is_duplicate: bool
    status: str
    matching_files: list[str] = field(default_factory=list)
    stale_files: list[str] = field(default_factory=list)
    active_file: Optional[str] = None
    existing_frontmatter: Optional[dict[str, Any]] = None
    command_executed: str = ""


def _escape_single_quote(text: str) -> str:
    """Escape text for inclusion inside single-quoted shell string."""
    # In bash: 'foo'\''bar'
    return text.replace("'", "'\\''")


def build_multi_grep_pipeline(
    policy_name: str,
    resource_name: str,
    store_path: str = DEFAULT_INCIDENT_STORE,
) -> str:
    """Construct the exact verbatim multi-grep pipeline with safe escaping."""
    safe_pol = _escape_single_quote(policy_name)
    safe_res = _escape_single_quote(resource_name)
    store = store_path.rstrip("/")

    cmd = (
        f"grep -FlZ 'policy_name: \"{safe_pol}\"' {store}/*.md 2>/dev/null \\\n"
        f"  | xargs -r0 grep -FlZ 'resource_name: \"{safe_res}\"' \\\n"
        f"  | xargs -r0 grep -El 'status: (mitigation-in-progress|ticketed|investigating)'"
    )
    return cmd


def append_duplicate_alert_to_incident(
    file_path: str | Path,
    alert: Any,
    config: Optional[Any] = None,
) -> dict[str, Any]:
    """Append duplicate alert to existing incident markdown file, refresh timestamp, and preserve ticket."""
    path = Path(file_path)
    if not path.exists():
        logger.warning("Target incident file %s does not exist for duplicate append", file_path)
        return {}

    content = path.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    if len(parts) < 3:
        logger.warning("Malformed incident markdown file %s (missing frontmatter)", file_path)
        return {}

    try:
        meta = yaml.safe_load(parts[1]) or {}
    except Exception as exc:
        logger.warning("Failed to parse YAML frontmatter in %s: %s", file_path, exc)
        meta = {}

    # 1. Refresh timestamp to current or alert started_at
    new_ts = getattr(alert, "started_at", None) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not str(new_ts).strip():
        new_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta["timestamp"] = new_ts

    # 2. Preserve active status and ticket fields
    if "status" not in meta or meta["status"] in ("open", "investigating"):
        meta["status"] = "mitigation-in-progress"

    # 3. Append or update alert in alerts list
    alerts_list = list(meta.get("alerts", [])) if isinstance(meta.get("alerts"), list) else []
    new_alert_entry = format_okf_alert_entry(alert, valid_ts=new_ts)

    found = False
    for i, a in enumerate(alerts_list):
        if isinstance(a, dict) and a.get("incident_id") == new_alert_entry["incident_id"]:
            if not new_alert_entry.get("labels") and a.get("labels"):
                new_alert_entry["labels"] = a["labels"]
                new_alert_entry["policy_user_labels"] = a.get("policy_user_labels") or a["labels"]
            if not new_alert_entry.get("component") and a.get("component"):
                new_alert_entry["component"] = a["component"]
            if not new_alert_entry.get("signal") and a.get("signal"):
                new_alert_entry["signal"] = a["signal"]
            alerts_list[i] = new_alert_entry
            found = True
            break
    if not found:
        alerts_list.append(new_alert_entry)
    meta["alerts"] = [format_okf_alert_entry(a, valid_ts=new_ts) for a in alerts_list]

    # Reconstruct file
    yaml_header = yaml.dump(meta, sort_keys=False, default_flow_style=False).strip()
    body = parts[2]
    comp_line = f"- Component: `{new_alert_entry.get('component')}`\n" if new_alert_entry.get("component") else ""
    sig_line = f"- Signal: `{new_alert_entry.get('signal')}`\n" if new_alert_entry.get("signal") else ""
    dup_note = (
        f"\n\n**Duplicate Alert Appended**\n"
        f"- Incident ID: `{new_alert_entry['incident_id']}`\n"
        f"- Policy: `{new_alert_entry['policy']}`\n"
        f"- Resource: `{new_alert_entry['resource']}`\n"
        f"{comp_line}"
        f"{sig_line}"
        f"- State: `{new_alert_entry['state']}`\n"
        f"- Timestamp: `{new_ts}`\n"
    )
    new_content = f"---\n{yaml_header}\n---{body}{dup_note}"
    path.write_text(new_content, encoding="utf-8")
    logger.info("Appended duplicate alert %s to %s and refreshed timestamp to %s", new_alert_entry["incident_id"], path, new_ts)

    # API fallback if mount unavailable or running in cloud environment
    if config and not getattr(config, "is_local", True) and getattr(config, "incidents_bucket", None):
        try:
            from google.cloud import storage
            client = storage.Client(project=getattr(config, "project_id", None))
            bucket_name = getattr(config, "effective_incidents_bucket", getattr(config, "incidents_bucket", None))
            bucket = client.bucket(bucket_name)
            filename = path.name
            blob_path = f"knowledge/incident-manager/{filename}"
            bucket.blob(blob_path).upload_from_string(new_content, content_type="text/markdown")
            logger.info("Saved updated duplicate file to GCS: gs://%s/%s", bucket_name, blob_path)
        except Exception as exc:
            logger.debug("GCS fallback for duplicate skipped: %s", exc)

    return meta


def _parse_file_timestamp(content: str) -> Optional[datetime]:
    """Extract and parse timestamp or started_at from incident markdown frontmatter."""
    parts = content.split("---")
    if len(parts) >= 3:
        try:
            meta = yaml.safe_load(parts[1])
            if isinstance(meta, dict):
                ts_val = meta.get("timestamp") or meta.get("started_at")
                if ts_val:
                    return _normalize_datetime(ts_val)
                # Check alerts array
                alerts = meta.get("alerts", [])
                if alerts and isinstance(alerts[0], dict):
                    alert_ts = alerts[0].get("started_at") or alerts[0].get("timestamp")
                    if alert_ts:
                        return _normalize_datetime(alert_ts)
        except Exception as exc:
            logger.debug("Failed to parse YAML frontmatter: %s", exc)

    # Fallback to regex matching
    match = re.search(r'(?:timestamp|started_at):\s*["\']?([^"\'\n\r]+)["\']?', content)
    if match:
        return _normalize_datetime(match.group(1).strip())

    return None


def _normalize_datetime(val: Any) -> Optional[datetime]:
    """Convert various timestamp formats into UTC datetime."""
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)

    if isinstance(val, (int, float)):
        if val > 1e11:
            val = val / 1000.0
        return datetime.fromtimestamp(val, tz=timezone.utc)

    if isinstance(val, str):
        val = val.strip()
        # ISO-8601 parsing
        for fmt in (
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y%m%dt%H%M%sz",
        ):
            try:
                dt = datetime.strptime(val, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
        try:
            # Let datetime.fromisoformat handle other forms
            iso_val = val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass

    return None


def check_for_duplicate(
    policy_name: str,
    resource_name: str,
    store_path: str = DEFAULT_INCIDENT_STORE,
    now: Optional[datetime] = None,
) -> DuplicateCheckResult:
    """Check for active duplicate incident records using multi-grep and 30m stale lock verification.
    
    Args:
        policy_name: Alert policy name to match.
        resource_name: Resource name to match.
        store_path: Path where incident markdown files are stored.
        now: Reference datetime (defaults to UTC now).
        
    Returns:
        DuplicateCheckResult with is_duplicate, status, active_file, and stale_files.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    cmd = build_multi_grep_pipeline(policy_name, resource_name, store_path=store_path)

    # Execute search via constrained execution engine
    store_dir = Path(store_path)
    if not store_dir.exists():
        return DuplicateCheckResult(
            is_duplicate=False,
            status="NO_DUPLICATE",
            command_executed=cmd,
        )

    exec_res = execute_incident_command(cmd, store_path=store_path)
    stdout = exec_res.stdout.strip()

    if not stdout:
        safe_pol = _escape_single_quote(policy_name)
        safe_res = _escape_single_quote(resource_name)
        store = store_path.rstrip("/")
        cmd_unquoted = (
            f"grep -FlZ 'policy_name: {safe_pol}' {store}/*.md 2>/dev/null \\\n"
            f"  | xargs -r0 grep -FlZ 'resource_name: {safe_res}' \\\n"
            f"  | xargs -r0 grep -El 'status: (mitigation-in-progress|ticketed|investigating)'"
        )
        exec_res = execute_incident_command(cmd_unquoted, store_path=store_path)
        stdout = exec_res.stdout.strip()
        if stdout:
            cmd = cmd_unquoted

    if not stdout:
        return DuplicateCheckResult(
            is_duplicate=False,
            status="NO_DUPLICATE",
            command_executed=cmd,
        )

    matched_paths = [p.strip() for p in stdout.splitlines() if p.strip()]
    active_matches: list[str] = []
    stale_matches: list[str] = []
    last_frontmatter: Optional[dict[str, Any]] = None

    for file_str in matched_paths:
        file_path = Path(file_str)
        if not file_path.is_absolute():
            file_path = store_dir / file_str

        if not file_path.exists():
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not read matching file %s: %s", file_path, exc)
            continue

        file_dt = _parse_file_timestamp(content)
        if file_dt is None:
            # If timestamp missing from file, check file mtime
            file_dt = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)

        age_seconds = (now - file_dt).total_seconds()

        if age_seconds <= STALE_LOCK_THRESHOLD_SECONDS:
            active_matches.append(str(file_path))
            try:
                parts = content.split("---")
                if len(parts) >= 3:
                    last_frontmatter = yaml.safe_load(parts[1])
            except Exception:
                pass
        else:
            age_min = round(age_seconds / 60.0, 1)
            logger.warning(
                "Detected stale incident lock (%s minutes old) in %s from crashed session; ignoring.",
                age_min,
                file_path,
            )
            stale_matches.append(str(file_path))

    if active_matches:
        return DuplicateCheckResult(
            is_duplicate=True,
            status="ACTIVE_DUPLICATE_PROCESSING",
            matching_files=active_matches,
            stale_files=stale_matches,
            active_file=active_matches[0],
            existing_frontmatter=last_frontmatter,
            command_executed=cmd,
        )

    if stale_matches:
        return DuplicateCheckResult(
            is_duplicate=False,
            status="STALE_LOCK_BYPASSED",
            matching_files=[],
            stale_files=stale_matches,
            command_executed=cmd,
        )

    return DuplicateCheckResult(
        is_duplicate=False,
        status="NO_DUPLICATE",
        command_executed=cmd,
    )
