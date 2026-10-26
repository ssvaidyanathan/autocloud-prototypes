import ast
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest
from bridge.app.contracts.findings import EvidenceItem
from bridge.app.contracts.incident import Incident
from bridge.app.contracts.report import CheckRunAnnotation, Report
from bridge.app.fixer import s1_lint
from bridge.app.fixer.interface import validate_fix_guardrails
from bridge.app.fixer.s1_lint import (
    LintDiagnostic,
    UnsafeFixError,
    collect_diagnostics,
    extract_import_names,
    fix_source,
    generate_lint_fix,
    organize_imports,
    parse_lint_diagnostics,
)
from bridge.app.fixer.writer import resolve_patch_content

SOURCE = '''"""Module docstring."""
import os
import sys
from typing import Any, Optional

counter = 1; total = 2
import json
import re


def handler(value: Any) -> None:
    print(value, sys.argv, re, counter, total); return None
'''

CONCISE_LOG = """\
2026-10-24T10:00:00.1234567Z pkg/mod.py:2:8: F401 [*] `os` imported but unused
pkg/mod.py:4:25: F401 [*] `typing.Optional` imported but unused
pkg/mod.py:6:12: E702 Multiple statements on one line (semicolon)
pkg/mod.py:7:1: E402 Module level import not at top of file
pkg/mod.py:7:8: F401 [*] `json` imported but unused
pkg/mod.py:8:1: E402 Module level import not at top of file
pkg/mod.py:12:47: E702 Multiple statements on one line (semicolon)
Found 7 errors.
"""

EXPECTED = '''"""Module docstring."""
import sys
from typing import Any
import re

counter = 1
total = 2


def handler(value: Any) -> None:
    print(value, sys.argv, re, counter, total)
    return None
'''


