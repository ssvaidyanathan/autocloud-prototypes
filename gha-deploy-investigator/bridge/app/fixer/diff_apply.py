"""Strict single-file unified diff application.

Used by the fix PR writer to turn diff-only patches (as emitted by the agent's LLM
patch extractor) into the full file content committed through the GitHub contents API.

The applier is deliberately strict: every context and removed line must match the
original exactly (a trailing ``\\r`` on the original is tolerated). When a hunk's
declared line number is wrong, the hunk is relocated only if its old-side lines occur
at exactly one later position; ambiguous or missing matches raise ``ValueError``.
"""

import re
from dataclasses import dataclass, field

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NO_EOL_MARKER = "\\"
_HEADER_PREFIXES: tuple[str, ...] = (
    "diff ",
    "index ",
    "--- ",
    "+++ ",
    "new file mode",
    "old mode",
    "new mode",
)


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_count: int
    # (kind, text) with kind in {" ", "-", "+"}
    lines: list[tuple[str, str]] = field(default_factory=list)
    old_no_eol: bool = False
    new_no_eol: bool = False

    @property
    def old_lines(self) -> list[str]:
        return [text for kind, text in self.lines if kind != "+"]


def _parse_hunks(diff: str) -> list[_Hunk]:
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    idx = 0
    while idx < len(lines) and not lines[idx].startswith("@@"):
        line = lines[idx]
        if line.strip() and not line.startswith(_HEADER_PREFIXES):
            raise ValueError(f"Unexpected line before first hunk: {line[:80]!r}")
        idx += 1

    hunks: list[_Hunk] = []
    while idx < len(lines):
        line = lines[idx]
        if not line.strip():
            idx += 1
            continue
        match = _HUNK_HEADER_RE.match(line)
        if not match:
            if line.startswith("--- "):
                raise ValueError("Diff contains more than one file section")
            raise ValueError(f"Unexpected line outside hunk: {line[:80]!r}")
        hunk = _Hunk(
            old_start=int(match.group(1)),
            old_count=int(match.group(2)) if match.group(2) is not None else 1,
            new_count=int(match.group(4)) if match.group(4) is not None else 1,
        )
        old_left, new_left = hunk.old_count, hunk.new_count
        idx += 1
        while idx < len(lines) and (old_left > 0 or new_left > 0):
            body = lines[idx]
            if body.startswith(_NO_EOL_MARKER):
                idx += 1
                continue
            kind, text = (body[0], body[1:]) if body else (" ", "")
            if kind == "+":
                new_left -= 1
            elif kind == "-":
                old_left -= 1
            elif kind == " ":
                old_left -= 1
                new_left -= 1
            else:
                raise ValueError(f"Invalid hunk body line: {body[:80]!r}")
            hunk.lines.append((kind, text))
            idx += 1
            # A marker directly after a line applies to that line's side(s).
            if idx < len(lines) and lines[idx].startswith(_NO_EOL_MARKER):
                if kind in (" ", "-"):
                    hunk.old_no_eol = True
                if kind in (" ", "+"):
                    hunk.new_no_eol = True
                idx += 1
        if old_left != 0 or new_left != 0:
            raise ValueError("Hunk line counts do not match hunk header")
        hunks.append(hunk)

    if not hunks:
        raise ValueError("Diff contains no hunks")
    return hunks


def _matches(original: list[str], start: int, expected: list[str]) -> bool:
    if start < 0 or start + len(expected) > len(original):
        return False
    return all(
        orig == exp or orig.rstrip("\r") == exp
        for orig, exp in zip(original[start : start + len(expected)], expected)
    )


def _locate(original: list[str], hunk: _Hunk, pos: int) -> int:
    """Return the 0-based index where the hunk's old side begins."""
    declared = hunk.old_start - 1 if hunk.old_count > 0 else hunk.old_start
    expected = hunk.old_lines
    if declared >= pos and _matches(original, declared, expected):
        return declared
    if not expected:
        raise ValueError(f"Insertion hunk at line {hunk.old_start} is out of range")
    candidates = [i for i in range(pos, len(original)) if _matches(original, i, expected)]
    if len(candidates) != 1:
        problem = "not found" if not candidates else "ambiguous"
        raise ValueError(f"Hunk at line {hunk.old_start}: context {problem} in original file")
    return candidates[0]


def apply_unified_diff(original: str, diff: str) -> str:
    """Apply a single-file unified diff to ``original`` and return the new content.

    Raises ``ValueError`` if the diff is malformed or does not apply cleanly.
    """
    hunks = _parse_hunks(diff)

    if original == "":
        orig_lines: list[str] = []
        ends_with_eol = True
    else:
        orig_lines = original.split("\n")
        ends_with_eol = orig_lines[-1] == ""
        if ends_with_eol:
            orig_lines.pop()
    crlf = bool(orig_lines) and all(line.endswith("\r") for line in orig_lines)

    out: list[str] = []
    pos = 0
    result_eol = ends_with_eol
    for hunk in hunks:
        start = _locate(orig_lines, hunk, pos)
        out.extend(orig_lines[pos:start])
        cursor = start
        for kind, text in hunk.lines:
            if kind == " ":
                out.append(orig_lines[cursor])
                cursor += 1
            elif kind == "-":
                cursor += 1
            else:
                out.append(text + "\r" if crlf else text)
        pos = cursor
        if pos == len(orig_lines):
            if hunk.old_no_eol and ends_with_eol:
                raise ValueError("Diff expects no trailing newline but original has one")
            if hunk.new_no_eol:
                result_eol = False
            elif hunk.old_no_eol:
                result_eol = True
        elif hunk.old_no_eol or hunk.new_no_eol:
            raise ValueError("'No newline at end of file' marker before end of file")
    out.extend(orig_lines[pos:])

    if not out:
        return ""
    return "\n".join(out) + ("\n" if result_eol else "")


__all__ = ["apply_unified_diff"]
