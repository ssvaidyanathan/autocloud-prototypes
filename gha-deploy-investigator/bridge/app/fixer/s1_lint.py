"""Deterministic (non-LLM) fix generator for Python lint failures.

Diagnostics are parsed from linter output found in the report (annotations and
evidence excerpts). Each supported rule maps to a generic, AST-verified source
transformation. If any diagnostic in a file cannot be fixed safely, the whole
proposal degrades to ``no_safe_fix`` - partial fixes are never proposed because
they cannot turn the pipeline green.
"""

import ast
import difflib
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from ..contracts.incident import Incident
from ..contracts.report import FilePatch, FixProposal, Report
from .diff_apply import detect_crlf
from .interface import MAX_CHANGED_FILES, validate_fix_guardrails

# Rule identifiers follow pycodestyle / pyflakes numbering as reported by ruff and flake8.
RULE_UNUSED_IMPORT = "F401"
RULE_MULTIPLE_STATEMENTS = "E702"
RULE_IMPORT_NOT_AT_TOP = "E402"
SUPPORTED_RULES: frozenset[str] = frozenset(
    {RULE_UNUSED_IMPORT, RULE_MULTIPLE_STATEMENTS, RULE_IMPORT_NOT_AT_TOP}
)

_CODE = r"(?P<code>[A-Z]{1,3}\d{3,4})"
# Concise format: path/to/file.py:12:1: F401 [*] `os` imported but unused
_CONCISE_RE = re.compile(
    r"(?P<path>[\w./\-]+\.py):(?P<line>\d+):(?P<col>\d+):\s+" + _CODE + r"\s+(?P<msg>.*)"
)
# Full format header: "F401 [*] `os` imported but unused" followed by " --> path:12:1"
_FULL_HEADER_RE = re.compile(r"^\s*(?:error\[)?" + _CODE + r"\]?:?\s+(?P<msg>.*)$")
_FULL_LOCATION_RE = re.compile(r"^\s*-->\s*(?P<path>[\w./\-]+\.py):(?P<line>\d+):(?P<col>\d+)")
_ANNOTATION_CODE_RE = re.compile(r"\b" + _CODE + r"\b")
_DOTTED_NAME = r"\.*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
# Import names in diagnostic messages, most explicit first: ruff uses backticks, flake8 uses
# single quotes, some formatters use double quotes, and GitHub annotations may be unquoted.
_IMPORT_NAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"`(" + _DOTTED_NAME + r")`"),
    re.compile(r"'(" + _DOTTED_NAME + r")'"),
    re.compile(r'"(' + _DOTTED_NAME + r')"'),
    re.compile(r"(?<![\w.])(" + _DOTTED_NAME + r")\s+imported but unused\b"),
)


@dataclass(frozen=True)
class LintDiagnostic:
    path: str
    line: int
    code: str
    message: str


class UnsafeFixError(Exception):
    """Raised when a deterministic fix cannot be applied without risk."""


def _normalize_path(path: str) -> str:
    return re.sub(r"^\./", "", path.strip())


def parse_lint_diagnostics(text: str) -> list[LintDiagnostic]:
    """Parse ruff/flake8 diagnostics in concise or full output formats."""
    diagnostics: list[LintDiagnostic] = []
    pending: Optional[tuple[str, str]] = None
    for raw_line in text.splitlines():
        line = re.sub(r"^\S*Z\s+", "", raw_line)  # strip GitHub Actions log timestamps
        m = _CONCISE_RE.search(line)
        if m:
            diagnostics.append(
                LintDiagnostic(
                    path=_normalize_path(m.group("path")),
                    line=int(m.group("line")),
                    code=m.group("code"),
                    message=m.group("msg").strip(),
                )
            )
            pending = None
            continue
        m_loc = _FULL_LOCATION_RE.match(line)
        if m_loc and pending:
            diagnostics.append(
                LintDiagnostic(
                    path=_normalize_path(m_loc.group("path")),
                    line=int(m_loc.group("line")),
                    code=pending[0],
                    message=pending[1],
                )
            )
            pending = None
            continue
        m_head = _FULL_HEADER_RE.match(line)
        if m_head:
            pending = (m_head.group("code"), m_head.group("msg").strip())
    return diagnostics


