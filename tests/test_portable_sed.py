#!/usr/bin/env python3
"""Unit tests verifying the check-portable-sed.sh guard against positive and negative fixtures."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_SCRIPT = REPO_ROOT / "scripts" / "check-portable-sed.sh"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "portable_sed"


def run_guard(*target_paths: Path) -> subprocess.CompletedProcess:
    args = [str(GUARD_SCRIPT)] + [str(p) for p in target_paths]
    return subprocess.run(args, capture_output=True, text=True, cwd=str(REPO_ROOT))


def test_clean_repo_passes() -> None:
    """The guard must pass cleanly on the repository files."""
    res = run_guard()
    assert res.returncode == 0, f"Expected clean repo check to pass, failed:\n{res.stderr}"
    assert "SUCCESS" in res.stdout


def test_bad_bare_i_fails() -> None:
    res = run_guard(FIXTURES_DIR / "bad_bare_i.sh")
    assert res.returncode != 0
    assert "Non-portable in-place sed invocation '-i'" in res.stderr


def test_bad_empty_suffix_fails() -> None:
    res = run_guard(FIXTURES_DIR / "bad_empty_suffix.sh")
    assert res.returncode != 0
    assert "Non-portable in-place sed invocation '-i'" in res.stderr


def test_bad_bak_suffix_fails() -> None:
    res = run_guard(FIXTURES_DIR / "bad_bak_suffix.sh")
    assert res.returncode != 0
    assert "Non-portable in-place sed invocation '-i.bak'" in res.stderr


def test_bad_inplace_long_fails() -> None:
    res = run_guard(FIXTURES_DIR / "bad_inplace_long.sh")
    assert res.returncode != 0
    assert "Non-portable in-place sed invocation '--in-place'" in res.stderr


def test_bad_bundled_flag_fails() -> None:
    res = run_guard(FIXTURES_DIR / "bad_bundled_flag.sh")
    assert res.returncode != 0
    assert "Non-portable in-place sed invocation '-Ei'" in res.stderr


def test_bad_missing_darwin_fails() -> None:
    res = run_guard(FIXTURES_DIR / "bad_missing_darwin.sh")
    assert res.returncode != 0
    assert "does not define sedi_args with required Darwin branch" in res.stderr


def test_bad_workflow_run_fails() -> None:
    res = run_guard(FIXTURES_DIR / "bad_workflow_run.yml")
    assert res.returncode != 0
    assert "Non-portable in-place sed invocation '-i'" in res.stderr


def test_good_portable_passes() -> None:
    res = run_guard(FIXTURES_DIR / "good_portable.sh")
    assert res.returncode == 0, f"Unexpected failure on good_portable.sh: {res.stderr}"
    assert "SUCCESS" in res.stdout


def test_good_readonly_passes() -> None:
    res = run_guard(FIXTURES_DIR / "good_readonly.sh")
    assert res.returncode == 0, f"Unexpected failure on good_readonly.sh: {res.stderr}"
    assert "SUCCESS" in res.stdout


def test_good_workflow_passes() -> None:
    res = run_guard(FIXTURES_DIR / "good_workflow.yml")
    assert res.returncode == 0, f"Unexpected failure on good_workflow.yml: {res.stderr}"
    assert "SUCCESS" in res.stdout


if __name__ == "__main__":
    tests = [
        test_clean_repo_passes,
        test_bad_bare_i_fails,
        test_bad_empty_suffix_fails,
        test_bad_bak_suffix_fails,
        test_bad_inplace_long_fails,
        test_bad_bundled_flag_fails,
        test_bad_missing_darwin_fails,
        test_bad_workflow_run_fails,
        test_good_portable_passes,
        test_good_readonly_passes,
        test_good_workflow_passes,
    ]
    for t in tests:
        print(f"Running {t.__name__}...")
        t()
    print(f"\nAll {len(tests)} test(s) passed successfully!")
