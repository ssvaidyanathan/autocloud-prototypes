"""LLM patch extraction and pre-flight validation (auto-fix strategy ``llm_patch``).

The synthesizer LLM may emit an optional ``fix_proposal`` object alongside its
root-cause diagnosis (see ``synthesizer.patch_instruction`` in ``agent.yaml``).
This module converts that untrusted output into a :class:`FixProposal` and runs
pre-flight safety validation. It never writes to any repository: it only
produces a proposal that the bridge-side fixer may later execute after its own
guardrail check.

Any doubt resolves to ``status="no_safe_fix"`` with a diagnostic rationale.
There is no fallback patch generation.

Accepted LLM shapes for ``fix_proposal``:

* ``{"status": "proposed", "confidence": 0.9, "rationale": "...",
  "patches": [{"path": "...", "action": "modify", "diff": "<unified diff>"}]}``
* ``{"confidence": 0.9, "rationale": "...", "diff": "<multi-file unified diff>"}``
* ``{"status": "no_safe_fix", "rationale": "..."}``
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .contracts.report import FilePatch, FixProposal

logger = logging.getLogger(__name__)

# Guardrail constants. These mirror bridge/app/fixer/interface.py; the agent and
# bridge are separately deployed, so the agent keeps its own copy for pre-flight.
MIN_PATCH_CONFIDENCE: float = 0.85
MAX_CHANGED_FILES: int = 3
MAX_LINE_DELTA: int = 50
FORBIDDEN_PATH_PATTERNS: tuple[str, ...] = (
    r"^\.github/.*",
    r".*\.env.*",
    r".*credentials.*",
    r".*id_rsa.*",
    r".*\.pem$",
)
ALLOWED_ACTIONS: frozenset[str] = frozenset({"modify", "create"})

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_STRATEGY = "llm_patch"


class PatchValidationError(ValueError):
    """Raised when an LLM patch fails structural or safety validation."""


def _no_safe_fix(reason: str, target_branch_base: str) -> FixProposal:
    return FixProposal(
        status="no_safe_fix",
        strategy=_STRATEGY,
        rationale=reason,
        patches=[],
        target_branch_base=target_branch_base,
    )


def _strip_diff_path(raw: str) -> Optional[str]:
    """Return a repo-relative path from a ``---``/``+++`` header value."""
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def validate_path(path: str) -> None:
    """Reject empty, absolute, traversal, or forbidden paths."""
    if not path or not path.strip():
        raise PatchValidationError("Patch has an empty file path")
    if path.startswith("/") or path.startswith("\\") or re.match(r"^[A-Za-z]:", path):
        raise PatchValidationError(f"Patch path '{path}' must be repository-relative")
    parts = re.split(r"[\\/]", path)
    if ".." in parts:
        raise PatchValidationError(f"Patch path '{path}' contains directory traversal")
    for pattern in FORBIDDEN_PATH_PATTERNS:
        if re.search(pattern, path):
            raise PatchValidationError(
                f"Patch touches forbidden path '{path}' (matches '{pattern}')"
            )


def _is_file_header(lines: list[str], idx: int) -> bool:
    """True if ``lines[idx]`` starts a new file section of a multi-file diff."""
    line = lines[idx]
    if line.startswith("diff --git "):
        return True
    return line.startswith("--- ") and idx + 1 < len(lines) and lines[idx + 1].startswith("+++ ")


def _header_counts_fit(lines: list[str], idx: int, old_left: int, new_left: int) -> bool:
    """True if the declared counts consume the hunk body exactly as a strict parser would.

    Such headers are left untouched, so diffs that are already valid (including ones
    whose removed/added lines resemble ``---``/``+++`` file headers) never change.
    """
    while idx < len(lines) and (old_left > 0 or new_left > 0):
        body = lines[idx].rstrip("\r")
        if body.startswith("\\"):
            idx += 1
            continue
        if body.startswith("+"):
            new_left -= 1
        elif body.startswith("-"):
            old_left -= 1
        elif body.startswith(" ") or body == "":
            old_left -= 1
            new_left -= 1
        else:
            return False
        idx += 1
    if old_left != 0 or new_left != 0:
        return False
    while idx < len(lines) and (lines[idx].startswith("\\") or not lines[idx].strip()):
        idx += 1
    return (
        idx == len(lines) or bool(_HUNK_HEADER_RE.match(lines[idx])) or _is_file_header(lines, idx)
    )


def _count_hunk_body(body: list[str]) -> Optional[tuple[int, int]]:
    """Return actual ``(old, new)`` line counts of a hunk body, or None if uncountable.

    Trailing empty lines are treated as separator whitespace rather than context.
    Returns None for an empty body or any line with an invalid prefix, so the strict
    validator still rejects it.
    """
    lines = [line.rstrip("\r") for line in body]
    while lines and lines[-1] == "":
        lines.pop()
    old = new = 0
    counted = False
    for line in lines:
        if line.startswith("\\"):  # "\ No newline at end of file" is not a hunk line
            continue
        if line.startswith("+"):
            new += 1
        elif line.startswith("-"):
            old += 1
        elif line.startswith(" ") or line == "":
            old += 1
            new += 1
        else:
            return None
        counted = True
    return (old, new) if counted else None


def reconcile_hunk_headers(diff: str) -> str:
    """Rewrite hunk header line counts to match the actual hunk bodies.

    LLM-generated diffs often miscount ``@@ -a,b +c,d @@`` header counts. This is a
    deterministic normalisation: only the counts in a header are changed; start
    lines, trailing headings, and every body line are preserved verbatim. Headers
    that already fit their body, and hunks whose body is empty or contains an
    invalid line, are left unchanged so strict validation can still reject them.
    """
    lines = diff.split("\n")
    idx = 0
    while idx < len(lines):
        match = _HUNK_HEADER_RE.match(lines[idx])
        if not match:
            idx += 1
            continue
        header_idx = idx
        idx += 1
        declared_old = int(match.group(2)) if match.group(2) is not None else 1
        declared_new = int(match.group(4)) if match.group(4) is not None else 1
        if _header_counts_fit(lines, idx, declared_old, declared_new):
            continue
        end = idx
        while end < len(lines) and not (
            _HUNK_HEADER_RE.match(lines[end]) or _is_file_header(lines, end)
        ):
            end += 1
        counts = _count_hunk_body(lines[idx:end])
        if counts is None:
            continue
        old_header = lines[header_idx]
        heading = old_header[match.end() :]
        new_header = f"@@ -{match.group(1)},{counts[0]} +{match.group(3)},{counts[1]} @@{heading}"
        if new_header != old_header:
            lines[header_idx] = new_header
            logger.info("Normalised hunk header from %s to %s", old_header, new_header)
    return "\n".join(lines)


def validate_unified_diff(diff: str) -> tuple[Optional[str], Optional[str], int]:
    """Validate a single-file unified diff.

    Returns ``(old_path, new_path, line_delta)``. ``line_delta`` counts added
    plus removed lines. Raises :class:`PatchValidationError` on any structural
    problem (missing headers, missing hunks, hunk line counts not matching).
    """
    lines = diff.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].startswith("--- "):
        if lines[idx].startswith(("@@", "+++ ")):
            raise PatchValidationError("Unified diff hunk appears before '---' header")
        idx += 1
    if idx >= len(lines):
        raise PatchValidationError("Unified diff is missing '---' file header")
    old_path = _strip_diff_path(lines[idx][4:])
    idx += 1
    if idx >= len(lines) or not lines[idx].startswith("+++ "):
        raise PatchValidationError("Unified diff is missing '+++' file header")
    new_path = _strip_diff_path(lines[idx][4:])
    idx += 1

    hunks = 0
    delta = 0
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("--- "):
            raise PatchValidationError("Unified diff contains more than one file section")
        match = _HUNK_HEADER_RE.match(line)
        if not match:
            if not line.strip():
                idx += 1
                continue
            raise PatchValidationError(f"Unexpected line outside hunk: {line[:80]!r}")
        hunks += 1
        old_left = int(match.group(2)) if match.group(2) is not None else 1
        new_left = int(match.group(4)) if match.group(4) is not None else 1
        idx += 1
        while idx < len(lines) and (old_left > 0 or new_left > 0):
            body = lines[idx]
            if body.startswith("\\"):  # "\ No newline at end of file"
                idx += 1
                continue
            if body.startswith("+"):
                new_left -= 1
                delta += 1
            elif body.startswith("-"):
                old_left -= 1
                delta += 1
            elif body.startswith(" ") or body == "":
                old_left -= 1
                new_left -= 1
            else:
                raise PatchValidationError(f"Invalid hunk body line: {body[:80]!r}")
            idx += 1
        if old_left != 0 or new_left != 0:
            raise PatchValidationError("Hunk line counts do not match hunk header")
        while idx < len(lines) and lines[idx].startswith("\\"):
            idx += 1

    if hunks == 0:
        raise PatchValidationError("Unified diff contains no hunks")
    return old_path, new_path, delta


def split_multi_file_diff(diff: str) -> list[str]:
    """Split a multi-file unified diff into per-file diff sections."""
    sections: list[list[str]] = []
    current: list[str] = []
    lines = diff.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        is_file_header = (
            line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ ")
        )
        if is_file_header and any(_HUNK_HEADER_RE.match(c) for c in current):
            sections.append(current)
            current = []
        if line.startswith("diff --git ") or line.startswith("index "):
            i += 1
            continue
        current.append(line)
        i += 1
    if current:
        sections.append(current)
    return ["\n".join(s) for s in sections if any(x.strip() for x in s)]


def _patch_from_diff(diff: str, declared_path: Optional[str], action: str) -> tuple[FilePatch, int]:
    diff = reconcile_hunk_headers(diff)
    old_path, new_path, delta = validate_unified_diff(diff)
    header_path = new_path or old_path
    if header_path is None:
        raise PatchValidationError("Unified diff headers reference no file")
    if declared_path and header_path != declared_path:
        raise PatchValidationError(
            f"Declared path '{declared_path}' does not match diff header path '{header_path}'"
        )
    if old_path and new_path and old_path != new_path:
        raise PatchValidationError(
            f"Diff renames '{old_path}' to '{new_path}'; renames unsupported"
        )
    if new_path is None:
        raise PatchValidationError(f"Diff deletes '{old_path}'; file deletion is unsupported")
    if old_path is None and action != "create":
        action = "create"
    validate_path(header_path)
    return FilePatch(path=header_path, action=action, diff=diff), delta


def _normalise_patch(raw: Any) -> tuple[FilePatch, int]:
    if not isinstance(raw, dict):
        raise PatchValidationError("Each patch entry must be an object")
    path = raw.get("path")
    path = str(path).strip() if path is not None else None
    action = str(raw.get("action") or "modify").strip().lower()
    if action not in ALLOWED_ACTIONS:
        raise PatchValidationError(
            f"Patch action '{action}' is not allowed (allowed: {sorted(ALLOWED_ACTIONS)})"
        )
    diff = raw.get("diff")
    content = raw.get("content")

    if isinstance(diff, str) and diff.strip():
        return _patch_from_diff(diff, path, action)

    if action == "create" and isinstance(content, str):
        validate_path(path or "")
        delta = len(content.splitlines())
        return FilePatch(path=path or "", action="create", content=content), delta

    raise PatchValidationError(
        f"Patch for '{path}' must provide a unified 'diff' "
        "(full-content replacement is only accepted for new files)"
    )


def _coerce_confidence(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return None
    if conf != conf or conf < 0.0 or conf > 1.0:  # NaN or out of range
        return None
    return conf


def _parse_raw(raw: Any) -> Any:
    if isinstance(raw, str):
        text = raw.strip()
        text = re.sub(r"^```(?:json|diff)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise PatchValidationError(f"fix_proposal is not valid JSON: {exc}") from exc
        # A bare unified diff carries no confidence; treated downstream.
        return {"diff": text}
    return raw


def extract_fix_proposal(raw: Any, target_branch_base: Optional[str]) -> FixProposal:
    """Convert raw synthesizer ``fix_proposal`` output into a validated FixProposal.

    Never raises: every failure becomes ``status="no_safe_fix"`` with rationale.
    """
    base = (target_branch_base or "").strip()
    if not base:
        return _no_safe_fix("No target branch base is known for the failing run", "")

    if raw is None:
        return _no_safe_fix("Synthesizer did not propose a patch", base)

    try:
        data = _parse_raw(raw)
    except PatchValidationError as exc:
        return _no_safe_fix(str(exc), base)

    if not isinstance(data, dict):
        return _no_safe_fix("fix_proposal must be a JSON object or unified diff", base)

    llm_rationale = str(data.get("rationale") or "").strip()
    declared_status = str(data.get("status") or "proposed").strip().lower()
    if declared_status != "proposed":
        reason = llm_rationale or "Synthesizer declined to propose a patch"
        return _no_safe_fix(f"Synthesizer reported '{declared_status}': {reason}", base)

    confidence = _coerce_confidence(data.get("confidence"))
    if confidence is None:
        return _no_safe_fix("Patch confidence is missing or invalid", base)
    if confidence < MIN_PATCH_CONFIDENCE:
        return _no_safe_fix(
            f"Patch confidence {confidence:.2f} is below threshold {MIN_PATCH_CONFIDENCE:.2f}",
            base,
        )

    raw_patches = data.get("patches")
    raw_diff = data.get("diff")
    try:
        entries: list[tuple[FilePatch, int]] = []
        if raw_patches:
            if not isinstance(raw_patches, list):
                raise PatchValidationError("'patches' must be a list")
            entries = [_normalise_patch(p) for p in raw_patches]
        elif isinstance(raw_diff, str) and raw_diff.strip():
            entries = [
                _patch_from_diff(section, None, "modify")
                for section in split_multi_file_diff(raw_diff)
            ]
        if not entries:
            raise PatchValidationError("fix_proposal contains no patches")

        paths = [p.path for p, _ in entries]
        if len(set(paths)) != len(paths):
            raise PatchValidationError("fix_proposal patches the same file more than once")
        if len(entries) > MAX_CHANGED_FILES:
            raise PatchValidationError(
                f"Patch touches {len(entries)} files, exceeding limit of {MAX_CHANGED_FILES}"
            )
        total_delta = sum(d for _, d in entries)
        if total_delta > MAX_LINE_DELTA:
            raise PatchValidationError(
                f"Total diff delta ({total_delta} lines) exceeds maximum allowed "
                f"({MAX_LINE_DELTA})"
            )
        if total_delta == 0:
            raise PatchValidationError("fix_proposal changes no lines")
    except PatchValidationError as exc:
        return _no_safe_fix(str(exc), base)

    return FixProposal(
        status="proposed",
        strategy=_STRATEGY,
        rationale=llm_rationale or "LLM-generated patch passed pre-flight validation",
        patches=[p for p, _ in entries],
        target_branch_base=base,
    )


__all__ = [
    "ALLOWED_ACTIONS",
    "FORBIDDEN_PATH_PATTERNS",
    "MAX_CHANGED_FILES",
    "MAX_LINE_DELTA",
    "MIN_PATCH_CONFIDENCE",
    "PatchValidationError",
    "extract_fix_proposal",
    "reconcile_hunk_headers",
    "split_multi_file_diff",
    "validate_path",
    "validate_unified_diff",
]