@pytest.fixture(autouse=True)
def _isolate_ruff(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the AST transformations alone unless a test class opts in to ruff."""
    if not getattr(request.cls, "uses_ruff", False):
        monkeypatch.setattr(s1_lint, "_ruff_executable", lambda: None)


def _incident(branch: str = "feature/x") -> Incident:
    return Incident(
        id="gha-1",
        source="github",
        external_id="1",
        external_key="acme/widgets#1",
        title="t",
        description="d",
        severity="P3",
        opened_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        github_repo="acme/widgets",
        github_run_id=1,
        github_head_sha="abc",
        github_branch=branch,
    )


def _report_with_log(log: str) -> Report:
    return Report(
        summary="lint failed",
        evidence_by_area={
            "logs": [
                EvidenceItem(
                    kind="github_runner_log",
                    excerpt=log,
                    timestamp=datetime(2026, 10, 24, tzinfo=timezone.utc),
                )
            ]
        },
    )


def _fetch(files: dict[str, str]):  # type: ignore[no-untyped-def]
    def fetch(path: str) -> Optional[str]:
        return files.get(path)

    return fetch


class TestParsing:
    def test_concise_format_with_timestamps(self) -> None:
        diags = parse_lint_diagnostics(CONCISE_LOG)
        assert len(diags) == 7
        assert diags[0] == LintDiagnostic(
            path="pkg/mod.py", line=2, code="F401", message="[*] `os` imported but unused"
        )

    def test_full_format(self) -> None:
        log = (
            "F401 [*] `os` imported but unused\n"
            " --> ./pkg/mod.py:2:8\n"
            "  |\n"
            "E402 Module level import not at top of file\n"
            " --> pkg/mod.py:7:1\n"
        )
        diags = parse_lint_diagnostics(log)
        assert [(d.path, d.line, d.code) for d in diags] == [
            ("pkg/mod.py", 2, "F401"),
            ("pkg/mod.py", 7, "E402"),
        ]

    def test_collect_from_annotations_and_evidence_dedupes(self) -> None:
        report = _report_with_log(CONCISE_LOG)
        report.annotations.append(
            CheckRunAnnotation(
                path="./pkg/mod.py",
                start_line=2,
                end_line=2,
                message="F401 `os` imported but unused",
            )
        )
        diags = collect_diagnostics(report)
        assert len(diags) == 7


class TestFixSource:
    def test_combined_fix(self) -> None:
        fixed = fix_source(SOURCE, parse_lint_diagnostics(CONCISE_LOG))
        assert fixed == EXPECTED
        ast.parse(fixed)

    def test_partial_alias_removal_keeps_other_names(self) -> None:
        src = "from typing import Any, Optional\n\nx: Any = 1\n"
        diag = LintDiagnostic("m.py", 1, "F401", "`typing.Optional` imported but unused")
        assert fix_source(src, [diag]) == "from typing import Any\n\nx: Any = 1\n"

    def test_aliased_import_removed_by_bound_name(self) -> None:
        src = "import numpy as np\nimport os\n\nprint(os)\n"
        diag = LintDiagnostic("m.py", 1, "F401", "`numpy` imported but unused")
        assert fix_source(src, [diag]) == "import os\n\nprint(os)\n"

    def test_late_unused_import_is_undone_not_moved(self) -> None:
        src = "import os\n\nprint(os)\nimport json\n"
        diags = [
            LintDiagnostic("m.py", 4, "E402", "Module level import not at top of file"),
            LintDiagnostic("m.py", 4, "F401", "`json` imported but unused"),
        ]
        assert fix_source(src, diags) == "import os\n\nprint(os)\n"

    def test_split_preserves_trailing_comment_and_indent(self) -> None:
        src = "def f():\n    a = 1; b = 2  # note\n    return a + b\n"
        diag = LintDiagnostic("m.py", 2, "E702", "Multiple statements on one line")
        assert fix_source(src, [diag]) == (
            "def f():\n    a = 1\n    b = 2  # note\n    return a + b\n"
        )

    def test_unsupported_rule_raises(self) -> None:
        with pytest.raises(UnsafeFixError, match="Unsupported"):
            fix_source("x = 1\n", [LintDiagnostic("m.py", 1, "F841", "unused var")])

    def test_removal_leaving_empty_block_raises(self) -> None:
        src = "try:\n    import os\nexcept ImportError:\n    pass\n"
        with pytest.raises(UnsafeFixError):
            fix_source(src, [LintDiagnostic("m.py", 2, "F401", "`os` imported but unused")])

    def test_syntax_error_source_raises(self) -> None:
        with pytest.raises(UnsafeFixError):
            fix_source("def (:\n", [LintDiagnostic("m.py", 1, "E702", "x")])

    @pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
    def test_fixed_output_passes_ruff(self, tmp_path: Path) -> None:
        target = tmp_path / "mod.py"
        target.write_text(fix_source(SOURCE, parse_lint_diagnostics(CONCISE_LOG)))
        result = subprocess.run(
            [
                "ruff",
                "check",
                "--isolated",
                "--select",
                "E402,E702,F401",
                str(target),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout


class TestGenerateLintFix:
    def test_generates_valid_proposal(self) -> None:
        proposal = generate_lint_fix(
            _incident(), _report_with_log(CONCISE_LOG), _fetch({"pkg/mod.py": SOURCE})
        )
        assert proposal.status == "proposed"
        assert proposal.strategy == "deterministic_linter"
        assert proposal.target_branch_base == "feature/x"
        assert len(proposal.patches) == 1
        patch = proposal.patches[0]
        assert patch.path == "pkg/mod.py"
        assert patch.action == "modify"
        assert patch.content == EXPECTED
        assert patch.diff and patch.diff.startswith("--- a/pkg/mod.py")
        assert "-import os" in patch.diff
        assert validate_fix_guardrails(proposal) == (True, "")

    def test_no_diagnostics_is_no_safe_fix(self) -> None:
        proposal = generate_lint_fix(_incident(), Report(summary="x"), _fetch({}))
        assert proposal.status == "no_safe_fix"
        assert proposal.patches == []

    def test_missing_file_is_no_safe_fix(self) -> None:
        proposal = generate_lint_fix(_incident(), _report_with_log(CONCISE_LOG), _fetch({}))
        assert proposal.status == "no_safe_fix"

    def test_unsupported_rule_is_no_safe_fix(self) -> None:
        log = "pkg/mod.py:2:8: F401 `os` imported but unused\npkg/mod.py:9:5: F841 unused\n"
        proposal = generate_lint_fix(
            _incident(), _report_with_log(log), _fetch({"pkg/mod.py": SOURCE})
        )
        assert proposal.status == "no_safe_fix"
        assert "F841" in proposal.rationale

    def test_too_many_files_is_no_safe_fix(self) -> None:
        log = "".join(f"pkg/m{i}.py:1:8: F401 `os` imported but unused\n" for i in range(4))
        files = {f"pkg/m{i}.py": "import os\n" for i in range(4)}
        proposal = generate_lint_fix(_incident(), _report_with_log(log), _fetch(files))
        assert proposal.status == "no_safe_fix"

    def test_autofix_branch_base_is_no_safe_fix(self) -> None:
        proposal = generate_lint_fix(
            _incident(branch="autofix/run-9"),
            _report_with_log(CONCISE_LOG),
            _fetch({"pkg/mod.py": SOURCE}),
        )
        assert proposal.status == "no_safe_fix"
        assert "forbidden target branch" in proposal.rationale

    def test_forbidden_path_is_no_safe_fix(self) -> None:
        log = ".github/scripts/tool.py:1:8: F401 `os` imported but unused\n"
        proposal = generate_lint_fix(
            _incident(),
            _report_with_log(log),
            _fetch({".github/scripts/tool.py": "import os\nx = 1\n"}),
        )
        assert proposal.status == "no_safe_fix"

    def test_s1_diff_roundtrips_crlf_source(self) -> None:
        original = "import os\r\nimport sys; x = 1\r\nprint(sys, x)\r\n"
        log = (
            "pkg/crlf.py:1:8: F401 `os` imported but unused\n"
            "pkg/crlf.py:2:11: E702 Multiple statements on one line (semicolon)\n"
        )
        proposal = generate_lint_fix(
            _incident(), _report_with_log(log), _fetch({"pkg/crlf.py": original})
        )
        assert proposal.status == "proposed", proposal.rationale
        patch = proposal.patches[0]
        assert patch.content is not None and "\r\n" in patch.content
        assert resolve_patch_content(patch, original) == patch.content

    def test_s1_diff_roundtrips_crlf_source_no_trailing_newline(self) -> None:
        original = "import os\r\nimport sys\r\nprint(sys); x = 1"
        log = (
            "pkg/crlf.py:1:8: F401 `os` imported but unused\n"
            "pkg/crlf.py:3:11: E702 Multiple statements on one line (semicolon)\n"
        )
        proposal = generate_lint_fix(
            _incident(), _report_with_log(log), _fetch({"pkg/crlf.py": original})
        )
        assert proposal.status == "proposed", proposal.rationale
        patch = proposal.patches[0]
        assert patch.content == "import sys\r\nprint(sys)\r\nx = 1"
        assert resolve_patch_content(patch, original) == patch.content

    def test_s1_diff_roundtrips_no_trailing_newline_eof_edit(self) -> None:
        original = "x = 1; y = 2"
        log = "pkg/eof.py:1:6: E702 Multiple statements on one line (semicolon)\n"
        proposal = generate_lint_fix(
            _incident(), _report_with_log(log), _fetch({"pkg/eof.py": original})
        )
        assert proposal.status == "proposed", proposal.rationale
        patch = proposal.patches[0]
        assert resolve_patch_content(patch, original) == patch.content


class TestImportNameExtraction:
    @pytest.mark.parametrize(
        "message, expected",
        [
            ("[*] `os` imported but unused", ["os"]),
            ("'os' imported but unused", ["os"]),
            ('"os" imported but unused', ["os"]),
            ("Linter Violation: F401: math imported but unused", ["math"]),
            ("Linter Violation: F401 math imported but unused", ["math"]),
            ("os.path imported but unused", ["os.path"]),
            ("`typing.Optional` imported but unused", ["typing.Optional"]),
            ("`.sibling` imported but unused", [".sibling"]),
            ("F401 'collections.abc' imported but unused", ["collections.abc"]),
            ("Unused import", []),
        ],
    )
    def test_extracts_names(self, message: str, expected: list[str]) -> None:
        assert extract_import_names(message) == expected


# Annotation shape emitted by the pipeline-logs specialist for live GitHub check runs:
# the rule code lives in the title and the message carries no quoting around the name.
LIVE_SOURCE = "import math\nimport sys\n\n\ndef main() -> None:\n    print(sys.argv)\n"
LIVE_EXPECTED = "import sys\n\n\ndef main() -> None:\n    print(sys.argv)\n"


def _live_annotation(message: str, title: Optional[str] = "Linter Violation: F401") -> Report:
    return Report(
        summary="lint failed",
        annotations=[
            CheckRunAnnotation(
                path="app/main.py",
                start_line=1,
                end_line=1,
                annotation_level="failure",
                title=title,
                message=message,
            )
        ],
    )


class TestUnquotedAndQuotedNames:
    @pytest.mark.parametrize(
        "message",
        [
            "Linter Violation: F401: math imported but unused",
            "math imported but unused",
            "'math' imported but unused",
            '"math" imported but unused',
            "`math` imported but unused",
        ],
    )
    def test_fix_source_accepts_message_variants(self, message: str) -> None:
        diag = LintDiagnostic("app/main.py", 1, "F401", message)
        assert fix_source(LIVE_SOURCE, [diag]) == LIVE_EXPECTED

    @pytest.mark.parametrize(
        "line",
        [
            "app/main.py:1:8: F401 [*] math imported but unused",
            "app/main.py:1:8: F401 'math' imported but unused",
            'app/main.py:1:8: F401 "math" imported but unused',
        ],
    )
    def test_concise_log_variants(self, line: str) -> None:
        diags = parse_lint_diagnostics(line)
        assert [(d.path, d.line, d.code) for d in diags] == [("app/main.py", 1, "F401")]
        assert extract_import_names(diags[0].message) == ["math"]
        assert fix_source(LIVE_SOURCE, diags) == LIVE_EXPECTED

    def test_full_format_unquoted(self) -> None:
        log = "F401 [*] math imported but unused\n --> app/main.py:1:8\n"
        diags = parse_lint_diagnostics(log)
        assert fix_source(LIVE_SOURCE, diags) == LIVE_EXPECTED

    def test_unquoted_name_selects_alias_in_multi_name_import(self) -> None:
        src = "from typing import Any, Optional\n\nx: Any = 1\n"
        diag = LintDiagnostic("m.py", 1, "F401", "typing.Optional imported but unused")
        assert fix_source(src, [diag]) == "from typing import Any\n\nx: Any = 1\n"


class TestAstFallback:
    def test_single_binding_used_when_message_has_no_name(self) -> None:
        diag = LintDiagnostic("app/main.py", 1, "F401", "Unused import")
        assert fix_source(LIVE_SOURCE, [diag]) == LIVE_EXPECTED

    def test_single_binding_used_when_message_name_mismatches(self) -> None:
        # A message name that contradicts the statement must not be replaced by a guess.
        diag = LintDiagnostic("app/main.py", 1, "F401", "`something.else` imported but unused")
        with pytest.raises(UnsafeFixError, match="Cannot locate unused import at line 1"):
            fix_source(LIVE_SOURCE, [diag])

    def test_contradicting_name_does_not_remove_other_import(self) -> None:
        # Regression (R-1): `os` named at the line of `import sys` must not remove the used `sys`.
        src = "import os\nimport sys\n\nprint(sys.argv)\n"
        diag = LintDiagnostic("m.py", 2, "F401", "`os` imported but unused")
        with pytest.raises(UnsafeFixError, match="Cannot locate unused import at line 2"):
            fix_source(src, [diag])

    def test_single_from_import_binding(self) -> None:
        src = "from pathlib import Path\nimport sys\n\nprint(sys)\n"
        diag = LintDiagnostic("m.py", 1, "F401", "unused import")
        assert fix_source(src, [diag]) == "import sys\n\nprint(sys)\n"

    def test_multi_name_import_without_name_is_unsafe(self) -> None:
        src = "from typing import Any, Optional\n\nx: Any = 1\n"
        diag = LintDiagnostic("m.py", 1, "F401", "unused import")
        with pytest.raises(UnsafeFixError, match="Cannot locate unused import"):
            fix_source(src, [diag])

    def test_star_import_without_name_is_unsafe(self) -> None:
        diag = LintDiagnostic("m.py", 1, "F401", "unused import")
        with pytest.raises(UnsafeFixError):
            fix_source("from os import *\n", [diag])

    def test_no_import_at_line_is_unsafe(self) -> None:
        diag = LintDiagnostic("m.py", 1, "F401", "math imported but unused")
        with pytest.raises(UnsafeFixError, match="Cannot locate unused import at line 1"):
            fix_source("x = 1\n", [diag])


class TestLiveAnnotationEndToEnd:
    def test_collects_unquoted_annotation(self) -> None:
        diags = collect_diagnostics(_live_annotation("math imported but unused"))
        assert len(diags) == 1
        assert (diags[0].path, diags[0].line, diags[0].code) == ("app/main.py", 1, "F401")
        assert extract_import_names(diags[0].message) == ["math"]

    @pytest.mark.parametrize(
        "title, message",
        [
            ("Linter Violation: F401", "math imported but unused"),
            ("Linter Violation", "F401: math imported but unused"),
            ("Linter Violation: F401", "'math' imported but unused"),
            ("Linter Violation: F401", '"math" imported but unused'),
            (None, "Linter Violation: F401: math imported but unused"),
        ],
    )
    def test_generate_lint_fix_from_live_annotation(
        self, title: Optional[str], message: str
    ) -> None:
        proposal = generate_lint_fix(
            _incident(),
            _live_annotation(message, title=title),
            _fetch({"app/main.py": LIVE_SOURCE}),
        )
        assert proposal.status == "proposed", proposal.rationale
        assert proposal.strategy == "deterministic_linter"
        patch = proposal.patches[0]
        assert patch.path == "app/main.py"
        assert patch.content == LIVE_EXPECTED
        assert patch.diff and "-import math" in patch.diff
        assert resolve_patch_content(patch, LIVE_SOURCE) == LIVE_EXPECTED
        assert validate_fix_guardrails(proposal) == (True, "")


# --------------------------------------------------------------------------- ruff I001

RUFF = shutil.which("ruff")
requires_ruff = pytest.mark.skipif(RUFF is None, reason="ruff CLI not installed")
REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_APP = REPO_ROOT / "demo" / "sample-pipeline-app"
# Defect injected by demo/scripts/trigger-lint-failure.sh, prepended to app/main.py.
S1_INJECTION = (
    "import math\n"
    "\n"
    "\n"
    "def format_order_currency(value: float) -> str:\n"
    '    formatted_val = round(value, 2); return f"${formatted_val:.2f}"\n'
    "\n"
)
# Diagnostics reported by `ruff check --output-format concise` on the injected file.
S1_LOG = """\
app/main.py:1:8: F401 [*] `math` imported but unused
app/main.py:5:36: E702 Multiple statements on one line (semicolon)
app/main.py:7:1: E402 Module level import not at top of file
app/main.py:8:1: E402 Module level import not at top of file
app/main.py:9:1: E402 Module level import not at top of file
app/main.py:10:1: E402 Module level import not at top of file
app/main.py:12:1: E402 Module level import not at top of file
app/main.py:13:1: E402 Module level import not at top of file
app/main.py:15:1: E402 Module level import not at top of file
Found 9 errors.
"""


def _ruff_lint(source: str, project: Path, rel_path: str, tmp_path: Path) -> str:
    """Lint source as rel_path inside a copy of project's ruff config; return diagnostics."""
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.encode())
    shutil.copy(project / "pyproject.toml", tmp_path / "pyproject.toml")
    assert RUFF is not None
    result = subprocess.run(
        [RUFF, "check", "--select", "E,F,I,W", "--output-format", "concise", rel_path],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.stdout if result.returncode else ""


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@requires_ruff
class TestRuffImportOrganization:
    uses_ruff = True

    def test_fix_source_sorts_and_sections_hoisted_imports(self) -> None:
        src = (
            "import os\n\nprint(os)\n"
            "from .local import helper\nimport fastapi\nimport json\n\n"
            "print(helper, fastapi, json)\n"
        )
        diags = [
            LintDiagnostic("pkg/m.py", line, "E402", "Module level import not at top of file")
            for line in (4, 5, 6)
        ]
        assert fix_source(src, diags) == (
            "import json\nimport os\n\nimport fastapi\n\nfrom .local import helper\n\n"
            "print(os)\n\nprint(helper, fastapi, json)\n"
        )

    def test_combined_fix_output_is_i001_clean(self, tmp_path: Path) -> None:
        fixed = fix_source(SOURCE, parse_lint_diagnostics(CONCISE_LOG))
        assert "import re\nimport sys\nfrom typing import Any\n" in fixed
        assert _ruff_lint(fixed, REPO_ROOT / "bridge", "pkg/mod.py", tmp_path) == ""

    def test_s1_demo_injection_fix_passes_ruff(self, tmp_path: Path) -> None:
        original = S1_INJECTION + (SAMPLE_APP / "app" / "main.py").read_text()
        assert "I001" not in _ruff_lint(original, SAMPLE_APP, "app/main.py", tmp_path)
        proposal = generate_lint_fix(
            _incident(), _report_with_log(S1_LOG), _fetch({"app/main.py": original})
        )
        assert proposal.status == "proposed", proposal.rationale
        patch = proposal.patches[0]
        assert patch.content is not None
        assert _ruff_lint(patch.content, SAMPLE_APP, "app/main.py", tmp_path) == ""
        assert resolve_patch_content(patch, original) == patch.content
        assert "import math" not in patch.content
        assert "    formatted_val = round(value, 2)\n    return " in patch.content
        assert validate_fix_guardrails(proposal) == (True, "")

    def test_crlf_preserved(self, tmp_path: Path) -> None:
        original = "import os\r\n\r\nprint(os)\r\nimport sys\r\nimport json\r\n\r\nprint(sys)\r\n"
        log = (
            "pkg/crlf.py:4:1: E402 Module level import not at top of file\n"
            "pkg/crlf.py:5:1: E402 Module level import not at top of file\n"
            "pkg/crlf.py:5:8: F401 `json` imported but unused\n"
        )
        proposal = generate_lint_fix(
            _incident(), _report_with_log(log), _fetch({"pkg/crlf.py": original})
        )
        assert proposal.status == "proposed", proposal.rationale
        patch = proposal.patches[0]
        expected = "import os\r\nimport sys\r\n\r\nprint(os)\r\n\r\nprint(sys)\r\n"
        assert patch.content == expected
        assert resolve_patch_content(patch, original) == expected
        assert "\n" not in patch.content.replace("\r\n", "")

    def test_crlf_without_trailing_newline_preserved(self) -> None:
        original = "import os\r\n\r\nprint(os)\r\nimport sys\r\nprint(sys); x = 1"
        log = (
            "pkg/crlf.py:4:1: E402 Module level import not at top of file\n"
            "pkg/crlf.py:5:11: E702 Multiple statements on one line (semicolon)\n"
        )
        proposal = generate_lint_fix(
            _incident(), _report_with_log(log), _fetch({"pkg/crlf.py": original})
        )
        assert proposal.status == "proposed", proposal.rationale
        patch = proposal.patches[0]
        assert patch.content == "import os\r\nimport sys\r\n\r\nprint(os)\r\nprint(sys)\r\nx = 1"
        assert resolve_patch_content(patch, original) == patch.content

    def test_unhoisted_fix_keeps_layout_ruff_rejects_in_original(self) -> None:
        # No blank line after the imports: ruff's defaults reject the original layout, so the
        # target evidently uses settings ruff cannot see; a fix that moves no import keeps it.
        original = "import os\r\nimport sys\r\nprint(sys); x = 1"
        diags = [
            LintDiagnostic("pkg/crlf.py", 1, "F401", "`os` imported but unused"),
            LintDiagnostic("pkg/crlf.py", 3, "E702", "Multiple statements on one line"),
        ]
        assert fix_source(original, diags) == "import sys\r\nprint(sys)\r\nx = 1"

    def test_unhoisted_fix_resections_emptied_section(self, tmp_path: Path) -> None:
        # Removing the only third-party import leaves two blank lines; ruff accepts the
        # original layout, so the result is re-sectioned to stay I001-clean.
        original = "import os\n\nimport fastapi\n\nfrom app.config import cfg\n\nprint(os, cfg)\n"
        diag = LintDiagnostic("app/main.py", 3, "F401", "`fastapi` imported but unused")
        fixed = fix_source(original, [diag])
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "__init__.py").touch()
        assert fixed == "import os\n\nfrom app.config import cfg\n\nprint(os, cfg)\n"
        assert _ruff_lint(fixed, SAMPLE_APP, "app/main.py", tmp_path) == ""

    def test_organize_imports_uses_stdin_path_for_relative_imports(self) -> None:
        src = "from .config import cfg\nimport os\n\nprint(os, cfg)\n"
        assert organize_imports(src, "app/main.py") == (
            "import os\n\nfrom .config import cfg\n\nprint(os, cfg)\n"
        )

    @pytest.mark.parametrize("path", ["app/main.py", "src/app/main.py", "app/api/routes.py"])
    def test_absolute_first_party_import_gets_own_section(self, path: str) -> None:
        src = "from app.config import cfg\nimport fastapi\nimport os\n\nprint(os, fastapi, cfg)\n"
        assert organize_imports(src, path) == (
            "import os\n\nimport fastapi\n\nfrom app.config import cfg\n\nprint(os, fastapi, cfg)\n"
        )

    def test_first_party_detection_is_not_hardcoded(self) -> None:
        src = "import requests\nfrom mypkg.util import x\n\nprint(requests, x)\n"
        expected = "import requests\n\nfrom mypkg.util import x\n\nprint(requests, x)\n"
        assert organize_imports(src, "mypkg/core.py") == expected
        # Outside its own package, the import is third-party and shares the section.
        assert organize_imports(src, "other/core.py") == (
            "import requests\nfrom mypkg.util import x\n\nprint(requests, x)\n"
        )

    @pytest.mark.parametrize("path", ["../escape/m.py", "/abs/pkg/m.py", "my-dir/m.py"])
    def test_unsafe_paths_are_not_mirrored(self, path: str) -> None:
        assert s1_lint._package_dirs(path) == []
        src = "import sys\nimport os\n\nprint(os, sys)\n"
        assert organize_imports(src, path) == "import os\nimport sys\n\nprint(os, sys)\n"

    def test_organize_imports_ignores_invoking_directory_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.ruff.lint.isort]\nforce-single-line = true\n"
        )
        monkeypatch.chdir(tmp_path)
        src = "from typing import Optional, Any\n\nprint(Any, Optional)\n"
        assert organize_imports(src, "m.py") == (
            "from typing import Any, Optional\n\nprint(Any, Optional)\n"
        )


