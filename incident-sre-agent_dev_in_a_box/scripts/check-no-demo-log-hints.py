#!/usr/bin/env python3
"""AST static guard check to ensure demo/shop-api/shop_app.py contains zero
occurrences of demo log hints ('Scenario', 'inject', 'leak', 'omitting') inside
any logger.* call arguments or string literals passed to logging.

Production services must emit realistic operational telemetry, not self-diagnosing
log confessions that bypass authentic SRE investigation and platform reasoning.
"""

import argparse
import ast
import sys
from pathlib import Path
from typing import List, Optional, Tuple

FORBIDDEN_LOG_HINTS = [
    "scenario",
    "inject",
    "leak",
    "omitting",
]

LOG_METHODS = {
    "debug",
    "info",
    "warning",
    "warn",
    "error",
    "critical",
    "fatal",
    "exception",
    "log",
}


def is_logger_call(node: ast.Call) -> bool:
    """Determine whether an AST Call node represents a logger invocation."""
    func = node.func
    if isinstance(func, ast.Attribute):
        attr_name = func.attr.lower()
        # Direct calls on a logger object: logger.info(...), app_logger.warning(...), etc.
        val = func.value
        if isinstance(val, ast.Name):
            val_id = val.id.lower()
            if val_id in ("logger", "log", "logging") or "logger" in val_id:
                if attr_name in LOG_METHODS or val_id == "logger":
                    return True
        # Attribute chain: self.logger.info(...), app.logger.error(...)
        elif isinstance(val, ast.Attribute):
            if val.attr.lower() in ("logger", "log"):
                if attr_name in LOG_METHODS:
                    return True
    return False


def find_log_call_violations(
    call_node: ast.Call, forbidden_terms: Optional[List[str]] = None
) -> List[Tuple[int, str]]:
    """Inspect arguments and keywords of a logger call for forbidden hints."""
    if forbidden_terms is None:
        forbidden_terms = FORBIDDEN_LOG_HINTS

    violations: List[Tuple[int, str]] = []
    arg_nodes: List[ast.AST] = list(call_node.args) + [
        kw.value for kw in call_node.keywords if kw.value is not None
    ]

    for arg in arg_nodes:
        arg_lineno = getattr(arg, "lineno", call_node.lineno)
        arg_text = ast.unparse(arg)
        arg_text_lower = arg_text.lower()

        # 1. Check string constants inside the argument subtree
        flagged_hints = set()
        for inner in ast.walk(arg):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                inner_val_lower = inner.value.lower()
                inner_lineno = getattr(inner, "lineno", arg_lineno)
                for hint in forbidden_terms:
                    if hint in inner_val_lower:
                        flagged_hints.add(hint)
                        violations.append(
                            (
                                inner_lineno,
                                f"Forbidden demo log hint '{hint}' in string literal: "
                                f"{inner.value!r}",
                            )
                        )

        # 2. Check unparsed argument expression for hints not already flagged
        for hint in forbidden_terms:
            if hint in arg_text_lower and hint not in flagged_hints:
                violations.append(
                    (
                        arg_lineno,
                        f"Forbidden demo log hint '{hint}' in log argument expression: {arg_text}",
                    )
                )

    return violations


def check_source_ast(
    source_code: str,
    filename: str = "<string>",
    forbidden_terms: Optional[List[str]] = None,
) -> List[Tuple[int, str]]:
    """Parse source code AST and return all forbidden log hint violations."""
    violations: List[Tuple[int, str]] = []
    tree = ast.parse(source_code, filename=filename)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and is_logger_call(node):
            call_violations = find_log_call_violations(node, forbidden_terms=forbidden_terms)
            violations.extend(call_violations)

    violations.sort(key=lambda v: v[0])
    return violations


def check_file_ast(
    file_path: Path, forbidden_terms: Optional[List[str]] = None
) -> List[Tuple[int, str]]:
    """Scan file AST for forbidden demo log hints in logger calls."""
    content = file_path.read_text(encoding="utf-8")
    return check_source_ast(content, filename=str(file_path), forbidden_terms=forbidden_terms)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify zero demo log hints in application logging calls."
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Target files to check (default: demo/shop-api/shop_app.py)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    targets: List[Path] = args.files or [repo_root / "demo" / "shop-api" / "shop_app.py"]

    print("========================================================")
    print("Checking for demo log hints in application logging...")
    print("========================================================")

    all_violations: List[str] = []

    for path in targets:
        if not path.exists():
            all_violations.append(f"Target file does not exist: {path}")
            continue

        try:
            rel_path = path.relative_to(repo_root)
        except ValueError:
            rel_path = path

        violations = check_file_ast(path)
        for lineno, msg in violations:
            all_violations.append(f"{rel_path}:{lineno}: {msg}")

    if all_violations:
        print(
            "ERROR: Forbidden demo log hints found in application logging:",
            file=sys.stderr,
        )
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "\nApplication logs must emit realistic operational telemetry, not self-diagnosing "
            "strings containing 'Scenario', 'inject', 'leak', or 'omitting'.",
            file=sys.stderr,
        )
        return 1

    print(f"[PASS] Zero demo log hints found across {len(targets)} target file(s).")
    print("SUCCESS: Demo log hints guard check passed cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
