"""Deterministic hunk-scoped revert of the failing commit (strategy ``commit_hunk_revert``).

When the diagnosis blames a configuration or build file that the failing commit changed,
this fixer reverts only the offending diff hunk(s) of that file by inverting them. Every
other file and hunk of the commit is left untouched.

Inputs are plain data so the module stays pure and testable:

* ``commit``: the GitHub REST commit object (``GET /repos/{owner}/{repo}/commits/{sha}``),
  whose ``files[].patch`` carries the per-file hunks.
* ``diagnosis_texts``: what the report blames (summary, root cause, causal remediation).
  A file is a target only if one of these texts names it (or a report annotation does).
* ``evidence_texts``: everything that may quote the offending lines (diagnosis texts,
  annotation messages, log/telemetry evidence). Evidence that merely reproduces the commit
  diff must be excluded by the caller, otherwise every changed line would look "referenced".

Claim rules (anything else returns ``None`` so later fixers can claim the failure):

* Target files are configuration/build files (see :func:`is_config_or_build_path`) that
  the commit modified in place and that the diagnosis names. Added, removed, renamed and
  copied files, merge commits, and files whose patch is absent or truncated are never
  targets.
* Offending hunks are hunks whose added/removed lines are quoted by the evidence, or whose
  new-side line range overlaps a report annotation on that file. If none is referenced and
  the commit has exactly one target file with exactly one hunk, that hunk is used.
  Otherwise the target is ambiguous and nothing is claimed.

Inversion swaps ``+``/``-`` body lines, keeps context lines and ``\\ No newline at end of
file`` markers attached to their line, transposes the header ranges and drops any trailing
heading text. The inverted diff is exact against the failing commit; the writer applies it
with :func:`~.diff_apply.apply_unified_diff` against the fix branch base and the incident is
handed off if it does not apply. The proposal must pass :func:`validate_fix_guardrails`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from ..contracts.report import CheckRunAnnotation, FilePatch, FixProposal
from .interface import normalize_patch_path, validate_fix_guardrails
from .s4_config import CONFIG_FILE_EXTENSIONS, is_editable_path

STRATEGY = "commit_hunk_revert"

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NO_EOL_PREFIX = "\\"
# Container image build definitions (``Dockerfile``, ``Containerfile.dev``, ``app.dockerfile``).
_BUILD_DEFINITION_RE = re.compile(
    r"(?:^|/)(?:docker|container)file(?:[.-][\w.-]+)?$|\.(?:docker|container)file$",
    re.IGNORECASE,
)
_CONFIG_EXTENSION_RE = re.compile(
    r"\.(?:" + "|".join(re.escape(ext) for ext in CONFIG_FILE_EXTENSIONS) + r")$",
    re.IGNORECASE,
)
# A changed line must carry at least this many non-space characters (and one alphanumeric)
# before a textual match in the evidence counts as a reference; ``fi`` or ``}`` never does.
MIN_REFERENCE_CHARS: int = 4
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class DiffHunk:
    """One parsed unified diff hunk.

    ``lines`` holds ``(kind, text, no_eol)`` tuples where ``kind`` is ``" "``, ``"-"`` or
    ``"+"`` and ``no_eol`` records a ``\\ No newline at end of file`` marker after the line.
    """

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str, bool]] = field(default_factory=list)

    @property
    def changed_lines(self) -> list[str]:
        return [text for kind, text, _ in self.lines if kind != " "]

    @property
    def new_range(self) -> tuple[int, int]:
        """Inclusive new-side line range (an empty side covers the line it follows)."""
        if self.new_count == 0:
            return self.new_start, self.new_start
        return self.new_start, self.new_start + self.new_count - 1


def parse_hunks(patch: str) -> list[DiffHunk]:
    """Parse a GitHub ``files[].patch`` value (hunks only, no file headers).

    Raises ``ValueError`` if the text is not a complete, well-formed sequence of hunks,
    which is how a truncated patch presents.
    """
    lines = patch.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    hunks: list[DiffHunk] = []
    idx = 0
    while idx < len(lines):
        match = _HUNK_HEADER_RE.match(lines[idx])
        if not match:
            raise ValueError(f"Expected hunk header, got {lines[idx][:80]!r}")
        hunk = DiffHunk(
            old_start=int(match.group(1)),
            old_count=int(match.group(2)) if match.group(2) is not None else 1,
            new_start=int(match.group(3)),
            new_count=int(match.group(4)) if match.group(4) is not None else 1,
        )
        old_left, new_left = hunk.old_count, hunk.new_count
        idx += 1
        while idx < len(lines) and (old_left > 0 or new_left > 0):
            body = lines[idx]
            kind, text = body[:1], body[1:]
            if kind == "+":
                new_left -= 1
            elif kind == "-":
                old_left -= 1
            elif kind == " ":
                old_left -= 1
                new_left -= 1
            else:
                raise ValueError(f"Invalid hunk body line: {body[:80]!r}")
            idx += 1
            no_eol = idx < len(lines) and lines[idx].startswith(_NO_EOL_PREFIX)
            if no_eol:
                idx += 1
            hunk.lines.append((kind, text, no_eol))
        if old_left != 0 or new_left != 0:
            raise ValueError("Hunk body is shorter than its header declares (truncated patch)")
        hunks.append(hunk)
    if not hunks:
        raise ValueError("Patch contains no hunks")
    return hunks


def _first_line(start: int, count: int) -> int:
    """1-based position of the first line a side covers (an empty side follows ``start``)."""
    return start if count > 0 else start + 1


def _header_start(first_line: int, count: int) -> int:
    return first_line if count > 0 else first_line - 1


def _swap_body(lines: list[tuple[str, str, bool]]) -> list[tuple[str, str, bool]]:
    """Swap ``+``/``-`` and keep each change run in conventional removed-then-added order."""
    swapped = [({"+": "-", "-": "+"}.get(kind, kind), text, no_eol) for kind, text, no_eol in lines]
    out: list[tuple[str, str, bool]] = []
    run: list[tuple[str, str, bool]] = []
    for line in swapped + [(" ", "", False)]:
        if line[0] == " ":
            out.extend(item for item in run if item[0] == "-")
            out.extend(item for item in run if item[0] == "+")
            run = []
            out.append(line)
        else:
            run.append(line)
    return out[:-1]


def invert_hunks(hunks: Sequence[DiffHunk], selected: Iterable[int]) -> list[DiffHunk]:
    """Invert the selected hunks so that applying them to the new file reverts them.

    The inverted old side is the original new side, so it is exact against the failing
    commit. The inverted new side start accounts only for the hunks being reverted, so
    reverting a subset still yields consistent line numbers.
    """
    chosen = sorted(set(selected))
    inverted: list[DiffHunk] = []
    offset = 0
    for index in chosen:
        hunk = hunks[index]
        result_first = _first_line(hunk.new_start, hunk.new_count) + offset
        inverted.append(
            DiffHunk(
                old_start=hunk.new_start,
                old_count=hunk.new_count,
                new_start=_header_start(result_first, hunk.old_count),
                new_count=hunk.old_count,
                lines=_swap_body(hunk.lines),
            )
        )
        offset += hunk.old_count - hunk.new_count
    return inverted


def render_hunks(path: str, hunks: Sequence[DiffHunk]) -> str:
    """Render hunks as a single-file unified diff with no hunk headings."""
    out = [f"--- a/{path}", f"+++ b/{path}"]
    for hunk in hunks:
        out.append(f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@")
        for kind, text, no_eol in hunk.lines:
            out.append(f"{kind}{text}")
            if no_eol:
                out.append("\\ No newline at end of file")
    return "\n".join(out) + "\n"


def is_config_or_build_path(path: str) -> bool:
    """True for editable configuration files and container image build definitions."""
    if not is_editable_path(path):
        return False
    return bool(_CONFIG_EXTENSION_RE.search(path) or _BUILD_DEFINITION_RE.search(path))


def _normalise_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _mentions_path(text: str, path: str) -> bool:
    pattern = r"(?<![\w./-])(?:\./)?" + re.escape(path) + r"(?!\.?[\w/-])"
    return re.search(pattern, text) is not None


def _is_referenced_line(line: str, haystacks: Sequence[str]) -> bool:
    needle = _normalise_text(line)
    if len(needle.replace(" ", "")) < MIN_REFERENCE_CHARS or not re.search(r"\w", needle):
        return False
    return any(needle in hay for hay in haystacks)


@dataclass(frozen=True)
class _ChangedFile:
    path: str
    hunks: list[DiffHunk]


def _modified_files(commit: dict[str, Any]) -> Optional[list[_ChangedFile]]:
    """Return config/build files the commit modified in place with a complete patch.

    Returns None when the commit cannot be inverted safely at all (merge commit, missing
    file list). Files that cannot be inverted are omitted.
    """
    parents = commit.get("parents")
    if isinstance(parents, list) and len(parents) > 1:
        return None
    files = commit.get("files")
    if not isinstance(files, list):
        return None
    out: list[_ChangedFile] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path, _ = normalize_patch_path(entry.get("filename"))
        if path is None or not is_config_or_build_path(path):
            continue
        if entry.get("status") != "modified" or entry.get("previous_filename"):
            continue
        patch = entry.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            continue
        try:
            hunks = parse_hunks(patch)
        except ValueError:
            continue
        out.append(_ChangedFile(path=path, hunks=hunks))
    return out


def _named_by_diagnosis(
    changed: _ChangedFile,
    all_paths: Sequence[str],
    diagnosis_texts: Sequence[str],
    annotations: Sequence[CheckRunAnnotation],
) -> bool:
    if any(_annotation_path(a) == changed.path for a in annotations):
        return True
    if any(_mentions_path(text, changed.path) for text in diagnosis_texts):
        return True
    basename = changed.path.rsplit("/", 1)[-1]
    if basename == changed.path:
        return False
    unique = sum(1 for p in all_paths if p.rsplit("/", 1)[-1] == basename) == 1
    return unique and any(_mentions_path(text, basename) for text in diagnosis_texts)


def _annotation_path(annotation: CheckRunAnnotation) -> str:
    return annotation.path.strip().removeprefix("./")


def _referenced_hunks(
    changed: _ChangedFile,
    haystacks: Sequence[str],
    annotations: Sequence[CheckRunAnnotation],
) -> list[int]:
    ranges = [
        (a.start_line, a.end_line) for a in annotations if _annotation_path(a) == changed.path
    ]
    picked: list[int] = []
    for index, hunk in enumerate(changed.hunks):
        first, last = hunk.new_range
        by_annotation = any(start <= last and end >= first for start, end in ranges)
        by_text = any(_is_referenced_line(line, haystacks) for line in hunk.changed_lines)
        if by_annotation or by_text:
            picked.append(index)
    return picked


def _result(status: str, rationale: str, base: str, patches: Optional[list[FilePatch]] = None):
    return FixProposal(
        status=status,  # type: ignore[arg-type]
        strategy=STRATEGY,
        rationale=rationale,
        patches=patches or [],
        target_branch_base=base,
    )


def _describe(path: str, hunks: Sequence[DiffHunk]) -> str:
    spans = []
    for hunk in hunks:
        first, last = hunk.old_start, hunk.old_start + max(hunk.old_count, 1) - 1
        spans.append(f"{first}" if first == last else f"{first}-{last}")
    return f"{path} (lines {', '.join(spans)})"


def generate_commit_hunk_revert(
    commit: Optional[dict[str, Any]],
    diagnosis_texts: Sequence[str],
    evidence_texts: Sequence[str],
    annotations: Sequence[CheckRunAnnotation],
    target_branch_base: str,
) -> Optional[FixProposal]:
    """Build a hunk-scoped revert proposal, or None when the fixer does not claim the failure."""
    if not isinstance(commit, dict):
        return None
    changed_files = _modified_files(commit)
    if not changed_files:
        return None
    all_paths = [entry.path for entry in changed_files]
    diagnosis = [t for t in diagnosis_texts if t]
    targets = [
        c for c in changed_files if _named_by_diagnosis(c, all_paths, diagnosis, annotations)
    ]
    if not targets:
        return None

    haystacks = [_normalise_text(t) for t in list(diagnosis) + list(evidence_texts) if t]
    selections: list[tuple[_ChangedFile, list[int]]] = []
    for target in targets:
        picked = _referenced_hunks(target, haystacks, annotations)
        if not picked and len(targets) == 1 and len(target.hunks) == 1:
            picked = [0]
        if not picked:
            return None  # ambiguous: the diagnosis names the file but not which change
        selections.append((target, picked))

    sha = str(commit.get("sha") or "")[:7] or "unknown"
    patches: list[FilePatch] = []
    described: list[str] = []
    for target, picked in selections:
        inverted = invert_hunks(target.hunks, picked)
        patches.append(
            FilePatch(path=target.path, action="modify", diff=render_hunks(target.path, inverted))
        )
        described.append(_describe(target.path, inverted))

    proposal = _result(
        "proposed",
        f"Reverting the hunk(s) of commit {sha} named by the diagnosis: {'; '.join(described)}",
        target_branch_base,
        patches,
    )
    ok, reason = validate_fix_guardrails(proposal)
    if not ok:
        return _result("no_safe_fix", f"Guardrail check failed: {reason}", target_branch_base)
    return proposal


__all__ = [
    "DiffHunk",
    "MIN_REFERENCE_CHARS",
    "STRATEGY",
    "generate_commit_hunk_revert",
    "invert_hunks",
    "is_config_or_build_path",
    "parse_hunks",
    "render_hunks",
]
