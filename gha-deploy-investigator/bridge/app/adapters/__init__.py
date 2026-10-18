from typing import Optional

from ..config import BridgeConfig, get_settings
from .base import BaseAdapter, IncidentRef
from .github import GitHubAdapter
from .servicenow import ServiceNowAdapter


def get_adapter(source: str, config: Optional[BridgeConfig] = None) -> BaseAdapter:
    """Factory to get the appropriate adapter for a platform source."""
    cfg = config or get_settings()
    src = source.lower().strip()
    if src == "github":
        return GitHubAdapter(config=cfg)
    elif src == "servicenow":
        return ServiceNowAdapter(config=cfg)
    else:
        raise ValueError(
            f"Unsupported adapter source: '{source}'. Supported: 'github', 'servicenow'"
        )


__all__ = [
    "BaseAdapter",
    "GitHubAdapter",
    "IncidentRef",
    "ServiceNowAdapter",
    "get_adapter",
]
