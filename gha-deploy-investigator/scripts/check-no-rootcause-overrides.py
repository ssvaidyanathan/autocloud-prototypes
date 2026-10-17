#!/usr/bin/env python3
"""AST static guard check to ensure no post-LLM root cause template overrides,
regex incident classifiers, or keyword-based remediation overrides exist in
synthesizer.py, dispatcher.py, or specialists.

Rules:
1. ProbableRootCause must not be constructed or assigned with canned templates or f-strings
   outside the explicitly labeled fallback branch (starting with
   'Automated fallback (LLM unavailable):').
2. report.probable_root_cause.text must not be assigned f-strings or canned template strings.
3. Neither synthesizer.py nor dispatcher.py may contain regex incident
   classifiers selecting templates.
4. Remediation actions must not be appended, inserted, or overridden based on keyword matches
   or hardcoded role/permission templates.
"""

import ast
import re
import sys
from pathlib import Path
from typing import List, Tuple

FORBIDDEN_CANNED_SUBSTRINGS = [
    "Container memory exhaustion",
    "Deployment / configuration regression",
    "missing mandatory configuration variable",
    "Downstream dependency failure",
    "Database connection pool exhaustion",
    "Elevated request latency",
    "Regression introduced by recent change",
]

FORBIDDEN_REMEDIATION_SUBSTRINGS = [
    "roles/iam",
    "serviceaccounts.actas",
    "serviceaccountuser",
    "actas",
    "cache_prewarm",
    "256mi",
    "memory=",
    "pre-warm",
    "cache-warmup",
]

LABELED_FALLBACK_PREFIX = "Automated fallback (LLM unavailable):"