def collect_diagnostics(report: Report) -> list[LintDiagnostic]:
    """Collect de-duplicated lint diagnostics from report annotations and evidence."""
    found: list[LintDiagnostic] = []
    for ann in report.annotations:
        text = " ".join(filter(None, [ann.title, ann.message]))
        m = _ANNOTATION_CODE_RE.search(text)
        if m:
            found.append(
                LintDiagnostic(
                    path=_normalize_path(ann.path),
                    line=ann.start_line,
                    code=m.group("code"),
                    message=text,
                )
            )
        if ann.raw_details:
            found.extend(parse_lint_diagnostics(ann.raw_details))
    for items in report.evidence_by_area.values():
        for item in items:
            found.extend(parse_lint_diagnostics(item.excerpt or ""))

    seen: set[tuple[str, int, str]] = set()
    unique: list[LintDiagnostic] = []
    for diag in found:
        key = (diag.path, diag.line, diag.code)
        if key not in seen:
            seen.add(key)
            unique.append(diag)
    return unique


# --------------------------------------------------------------------------- AST helpers


def _iter_bodies(tree: ast.AST) -> Iterable[list[ast.stmt]]:
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                yield body
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                yield handler.body


def _import_at_line(tree: ast.Module, line: int) -> Optional[ast.Import | ast.ImportFrom]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = node.end_lineno or node.lineno
            if node.lineno <= line <= end:
                return node
    return None


def _alias_matches(node: ast.Import | ast.ImportFrom, alias: ast.alias, name: str) -> bool:
    candidates = {alias.name, alias.asname or alias.name}
    if isinstance(node, ast.ImportFrom):
        module = "." * node.level + (node.module or "")
        candidates.add(f"{module}.{alias.name}" if node.module else f"{module}{alias.name}")
    return name in candidates


def extract_import_names(message: str) -> list[str]:
    """Return candidate import names mentioned in a diagnostic message, in priority order."""
    names: list[str] = []
    for pattern in _IMPORT_NAME_RES:
        for name in pattern.findall(message):
            if name not in names:
                names.append(name)
    return names


def _resolve_unused_name(node: ast.Import | ast.ImportFrom, message: str) -> Optional[str]:
    """Pick the import binding a diagnostic refers to.

    A name from the message is used when it matches an alias of the import statement.
    Only when the message names no import at all, and the statement binds exactly one
    (non-star) name, is that binding taken from the AST. A message name that contradicts
    the statement is never overridden by a guess; the caller treats it as unsafe.
    """
    names = extract_import_names(message)
    for name in names:
        if any(_alias_matches(node, alias, name) for alias in node.names):
            return name
    if not names and len(node.names) == 1 and node.names[0].name != "*":
        return node.names[0].name
    return None


def _render_import(node: ast.Import | ast.ImportFrom, indent: str) -> str:
    return indent + ast.unparse(node)


def _replace_lines(lines: list[str], start: int, end: int, new: list[str]) -> list[str]:
    """Replace 1-based inclusive line range [start, end] with new lines."""
    return lines[: start - 1] + new + lines[end:]


def _find_unique_import(tree: ast.Module, key: str) -> Optional[ast.Import | ast.ImportFrom]:
    matches = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom)) and ast.dump(n) == key
    ]
    if len(matches) > 1:
        raise UnsafeFixError("Ambiguous duplicate import statements")
    return matches[0] if matches else None


# --------------------------------------------------------------------------- rule fixers


def _split_multiple_statements(lines: list[str], tree: ast.Module, line_no: int) -> list[str]:
    """Split simple statements joined by ';' on one physical line onto separate lines."""
    on_line: list[ast.stmt] = []
    for body in _iter_bodies(tree):
        stmts = [s for s in body if s.lineno == line_no]
        if len(stmts) > 1:
            on_line = stmts
            break
    if not on_line:
        raise UnsafeFixError(f"No multi-statement line found at line {line_no}")
    if any((s.end_lineno or s.lineno) != line_no for s in on_line):
        raise UnsafeFixError(f"Multi-line compound statement at line {line_no}")

    source_line = lines[line_no - 1]
    indent = source_line[: len(source_line) - len(source_line.lstrip())]
    segments = [source_line[s.col_offset : s.end_col_offset] for s in on_line]
    remainder = source_line[on_line[-1].end_col_offset :].strip()
    if remainder.startswith(";"):
        remainder = remainder[1:].strip()
    if remainder and not remainder.startswith("#"):
        raise UnsafeFixError(f"Unrecognised trailing content at line {line_no}")
    if source_line[: on_line[0].col_offset].strip():
        raise UnsafeFixError(f"Statement shares line with a compound header at line {line_no}")

    new_lines = [indent + seg for seg in segments]
    if remainder:
        new_lines[-1] = f"{new_lines[-1]}  {remainder}"
    return _replace_lines(lines, line_no, line_no, new_lines)


