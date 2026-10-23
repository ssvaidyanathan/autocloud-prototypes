"""Test AST static guard ensuring zero inline system instructions or prompt templates."""

import subprocess
import sys
from pathlib import Path


def test_zero_inline_instructions_or_prompts_in_agent_app() -> None:
    """Assert all Python files in agent/app contain zero hardcoded instructions or prompts."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    script_path = repo_root / "scripts" / "check-no-inline-prompts.py"
    assert script_path.exists(), f"Guard script not found: {script_path}"

    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    assert result.returncode == 0, (
        f"AST static prompt guard check failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
