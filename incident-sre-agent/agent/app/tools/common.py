"""Common helper utilities for SRE agent tools and specialists."""

import os
from typing import Optional


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