def _remove_unused_alias(
    lines: list[str], tree: ast.Module, key: str, name: str
) -> tuple[list[str], Optional[str]]:
    """Remove an unused alias from the import identified by key. Returns new lines and new key."""
    node = _find_unique_import(tree, key)
    if node is None:
        raise UnsafeFixError(f"Import binding '{name}' not found")
    if any(a.name == "*" for a in node.names):
        raise UnsafeFixError("Star imports are not fixed deterministically")
    remaining = [a for a in node.names if not _alias_matches(node, a, name)]
    if len(remaining) == len(node.names):
        raise UnsafeFixError(f"Import binding '{name}' not found in statement")

    start, end = node.lineno, node.end_lineno or node.lineno
    first = lines[start - 1]
    indent = first[: len(first) - len(first.lstrip())]
    if first[: node.col_offset].strip() or lines[end - 1][node.end_col_offset :].strip(" \t;"):
        raise UnsafeFixError("Import shares its line with other code")

    if remaining:
        node.names = remaining
        return _replace_lines(lines, start, end, [_render_import(node, indent)]), ast.dump(node)

    for body in _iter_bodies(tree):
        if node in body and len(body) == 1:
            raise UnsafeFixError("Removing import would leave an empty block")
    return _replace_lines(lines, start, end, []), None


def _hoist_import(lines: list[str], tree: ast.Module, key: str) -> list[str]:
    """Move a module-level import up to the end of the leading import block."""
    node = _find_unique_import(tree, key)
    if node is None:
        return lines  # removed by an unused-import fix: offending import already undone
    if node not in tree.body:
        raise UnsafeFixError("Only module-level imports can be relocated")
    start, end = node.lineno, node.end_lineno or node.lineno
    if lines[start - 1][: node.col_offset].strip() or lines[end - 1][node.end_col_offset :].strip(
        " \t;"
    ):
        raise UnsafeFixError("Import shares its line with other code")

    insert_after = 0
    for idx, stmt in enumerate(tree.body):
        is_docstring = (
            idx == 0
            and isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
        if stmt is node:
            break
        if is_docstring or isinstance(stmt, (ast.Import, ast.ImportFrom)):
            insert_after = stmt.end_lineno or stmt.lineno
            continue
        break
    if insert_after >= start:
        raise UnsafeFixError("Import already within leading import block")

    moved = lines[start - 1 : end]
    without = _replace_lines(lines, start, end, [])
    return without[:insert_after] + moved + without[insert_after:]


def fix_source(source: str, diagnostics: list[LintDiagnostic]) -> str:
    """Apply deterministic fixes for diagnostics in a single file. Raises UnsafeFixError."""
    unsupported = sorted({d.code for d in diagnostics} - SUPPORTED_RULES)
    if unsupported:
        raise UnsafeFixError(f"Unsupported lint rules: {', '.join(unsupported)}")

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise UnsafeFixError(f"Source does not parse: {exc}") from exc

    trailing_newline = source.endswith("\n")
    lines = source.splitlines()
    # Preserve CRLF using the same rule as ``apply_unified_diff`` so content and diff agree.
    eol = "\r\n" if detect_crlf(source) else "\n"

    # Resolve position-independent targets against the original tree.
    unused: list[tuple[str, str]] = []
    for diag in diagnostics:
        if diag.code != RULE_UNUSED_IMPORT:
            continue
        node = _import_at_line(tree, diag.line)
        name = _resolve_unused_name(node, diag.message) if node is not None else None
        if node is None or name is None:
            raise UnsafeFixError(f"Cannot locate unused import at line {diag.line}")
        unused.append((ast.dump(node), name))

    hoist_keys: list[str] = []
    for diag in diagnostics:
        if diag.code != RULE_IMPORT_NOT_AT_TOP:
            continue
        node = _import_at_line(tree, diag.line)
        if node is None:
            raise UnsafeFixError(f"Cannot locate late import at line {diag.line}")
        hoist_keys.append(ast.dump(node))

    # 1. Split joined statements, bottom-up so earlier line numbers stay valid.
    split_lines = sorted(
        {d.line for d in diagnostics if d.code == RULE_MULTIPLE_STATEMENTS}, reverse=True
    )
    for line_no in split_lines:
        lines = _split_multiple_statements(lines, tree, line_no)
    if split_lines:
        tree = ast.parse("\n".join(lines))

    # 2. Remove unused import bindings (this also undoes late unused imports outright).
    for idx in range(len(unused)):
        key, name = unused[idx]
        if not key:
            raise UnsafeFixError(f"Import binding '{name}' already removed")
        lines, new_key = _remove_unused_alias(lines, tree, key, name)
        tree = ast.parse("\n".join(lines))
        # The edited statement has a new identity (or none, if fully removed).
        replacement = new_key or ""
        unused = [(replacement if k == key else k, n) for k, n in unused]
        hoist_keys = [replacement if k == key else k for k in hoist_keys]

    # 3. Relocate remaining late imports to the leading import block.
    for key in hoist_keys:
        if not key:
            continue
        lines = _hoist_import(lines, tree, key)
        tree = ast.parse("\n".join(lines))

    fixed = eol.join(lines) + (eol if trailing_newline else "")
    try:
        ast.parse(fixed)
    except SyntaxError as exc:  # pragma: no cover - defensive
        raise UnsafeFixError(f"Fixed source does not parse: {exc}") from exc
    return fixed


def _unified_diff(path: str, before: str, after: str) -> str:
    """Generate a unified diff that round-trips through ``apply_unified_diff``."""
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )


