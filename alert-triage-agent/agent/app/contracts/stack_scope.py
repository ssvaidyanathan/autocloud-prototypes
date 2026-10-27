"""Stack isolation: which alerts belong to this deployment (NAME_PREFIX).

Byte-identical copies live in agent/, bridge/ and tools/ app/contracts/ (tests enforce it).

Rule: an alert is in scope iff
  1. a policy display name or a resource-derived component name starts with f"{NAME_PREFIX}-", and
  2. when the policy carries a `stack` user label, that label equals NAME_PREFIX.
A mismatching `stack` label is rejected and logged at WARNING with both values. The shared
`app=alert-triage-agent` label is set by every stack, so it never puts an alert in scope.
Alerts without a `stack` label are accepted on the name check alone (transition: policies
get the label on their next Terraform apply).
"""

import logging
import re
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

# Same rule as the Terraform `name_prefix` validation: ^[a-z][a-z0-9]{1,9}$
NAME_PREFIX_PATTERN = r"[a-z][a-z0-9]{1,9}"
STACK_LABEL = "stack"


def validate_name_prefix(value: Any) -> str:
    """Return the prefix unchanged, or raise ValueError when it breaks the Terraform rule."""
    if not isinstance(value, str) or not re.fullmatch(NAME_PREFIX_PATTERN, value):
        raise ValueError(
            f"NAME_PREFIX {value!r} is invalid: it must match ^{NAME_PREFIX_PATTERN}$ "
            "(the Terraform name_prefix rule)"
        )
    return value


def stack_label_of(labels: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The `stack` user label, or None when the label is absent."""
    if isinstance(labels, Mapping) and STACK_LABEL in labels and labels[STACK_LABEL] is not None:
        return str(labels[STACK_LABEL])
    return None


def name_has_prefix(name: Any, name_prefix: str) -> bool:
    """True when the name (or its last '/' or ':' segment) starts with f"{name_prefix}-"."""
    if not name:
        return False
    text = str(name).strip().lower()
    want = f"{validate_name_prefix(name_prefix)}-"
    last = re.split(r"[/:]", text)[-1]
    return text.startswith(want) or last.startswith(want)


def in_stack_scope(
    name_prefix: str,
    names: Iterable[Any],
    stack_label: Optional[str],
    alert_ref: str = "",
) -> bool:
    """Apply the stack isolation rule (see module docstring)."""
    prefix = validate_name_prefix(name_prefix)
    if stack_label is not None and stack_label.strip().lower() != prefix:
        logger.warning(
            "Rejecting alert %s: policy label stack=%r does not match NAME_PREFIX=%r",
            alert_ref or "<unknown>",
            stack_label,
            prefix,
        )
        return False
    return any(name_has_prefix(n, prefix) for n in names)
