from datetime import datetime, timezone
import re
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator


def _parse_timestamp(val: Any) -> str:
    """Normalize epoch or string timestamp to RFC3339 string."""
    if val is None or val == "":
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(val, (int, float)):
        if val > 1e11:
            val = val / 1000.0
        return datetime.fromtimestamp(val, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if val.endswith("Z") or "+" in val:
            return val
        return f"{val}Z"
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str) -> str:
    """Create a URL/filename-safe slug from text."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def normalize_resource_name(
    raw_name: str = "",
    res_type: str = "",
    res_labels: Optional[dict[str, Any]] = None,
) -> str:
    """Normalize raw Cloud Monitoring resource string, type, and labels into a canonical identifier.

    Rules:
    - Cloud Run: res_labels.get('service_name') or res_labels.get('configuration_name')
    - Apigee: res_labels.get('proxy_name') or res_labels.get('apiproxy') or res_labels.get('proxy')
    - Cloud SQL: res_labels.get('database_id', '').split(':')[-1] or res_labels.get('instance_id')
    - If raw resource_name contains 'service_name=...', 'proxy_name=...', 'database_id=...', regex-extract the identifier.
    """
    labels = res_labels or {}
    rt = (res_type or "").lower()

    # 1. Type-specific label extraction
    if "run" in rt or "cloud_run" in rt:
        val = labels.get("service_name") or labels.get("configuration_name")
        if val:
            return str(val).strip()
    elif "apigee" in rt or "proxy" in rt:
        val = labels.get("proxy_name") or labels.get("apiproxy") or labels.get("proxy")
        if val:
            return str(val).strip()
    elif "sql" in rt or "database" in rt:
        db_id = labels.get("database_id", "")
        val = (str(db_id).split(":")[-1] if db_id else None) or labels.get("instance_id")
        if val:
            return str(val).strip()

    # 2. General label extraction if type did not explicitly match
    val = (
        labels.get("service_name")
        or labels.get("configuration_name")
        or labels.get("proxy_name")
        or labels.get("apiproxy")
        or labels.get("proxy")
        or (str(labels.get("database_id", "")).split(":")[-1] if labels.get("database_id") else None)
        or labels.get("instance_id")
        or labels.get("backend_name")
    )
    if val:
        return str(val).strip()

    # 3. Regex extraction from raw resource string if it contains key=value pairs
    if raw_name:
        raw_str = str(raw_name).strip()
        m = re.search(r"service_name\s*=\s*['\"]?([^,'\"\s}]+)", raw_str)
        if m:
            return m.group(1).strip()
        m = re.search(r"configuration_name\s*=\s*['\"]?([^,'\"\s}]+)", raw_str)
        if m:
            return m.group(1).strip()
        m = re.search(r"(?:proxy_name|apiproxy|proxy)\s*=\s*['\"]?([^,'\"\s}]+)", raw_str)
        if m:
            return m.group(1).strip()
        m = re.search(r"database_id\s*=\s*['\"]?([^,'\"\s}]+)", raw_str)
        if m:
            db_id = m.group(1).strip()
            return db_id.split(":")[-1]
        m = re.search(r"instance_id\s*=\s*['\"]?([^,'\"\s}]+)", raw_str)
        if m:
            return m.group(1).strip()

        # If it's a resource path e.g. projects/.../services/service-name
        if " " not in raw_str and "{" not in raw_str and "/" in raw_str:
            return raw_str.split("/")[-1].strip()

        # If it's already a clean identifier (no spaces or braces)
        if " " not in raw_str and "{" not in raw_str:
            return raw_str

    return str(raw_name or "unknown-resource").strip()


import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Optional, Set
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# NOTE (Architectural Principle): This copy deliberately diverges from the agent and tools
# contracts. Per the LEAN BRIDGE user principle, the bridge does NOT validate components
# or read topology. In the bridge, policy_user_labels are parsed and passed through to
# the agent unchanged, and no topology modules are packaged or imported in the bridge container.
# Component validation is strictly agent-owned.


def is_valid_component(component: str) -> bool:
    """In lean bridge, component validation is skipped (topology is agent-owned)."""
    return bool(component and isinstance(component, str) and component.strip())


def extract_numeric_policy_id(payload: dict[str, Any], inc: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Extract numeric policy id from payload if present (e.g. incident.policy_name resource path,
    condition_name, policy object, or explicit policy_id field).
    """
    incident = inc if isinstance(inc, dict) else (payload.get("incident") if isinstance(payload, dict) and isinstance(payload.get("incident"), dict) else {})
    candidates: list[str] = []

    # 1. Explicit fields in incident or payload
    for k in ("policy_id", "policyId"):
        if incident.get(k) is not None:
            candidates.append(str(incident[k]))
        if isinstance(payload, dict) and payload.get(k) is not None:
            candidates.append(str(payload[k]))

    # 2. Policy object in incident or payload
    for pol_source in (incident.get("policy"), (payload.get("policy") if isinstance(payload, dict) else None)):
        if isinstance(pol_source, dict):
            for k in ("name", "id", "policy_id", "policyId"):
                if pol_source.get(k) is not None:
                    candidates.append(str(pol_source[k]))
        elif isinstance(pol_source, str):
            candidates.append(pol_source)

    # 3. Policy name string
    for pol_name in (incident.get("policy_name"), (payload.get("policy_name") if isinstance(payload, dict) else None)):
        if isinstance(pol_name, str):
            candidates.append(pol_name)

    # 4. Condition name or condition object
    for cond_source in (incident.get("condition"), (payload.get("condition") if isinstance(payload, dict) else None)):
        if isinstance(cond_source, dict):
            for k in ("name", "id"):
                if cond_source.get(k) is not None:
                    candidates.append(str(cond_source[k]))
        elif isinstance(cond_source, str):
            candidates.append(cond_source)

    for cond_name in (incident.get("condition_name"), (payload.get("condition_name") if isinstance(payload, dict) else None)):
        if isinstance(cond_name, str):
            candidates.append(cond_name)

    # 5. Search candidates for numeric policy id
    for item in candidates:
        item = item.strip()
        if not item:
            continue
        # Direct numeric check
        if item.isdigit():
            return item
        # Resource path matching alertPolicies/<digits>
        m = re.search(r"alertPolicies/(\d+)", item)
        if m:
            return m.group(1)
        # Path matching policies/<digits>
        m2 = re.search(r"policies/(\d+)", item)
        if m2:
            return m2.group(1)

    return None