FIRST_PARTY_SOURCE = (
    "import json\nimport os\n\nimport fastapi\n\nfrom app.config import cfg\n\n"
    "print(os, fastapi, cfg)\n"
)


@requires_ruff
class TestAbsoluteFirstPartyImports:
    """Regression for review finding R1: first-party imports must keep their own section."""

    uses_ruff = True

    @pytest.fixture(autouse=True)
    def _app_package(self, tmp_path: Path) -> None:
        # The lint checker sees the target's own package on disk, as in the sample app.
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "__init__.py").touch()

    def test_original_is_i001_clean(self, tmp_path: Path) -> None:
        diagnostics = _ruff_lint(FIRST_PARTY_SOURCE, SAMPLE_APP, "app/main.py", tmp_path)
        assert "F401" in diagnostics and "I001" not in diagnostics

    def test_f401_only_fix_passes_ruff(self, tmp_path: Path) -> None:
        diag = LintDiagnostic("app/main.py", 1, "F401", "`json` imported but unused")
        fixed = fix_source(FIRST_PARTY_SOURCE, [diag])
        assert fixed == (
            "import os\n\nimport fastapi\n\nfrom app.config import cfg\n\nprint(os, fastapi, cfg)\n"
        )
        assert _ruff_lint(fixed, SAMPLE_APP, "app/main.py", tmp_path) == ""

    def test_e402_hoist_passes_ruff(self, tmp_path: Path) -> None:
        original = (
            "import os\n\nimport fastapi\n\nprint(os, fastapi)\n"
            "from app.config import cfg\nimport json\n\nprint(cfg, json)\n"
        )
        diags = [
            LintDiagnostic("app/main.py", line, "E402", "Module level import not at top of file")
            for line in (6, 7)
        ]
        fixed = fix_source(original, diags)
        assert fixed == (
            "import json\nimport os\n\nimport fastapi\n\nfrom app.config import cfg\n\n"
            "print(os, fastapi)\n\nprint(cfg, json)\n"
        )
        assert _ruff_lint(fixed, SAMPLE_APP, "app/main.py", tmp_path) == ""

    def test_src_layout_hoist_passes_ruff(self, tmp_path: Path) -> None:
        project = tmp_path / "config"
        project.mkdir()
        (project / "pyproject.toml").write_text('[tool.ruff]\nsrc = ["src"]\n')
        (tmp_path / "src" / "app").mkdir(parents=True)
        (tmp_path / "src" / "app" / "__init__.py").touch()
        original = (
            "import os\n\nprint(os)\nfrom app.config import cfg\nimport fastapi\n\n"
            "print(cfg, fastapi)\n"
        )
        diags = [
            LintDiagnostic("src/app/main.py", line, "E402", "Module level import not at top")
            for line in (4, 5)
        ]
        fixed = fix_source(original, diags)
        assert "import fastapi\n\nfrom app.config import cfg\n" in fixed
        assert _ruff_lint(fixed, project, "src/app/main.py", tmp_path) == ""

    def test_subpackage_named_like_third_party_stays_third_party(self, tmp_path: Path) -> None:
        # Review finding R2: only the top-level package is first-party, so ``celery`` stays
        # third-party even inside ``app/celery/``.
        (tmp_path / "app" / "celery").mkdir()
        (tmp_path / "app" / "celery" / "__init__.py").touch()
        path = "app/celery/tasks.py"
        original = (
            "import os\n\nfrom celery import Celery\n\nfrom app.config import cfg\n\n"
            "print(os, cfg)\nimport json\n\nprint(Celery, json)\n"
        )
        assert _ruff_lint(original, SAMPLE_APP, path, tmp_path).count("E402") == 1
        diag = LintDiagnostic(path, 8, "E402", "Module level import not at top of file")
        fixed = fix_source(original, [diag])
        assert fixed == (
            "import json\nimport os\n\nfrom celery import Celery\n\nfrom app.config import cfg\n\n"
            "print(os, cfg)\n\nprint(Celery, json)\n"
        )
        assert _ruff_lint(fixed, SAMPLE_APP, path, tmp_path) == ""


