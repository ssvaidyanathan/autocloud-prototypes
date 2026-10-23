#!/usr/bin/env python3
"""AST static guard check to ensure NO system instructions, prompt templates,
or f-string prompts are hardcoded in Python files under agent/app.

Rules:
1. No string literal or f-string passed to keyword arguments:
   - instruction
   - system_instruction
   - global_instruction
   - static_instruction
   - prompt
   - contents (when calling generate_content)
   - text (when calling Part)
2. No string literal or f-string assigned to dictionary key 'instruction' or 'prompt'.
3. No canned Turn protocol instructions or prompt phrases hardcoded in Python.
"""

import ast
import os
import sys
from pathlib import Path
from typing import List, Tuple

FORBIDDEN_KEYWORD_ARGS = {
    "instruction",
    "system_instruction",
    "global_instruction",
    "static_instruction",
}

FORBIDDEN_PROMPT_PHRASES = [
    "CRITICAL INSTRUCTIONS:",
    "Evidence collection is finished. Zero infrastructure defects exist.",
    "Turn 1: Invoke load_skill",
    "Turn 2: Formulate at most",
    "Turn 3: Conclude immediately",
    "Turn 1: If investigating a single known service",
    "STRICT PROHIBITION ON MULTI-QUERY HUNTING",
    "STRICT PROHIBITION ON CROSS-PLATFORM HUNTING",
]


def is_string_or_fstring_or_concat(node: ast.AST) -> bool:
    """Check if an AST node is a string literal, f-string, or string concatenation."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return is_string_or_fstring_or_concat(node.left) or is_string_or_fstring_or_concat(
            node.right
        )
    return False


def get_call_func_name(node: ast.Call) -> str:
    """Extract readable function name from Call node."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def check_file(file_path: Path) -> List[Tuple[int, str]]:
    """Scan Python file for hardcoded instructions, prompt templates, or f-string prompts."""
    violations: List[Tuple[int, str]] = []
    content = file_path.read_text(encoding="utf-8")

    # 1. Text-based scan for canned prompt phrases in code lines (ignore comments)
    for line_no, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for phrase in FORBIDDEN_PROMPT_PHRASES:
            if phrase in line:
                violations.append(
                    (
                        line_no,
                        f"Found hardcoded prompt phrase '{phrase}'. All prompts must load from agent.yaml.",
                    )
                )

    # 2. AST analysis
    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError as err:
        violations.append((err.lineno or 1, f"SyntaxError parsing file: {err}"))
        return violations

    for node in ast.walk(tree):
        # Check call arguments
        if isinstance(node, ast.Call):
            func_name = get_call_func_name(node)
            for kw in node.keywords:
                arg_name = kw.arg
                if not arg_name:
                    continue

                # Any instruction-related keyword arg
                if arg_name in FORBIDDEN_KEYWORD_ARGS:
                    if is_string_or_fstring_or_concat(kw.value):
                        violations.append(
                            (
                                kw.value.lineno,
                                f"Forbidden hardcoded string or f-string passed to keyword '{arg_name}'. "
                                "Load instruction from agent.yaml via agent_config.",
                            )
                        )

                # generate_content(contents=...) or generate_content(prompt=...)
                if func_name == "generate_content" and arg_name in ("contents", "prompt"):
                    if is_string_or_fstring_or_concat(kw.value):
                        violations.append(
                            (
                                kw.value.lineno,
                                f"Forbidden hardcoded string or f-string passed to generate_content({arg_name}=...). "
                                "Load prompt from agent.yaml via agent_config.",
                            )
                        )

                # Part(text=...)
                if func_name in ("Part", "types.Part") and arg_name == "text":
                    if is_string_or_fstring_or_concat(kw.value):
                        violations.append(
                            (
                                kw.value.lineno,
                                "Forbidden hardcoded string or f-string passed to Part(text=...). "
                                "Load prompt from agent.yaml via agent_config.",
                            )
                        )

        # Check dictionary assignments: obj["instruction"] = "..."
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    slice_node = target.slice
                    if isinstance(slice_node, ast.Constant) and slice_node.value in (
                        "instruction",
                        "system_instruction",
                        "prompt",
                    ):
                        if is_string_or_fstring_or_concat(node.value):
                            violations.append(
                                (
                                    node.lineno,
                                    f"Forbidden hardcoded string assigned to dict key '{slice_node.value}'. "
                                    "Load instruction from agent.yaml via agent_config.",
                                )
                            )

        # Check dictionary literals: {"instruction": "..."}
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if k and isinstance(k, ast.Constant) and k.value in (
                    "instruction",
                    "system_instruction",
                    "prompt",
                ):
                    if is_string_or_fstring_or_concat(v):
                        violations.append(
                            (
                                v.lineno,
                                f"Forbidden hardcoded string in dict literal for key '{k.value}'. "
                                "Load instruction from agent.yaml via agent_config.",
                            )
                        )

    return violations


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    agent_app_dir = repo_root / "agent" / "app"

    if not agent_app_dir.is_dir():
        print(f"Error: Target directory not found: {agent_app_dir}", file=sys.stderr)
        return 1

    print("========================================================")
    print("Checking for inline instructions and prompts in agent/app...")
    print("========================================================")

    total_violations = 0
    scanned_files = 0

    for root, _, files in os.walk(agent_app_dir):
        for file in sorted(files):
            if not file.endswith(".py"):
                continue
            scanned_files += 1
            file_path = Path(root) / file
            rel_path = file_path.relative_to(repo_root)

            violations = check_file(file_path)
            if violations:
                for line_no, msg in violations:
                    print(f"[FAIL] {rel_path}:{line_no} -> {msg}", file=sys.stderr)
                    total_violations += 1

    if total_violations > 0:
        print(
            f"\n[FAIL] Found {total_violations} forbidden inline instruction/prompt violation(s) across {scanned_files} file(s).",
            file=sys.stderr,
        )
        return 1

    print(f"[PASS] Zero inline instructions or prompt templates found across {scanned_files} Python file(s).")
    print("SUCCESS: All instructions and prompt templates load from agent.yaml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
