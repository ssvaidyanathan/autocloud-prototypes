#!/usr/bin/env python3
"""AST static guard check to ensure no hardcoded system instructions or prompt
templates exist in Python source files under the agent application.

All instructions, system instructions, and prompt templates must be loaded
from agent.yaml via agent_config.

Rules:
1. No string literal or f-string may be passed as `instruction`,
   `system_instruction`, `global_instruction`, `static_instruction`, or
   `prompt` keyword arguments in any function or class call.
2. No string literal or f-string may be assigned to variables named
   `instruction`, `system_instruction`, `global_instruction`,
   `static_instruction`, `prompt`, `runner_prompt`, or `prompt_lines`.
3. No string literal or f-string may be passed as the runner prompt argument
   to `_execute_adk_runner(...)`.
4. No string literal or f-string instructions may be passed to `LlmAgent(...)`.
"""

import ast
import os
import sys
from pathlib import Path
from typing import List, Tuple

FORBIDDEN_KWARGS = {
    "instruction",
    "system_instruction",
    "global_instruction",
    "static_instruction",
    "prompt",
}

FORBIDDEN_VAR_NAMES = {
    "instruction",
    "system_instruction",
    "global_instruction",
    "static_instruction",
    "prompt",
    "runner_prompt",
    "prompt_lines",
}


def _is_hardcoded_string_or_fstring(node: ast.AST) -> bool:
    """Check if an AST node is a string literal, f-string, or concatenation thereof."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_is_hardcoded_string_or_fstring(elt) for elt in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _is_hardcoded_string_or_fstring(
            node.left
        ) or _is_hardcoded_string_or_fstring(node.right)
    return False


def _get_call_name(node: ast.Call) -> str:
    """Extract call function name (e.g. 'LlmAgent' or '_execute_adk_runner')."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def check_file_ast(file_path: Path) -> List[Tuple[int, str]]:
    """Scan file AST for forbidden hardcoded instructions or prompt templates."""
    violations: List[Tuple[int, str]] = []

    content = file_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError as err:
        return [(err.lineno or 1, f"Syntax error parsing file: {err}")]

    for node in ast.walk(tree):
        # 1. Check Call nodes
        if isinstance(node, ast.Call):
            call_name = _get_call_name(node)

            # Check keyword arguments
            for kw in node.keywords:
                if kw.arg in FORBIDDEN_KWARGS:
                    if _is_hardcoded_string_or_fstring(kw.value):
                        violations.append(
                            (
                                node.lineno,
                                f"Hardcoded string literal or f-string passed to keyword argument "
                                f"'{kw.arg}' in call to '{call_name or 'function'}'. "
                                "Prompts must load from agent.yaml.",
                            )
                        )

            # Check _execute_adk_runner positional prompt (arg index 1)
            if call_name == "_execute_adk_runner":
                if len(node.args) >= 2 and _is_hardcoded_string_or_fstring(
                    node.args[1]
                ):
                    violations.append(
                        (
                            node.lineno,
                            "Hardcoded string literal or f-string passed as runner prompt (arg 1) "
                            "to '_execute_adk_runner'. Runner prompts must load from agent.yaml.",
                        )
                    )

            # Check LlmAgent positional arguments if any are strings/f-strings beyond name/model
            if call_name == "LlmAgent":
                for idx, arg in enumerate(node.args):
                    if idx >= 2 and _is_hardcoded_string_or_fstring(arg):
                        violations.append(
                            (
                                node.lineno,
                                f"Hardcoded string literal or f-string passed as positional "
                                f"argument {idx} to 'LlmAgent'. "
                                "Instruction must load from agent.yaml.",
                            )
                        )

        # 2. Check variable assignments: prompt = ..., instruction = ...
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in FORBIDDEN_VAR_NAMES:
                    if _is_hardcoded_string_or_fstring(node.value):
                        violations.append(
                            (
                                node.lineno,
                                f"Hardcoded string literal or f-string assigned to variable "
                                f"'{target.id}'. Instructions and prompt templates must load "
                                "from agent.yaml.",
                            )
                        )

        # 3. Check annotated variable assignments: instruction: str = ...
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id in FORBIDDEN_VAR_NAMES
            ):
                if node.value and _is_hardcoded_string_or_fstring(node.value):
                    violations.append(
                        (
                            node.lineno,
                            f"Hardcoded string literal or f-string assigned to variable "
                            f"'{node.target.id}'. Instructions and prompt templates must load "
                            "from agent.yaml.",
                        )
                    )

    return violations


def find_agent_python_files(agent_app_dir: Path) -> List[Path]:
    """Find all Python source files under the agent app directory."""
    files: List[Path] = []
    for root, _, filenames in os.walk(agent_app_dir):
        if "__pycache__" in root:
            continue
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                files.append(Path(root) / fn)
    return files


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    agent_app_dir = repo_root / "agent" / "app"

    if len(sys.argv) > 1:
        files_to_check = [Path(arg).resolve() for arg in sys.argv[1:]]
    else:
        files_to_check = find_agent_python_files(agent_app_dir)

    all_violations: List[str] = []

    print("========================================================")
    print("Checking for hardcoded instructions & prompt templates...")
    print("========================================================")

    for path in files_to_check:
        if not path.exists():
            all_violations.append(f"Target file does not exist: {path}")
            continue

        try:
            rel_path = path.relative_to(repo_root)
        except ValueError:
            rel_path = path

        violations = check_file_ast(path)
        for line_no, msg in violations:
            all_violations.append(f"{rel_path}:{line_no}: {msg}")

    if all_violations:
        print(
            "ERROR: Hardcoded instructions or prompt templates found in Python source:",
            file=sys.stderr,
        )
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        return 1

    print(f"[PASS] Scanned {len(files_to_check)} file(s).")
    print(
        "SUCCESS: Zero hardcoded instructions or prompt templates found in agent app."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
