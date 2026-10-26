"""Tests for the deterministic hunk-scoped commit revert fixer (synthetic fixtures only)."""

import difflib
from typing import Any, Optional

import pytest
from bridge.app.contracts.report import CheckRunAnnotation
from bridge.app.fixer.commit_hunk_revert import (
    generate_commit_hunk_revert,
    invert_hunks,
    is_config_or_build_path,
    parse_hunks,
    render_hunks,
)
from bridge.app.fixer.diff_apply import apply_unified_diff

BASE = "trunk"


def _patch(before: str, after: str, context: int = 3) -> str:
    """GitHub-style ``files[].patch``: hunks only, no file headers."""
    lines = list(
        difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=context)
    )
    return "\n".join(lines[2:])


def _revert_all(before: str, after: str, context: int = 3) -> str:
    hunks = parse_hunks(_patch(before, after, context))
    diff = render_hunks("cfg/x.yaml", invert_hunks(hunks, range(len(hunks))))
    return apply_unified_diff(after, diff)


def _numbered(n: int, prefix: str = "line") -> list[str]:
    return [f"{prefix} {i}" for i in range(1, n + 1)]


class TestParseHunks:
    def test_omitted_counts_default_to_one(self) -> None:
        (hunk,) = parse_hunks("@@ -3 +3 @@\n-old\n+new")
        assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (3, 1, 3, 1)

    def test_truncated_body_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_hunks("@@ -1,3 +1,3 @@\n a\n-b")

    def test_trailing_garbage_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_hunks("@@ -1 +1 @@\n-a\n+b\nnot a hunk line")

    def test_heading_text_is_ignored_and_not_rendered(self) -> None:
        hunks = parse_hunks("@@ -1,2 +1,2 @@ section heading @@ text\n ctx\n-a\n+b")
        rendered = render_hunks("p.yaml", invert_hunks(hunks, [0]))
        assert "heading" not in rendered
        assert "@@ -1,2 +1,2 @@\n ctx\n-b\n+a\n" in rendered

    def test_no_newline_marker_attaches_to_preceding_line(self) -> None:
        (hunk,) = parse_hunks(
            "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n"
            "\\ No newline at end of file"
        )
        assert hunk.lines == [(" ", "a", False), ("-", "b", True), ("+", "c", True)]


class TestInvertHunks:
    def test_modify_round_trips(self) -> None:
        before = "\n".join(_numbered(10)) + "\n"
        after = before.replace("line 5\n", "line five\n")
        assert _revert_all(before, after) == before

    def test_pure_addition_round_trips(self) -> None:
        before = "\n".join(_numbered(6)) + "\n"
        after = before.replace("line 3\n", "line 3\nadded a\nadded b\n")
        assert _revert_all(before, after) == before

    def test_pure_removal_round_trips(self) -> None:
        before = "\n".join(_numbered(6)) + "\n"
        after = before.replace("line 2\nline 3\n", "")
        assert _revert_all(before, after) == before

    def test_zero_context_insertion_and_removal_headers(self) -> None:
        before = "\n".join(_numbered(6)) + "\n"
        after = before.replace("line 3\n", "line 3\nnew\n")
        patch = _patch(before, after, context=0)
        assert patch.startswith("@@ -3,0 +4 @@")
        (inverted,) = invert_hunks(parse_hunks(patch), [0])
        assert (inverted.old_start, inverted.old_count) == (4, 1)
        assert (inverted.new_start, inverted.new_count) == (3, 0)
        assert _revert_all(before, after, context=0) == before
        removal = before.replace("line 4\n", "")
        assert _revert_all(before, removal, context=0) == before

    def test_multi_hunk_round_trips(self) -> None:
        lines = _numbered(40)
        before = "\n".join(lines) + "\n"
        changed = list(lines)
        changed[2] = "changed 3"
        changed.insert(20, "inserted")
        del changed[35]
        after = "\n".join(changed) + "\n"
        hunks = parse_hunks(_patch(before, after))
        assert len(hunks) == 3
        inverted = invert_hunks(hunks, range(3))
        assert [h.new_start for h in inverted] == [h.old_start for h in hunks]
        assert _revert_all(before, after) == before

    def test_subset_reverts_only_selected_hunk_with_consistent_numbers(self) -> None:
        lines = _numbered(40)
        before = "\n".join(lines) + "\n"
        changed = list(lines)
        changed[2:3] = ["first a", "first b"]
        changed[30] = "second"
        after = "\n".join(changed) + "\n"
        hunks = parse_hunks(_patch(before, after))
        (inverted,) = invert_hunks(hunks, [1])
        # Hunk 0 is kept, so the reverted hunk keeps the failing commit's numbering.
        assert inverted.new_start == inverted.old_start
        result = apply_unified_diff(after, render_hunks("f.yaml", [inverted]))
        assert result == after.replace("second\n", "line 30\n")

    def test_removed_then_added_order_after_swap(self) -> None:
        (inverted,) = invert_hunks(parse_hunks("@@ -1,3 +1,3 @@\n a\n-b\n+c\n d"), [0])
        assert [kind for kind, _, _ in inverted.lines] == [" ", "-", "+", " "]
        assert inverted.lines[1][1] == "c" and inverted.lines[2][1] == "b"

    def test_no_newline_marker_round_trips(self) -> None:
        before = "a\nb"
        after = "a\nb\nc\n"
        patch = "@@ -1,2 +1,3 @@\n a\n-b\n\\ No newline at end of file\n+b\n+c"
        diff = render_hunks("f.yaml", invert_hunks(parse_hunks(patch), [0]))
        assert "+b\n\\ No newline at end of file\n" in diff
        assert apply_unified_diff(after, diff) == before