class TestRuffFallback:
    SRC = "import sys\nimport os\n\nprint(os, sys)\n"

    def test_missing_ruff_returns_source(self, caplog: pytest.LogCaptureFixture) -> None:
        assert organize_imports(self.SRC, "m.py") == self.SRC
        assert "ruff CLI not found" in caplog.text

    def test_missing_ruff_fix_source_still_fixes(self) -> None:
        diag = LintDiagnostic("m.py", 4, "E402", "Module level import not at top of file")
        assert fix_source("import os\n\nprint(os)\nimport sys\n", [diag]) == (
            "import os\nimport sys\n\nprint(os)\n"
        )

    @pytest.mark.parametrize(
        "outcome",
        [
            _FakeCompleted(2, stderr="error: boom"),
            _FakeCompleted(1, stdout="import sys\n", stderr="invalid-syntax"),
            _FakeCompleted(0, stdout=""),
            _FakeCompleted(0, stdout="import os\nimport sys\n\nprint(os)\n"),  # code altered
            _FakeCompleted(0, stdout="import os\nimport sys\n\nprint(os, sys\n"),  # unparsable
            _FakeCompleted(0, stdout="import sys\n\nprint(os, sys)\n"),  # import dropped
            subprocess.TimeoutExpired(cmd="ruff", timeout=10),
            FileNotFoundError("ruff"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ],
    )
    def test_ruff_failure_returns_source(
        self,
        outcome: object,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        calls: list[dict[str, object]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            calls.append({"cmd": cmd, **kwargs})
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(s1_lint, "_ruff_executable", lambda: "/usr/bin/ruff")
        monkeypatch.setattr(s1_lint.subprocess, "run", fake_run)
        assert organize_imports(self.SRC, "pkg/m.py") == self.SRC
        assert "ruff import organization" in caplog.text
        assert calls[0]["timeout"] == 10
        assert calls[0]["input"] == self.SRC
        assert calls[0]["encoding"] == "utf-8"
        cmd = calls[0]["cmd"]
        assert isinstance(cmd, list)
        assert cmd[-3:] == ["--stdin-filename", "pkg/m.py", "-"]
        assert "--isolated" in cmd and "--fix" in cmd
        assert cmd[cmd.index("--config") + 1] == 'lint.isort.known-first-party = ["pkg"]'

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("app/celery/tasks.py", '["app"]'),
            ("app/db/redis/client.py", '["app"]'),
            ("src/app/celery/tasks.py", '["app", "src"]'),
            ("src/m.py", '["src"]'),
        ],
    )
    def test_only_top_level_package_is_first_party(
        self, path: str, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            calls.append(cmd)
            return _FakeCompleted(0, stdout=self.SRC)

        monkeypatch.setattr(s1_lint, "_ruff_executable", lambda: "/usr/bin/ruff")
        monkeypatch.setattr(s1_lint.subprocess, "run", fake_run)
        assert organize_imports(self.SRC, path) == self.SRC
        cmd = calls[0]
        assert cmd[cmd.index("--config") + 1] == f"lint.isort.known-first-party = {expected}"

    def test_top_level_file_passes_no_first_party_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            calls.append(cmd)
            return _FakeCompleted(0, stdout=self.SRC)

        monkeypatch.setattr(s1_lint, "_ruff_executable", lambda: "/usr/bin/ruff")
        monkeypatch.setattr(s1_lint.subprocess, "run", fake_run)
        assert organize_imports(self.SRC, "m.py") == self.SRC
        assert "--config" not in calls[0]

    def test_generate_lint_fix_passes_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def fake_organize(source: str, path: str) -> str:
            seen.append(path)
            return source

        monkeypatch.setattr(s1_lint, "organize_imports", fake_organize)
        log = "pkg/mod.py:2:8: F401 `os` imported but unused\n"
        files = {"pkg/mod.py": "import sys\nimport os\n\nprint(sys)\n"}
        proposal = generate_lint_fix(_incident(), _report_with_log(log), _fetch(files))
        assert proposal.status == "proposed", proposal.rationale
        assert seen == ["pkg/mod.py"]

    def test_fix_source_path_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def fake_organize(source: str, path: str) -> str:
            seen.append(path)
            return source

        monkeypatch.setattr(s1_lint, "organize_imports", fake_organize)
        diag = LintDiagnostic("pkg/a.py", 1, "F401", "`os` imported but unused")
        fix_source("import os\nx = 1\n", [diag])
        fix_source("import os\nx = 1\n", [diag], path="explicit/b.py")
        fix_source("x = 1\n", [])
        assert seen == ["pkg/a.py", "explicit/b.py", "module.py"]
