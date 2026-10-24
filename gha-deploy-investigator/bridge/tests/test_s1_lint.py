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
from bridge.app.fixer.interface import validate_fix_guardrails
from bridge.app.fixer.s1_lint import (
    LintDiagnostic,
    UnsafeFixError,
    collect_diagnostics,
    fix_source,
    generate_lint_fix,
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