def get_constant_string(node: ast.AST) -> str:
    """Extract constant string value from AST node if possible."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def is_labeled_fallback_fstring(node: ast.JoinedStr) -> bool:
    """Check if an f-string starts with the required fallback prefix."""
    if node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value.strip().startswith(LABELED_FALLBACK_PREFIX)
    return False


def check_file_ast(file_path: Path) -> List[Tuple[int, str]]:
    """Scan file AST for forbidden ProbableRootCause constructions or assignments."""
    violations: List[Tuple[int, str]] = []

    content = file_path.read_text(encoding="utf-8")
    tree = ast.parse(content, filename=str(file_path))

    for node in ast.walk(tree):
        # 1. Check ProbableRootCause(...) constructor calls
        if isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr

            if func_name == "ProbableRootCause":
                for kw in node.keywords:
                    if kw.arg == "text":
                        if isinstance(kw.value, ast.JoinedStr):
                            if not is_labeled_fallback_fstring(kw.value):
                                violations.append(
                                    (
                                        kw.value.lineno,
                                        "ProbableRootCause constructed with f-string outside "
                                        f"labeled fallback: {ast.unparse(kw.value)}",
                                    )
                                )
                        elif isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                            val = kw.value.value
                            for canned in FORBIDDEN_CANNED_SUBSTRINGS:
                                if canned.lower() in val.lower():
                                    violations.append(
                                        (
                                            kw.value.lineno,
                                            "ProbableRootCause constructed with canned template: "
                                            f"'{canned}'",
                                        )
                                    )

        # 2. Check assignments to *.probable_root_cause.text
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = None
            if isinstance(node, ast.Assign) and node.targets:
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target

            if isinstance(target, ast.Attribute) and target.attr == "text":
                val_node = node.value
                if val_node is not None:
                    if isinstance(val_node, ast.JoinedStr):
                        violations.append(
                            (
                                val_node.lineno,
                                "Direct f-string assignment to probable_root_cause.text: "
                                f"{ast.unparse(val_node)}",
                            )
                        )
                    elif isinstance(val_node, ast.Constant) and isinstance(val_node.value, str):
                        for canned in FORBIDDEN_CANNED_SUBSTRINGS:
                            if canned.lower() in val_node.value.lower():
                                violations.append(
                                    (
                                        val_node.lineno,
                                        "Direct canned template assignment to "
                                        f"probable_root_cause.text: '{canned}'",
                                    )
                                )

    return violations


def check_no_remediation_overrides(file_path: Path) -> List[Tuple[int, str]]:
    """Scan file AST for keyword-matched remediation injection or overrides."""
    violations: List[Tuple[int, str]] = []
    content = file_path.read_text(encoding="utf-8")
    tree = ast.parse(content, filename=str(file_path))

    for node in ast.walk(tree):
        # Check calls to .append() or .insert() on remediation lists
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method_name = node.func.attr
            if method_name in ("append", "insert"):
                # Check target object name
                caller_name = ""
                if isinstance(node.func.value, ast.Name):
                    caller_name = node.func.value.id
                elif isinstance(node.func.value, ast.Attribute):
                    caller_name = node.func.value.attr

                if any(k in caller_name.lower() for k in ("remediation", "recommended_actions")):
                    # Inspect all string constants in argument list
                    for arg in node.args:
                        for inner in ast.walk(arg):
                            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                                val_lower = inner.value.lower()
                                for forbidden in FORBIDDEN_REMEDIATION_SUBSTRINGS:
                                    if forbidden in val_lower:
                                        violations.append(
                                            (
                                                inner.lineno,
                                                "Hardcoded remediation override matching forbidden "
                                                f"pattern '{forbidden}': {inner.value!r}",
                                            )
                                        )

    return violations


def check_no_regex_incident_classifiers(file_path: Path) -> List[Tuple[int, str]]:
    """Scan file text for regex incident-type classifiers and keyword override flags."""
    violations: List[Tuple[int, str]] = []
    forbidden_classifier_patterns = [
        (r"\bis_memory_saturation\b", "Regex incident classifier 'is_memory_saturation'"),
        (r"\bis_config_regression\b", "Regex incident classifier 'is_config_regression'"),
        (r"\bis_dependency_failure\b", "Regex incident classifier 'is_dependency_failure'"),
        (r"\bis_db_conn_exhaustion\b", "Regex incident classifier 'is_db_conn_exhaustion'"),
        (r"\bis_latency_prominent\b", "Regex incident classifier 'is_latency_prominent'"),
        (r"\bhas_iam_actas\b", "Keyword remediation flag 'has_iam_actas'"),
        (r"\bhas_iam_or_actas\b", "Keyword remediation flag 'has_iam_or_actas'"),
    ]

    content = file_path.read_text(encoding="utf-8")
    for i, line in enumerate(content.splitlines(), start=1):
        for pattern, desc in forbidden_classifier_patterns:
            if re.search(pattern, line):
                violations.append((i, desc))

    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    if len(sys.argv) > 1:
        files_to_check = [Path(arg).resolve() for arg in sys.argv[1:]]
    else:
        files_to_check = [
            repo_root / "agent" / "app" / "synthesizer.py",
            repo_root / "agent" / "app" / "dispatcher.py",
            repo_root / "agent" / "app" / "specialists" / "runtime_health.py",
        ]

    all_violations: List[str] = []

    print("========================================================")
    print("Checking for post-LLM root cause & remediation overrides...")
    print("========================================================")

    for path in files_to_check:
        if not path.exists():
            all_violations.append(f"Target file does not exist: {path}")
            continue

        rel_path = path.relative_to(repo_root)

        # 1. AST check for root cause overrides
        ast_violations = check_file_ast(path)
        for line_no, msg in ast_violations:
            all_violations.append(f"{rel_path}:{line_no}: {msg}")

        # 2. AST check for remediation overrides
        rem_violations = check_no_remediation_overrides(path)
        for line_no, msg in rem_violations:
            all_violations.append(f"{rel_path}:{line_no}: {msg}")

        # 3. Classifier and keyword pattern check
        classifier_violations = check_no_regex_incident_classifiers(path)
        for line_no, msg in classifier_violations:
            all_violations.append(f"{rel_path}:{line_no}: {msg}")

    if all_violations:
        print(
            "ERROR: Forbidden root cause / remediation overrides or classifiers found:",
            file=sys.stderr,
        )
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        return 1

    print("[PASS] Zero post-LLM root cause or remediation overrides found.")
    print("SUCCESS: Root cause and remediation guard check passed cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
