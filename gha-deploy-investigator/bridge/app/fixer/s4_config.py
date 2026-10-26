"""Deterministic deploy-config fixer for memory exhaustion (strategy ``config_tuning``).

Given a synthesized :class:`Report` for a deploy-stage failure whose evidence
indicates memory exhaustion (exit code 137 / OOM / memory limit exceeded) and
the current contents of candidate repository files, this module produces a
:class:`FixProposal` that raises the declared memory allocation.

Safety model:

* Only two line shapes are ever edited, and only the numeric memory value in
  them changes:
    - shell/env variable assignments whose name contains ``MEM``
      (``NAME=<n>Mi``, ``export NAME="<n>Gi"``, ``NAME="${NAME:-<n>Mi}"``)
    - YAML/config key-value pairs whose key contains ``memory``
      (``memory: <n>Mi``)
* Lines carrying shell control or command syntax (command substitution, pipes,
  chaining, redirection, flags) are never edited, so executable commands are
  never rewritten.
* Pipeline definitions (GitHub workflows, Jenkinsfile, Cloud Build, GitLab CI,
  etc.) and paths in ``FORBIDDEN_PATH_PATTERNS`` are never edited.
* New value is ``min(current * 2, max_memory_mib)``; values already at or above
  the cap yield ``no_safe_fix``.
* The final proposal must pass :func:`validate_fix_guardrails`.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Mapping, Optional

from ..contracts.report import FilePatch, FixProposal, Report
from .interface import FORBIDDEN_PATH_PATTERNS, validate_fix_guardrails

STRATEGY = "config_tuning"
DEFAULT_MAX_MEMORY_MIB: int = 2048
MIN_MEMORY_MIB: int = 128
DEPLOY_STAGES: frozenset[str] = frozenset({"deploy", "iac_config"})
# File extensions treated as configuration (deploy scripts, manifests, settings files).
CONFIG_FILE_EXTENSIONS: tuple[str, ...] = (
    "sh",
    "bash",
    "yml",
    "yaml",
    "json",
    "toml",
    "tf",
    "tfvars",
    "conf",
    "cfg",
    "ini",
    "properties",
)

OOM_SIGNAL_RE = re.compile(
    r"exit(?:ed)?\s*(?:with\s*)?(?:status\s*|code\s*)?[:=]?\s*137\b"
    r"|\bOOM(?:Killed)?\b"
    r"|out[\s-]of[\s-]memory"
    r"|memory\s+limit\s+of\s+\S+\s+exceeded"
    r"|\bsignal\s*9\b|\bSIGKILL\b",
    re.IGNORECASE,
)

PIPELINE_DEFINITION_PATTERNS: tuple[str, ...] = (
    r"(^|/)\.github/",
    r"(^|/)\.gitlab-ci\.ya?ml$",
    r"(^|/)\.circleci/",
    r"(^|/)Jenkinsfile[^/]*$",
    r"(^|/)azure-pipelines[^/]*\.ya?ml$",
    r"(^|/)bitbucket-pipelines\.ya?ml$",
    r"(^|/)cloudbuild[^/]*\.ya?ml$",
    r"(^|/)\.buildkite/",
    r"(^|/)\.drone\.ya?ml$",
    r"(^|/)Makefile$",
)

_VALUE = r"(?P<q>[\"']?)(?P<num>\d+)(?P<unit>Mi|Gi)(?P=q)"
_TRAILER = r"(?P<trail>\s*(?:#.*)?)$"
_ENV_ASSIGN_RE = re.compile(
    r"^(?P<lead>\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=)" + _VALUE + _TRAILER
)
# Self-defaulting shell assignment: NAME="${NAME:-<n>Mi}" (only the default value changes).
_ENV_DEFAULT_RE = re.compile(
    r"^(?P<lead>\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<q>[\"']?)"
    r"\$\{(?P=name):?-)(?P<num>\d+)(?P<unit>Mi|Gi)\}(?P=q)" + _TRAILER
)
_YAML_KV_RE = re.compile(
    r"^(?P<lead>\s*(?:-\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)\s*:\s*)" + _VALUE + _TRAILER
)
# Anything resembling execution or chaining disqualifies a line.
_UNSAFE_LINE_RE = re.compile(r"\$\(|`|;|&&|\|\||\||>|<|\beval\b|\bexec\b|\s--?[A-Za-z]")


@dataclass(frozen=True)
class MemoryEdit:
    path: str
    line_no: int
    old_line: str
    new_line: str
    old_mib: int
    new_mib: int


def _to_mib(num: int, unit: str) -> int:
    return num * 1024 if unit == "Gi" else num


def _format(mib: int, unit: str) -> str:
    if unit == "Gi":
        if mib % 1024 != 0:
            return f"{mib}Mi"
        return f"{mib // 1024}Gi"
    return f"{mib}Mi"


def is_oom_failure(report: Report) -> bool:
    """Return True if the report describes a deploy-stage memory exhaustion failure."""
    stage = str(report.metadata.get("failing_stage") or "").strip().lower()
    if stage not in DEPLOY_STAGES:
        return False
    texts: list[str] = [report.summary]
    if report.probable_root_cause:
        texts.append(report.probable_root_cause.text)
    texts.extend(report.causal_remediation)
    for items in report.evidence_by_area.values():
        texts.extend(item.excerpt for item in items if item.excerpt)
    return any(OOM_SIGNAL_RE.search(t) for t in texts if t)


def is_editable_path(path: str) -> bool:
    """Return False for forbidden paths and CI/CD pipeline definitions."""
    if not path or path.startswith("/") or ".." in re.split(r"[\\/]", path):
        return False
    for pattern in FORBIDDEN_PATH_PATTERNS + PIPELINE_DEFINITION_PATTERNS:
        if re.search(pattern, path):
            return False
    return True


def _tune_line(line: str, max_mib: int) -> Optional[tuple[str, int, int]]:
    """Return (new_line, old_mib, new_mib) for a safe memory line, else None."""
    match = _ENV_ASSIGN_RE.match(line) or _ENV_DEFAULT_RE.match(line)
    if match and "MEM" not in match.group("name").upper():
        match = None
    if match is None:
        match = _YAML_KV_RE.match(line)
        if match and "memory" not in match.group("name").lower():
            match = None
    if match is None:
        return None
    body = line[: match.start("trail")]
    if _UNSAFE_LINE_RE.search(body):
        return None

    unit = match.group("unit")
    old_mib = _to_mib(int(match.group("num")), unit)
    if old_mib < MIN_MEMORY_MIB or old_mib >= max_mib:
        return None
    new_mib = min(old_mib * 2, max_mib)
    head, tail = line[: match.start("num")], line[match.end("unit") :]
    new_line = f"{head}{_format(new_mib, unit)}{tail}"
    return new_line, old_mib, new_mib


def find_memory_edits(
    files: Mapping[str, str], max_memory_mib: int = DEFAULT_MAX_MEMORY_MIB
) -> list[MemoryEdit]:
    """Scan editable files for tunable memory settings."""
    edits: list[MemoryEdit] = []
    for path in sorted(files):
        if not is_editable_path(path):
            continue
        for idx, line in enumerate(files[path].splitlines()):
            tuned = _tune_line(line, max_memory_mib)
            if tuned:
                new_line, old_mib, new_mib = tuned
                edits.append(MemoryEdit(path, idx + 1, line, new_line, old_mib, new_mib))
    return edits


def _apply(content: str, edits: list[MemoryEdit]) -> str:
    lines = content.splitlines(keepends=True)
    for edit in edits:
        original = lines[edit.line_no - 1]
        ending = original[len(original.rstrip("\r\n")) :]
        lines[edit.line_no - 1] = edit.new_line + ending
    return "".join(lines)


def _unified_diff(path: str, before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )


def _result(status: str, rationale: str, base: str, patches: Optional[list[FilePatch]] = None):
    return FixProposal(
        status=status,  # type: ignore[arg-type]
        strategy=STRATEGY,
        rationale=rationale,
        patches=patches or [],
        target_branch_base=base,
    )


def generate_memory_config_fix(
    report: Report,
    files: Mapping[str, str],
    target_branch_base: str,
    max_memory_mib: int = DEFAULT_MAX_MEMORY_MIB,
) -> FixProposal:
    """Produce a memory-tuning FixProposal for a deploy OOM failure."""
    if not is_oom_failure(report):
        return _result(
            "unsupported",
            "Report does not describe a deploy-stage memory exhaustion failure",
            target_branch_base,
        )

    edits = find_memory_edits(files, max_memory_mib)
    if not edits:
        return _result(
            "no_safe_fix",
            "No safely tunable memory setting below the "
            f"{max_memory_mib}Mi cap was found in editable configuration files",
            target_branch_base,
        )

    patches: list[FilePatch] = []
    by_path: dict[str, list[MemoryEdit]] = {}
    for edit in edits:
        by_path.setdefault(edit.path, []).append(edit)
    for path, path_edits in by_path.items():
        before = files[path]
        after = _apply(before, path_edits)
        patches.append(
            FilePatch(
                path=path,
                action="modify",
                content=after,
                diff=_unified_diff(path, before, after),
            )
        )

    changes = ", ".join(f"{e.path}:{e.line_no} {e.old_mib}Mi -> {e.new_mib}Mi" for e in edits)
    proposal = _result(
        "proposed",
        f"Deploy failed with memory exhaustion; raising memory allocation ({changes})",
        target_branch_base,
        patches,
    )
    ok, reason = validate_fix_guardrails(proposal)
    if not ok:
        return _result("no_safe_fix", f"Guardrail check failed: {reason}", target_branch_base)
    return proposal


__all__ = [
    "CONFIG_FILE_EXTENSIONS",
    "DEFAULT_MAX_MEMORY_MIB",
    "MemoryEdit",
    "find_memory_edits",
    "generate_memory_config_fix",
    "is_editable_path",
    "is_oom_failure",
]
