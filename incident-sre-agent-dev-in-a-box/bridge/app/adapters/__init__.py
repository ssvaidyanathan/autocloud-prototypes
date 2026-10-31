from typing import Optional

from ..config import BridgeConfig, get_settings
from .base import BaseAdapter, IncidentRef, parse_utc_datetime
from .generic import GenericAdapter
from .jira import JiraAdapter
from .servicenow import ServiceNowAdapter

_adapter_cache: dict[str, BaseAdapter] = {}


def get_adapter(source: str, config: Optional[BridgeConfig] = None) -> BaseAdapter:
    """Retrieve or instantiate an adapter instance for the specified ticketing source."""
    cfg = config or get_settings()
    src = source.lower().strip()

    if src == "servicenow":
        return ServiceNowAdapter(config=cfg)
    elif src == "jira":
        return JiraAdapter(config=cfg)
    elif src == "generic":
        return GenericAdapter(config=cfg)
    else:
        raise ValueError(
            f"Unsupported adapter source: '{source}'. Supported: servicenow, jira, generic"
        )


__all__ = [
    "BaseAdapter",
    "IncidentRef",
    "ServiceNowAdapter",
    "JiraAdapter",
    "GenericAdapter",
    "get_adapter",
    "parse_utc_datetime",
]
