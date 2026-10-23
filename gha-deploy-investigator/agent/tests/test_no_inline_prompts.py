"""Test AST static guard ensuring zero inline system instructions or prompt templates."""

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _get_guard_module() -> Any:
    repo_root = Path(__file__).resolve().parent.parent.parent
    script_path = repo_root / "scripts" / "check-no-inline-prompts.py"
    spec = importlib.util.spec_from_file_location("check_no_inline_prompts", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def test_guard_cli_argv_single_file_clean() -> None:
    """Test guard script invoked with a specific clean Python file via CLI argv."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    script_path = repo_root / "scripts" / "check-no-inline-prompts.py"
    clean_file = repo_root / "agent" / "app" / "main.py"

    result = subprocess.run(
        [sys.executable, str(script_path), str(clean_file)],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    assert result.returncode == 0
    assert "Zero inline instructions" in result.stdout


def test_guard_cli_argv_single_file_with_inline_prompt() -> None:
    """Test guard script detects inline prompt in a single file passed via CLI argv."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    script_path = repo_root / "scripts" / "check-no-inline-prompts.py"

    with tempfile.NamedTemporaryFile("w", suffix=".py") as tmp:
        tmp.write("def make_agent():\n    return Agent(instruction='Inline prompt string')\n")
        tmp.flush()

        result = subprocess.run(
            [sys.executable, str(script_path), tmp.name],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        assert result.returncode == 1
        assert "Forbidden hardcoded string" in result.stderr


def test_guard_cli_argv_directory_target() -> None:
    """Test guard script scanning an arbitrary directory passed via CLI argv."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    script_path = repo_root / "scripts" / "check-no-inline-prompts.py"

    with tempfile.TemporaryDirectory() as tmp_dir:
        file1 = Path(tmp_dir) / "clean1.py"
        file1.write_text("x = 1\n", encoding="utf-8")
        file2 = Path(tmp_dir) / "clean2.py"
        file2.write_text("y = 2\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(script_path), tmp_dir],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        assert result.returncode == 0
        assert "across 2 Python file(s)" in result.stdout


def test_guard_cli_argv_nonexistent_path() -> None:
    """Test guard script fails when a nonexistent target path is passed."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    script_path = repo_root / "scripts" / "check-no-inline-prompts.py"

    result = subprocess.run(
        [sys.executable, str(script_path), "/path/does/not/exist/foo.py"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    assert result.returncode == 1
    assert "Target path not found" in result.stderr


def test_guard_main_function_argv_parameter() -> None:
    """Test calling main(argv=[...]) directly in Python."""
    guard_mod = _get_guard_module()
    with tempfile.NamedTemporaryFile("w", suffix=".py") as tmp:
        tmp.write("def foo():\n    return 42\n")
        tmp.flush()

        # Clean invocation
        rc = guard_mod.main(argv=[tmp.name])
        assert rc == 0

        # Nonexistent path
        rc_missing = guard_mod.main(argv=["/nonexistent/file.py"])
        assert rc_missing == 1


def test_guard_detects_synthetic_inline_violations() -> None:
    """Test AST static guard against various synthetic prompt patterns."""
    guard_mod = _get_guard_module()

    test_cases = [
        ("Agent(instruction='hardcoded')", True),
        ("Agent(instruction=f'hardcoded {x}')", True),
        ("Agent(system_instruction='hardcoded')", True),
        ("Agent(instruction=load_from_config())", False),
        ("client.models.generate_content(contents='hardcoded prompt')", True),
        ("client.models.generate_content(contents=loaded_prompt)", False),
        ("types.Part(text='hardcoded text')", True),
        ("types.Part(text=prompt_text)", False),
        ("Part(text='hardcoded text')", True),
        ("Part(text=prompt_text)", False),
        ("d['instruction'] = 'hardcoded instruction'", True),
        ("d['prompt'] = 'hardcoded prompt'", True),
        ("d['other_key'] = 'some string'", False),
    ]

    for snippet, should_violate in test_cases:
        with tempfile.NamedTemporaryFile("w", suffix=".py") as tmp:
            tmp.write(f"def test_func():\n    {snippet}\n")
            tmp.flush()

            violations = guard_mod.check_file(Path(tmp.name))
            if should_violate:
                assert len(violations) > 0, f"Expected violation for: {snippet}"
            else:
                assert len(violations) == 0, f"Unexpected violation for: {snippet}"


def test_guard_detects_negative_fixture() -> None:
    """Verify check-no-inline-prompts.py catches every violation in negative fixture."""
    guard_mod = _get_guard_module()
    fixture_path = (
        Path(__file__).resolve().parent / "fixtures" / "negative_inline_prompt.py"
    )
    assert fixture_path.exists(), f"Negative fixture not found: {fixture_path}"

    violations = guard_mod.check_file(fixture_path)
    # The negative fixture contains 7 distinct violations:
    # 2 canned prompt phrases ('CRITICAL INSTRUCTIONS:', 'Turn 1: Invoke load_skill'),
    # 1 variable assignment to prompt_lines,
    # 1 dict assignment with 'instruction',
    # 1 keyword argument instruction=...,
    # 1 positional runner prompt to _execute_adk_runner,
    # 1 Part(text=...) prompt call.
    assert len(violations) >= 6, (
        f"Expected at least 6 violations in negative fixture, got {len(violations)}: {violations}"
    )
