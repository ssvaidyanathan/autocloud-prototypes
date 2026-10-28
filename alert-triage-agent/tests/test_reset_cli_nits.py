"""reset-scenarios.sh option parsing nits (predeploy2-review):

- is_ignored_group compares entries literally: no pathname expansion of IGNORE_GROUPS
  (a '*' entry used to expand to the files in the cwd), and the caller's noglob state is kept.
- An option that takes a value, given without one, fails with a usage error instead of
  bash's '$2: unbound variable'.

Run under ATA_TEST_BASH (bash 3.2 compatible), as in test_bash32_compat.py.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RESET_SCRIPT = Path(__file__).parent.parent / "scripts" / "reset-scenarios.sh"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))

VALUE_OPTIONS = [
    "--ignore-group", "--quiet-period", "--timeout", "--poll-interval", "--max-alert-age",
    "--alert-lag", "--serving-revisions", "--monitoring-token-source",
]


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATA_RESET_")}
    env.update(extra)
    return env


def _probe(tmp_path, ignore_groups, body):
    script = f'set -- --mock\nsource "{RESET_SCRIPT}"\n{body}\n'
    return subprocess.run([TEST_BASH, "-c", script], cwd=str(tmp_path), capture_output=True, text=True,
                          timeout=30, check=False, env=_env(ATA_RESET_IGNORE_GROUPS=ignore_groups))


def _is_ignored(tmp_path, ignore_groups, name):
    res = _probe(tmp_path, ignore_groups, f'if is_ignored_group "{name}"; then echo YES; else echo NO; fi')
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout.strip().splitlines()[-1] == "YES"


def test_star_entry_does_not_expand_to_cwd_files(tmp_path):
    (tmp_path / "incident-local.md").write_text("x")
    assert not _is_ignored(tmp_path, "*", "incident-local.md")


def test_wildcard_entry_is_literal(tmp_path):
    (tmp_path / "incident-a.md").write_text("x")
    assert not _is_ignored(tmp_path, "incident-*.md", "incident-a.md")
    assert _is_ignored(tmp_path, "incident-*.md", "incident-*.md")


@pytest.mark.parametrize("groups", ["incident-a.md", "x,incident-a.md", "x incident-a.md", "a", "x, a"])
def test_listed_groups_still_match(tmp_path, groups):
    assert _is_ignored(tmp_path, groups, "incident-a.md")


def test_unlisted_and_empty(tmp_path):
    assert not _is_ignored(tmp_path, "incident-b.md", "incident-a.md")
    assert not _is_ignored(tmp_path, "", "incident-a.md")


def test_noglob_state_of_the_caller_is_kept(tmp_path):
    res = _probe(tmp_path, "a,b", 'is_ignored_group incident-a.md; case $- in *f*) echo F1;; *) echo G1;; esac\n'
                 'set -f; is_ignored_group incident-z.md || true; case $- in *f*) echo F2;; *) echo G2;; esac')
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.split()[-2:] == ["G1", "F2"]


@pytest.mark.parametrize("opt", VALUE_OPTIONS)
def test_value_option_without_argument_is_a_usage_error(tmp_path, opt):
    res = subprocess.run([TEST_BASH, str(RESET_SCRIPT), "--mock", opt], cwd=str(tmp_path), capture_output=True,
                         text=True, timeout=30, check=False, env=_env())
    combined = res.stdout + res.stderr
    assert res.returncode == 1, combined
    assert f"Option {opt} requires a value" in combined
    assert "unbound variable" not in combined


def test_value_option_with_argument_still_parses(tmp_path):
    script = f'set -- --mock --quiet-period 2m --timeout=30\nsource "{RESET_SCRIPT}"\necho "Q=$QUIET_PERIOD_SECS T=$WAIT_TIMEOUT_SECS"\n'
    res = subprocess.run([TEST_BASH, "-c", script], cwd=str(tmp_path), capture_output=True, text=True,
                         timeout=30, check=False, env=_env())
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Q=120 T=30" in res.stdout