class TestPathClassification:
    @pytest.mark.parametrize(
        "path",
        ["ops/release.sh", "k8s/svc.yaml", "Containerfile", "img/Dockerfile.dev", "x.dockerfile"],
    )
    def test_config_and_build_files(self, path: str) -> None:
        assert is_config_or_build_path(path)

    @pytest.mark.parametrize(
        "path", ["src/app.py", "README.md", ".github/workflows/ci.yml", "secrets/.env.yaml"]
    )
    def test_other_files(self, path: str) -> None:
        assert not is_config_or_build_path(path)


SCRIPT_BEFORE = (
    "\n".join(
        ["#!/usr/bin/env bash", "set -eu", "WORKER_LIMIT=4", "QUEUE=jobs"] + _numbered(20, "step")
    )
    + "\n"
)
SCRIPT_AFTER = SCRIPT_BEFORE.replace("WORKER_LIMIT=4\n", "WORKER_LIMIT=1\nEXTRA_FLAG=on\n")
BUILD_BEFORE = "FROM base:1\nWORKDIR /srv\nADD app/ /srv/\nRUN make\n"
BUILD_AFTER = BUILD_BEFORE.replace(
    "ADD app/ /srv/\n", "ADD app/ /srv/\nADD assets/bundle.tar /srv/\n"
)


def _file(
    path: str, before: str, after: str, status: str = "modified", **extra: Any
) -> dict[str, Any]:
    return {"filename": path, "status": status, "patch": _patch(before, after), **extra}


def _commit(*files: dict[str, Any], parents: int = 1) -> dict[str, Any]:
    return {
        "sha": "0123456789abcdef",
        "parents": [{"sha": f"p{i}"} for i in range(parents)],
        "files": list(files),
    }


def _generate(
    commit: Optional[dict[str, Any]],
    diagnosis: list[str],
    evidence: Optional[list[str]] = None,
    annotations: Optional[list[CheckRunAnnotation]] = None,
):
    return generate_commit_hunk_revert(
        commit, diagnosis, (evidence or []), (annotations or []), BASE
    )


