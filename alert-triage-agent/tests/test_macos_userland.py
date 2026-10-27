"""The demo scripts run on macOS: bash 3.2 (test_bash32_compat.py) and the BSD userland.

GNU-only options fail or behave differently on macOS, so they must not appear in
scripts/ (comments excluded). In-place sed has its own guard (scripts/check-portable-sed.sh).
"""

import re
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
ALL_SCRIPTS = sorted(SCRIPTS_DIR.rglob("*.sh"))

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


@pytest.mark.parametrize("path", ALL_SCRIPTS, ids=lambda p: str(p.relative_to(SCRIPTS_DIR)))
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
