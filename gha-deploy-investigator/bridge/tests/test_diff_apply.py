import difflib

import pytest
from bridge.app.fixer import apply_unified_diff

ORIGINAL = "".join(f"line {i}\n" for i in range(1, 21))


def _difflib(before: str, after: str, path: str = "src/app.py") -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


class TestApplyUnifiedDiff:
    def test_clean_application(self) -> None:
        diff = "--- a/m.py\n+++ b/m.py\n@@ -1,2 +1 @@\n-import os\n x = 1\n"
        assert apply_unified_diff("import os\nx = 1\n", diff) == "x = 1\n"

    def test_single_line_change(self) -> None:
        diff = "--- a/c.sh\n+++ b/c.sh\n@@ -2 +2 @@\n-MEM=1Gi\n+MEM=2Gi\n"
        assert apply_unified_diff("a\nMEM=1Gi\nb\n", diff) == "a\nMEM=2Gi\nb\n"

    def test_multiple_hunks(self) -> None:
        after = ORIGINAL.replace("line 2\n", "line two\n").replace("line 18\n", "")
        diff = _difflib(ORIGINAL, after)
        assert diff.count("@@ -") == 2
        assert apply_unified_diff(ORIGINAL, diff) == after

    def test_roundtrip_matches_difflib_output_without_trailing_newline_terminators(self) -> None:
        # Shape produced by the S4 fixer: lineterm="" joined with "\n".
        after = ORIGINAL.replace("line 10\n", "line ten\n")
        diff = "\n".join(
            difflib.unified_diff(
                ORIGINAL.splitlines(), after.splitlines(), "a/x", "b/x", lineterm=""
            )
        )
        assert apply_unified_diff(ORIGINAL, diff) == after

    def test_context_mismatch_raises(self) -> None:
        diff = "--- a/m.py\n+++ b/m.py\n@@ -1,2 +1 @@\n-import sys\n x = 1\n"
        with pytest.raises(ValueError, match="context not found"):
            apply_unified_diff("import os\nx = 1\n", diff)

    def test_wrong_line_number_is_relocated_when_context_is_unique(self) -> None:
        diff = "--- a/x\n+++ b/x\n@@ -3,3 +3,3 @@\n line 11\n-line 12\n+line twelve\n line 13\n"
        assert apply_unified_diff(ORIGINAL, diff) == ORIGINAL.replace("line 12\n", "line twelve\n")

    def test_ambiguous_relocation_raises(self) -> None:
        original = "a\nx\nb\na\nx\nb\nz\n"
        diff = "--- a/f\n+++ b/f\n@@ -7,2 +7,2 @@\n a\n-x\n+y\n"
        with pytest.raises(ValueError, match="ambiguous"):
            apply_unified_diff(original, diff)

    def test_hunk_count_mismatch_raises(self) -> None:
        diff = "--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,3 @@\n-import os\n x = 1\n"
        with pytest.raises(ValueError, match="counts"):
            apply_unified_diff("import os\nx = 1\n", diff)

    @pytest.mark.parametrize(
        "diff",
        [
            "",
            "--- a/m.py\n+++ b/m.py\n",
            "garbage\n@@ -1 +1 @@\n-a\n+b\n",
            "--- a/m\n+++ b/m\n@@ -1 +1 @@\n-a\n+b\n--- a/n\n+++ b/n\n@@ -1 +1 @@\n-a\n+b\n",
            "--- a/m\n+++ b/m\n@@ -1 +1 @@\n*a\n+b\n",
        ],
    )
    def test_malformed_diff_raises(self, diff: str) -> None:
        with pytest.raises(ValueError):
            apply_unified_diff("a\n", diff)

    def test_original_without_trailing_newline_is_preserved(self) -> None:
        diff = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n-a\n+A\n b\n"
        assert apply_unified_diff("a\nb", diff) == "A\nb"

    def test_marker_adds_trailing_newline(self) -> None:
        after = "a\nB\n"
        diff = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+B\n"
        assert apply_unified_diff("a\nb", diff) == after

    def test_marker_removes_trailing_newline(self) -> None:
        diff = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n\\ No newline at end of file\n"
        assert apply_unified_diff("a\nb\n", diff) == "a\nB"

    def test_old_side_marker_contradicting_original_raises(self) -> None:
        diff = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+B\n"
        with pytest.raises(ValueError, match="trailing newline"):
            apply_unified_diff("a\nb\n", diff)

    def test_create_from_dev_null(self) -> None:
        diff = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
        assert apply_unified_diff("", diff) == "hello\nworld\n"

    def test_blank_context_line_without_leading_space(self) -> None:
        diff = "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n\n-c\n+C\n"
        assert apply_unified_diff("a\n\nc\n", diff) == "a\n\nC\n"

    def test_crlf_original_preserves_line_endings(self) -> None:
        diff = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
        assert apply_unified_diff("a\r\nb\r\n", diff) == "a\r\nB\r\n"