class TestGenerateCommitHunkRevert:
    def test_single_hunk_in_named_file_is_reverted(self) -> None:
        commit = _commit(
            _file("ops/release.sh", SCRIPT_BEFORE, SCRIPT_AFTER),
            _file("src/app.py", "x = 1\n", "x = 2\n"),
        )
        proposal = _generate(commit, ["Release failed after ops/release.sh was changed"])
        assert proposal is not None and proposal.status == "proposed"
        assert proposal.strategy == "commit_hunk_revert"
        assert proposal.target_branch_base == BASE
        assert [p.path for p in proposal.patches] == ["ops/release.sh"]
        patch = proposal.patches[0]
        assert patch.action == "modify" and patch.content is None
        assert apply_unified_diff(SCRIPT_AFTER, patch.diff or "") == SCRIPT_BEFORE
        assert "0123456" in proposal.rationale and "ops/release.sh" in proposal.rationale

    def test_build_definition_named_by_basename(self) -> None:
        commit = _commit(_file("images/Containerfile", BUILD_BEFORE, BUILD_AFTER))
        proposal = _generate(commit, ["The ADD instruction in Containerfile failed"])
        assert proposal is not None and proposal.status == "proposed"
        assert apply_unified_diff(BUILD_AFTER, proposal.patches[0].diff or "") == BUILD_BEFORE

    def test_only_referenced_hunk_is_reverted(self) -> None:
        lines = _numbered(40, "key")
        before = "\n".join(lines) + "\n"
        changed = list(lines)
        changed[3] = "timeout: 1s"
        changed[30] = "replicas: 9"
        after = "\n".join(changed) + "\n"
        commit = _commit(_file("deploy/app.yaml", before, after))
        proposal = _generate(
            commit,
            ["Rollout of deploy/app.yaml failed"],
            evidence=["probe error: timeout: 1s exceeded"],
        )
        assert proposal is not None and proposal.status == "proposed"
        result = apply_unified_diff(after, proposal.patches[0].diff or "")
        assert "key 4" in result and "timeout: 1s" not in result
        assert "replicas: 9" in result

    def test_hunk_selected_by_annotation_range(self) -> None:
        lines = _numbered(40, "key")
        before = "\n".join(lines) + "\n"
        changed = list(lines)
        changed[3] = "alpha: 1"
        changed[30] = "beta: 2"
        after = "\n".join(changed) + "\n"
        commit = _commit(_file("deploy/app.yaml", before, after))
        annotation = CheckRunAnnotation(
            path="deploy/app.yaml", start_line=31, end_line=31, message="invalid value"
        )
        proposal = _generate(commit, ["Validation failed"], annotations=[annotation])
        assert proposal is not None and proposal.status == "proposed"
        result = apply_unified_diff(after, proposal.patches[0].diff or "")
        assert "beta: 2" not in result and "alpha: 1" in result

    def test_multi_hunk_without_line_reference_is_ambiguous(self) -> None:
        lines = _numbered(40, "key")
        before = "\n".join(lines) + "\n"
        changed = list(lines)
        changed[3] = "alpha: 1"
        changed[30] = "beta: 2"
        after = "\n".join(changed) + "\n"
        commit = _commit(_file("deploy/app.yaml", before, after))
        assert _generate(commit, ["deploy/app.yaml is invalid"]) is None

    def test_two_named_files_need_line_references(self) -> None:
        commit = _commit(
            _file("ops/release.sh", SCRIPT_BEFORE, SCRIPT_AFTER),
            _file("images/Containerfile", BUILD_BEFORE, BUILD_AFTER),
        )
        diagnosis = ["Both ops/release.sh and images/Containerfile changed"]
        assert _generate(commit, diagnosis) is None
        proposal = _generate(
            commit, diagnosis, evidence=["WORKER_LIMIT=1", "ADD assets/bundle.tar /srv/"]
        )
        assert proposal is not None and proposal.status == "proposed"
        assert sorted(p.path for p in proposal.patches) == [
            "images/Containerfile",
            "ops/release.sh",
        ]

    def test_trivial_lines_do_not_count_as_references(self) -> None:
        lines = _numbered(40, "key")
        before = "\n".join(lines) + "\n"
        changed = list(lines)
        changed[3] = "fi"
        changed[30] = "}"
        after = "\n".join(changed) + "\n"
        commit = _commit(_file("deploy/app.yaml", before, after))
        assert _generate(commit, ["deploy/app.yaml: fi } errors"]) is None

    def test_file_not_named_by_diagnosis_is_not_claimed(self) -> None:
        commit = _commit(_file("ops/release.sh", SCRIPT_BEFORE, SCRIPT_AFTER))
        assert _generate(commit, ["Unit tests failed in tests/test_core.py"]) is None

    def test_evidence_mention_alone_does_not_name_the_file(self) -> None:
        commit = _commit(_file("ops/release.sh", SCRIPT_BEFORE, SCRIPT_AFTER))
        proposal = _generate(
            commit, ["Assertion failed in unit tests"], evidence=["touched ops/release.sh"]
        )
        assert proposal is None

    def test_application_code_change_is_not_claimed(self) -> None:
        commit = _commit(_file("src/app.py", "x = 1\n", "x = 2\n"))
        assert _generate(commit, ["Unit test failure in src/app.py"]) is None

    def test_no_config_change_in_commit_is_not_claimed(self) -> None:
        commit = _commit(_file("docs/notes.txt", "a\n", "b\n"))
        assert _generate(commit, ["Container exited with code 137 (out of memory)"]) is None

    @pytest.mark.parametrize("status", ["added", "removed", "renamed", "copied"])
    def test_non_modified_files_are_not_claimed(self, status: str) -> None:
        commit = _commit(_file("ops/release.sh", SCRIPT_BEFORE, SCRIPT_AFTER, status=status))
        assert _generate(commit, ["ops/release.sh broke the release"]) is None

    def test_rename_with_edits_is_not_claimed(self) -> None:
        entry = _file("ops/release.sh", SCRIPT_BEFORE, SCRIPT_AFTER, previous_filename="ops/old.sh")
        assert _generate(_commit(entry), ["ops/release.sh broke the release"]) is None

    def test_missing_or_truncated_patch_is_not_claimed(self) -> None:
        missing = {"filename": "ops/release.sh", "status": "modified"}
        assert _generate(_commit(missing), ["ops/release.sh broke"]) is None
        truncated = _file("ops/release.sh", SCRIPT_BEFORE, SCRIPT_AFTER)
        truncated["patch"] = truncated["patch"].rsplit("\n", 2)[0]
        assert _generate(_commit(truncated), ["ops/release.sh broke"]) is None

    def test_merge_commit_and_missing_commit_are_not_claimed(self) -> None:
        commit = _commit(_file("ops/release.sh", SCRIPT_BEFORE, SCRIPT_AFTER), parents=2)
        assert _generate(commit, ["ops/release.sh broke"]) is None
        assert _generate(None, ["ops/release.sh broke"]) is None
        assert _generate({"sha": "x"}, ["ops/release.sh broke"]) is None

    def test_guardrail_violation_yields_no_safe_fix(self) -> None:
        before = "\n".join(_numbered(5)) + "\n"
        after = before + "".join(f"extra {i}\n" for i in range(60))
        commit = _commit(_file("ops/release.sh", before, after))
        proposal = _generate(commit, ["ops/release.sh broke"])
        assert proposal is not None and proposal.status == "no_safe_fix"
        assert "Guardrail" in proposal.rationale