def _no_safe_fix(reason: str, base: str) -> FixProposal:
    return FixProposal(
        status="no_safe_fix",
        strategy="deterministic_linter",
        rationale=reason,
        patches=[],
        target_branch_base=base,
    )


def generate_lint_fix(
    incident: Incident,
    report: Report,
    fetch_file: Callable[[str], Optional[str]],
    diagnostics: Optional[list[LintDiagnostic]] = None,
) -> FixProposal:
    """Build a deterministic lint FixProposal.

    ``fetch_file`` returns the file content at the failing commit (or None if missing).
    """
    base = incident.github_branch or ""
    diags = diagnostics if diagnostics is not None else collect_diagnostics(report)
    if not diags:
        return _no_safe_fix("No lint diagnostics found in investigation evidence.", base)

    by_path: dict[str, list[LintDiagnostic]] = {}
    for diag in diags:
        by_path.setdefault(diag.path, []).append(diag)

    if len(by_path) > MAX_CHANGED_FILES:
        return _no_safe_fix(
            f"Lint failures span {len(by_path)} files, exceeding limit of {MAX_CHANGED_FILES}.",
            base,
        )

    patches: list[FilePatch] = []
    summaries: list[str] = []
    for path in sorted(by_path):
        if not path.endswith(".py"):
            return _no_safe_fix(f"Unsupported file type for deterministic fix: {path}", base)
        original = fetch_file(path)
        if original is None:
            return _no_safe_fix(f"Could not read source for {path}.", base)
        try:
            fixed = fix_source(original, by_path[path])
        except UnsafeFixError as exc:
            return _no_safe_fix(f"{path}: {exc}", base)
        if fixed == original:
            return _no_safe_fix(f"{path}: deterministic fix produced no change.", base)
        patches.append(
            FilePatch(
                path=path,
                action="modify",
                content=fixed,
                diff=_unified_diff(path, original, fixed),
            )
        )
        codes = ", ".join(sorted({d.code for d in by_path[path]}))
        summaries.append(f"{path} ({codes})")

    proposal = FixProposal(
        status="proposed",
        strategy="deterministic_linter",
        rationale="Deterministic lint remediation for " + "; ".join(summaries) + ".",
        patches=patches,
        target_branch_base=base,
    )
    valid, reason = validate_fix_guardrails(proposal)
    if not valid:
        return _no_safe_fix(reason, base)
    return proposal


__all__ = [
    "LintDiagnostic",
    "SUPPORTED_RULES",
    "UnsafeFixError",
    "collect_diagnostics",
    "extract_import_names",
    "fix_source",
    "generate_lint_fix",
    "parse_lint_diagnostics",
]
