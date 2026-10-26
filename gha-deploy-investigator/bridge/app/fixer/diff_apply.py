"""Strict single-file unified diff application.

Used by the fix PR writer to turn diff-only patches (as emitted by the agent's LLM
patch extractor) into the full file content committed through the GitHub contents API.

The applier is deliberately strict: every context and removed line must match the
original exactly (a trailing ``\\r`` on the original is tolerated). When a hunk's
declared line number is wrong, the hunk is relocated only if its old-side lines occur
at exactly one later position; ambiguous or missing matches raise ``ValueError``.
"""

import logging
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

logger = logging.getLogger(__name__)


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
        if body.startswith(_NO_EOL_MARKER):
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
    while idx < len(lines) and (lines[idx].startswith(_NO_EOL_MARKER) or not lines[idx].strip()):
        idx += 1
    return (
        idx == len(lines) or bool(_HUNK_HEADER_RE.match(lines[idx])) or _is_file_header(lines, idx)
    )


def _count_hunk_body(body: list[str]) -> tuple[int, int] | None:
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
        if line.startswith(_NO_EOL_MARKER):  # "\ No newline at end of file" is not a hunk line
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
    # A diff ending in "\n" splits into a trailing "" that is not a hunk line; left in
    # place it would be counted as blank context and mask a surplus of one.
    trailing_nl = bool(lines) and lines[-1] == ""
    if trailing_nl:
        lines.pop()
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
        if declared_old > 0 and counts[0] == 0:
            # Old count 0 means "insert after line N" while > 0 means "start at line N";
            # rewriting the count alone would shift the insertion point by one line.
            continue
        old_header = lines[header_idx]
        heading = old_header[match.end() :]
        new_header = f"@@ -{match.group(1)},{counts[0]} +{match.group(3)},{counts[1]} @@{heading}"
        if new_header != old_header:
            lines[header_idx] = new_header
            logger.info("Normalised hunk header from %s to %s", old_header, new_header)
    return "\n".join(lines) + ("\n" if trailing_nl else "")


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


def detect_crlf(text: str) -> bool:
    """Return True if every newline-terminated line of ``text`` ends with ``\\r``.

    Unterminated content after the last newline is ignored, so a CRLF file without a
    trailing newline is still detected as CRLF. Text with no newline is not CRLF.
    """
    if "\n" not in text:
        return False
    lines = text.split("\n")
    # The final element is either "" (trailing newline) or unterminated content.
    lines.pop()
    return bool(lines) and all(line.endswith("\r") for line in lines)


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

    Hunk header line counts are first reconciled with the hunk bodies (see
    :func:`reconcile_hunk_headers`). Raises ``ValueError`` if the diff is malformed
    or does not apply cleanly.
    """
    hunks = _parse_hunks(reconcile_hunk_headers(diff))

    if original == "":
        orig_lines: list[str] = []
        ends_with_eol = True
    else:
        orig_lines = original.split("\n")
        ends_with_eol = orig_lines[-1] == ""
        if ends_with_eol:
            orig_lines.pop()
    crlf = detect_crlf(original)

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
    if crlf:
        # Every terminated line ends with CRLF. The original's unterminated last line
        # has no ``\r`` and needs one if it is no longer last; an unterminated final
        # output line carries no line ending at all.
        out = [line if line.endswith("\r") else line + "\r" for line in out]
        if not result_eol:
            out[-1] = out[-1][:-1]
    return "\n".join(out) + ("\n" if result_eol else "")


__all__ = ["apply_unified_diff", "detect_crlf", "reconcile_hunk_headers"]