class AlertEvent(BaseModel):
    """Normalized Cloud Monitoring Alert Event."""
    model_config = ConfigDict(extra="allow")

    incident_id: str
    policy_name: str
    policy_id: Optional[str] = None
    condition_name: Optional[str] = None
    resource_name: str
    resource_type: Optional[str] = None
    resource_labels: dict[str, str] = Field(default_factory=dict)
    policy_user_labels: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    state: str = "open"
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    ended_at: Optional[str] = None
    summary: Optional[str] = None

    @property
    def component(self) -> str:
        return self.policy_user_labels.get("component") or self.resource_name

    @property
    def signal(self) -> Optional[str]:
        return self.policy_user_labels.get("signal")

    @model_validator(mode="before")
    @classmethod
    def validate_and_map_component(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        # Allow passing 'labels' alias for 'policy_user_labels'
        p_labels = data.get("policy_user_labels")
        if p_labels is None and "labels" in data:
            p_labels = data.get("labels")
            data["policy_user_labels"] = p_labels

        if isinstance(p_labels, dict) and p_labels:
            clean_labels = {str(k): str(v) for k, v in p_labels.items()}
            data["policy_user_labels"] = clean_labels
            data["labels"] = clean_labels
            comp = clean_labels.get("component")
            if comp:
                comp_clean = str(comp).strip()
                # Lean bridge does not validate components against topology.
                # Component label maps directly to resource_name and passes through to agent unchanged.
                inc_id = data.get("incident_id", "unknown")
                data["resource_name"] = comp_clean
                logger.info(
                    "Mapped alert %s to component '%s' from policy_user_labels['component'] (bridge pass-through)",
                    inc_id,
                    comp_clean,
                )
        elif "labels" not in data or not data["labels"]:
            data["labels"] = {}

        if not data.get("policy_id") or not str(data.get("policy_id")).strip().isdigit():
            extracted_pid = extract_numeric_policy_id(data)
            if extracted_pid:
                data["policy_id"] = extracted_pid

        return data

    @field_validator("started_at", mode="before")
    @classmethod
    def ensure_valid_timestamp(cls, val: Any) -> str:
        if not val or not str(val).strip():
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return _parse_timestamp(val)

    @field_validator("resource_name", mode="before")
    @classmethod
    def ensure_canonical_resource_name(cls, val: Any) -> str:
        if not val or not str(val).strip():
            return "unknown-resource"
        return normalize_resource_name(raw_name=str(val))

    url: Optional[str] = None
    metric_type: Optional[str] = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def policy_slug(self) -> str:
        name = self.policy_name.split("/")[-1]
        return slugify(name)

    @property
    def resource_slug(self) -> str:
        name = self.resource_name.split("/")[-1]
        return slugify(name)

    @classmethod
    def from_monitoring_payload(cls, payload: dict[str, Any]) -> "AlertEvent":
        inc = payload.get("incident", payload)
        if not isinstance(inc, dict):
            raise ValueError("Invalid alert payload format: expected object")

        inc_id = str(
            inc.get("incident_id")
            or inc.get("id")
            or inc.get("alertId")
            or (inc.get("name", "").split("/")[-1] if inc.get("name") else "")
        )
        if not inc_id:
            raise ValueError("Missing incident_id in alert payload")

        pol_val = inc.get("policy_name") or inc.get("policy")
        if isinstance(pol_val, dict):
            policy = str(pol_val.get("name") or pol_val.get("displayName") or "unknown-policy")
        elif pol_val:
            policy = str(pol_val)
        else:
            cond_disp = (inc.get("condition", {}) if isinstance(inc.get("condition"), dict) else {}).get("displayName")
            policy = str(cond_disp or "unknown-policy")

        res_dict = inc.get("resource", {})
        res_labels: dict[str, str] = {}
        if isinstance(res_dict, dict):
            res_labels = {str(k): str(v) for k, v in res_dict.get("labels", {}).items()}

        res_type = str(
            inc.get("resource_type")
            or (res_dict.get("type") if isinstance(res_dict, dict) else None)
            or ""
        )

        raw_res_name = str(
            inc.get("resource_name")
            or (res_dict.get("name") if isinstance(res_dict, dict) else "")
            or ""
        )

        # Extract policy user labels
        pol_user_labels: dict[str, str] = {}
        raw_labels = (
            inc.get("policy_user_labels")
            or inc.get("policyUserLabels")
            or inc.get("user_labels")
            or inc.get("userLabels")
            or (pol_val.get("user_labels") if isinstance(pol_val, dict) else None)
            or (pol_val.get("userLabels") if isinstance(pol_val, dict) else None)
            or payload.get("policy_user_labels")
            or payload.get("policyUserLabels")
            or payload.get("user_labels")
            or payload.get("userLabels")
            or {}
        )
        if isinstance(raw_labels, dict):
            pol_user_labels = {str(k): str(v) for k, v in raw_labels.items()}

        # Map alert -> component FIRST from policy user label 'component' (bridge pass-through, no topology check)
        component_label = pol_user_labels.get("component")
        if component_label:
            comp_clean = str(component_label).strip()
            res_name = comp_clean
            logger.info(
                "Mapped alert %s to component '%s' from policy user label 'component' (bridge pass-through)",
                inc_id,
                res_name,
            )
        else:
            res_name = normalize_resource_name(
                raw_name=raw_res_name,
                res_type=res_type,
                res_labels=res_labels,
            )
            logger.info(
                "Mapped alert %s to component '%s' from resource-label canonicalization",
                inc_id,
                res_name,
            )

        condition_dict = inc.get("condition")
        cond_name = None
        if isinstance(condition_dict, dict):
            cond_name = condition_dict.get("displayName") or condition_dict.get("name")
        elif isinstance(inc.get("condition_name"), str):
            cond_name = inc.get("condition_name")

        metric_dict = inc.get("metric")
        metric_type = None
        if isinstance(metric_dict, dict):
            metric_type = metric_dict.get("type")

        started_at = _parse_timestamp(inc.get("started_at") or inc.get("openTime") or inc.get("startTime"))
        ended_at_val = inc.get("ended_at") or inc.get("closeTime") or inc.get("endTime")
        ended_at = _parse_timestamp(ended_at_val) if ended_at_val else None

        summary = inc.get("summary")
        if not summary and isinstance(inc.get("policy"), dict):
            summary = inc.get("policy", {}).get("displayName")

        # Extract real policy numeric id if present
        pol_id = extract_numeric_policy_id(payload, inc)

        return cls(
            incident_id=inc_id,
            policy_name=policy,
            policy_id=pol_id,
            condition_name=cond_name,
            resource_name=res_name,
            resource_type=res_type,
            resource_labels=res_labels,
            policy_user_labels=pol_user_labels,
            labels=pol_user_labels,
            state=str(inc.get("state", "open")).lower(),
            started_at=started_at,
            ended_at=ended_at,
            summary=summary,
            url=inc.get("url"),
            metric_type=metric_type,
            raw_payload=payload,
        )

    def is_ata_alert(self) -> bool:
        """Check if this AlertEvent belongs to ata."""
        return is_ata_alert_event(self)


def is_ata_alert_payload(payload: dict[str, Any]) -> bool:
    """Determine whether a raw Cloud Monitoring alert incident payload belongs to ata.

    Scoping criteria:
    - Policy display name prefix 'ata-'
    - Policy name containing 'ata-'
    - Incident summary containing 'ata-'
    - user_labels containing 'app': 'alert-triage-agent' or 'managed_by': 'alert-triage-agent'
    - Policy display name or name containing 'alert-triage-agent'
    """
    if not isinstance(payload, dict):
        return False
    inc = payload.get("incident", payload)
    if not isinstance(inc, dict):
        inc = {}

    pol_val = inc.get("policy") or payload.get("policy")
    pol_display = ""
    pol_name = ""
    if isinstance(pol_val, dict):
        pol_display = str(pol_val.get("displayName") or pol_val.get("display_name") or "")
        pol_name = str(pol_val.get("name") or "")
    elif isinstance(pol_val, str):
        pol_name = pol_val

    if not pol_name:
        pol_name = str(inc.get("policy_name") or payload.get("policy_name") or "")
    if not pol_display:
        pol_display = str(inc.get("policy_display_name") or payload.get("policy_display_name") or "")
    if not pol_display and isinstance(inc.get("condition"), dict):
        pol_display = str(inc.get("condition", {}).get("displayName") or inc.get("condition", {}).get("display_name") or "")

    p_disp = pol_display.lower().strip()
    p_name = pol_name.lower().strip()

    # 1. Policy display name prefix 'ata-'
    if p_disp.startswith("ata-"):
        return True

    # 2. Policy name containing 'ata-' or starting with 'ata-'
    if "ata-" in p_name:
        return True

    # Policy name / display name containing 'alert-triage-agent'
    if "alert-triage-agent" in p_disp or "alert-triage-agent" in p_name:
        return True

    # 3. Incident summary containing 'ata-'
    summary = str(inc.get("summary") or payload.get("summary") or "").lower()
    if "ata-" in summary:
        return True

    # 4. user_labels containing 'app': 'alert-triage-agent' or 'managed_by': 'alert-triage-agent'
    user_labels = (
        inc.get("policy_user_labels")
        or inc.get("policyUserLabels")
        or inc.get("user_labels")
        or inc.get("userLabels")
        or payload.get("policy_user_labels")
        or payload.get("policyUserLabels")
        or payload.get("user_labels")
        or payload.get("userLabels")
        or (pol_val.get("user_labels") if isinstance(pol_val, dict) else None)
        or (pol_val.get("userLabels") if isinstance(pol_val, dict) else None)
        or {}
    )
    if isinstance(user_labels, dict):
        if user_labels.get("app") == "alert-triage-agent" or user_labels.get("managed_by") == "alert-triage-agent":
            return True

    return False


def is_ata_alert_event(alert: AlertEvent) -> bool:
    """Check if an AlertEvent instance belongs to ata."""
    if alert.raw_payload and is_ata_alert_payload(alert.raw_payload):
        return True

    if getattr(alert, "policy_user_labels", None):
        if alert.policy_user_labels.get("app") == "alert-triage-agent" or alert.policy_user_labels.get("managed_by") == "alert-triage-agent":
            return True

    p_name = (alert.policy_name or "").lower().strip()
    if p_name.startswith("ata-") or "ata-" in p_name or "alert-triage-agent" in p_name:
        return True

    if alert.summary and "ata-" in alert.summary.lower():
        return True

    if alert.condition_name and "ata-" in alert.condition_name.lower():
        return True

    if alert.resource_labels:
        if alert.resource_labels.get("app") == "alert-triage-agent" or alert.resource_labels.get("managed_by") == "alert-triage-agent":
            return True

    return False


def is_ata_alert(alert_or_payload: Any) -> bool:
    """Universal scoping filter: checks both raw alert payload dict and AlertEvent instance."""
    if isinstance(alert_or_payload, dict):
        return is_ata_alert_payload(alert_or_payload)
    if hasattr(alert_or_payload, "policy_name"):
        return is_ata_alert_event(alert_or_payload)
    return False

