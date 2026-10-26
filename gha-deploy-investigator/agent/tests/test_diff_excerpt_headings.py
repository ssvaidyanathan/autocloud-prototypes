"""Hunk heading sanitization in commit diff evidence (synthetic fixtures)."""

from agent.app.specialists.base import format_git_diff_excerpt, strip_hunk_headings


def test_format_git_diff_excerpt_strips_hunk_headings() -> None:
    patch = (
        "@@ -3,2 +3,2 @@ section_one:\n ctx\n-a\n+b\n"
        "@@ -20 +20 @@\n-c\n+d\n"
        "@@ -40,0 +41,1 @@ def handler(): @@ nested\n+e"
    )
    res = format_git_diff_excerpt({"files": [{"filename": "cfg/app.yaml", "patch": patch}]})
    assert "section_one" not in res and "handler" not in res
    assert "@@ -3,2 +3,2 @@\n ctx\n-a\n+b\n@@ -20 +20 @@\n-c\n+d\n@@ -40,0 +41,1 @@\n+e" in res


def test_strip_hunk_headings_leaves_body_lines_alone() -> None:
    text = "+@@ -1 +1 @@ keep\n @@ text"
    assert strip_hunk_headings(text) == text
