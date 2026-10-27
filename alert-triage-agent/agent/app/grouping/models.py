"""Incident grouping models and status definitions for Alert Triage Agent."""

from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml


class IncidentGroupStatus(str, Enum):
    """Canonical lifecycle status for an incident group."""
    OPEN = "open"
    INVESTIGATING = "investigating"
    TICKETED = "ticketed"
    COOLING_DOWN = "cooling_down"
    RESOLVED = "resolved"
    # Terminal: monitoring recovered after cool-down but the ticket was left open
    # for a human to verify and resolve (AUTO_RESOLVE_ON_RECOVERY=false).
    RECOVERED = "recovered"
    ARCHIVED = "archived"
    DEGRADED = "degraded"


COOLING_WINDOW_SECONDS: float = 180.0


def parse_iso_timestamp(ts_val: Optional[Union[str, int, float, datetime]]) -> Optional[datetime]:
    """Parse various timestamp representations into a UTC datetime object."""
    if ts_val is None:
        return None
    if isinstance(ts_val, datetime):
        if ts_val.tzinfo is None:
            return ts_val.replace(tzinfo=timezone.utc)
        return ts_val.astimezone(timezone.utc)
    if isinstance(ts_val, (int, float)):
        return datetime.fromtimestamp(ts_val, tz=timezone.utc)
    if isinstance(ts_val, str):
        cleaned = ts_val.strip()
        if not cleaned:
            return None
        # Handle numeric string
        try:
            return datetime.fromtimestamp(float(cleaned), tz=timezone.utc)
        except ValueError:
            pass
        # Handle ISO format
        try:
            iso_str = cleaned.replace("Z", "+00:00")
            return datetime.fromisoformat(iso_str)
        except Exception:
            return None
    return None


def format_okf_alert_entry(
    alert: Any,
    valid_ts: Optional[str] = None,
    received_at: Optional[str] = None,
) -> dict[str, Any]:
    """Format an alert object or dict into a standard OKF alert entry dictionary.
    Guarantees presence of:
    - incident_id
    - policy / policy_name
    - policy_id (numeric string or None)
    - resource / resource_name
    - component
    - signal
    - state
    - started_at
    - url
    - labels: {component, signal, severity, tier, ...}
    - policy_user_labels
    """
    if isinstance(alert, dict):
        inc_id = str(alert.get("incident_id") or "0.unknown")
        pol_name = str(alert.get("policy_name") or alert.get("policy") or "unknown-policy")
        pol_id = alert.get("policy_id")
        res_name = str(alert.get("resource_name") or alert.get("resource") or "unknown-resource")
        state = str(alert.get("state") or "open")
        st_at = alert.get("started_at") or valid_ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = alert.get("url")
        raw_labels = alert.get("labels") or alert.get("policy_user_labels") or {}
        labels = {str(k): str(v) for k, v in raw_labels.items()} if isinstance(raw_labels, dict) else {}
        comp = alert.get("component") or labels.get("component") or res_name
        sig = alert.get("signal") or labels.get("signal")
        rec_at = alert.get("received_at") or received_at
    else:
        inc_id = getattr(alert, "incident_id", "0.unknown")
        pol_name = getattr(alert, "policy_name", "unknown-policy")
        pol_id = getattr(alert, "policy_id", None)
        res_name = getattr(alert, "resource_name", "unknown-resource")
        state = getattr(alert, "state", "open")
        st_at = getattr(alert, "started_at", None) or valid_ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = getattr(alert, "url", None)
        raw_labels = getattr(alert, "policy_user_labels", {}) or getattr(alert, "labels", {}) or {}
        labels = {str(k): str(v) for k, v in raw_labels.items()} if isinstance(raw_labels, dict) else {}
        comp = getattr(alert, "component", None) or labels.get("component") or res_name
        sig = getattr(alert, "signal", None) or labels.get("signal")
        rec_at = received_at

    entry: dict[str, Any] = {
        "incident_id": inc_id,
        "policy": pol_name,
        "policy_name": pol_name,
        "policy_id": pol_id,
        "resource": res_name,
        "resource_name": res_name,
        "component": comp,
        "signal": sig,
        "state": state,
        "started_at": st_at,
        "url": url,
        "labels": labels,
        "policy_user_labels": labels,
    }
    if rec_at:
        entry["received_at"] = rec_at
    return entry


