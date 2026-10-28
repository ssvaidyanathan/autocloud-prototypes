"""The demo scripts run on macOS: bash 3.2 (test_bash32_compat.py) and the BSD userland.

GNU-only options fail or behave differently on macOS, so they must not appear in the
scripts an operator runs on a Mac (comments excluded): scripts/, demo/apigee/ and the
component deploy.sh scripts. In-place sed has its own guard (scripts/check-portable-sed.sh).

bash 3.2 also differs from bash 4+ in how it parses some constructs that Linux CI (bash 5,
/bin/sh = dash) cannot show at run time, so they are guarded statically here, in the
scripts and in the sh stubs the tests write (on macOS, /bin/sh is bash 3.2 too).
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = ROOT / "scripts"
ALL_SCRIPTS = sorted(SCRIPTS_DIR.rglob("*.sh"))
# Every shell script an operator runs on a Mac: scripts/, plus what they source or call.
MAC_SCRIPTS = ALL_SCRIPTS + sorted(
    [*(ROOT / "demo" / "apigee").glob("*.sh"),
     *(ROOT / c / "scripts" / "deploy.sh" for c in ("agent", "bridge", "tools", "demo/ata-inventory-api"))])
TEST_FILES = sorted(p for p in (ROOT / "tests").glob("*.py") if p.name != Path(__file__).name)

GNU_ONLY = {
    "date -d / --date (BSD: date -j -f, date -v)": re.compile(r"\bdate\s+(-[a-zA-Z]*d\b|--date|--iso-8601|-I\b)"),
    "grep -P (no PCRE in BSD grep)": re.compile(r"\bgrep\s+(-[a-zA-Z]*P|--perl-regexp)"),
    "readlink -f": re.compile(r"\breadlink\s+-[a-zA-Z]*f"),
    "timeout / gtimeout (not on macOS)": re.compile(r"(^|[;&|(]\s*|\$\(\s*)(g?timeout)\s+[0-9]"),
    "stat -c (BSD: stat -f)": re.compile(r"\bstat\s+(-c|--format)"),
    "base64 -w": re.compile(r"\bbase64\s+-w"),
    "xargs -r": re.compile(r"\bxargs\s+(-[a-zA-Z]*r\b|--no-run-if-empty)"),
    "sort -V": re.compile(r"\bsort\s+(-[a-zA-Z]*V|--version-sort)"),
    "mktemp --suffix / long options": re.compile(r"\bmktemp\s+--"),
    "echo -e (use printf)": re.compile(r"\becho\s+-[a-zA-Z]*e\b"),
    "find -printf": re.compile(r"\bfind\b.*\s-printf\b"),
    "tac": re.compile(r"(^|[|;&]\s*)tac\b"),
    "sed -r (BSD: sed -E)": re.compile(r"\bsed\s+-[a-zA-Z]*r\b"),
}


def code_lines(path):
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield n, line


@pytest.mark.parametrize("path", MAC_SCRIPTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_gnu_only_userland(path):
    hits = [f"{path.name}:{n}: {name}: {line.strip()}"
            for n, line in code_lines(path) for name, rx in GNU_ONLY.items() if rx.search(line)]
    assert not hits, "\n".join(hits)


@pytest.mark.parametrize("line", [
    'date -d "2026-10-27"', "grep -oP 'x'", "readlink -f .", "timeout 5 curl x", "x=$(timeout 3 y)",
    "stat -c %s f", "base64 -w0 f", "xargs -r rm", "sort -V", "mktemp --suffix=.x", 'echo -e "a"',
    "find . -printf '%p'", "cat f | tac", "sed -r 's/a/b/'",
])
def test_patterns_catch_gnu_only_usage(line):
    assert any(rx.search(line) for rx in GNU_ONLY.values()), line


@pytest.mark.parametrize("line", [
    "date -u +'%Y-%m-%dT%H:%M:%SZ'", "date +%s", "--connect-timeout 5 --max-time 10", "grep -q x",
    'mktemp "${TMPDIR:-/tmp}/x.XXXXXX"', "printf '%s\\n' x", "sed -e 's|a||'", "sort -u",
])
def test_patterns_allow_portable_usage(line):
    assert not any(rx.search(line) for rx in GNU_ONLY.values()), line


# bash 3.2 keeps the backslash of an escaped "}" inside a double-quoted ${...} word:
# "${X:-{\}}" is {\} there and {} in bash 4+. The terraform stub of test_fresh_deploy_scripts.py
# printed that as its default `terraform show -json` (a #!/bin/sh stub: dash on Linux, bash 3.2
# on macOS), so the teardown tests failed only on the macOS CI job. Use '{}' in its own quotes.
BASH32_QUIRKS = {
    # A nested ${...} before the escaped "}" ("${A:-${B}\}}") is skipped over, not taken as the end.
    'escaped "}" inside ${...} (bash 3.2 keeps the backslash)': re.compile(r"\$\{(?:\$\{[^{}]*\}|[^}])*\\\}"),
}


def bash32_hits(path):
    return [f"{path.name}:{n}: {name}: {line.strip()}"
            for n, line in code_lines(path) for name, rx in BASH32_QUIRKS.items() if rx.search(line)]


@pytest.mark.parametrize("path", MAC_SCRIPTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_scripts_avoid_bash32_parse_quirks(path):
    hits = bash32_hits(path)
    assert not hits, "\n".join(hits)


@pytest.mark.parametrize("path", TEST_FILES, ids=lambda p: p.name)
def test_test_stubs_avoid_bash32_parse_quirks(path):
    """The sh stubs live in the test modules as strings; scanning the whole file covers them."""
    hits = bash32_hits(path)
    assert not hits, "\n".join(hits)


@pytest.mark.parametrize("line", [
    'printf "%s\\n" "${STUB_TF_SHOW_JSON:-{\\}}"', 'x="${A:-a\\}b}"', 'z="${A:+\\}}"', 'w="${A:-${B}\\}}"',
])
def test_bash32_patterns_catch_quirks(line):
    """Double-quoted words: bash 3.2 keeps the backslash in each of these."""
    assert any(rx.search(line) for rx in BASH32_QUIRKS.values()), line


def test_bash32_patterns_flag_unquoted_form_conservatively():
    """Unquoted, bash 3.2 prints "}" like bash 5 (fresh-rev-macos), so ${B-\\}} is fine on 3.2. The
    guard flags it anyway: it does not track quoting, and '}' in its own quotes works everywhere."""
    assert any(rx.search("y=${B-\\}}") for rx in BASH32_QUIRKS.values())


@pytest.mark.parametrize("line", [
    'printf "%s\\n" "${STUB_TF_SHOW_JSON}"', "echo '{}'", 'x="${A:-{}}"', 'json="{\\"a\\": 1}"',
    'd="${D:-}"; echo "{${d}}"',
])
def test_bash32_patterns_allow_portable_usage(line):
    assert not any(rx.search(line) for rx in BASH32_QUIRKS.values()), line
