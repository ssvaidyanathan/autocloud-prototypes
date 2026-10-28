"""Guard: no hardcoded stack prefix, project id, project number, Cloud Run hash or region.

Every resource name must derive from NAME_PREFIX, and every project, region and URL from
config (user rule: NOTHING hardcoded). The test scans runtime code, Terraform, the topology and
the Apigee bundles, and fails on:
  - 'ata' resource literals: `ata-<x>`, `/ata/`, a bare "ata" value, `${VAR:-ata}`
  - the live project id, project number and Cloud Run URL hash
  - GCP region literals (us-west1, ...)

Not flagged:
  - `ATA_*` env names and `ata_*` / `is_ata_*` identifiers, the `alert-triage-agent` name,
    the `ata-diagnostic-report-` attachment name and pyproject package names
  - Python comments and docstrings, and full-line comments in sh/tf/toml/Dockerfile/xml
  - a line carrying a `prefix-ok: <reason>` marker
  - entries in tests/prefix_allowlist.txt (`path-glob:regex`). Stale entries FAIL the test, so
    the list only shrinks.

Violations print as `file:line:literal` (line 0 = the file or directory name itself).
"""

from __future__ import annotations

import fnmatch
import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "tests" / "prefix_allowlist.txt"

CODE_SUFFIXES = {".py", ".sh", ".tf", ".xml", ".toml"}
SCAN_DIRS = ("agent", "bridge", "tools", "demo", "scripts", "topology", "infra")
EXCLUDED_PARTS = {"tests", "__pycache__", ".terraform", "skills", "node_modules", ".venv"}

# The live project/number/hash are banned in every form (env defaults, URLs, examples).
LIVE_IDS = ("cloud-assist-fde-2", "185099261133", "4klvlqnmpa")
VIOLATIONS = [
    re.compile(r"(?<![A-Za-z0-9_])ata-[a-z]"),
    re.compile(r"/ata/"),
    re.compile(r"""(?<![A-Za-z0-9])["'=]ata(?=["'\s]|$)"""),
    re.compile(r""":-ata[}"]"""),
    re.compile("|".join(re.escape(x) for x in LIVE_IDS)),
    re.compile(
        r"\b(?:us|europe|asia|northamerica|southamerica|australia|me|africa)-"
        r"(?:north|south|east|west|central|northeast|southeast|northwest|southwest)[0-9]{1,2}\b"
    ),
]
ALLOWED = [
    re.compile(r"\bATA_[A-Z0-9_]+\b"),
    re.compile(r"\b(?:is_|query_open_)?ata_[a-z0-9_]+\b"),
    re.compile(r"alert-triage-agent"),
    re.compile(r"ata-diagnostic-report-"),
    re.compile(r"""^name\s*=\s*"ata-[a-z-]+"\s*$"""),  # pyproject package name
]
MARKER = "prefix-ok:"


def scanned_files() -> list[Path]:
    files: list[Path] = []
    for top in SCAN_DIRS:
        for path in sorted((ROOT / top).rglob("*")):
            rel = path.relative_to(ROOT)
            if not path.is_file() or EXCLUDED_PARTS.intersection(rel.parts):
                continue
            if path.name.endswith(".example"):
                continue
            if top == "infra" and path.suffix != ".tf":
                continue
            if top == "topology" and path.suffix not in {".md", ".py"}:
                continue
            if path.suffix in CODE_SUFFIXES or path.name == "Dockerfile" or (top == "topology" and path.suffix == ".md"):
                files.append(path)
    return files