class OKFDocument(BaseModel):
    """Model representing an Open Knowledge File (OKF) with YAML frontmatter and markdown body."""
    model_config = ConfigDict(extra="allow")

    type: str = "incident"
    group_id: str
    status: Union[IncidentGroupStatus, str] = IncidentGroupStatus.OPEN
    project: Optional[str] = None
    policy_name: Optional[str] = None
    resource_name: Optional[str] = None
    origin: Optional[str] = None
    blast_radius: list[str] = Field(default_factory=list)
    priority: Optional[str] = "P1"

    # ServiceNow Ticket Linkage
    servicenow_sys_id: Optional[str] = None
    servicenow_number: Optional[str] = None
    servicenow_url: Optional[str] = None
    # Ticket title/priority as last sent to ServiceNow (at creation, then on late-alert updates).
    ticket_short_description: Optional[str] = None
    ticket_priority: Optional[str] = None
    ticket_title_problem: Optional[str] = None
    ticket_origin: Optional[str] = None

    # Deadlines & Timers (ISO-8601 UTC)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    timestamp: Optional[str] = None
    settle_deadline: Optional[str] = None
    max_wait_deadline: Optional[str] = None
    cooling_deadline: Optional[str] = None

    # Monitoring recovery (cool-down expired without auto-resolve)
    recovered_at: Optional[str] = None
    recovery_note_posted_at: Optional[str] = None

    # Stuck-investigation recovery (grouping/stuck_investigation.py): last wake attempt that
    # (re)started the investigation, and one record per recovery.
    investigation_started_at: Optional[str] = None
    investigation_recoveries: list[dict[str, Any]] = Field(default_factory=list)
    # A group degraded after INVESTIGATION_MAX_RECOVERIES ('stuck_investigation') records the
    # new group that lists its alerts in its ticket; that new group records them here.
    degraded_cause: Optional[str] = None
    carried_to_group_id: Optional[str] = None
    degraded_predecessors: list[dict[str, Any]] = Field(default_factory=list)

    # Degraded mode flags
    degraded: bool = False
    degraded_reason: Optional[str] = None

    # Suppression flags
    suppressed: bool = False
    suppression_reason: Optional[str] = None

    # Correlation decision that opened this group (decision, best_score, thresholds, open
    # groups compared), stated in the ticket so single-alert groups also show why they are new.
    correlation: Optional[dict[str, Any]] = None

    # Correlated Member Alerts
    alerts: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_alerts(cls, data: Any) -> Any:
        if isinstance(data, dict) and "alerts" in data and isinstance(data["alerts"], list):
            data["alerts"] = [format_okf_alert_entry(a) for a in data["alerts"]]
        return data

    # Markdown body
    body: str = ""

    @property
    def canonical_status(self) -> IncidentGroupStatus:
        """Return status as an IncidentGroupStatus enum if possible."""
        try:
            return IncidentGroupStatus(str(self.status))
        except ValueError:
            return IncidentGroupStatus.OPEN

    @classmethod
    def from_markdown(cls, content: str) -> "OKFDocument":
        """Parse OKF Markdown string into OKFDocument."""
        parts = content.split("---", 2)
        if len(parts) >= 3:
            raw_meta = yaml.safe_load(parts[1]) or {}
            body = parts[2].lstrip("\n")
        else:
            raw_meta = {}
            body = content
        return cls(**raw_meta, body=body)

    def to_markdown(self) -> str:
        """Serialize OKFDocument into OKF Markdown string with YAML frontmatter."""
        data = self.model_dump(exclude={"body"}, exclude_none=False)
        # Convert Enum values to string for YAML serialization
        if isinstance(data.get("status"), Enum):
            data["status"] = data["status"].value
        frontmatter_yaml = yaml.safe_dump(data, sort_keys=False)
        body_content = self.body.strip()
        if body_content:
            return f"---\n{frontmatter_yaml}---\n\n{body_content}\n"
        return f"---\n{frontmatter_yaml}---\n"

    def has_alert(self, incident_id: str) -> bool:
        """Check if an alert with incident_id already belongs to this group."""
        if not incident_id:
            return False
        return any(a.get("incident_id") == incident_id for a in self.alerts)

    def add_alert(self, alert_data: dict[str, Any]) -> bool:
        """Add alert if not already present by incident_id. Returns True if added, False if duplicate."""
        inc_id = alert_data.get("incident_id")
        if inc_id and self.has_alert(inc_id):
            return False
        entry = format_okf_alert_entry(alert_data)
        self.alerts.append(entry)
        now_dt = datetime.now(timezone.utc)
        self.updated_at = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return True

    def update_alert_state(self, incident_id: str, new_state: str) -> bool:
        """Update individual alert state by incident_id."""
        for a in self.alerts:
            if a.get("incident_id") == incident_id:
                a["state"] = new_state
                now_dt = datetime.now(timezone.utc)
                self.updated_at = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                return True
        return False

    def all_alerts_closed(self) -> bool:
        """Return True if all member alerts have closed state."""
        if not self.alerts:
            return False
        return all(a.get("state", "").lower() == "closed" for a in self.alerts)

    def settle_deadline_epoch(self) -> Optional[float]:
        """Return settle deadline as epoch timestamp in seconds."""
        dt = parse_iso_timestamp(self.settle_deadline)
        return dt.timestamp() if dt else None

    def max_wait_deadline_epoch(self) -> Optional[float]:
        """Return max wait deadline as epoch timestamp in seconds."""
        dt = parse_iso_timestamp(self.max_wait_deadline)
        return dt.timestamp() if dt else None

    def cooling_deadline_epoch(self) -> Optional[float]:
        """Return cooling deadline as epoch timestamp in seconds."""
        dt = parse_iso_timestamp(self.cooling_deadline)
        return dt.timestamp() if dt else None

    def transition_to(self, new_status: Union[IncidentGroupStatus, str]) -> None:
        """Transition group status with updated timestamp."""
        status_val = new_status.value if isinstance(new_status, IncidentGroupStatus) else str(new_status)
        self.status = status_val
        now_dt = datetime.now(timezone.utc)
        self.updated_at = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def extend_settle(
        self,
        current_time: Optional[float] = None,
        settle_duration: float = 120.0,
        cap_at_max_wait: bool = True,
    ) -> float:
        """Extend settle deadline by settle_duration, optionally capped at max_wait_deadline.
        
        Returns the new settle deadline as an epoch timestamp.
        """
        now = current_time if current_time is not None else datetime.now(timezone.utc).timestamp()
        new_settle = now + settle_duration

        if cap_at_max_wait:
            max_epoch = self.max_wait_deadline_epoch()
            if max_epoch is not None and new_settle > max_epoch:
                new_settle = max_epoch

        new_settle_dt = datetime.fromtimestamp(new_settle, tz=timezone.utc)
        self.settle_deadline = new_settle_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.updated_at = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return new_settle


class IncidentGroup(BaseModel):
    """In-memory or serialized representation of a correlated incident group."""
    model_config = ConfigDict(extra="allow")

    group_id: str
    filename: Optional[str] = None
    status: Union[IncidentGroupStatus, str] = IncidentGroupStatus.OPEN
    origin: Optional[str] = None
    blast_radius: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    settle_deadline: Optional[str] = None
    max_wait_deadline: Optional[str] = None
    cooling_deadline: Optional[str] = None
    degraded: bool = False
    degraded_reason: Optional[str] = None
    suppressed: bool = False
    suppression_reason: Optional[str] = None
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    servicenow_sys_id: Optional[str] = None
    servicenow_number: Optional[str] = None
    servicenow_url: Optional[str] = None

    @classmethod
    def from_okf(cls, doc: OKFDocument, filename: Optional[str] = None) -> "IncidentGroup":
        """Convert OKFDocument into IncidentGroup."""
        return cls(
            group_id=doc.group_id,
            filename=filename or f"incident-{doc.group_id}.md",
            status=doc.status,
            origin=doc.origin,
            blast_radius=doc.blast_radius,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            settle_deadline=doc.settle_deadline,
            max_wait_deadline=doc.max_wait_deadline,
            cooling_deadline=doc.cooling_deadline,
            degraded=doc.degraded,
            degraded_reason=doc.degraded_reason,
            suppressed=doc.suppressed,
            suppression_reason=doc.suppression_reason,
            alerts=doc.alerts,
            servicenow_sys_id=doc.servicenow_sys_id,
            servicenow_number=doc.servicenow_number,
            servicenow_url=doc.servicenow_url,
        )

    def to_okf(self) -> OKFDocument:
        """Convert IncidentGroup into OKFDocument."""
        return OKFDocument(
            group_id=self.group_id,
            status=self.status,
            origin=self.origin,
            blast_radius=self.blast_radius,
            created_at=self.created_at,
            updated_at=self.updated_at,
            settle_deadline=self.settle_deadline,
            max_wait_deadline=self.max_wait_deadline,
            cooling_deadline=self.cooling_deadline,
            degraded=self.degraded,
            degraded_reason=self.degraded_reason,
            suppressed=self.suppressed,
            suppression_reason=self.suppression_reason,
            alerts=self.alerts,
            servicenow_sys_id=self.servicenow_sys_id,
            servicenow_number=self.servicenow_number,
            servicenow_url=self.servicenow_url,
        )