def _python_code_lines(text: str) -> list[str]:
    """Source lines with comments and docstrings blanked out."""
    lines = text.splitlines()
    blank: list[tuple[tuple[int, int], tuple[int, int]]] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return lines
    prev_sig = None
    for i, tok in enumerate(tokens):
        if tok.type == tokenize.COMMENT:
            blank.append((tok.start, tok.end))
        elif tok.type == tokenize.STRING and prev_sig in (None, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            nxt = next((t for t in tokens[i + 1:] if t.type not in (tokenize.NL, tokenize.COMMENT)), None)
            if nxt is None or nxt.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                blank.append((tok.start, tok.end))
        if tok.type not in (tokenize.NL, tokenize.COMMENT):
            prev_sig = tok.type
    out = list(lines)
    for (srow, scol), (erow, ecol) in blank:
        for row in range(srow, erow + 1):
            if row - 1 >= len(out):
                continue
            line = out[row - 1]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(line)
            out[row - 1] = line[:start] + " " * (end - start) + line[end:]
    return out


def code_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".py":
        return _python_code_lines(text)
    if path.suffix == ".xml":
        text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
        return text.splitlines()
    if path.suffix == ".md":
        return text.splitlines()
    out = []
    for n, line in enumerate(text.splitlines()):
        stripped = line.lstrip()
        if (stripped.startswith("#") and not (n == 0 and stripped.startswith("#!"))) or stripped.startswith("//"):
            out.append("")
        else:
            out.append(line)
    return out


def _token_at(line: str, m: re.Match) -> str:
    """The whole literal around a match (e.g. 'ata-timer-queue', not 'ata-t'), for the report."""
    start, end = m.start(), m.end()
    while start > 0 and re.match(r"[A-Za-z0-9_.@/-]", line[start - 1]) and line[start - 1] not in "\"'=":
        start -= 1
    while end < len(line) and re.match(r"[A-Za-z0-9_.@/-]", line[end]):
        end += 1
    return line[start:end].strip("\"'=")


def find_violations() -> list[tuple[str, int, str, str]]:
    """(rel path, line, literal, full line) for every hit."""
    hits: list[tuple[str, int, str, str]] = []
    seen_dirs: set[str] = set()
    for path in scanned_files():
        rel = path.relative_to(ROOT).as_posix()
        # The path itself (directory and file names).
        parts = rel.split("/")
        for depth in range(1, len(parts) + 1):
            sub = "/".join(parts[:depth])
            if sub in seen_dirs:
                continue
            seen_dirs.add(sub)
            name = parts[depth - 1]
            for rx in VIOLATIONS:
                m = rx.search("/" + name if rx.pattern == "/ata/" else name)
                if m:
                    hits.append((sub, 0, m.group(0), sub))
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for lineno, line in enumerate(code_lines(path), start=1):
            raw = raw_lines[lineno - 1] if lineno - 1 < len(raw_lines) else line
            if MARKER in raw:
                continue
            scrubbed = line
            for rx in ALLOWED:
                scrubbed = rx.sub(lambda m: " " * len(m.group(0)), scrubbed)
            for rx in VIOLATIONS:
                for m in rx.finditer(scrubbed):
                    hits.append((rel, lineno, _token_at(scrubbed, m), raw))
    return hits


def load_allowlist() -> list[tuple[int, str, re.Pattern]]:
    entries = []
    for n, line in enumerate(ALLOWLIST.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path_glob, sep, regex = line.partition(":")
        assert sep and regex, f"prefix_allowlist.txt:{n}: expected 'path-glob:regex', got {line!r}"
        entries.append((n, path_glob, re.compile(regex)))
    return entries


def _allowed(hit, entries) -> list[int]:
    rel, _lineno, _literal, raw = hit
    return [n for n, glob, rx in entries if fnmatch.fnmatchcase(rel, glob) and rx.search(raw)]


def test_no_hardcoded_prefix_project_or_region_literals():
    entries = load_allowlist()
    bad = [h for h in find_violations() if not _allowed(h, entries)]
    report = "\n".join(f"{rel}:{lineno}:{literal}" for rel, lineno, literal, _ in bad)
    assert not bad, (
        f"{len(bad)} hardcoded literal(s); derive them from NAME_PREFIX/config, mark a genuine "
        f"example with '# prefix-ok: <reason>', or (other lanes only) list them in "
        f"tests/prefix_allowlist.txt:\n{report}"
    )


def test_prefix_allowlist_has_no_stale_entries():
    entries = load_allowlist()
    used: set[int] = set()
    for hit in find_violations():
        used.update(_allowed(hit, entries))
    stale = [f"prefix_allowlist.txt:{n}: {glob}:{rx.pattern}" for n, glob, rx in entries if n not in used]
    assert not stale, "stale allowlist entries (the literal is gone; delete the line):\n" + "\n".join(stale)


@pytest.mark.parametrize(
    "line, flagged",
    [
        ('TIMER_QUEUE = "ata-timer-queue"', True),
        ('BASE = "/ata/orders/v1"', True),
        ('NAME_PREFIX="${NAME_PREFIX:-ata}"', True),
        ('name_prefix = "ata"', True),
        ('url = "https://x-4klvlqnmpa-uw.a.run.app"', True),
        ('project = "cloud-assist-fde-2"', True),
        ('region: str = "us-west1"', True),
        ('os.environ["ATA_API_KEY"]', False),
        ("def is_ata_alert(x):", False),
        ('labels = {"app": "alert-triage-agent"}', False),
        ('queue = f"{prefix}-timer-queue"', False),
        ('name = "data-pipeline"', False),
        ('s = "atat-orders-v1"', False),
    ],
)
def test_guard_patterns_flag_literals_and_spare_identifiers(line, flagged):
    scrubbed = line
    for rx in ALLOWED:
        scrubbed = rx.sub(lambda m: " " * len(m.group(0)), scrubbed)
    assert any(rx.search(scrubbed) for rx in VIOLATIONS) is flagged


def test_python_comments_and_docstrings_are_not_scanned(tmp_path):
    src = tmp_path / "m.py"
    src.write_text('"""Docs mention ata-agent."""\nx = 1  # ata-bridge\ny = "ata-tools"\n')
    lines = _python_code_lines(src.read_text())
    assert "ata-agent" not in lines[0] and "ata-bridge" not in lines[1]
    assert "ata-tools" in lines[2]


# Example config files (*.example) are skipped above because they document the default `ata`
# prefix. They must still carry only placeholders, never a live stack identifier.
EXAMPLE_LIVE_IDS = LIVE_IDS + (
    "dev377204",                 # live ServiceNow instance
    "ssvaidyanathan",            # personal account / email
    "8.233.247.52",              # live Apigee nip.io hostname
    "scion-fde-ops",             # live operator service account
    "deploy-demo-test",          # live deployer service account
)
EXAMPLE_VIOLATIONS = [
    re.compile("|".join(re.escape(x) for x in EXAMPLE_LIVE_IDS)),
    VIOLATIONS[-1],  # GCP region literal
]


EXAMPLE_EXCLUDED_PARTS = {".terraform", "node_modules", ".venv", "__pycache__"}


def example_files() -> list[Path]:
    return [
        p for p in sorted(ROOT.rglob("*.example"))
        if p.is_file() and not EXAMPLE_EXCLUDED_PARTS.intersection(p.relative_to(ROOT).parts)
    ]


def test_example_configs_carry_no_live_identifiers():
    files = example_files()
    rels = {p.relative_to(ROOT).as_posix() for p in files}
    # Non-vacuous: the env and tfvars examples are all in scope.
    assert {"agent/.env.example", "bridge/.env.example",
            "infra/terraform/foundation/terraform.tfvars.example"} <= rels, rels
    bad = [
        f"{p.relative_to(ROOT).as_posix()}:{n}:{m.group(0)}"
        for p in files
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), start=1)
        for rx in EXAMPLE_VIOLATIONS
        for m in rx.finditer(line)
    ]
    assert not bad, "live identifier(s) in example config; use your-project-id / your-region placeholders:\n" + "\n".join(bad)


@pytest.mark.parametrize(
    "line, flagged",
    [
        ("PROJECT_ID=cloud-assist-fde-2", True),
        ("REGION=us-west1", True),
        ("SERVICENOW_INSTANCE_URL=https://dev377204.service-now.com", True),
        ('deployer_members = ["user:ssvaidyanathan@google.com"]', True),
        ("APIGEE_HOST=8.233.247.52.nip.io", True),
        ("SA=deploy-demo-test@x.iam.gserviceaccount.com", True),
        ("PROJECT_ID=your-project-id", False),
        ("REGION=your-region", False),
        ("SERVICENOW_INSTANCE_URL=https://your-instance.service-now.com", False),
    ],
)
def test_example_guard_patterns(line, flagged):
    assert any(rx.search(line) for rx in EXAMPLE_VIOLATIONS) is flagged
