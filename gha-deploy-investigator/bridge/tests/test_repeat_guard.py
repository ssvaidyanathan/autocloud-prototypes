"""Tests for the repeat-fix guard (synthetic fixtures only)."""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import pytest
from bridge.app.config import BridgeConfig
from bridge.app.contracts.incident import Incident
from bridge.app.fixer.interface import AUTOFIX_BRANCH_PREFIX, FixPRWriterBase
from bridge.app.fixer.repeat_guard import (
    RepeatAutofixDetection,
    check_repeat_autofix,
    detect_repeat_autofix,
    format_repeat_guard_work_note,
)
from bridge.app.fixer.writer import FixPRWriter
from bridge.tests.test_fixer_writer import API, OWNER, REPO, FakeGitHub

SHA = "9f8e7d6c5b4a3210"
DEFAULT = "trunk"
FIX_BRANCH = f"{AUTOFIX_BRANCH_PREFIX}run-41"
BOT = "remediator-app[bot]"


def _incident(branch: Optional[str] = DEFAULT, run_id: Optional[int] = 777) -> Incident:
    return Incident(
        id="gha-guard",
        source="github",
        external_id="777",
        external_key=f"{OWNER}/{REPO}#777",
        title="CI failure",
        description="d",
        severity="P3",
        opened_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
        github_repo=f"{OWNER}/{REPO}",
        github_run_id=run_id,
        github_head_sha=SHA,
        github_branch=branch,
    )


def _commit(
    message: str = "Regular change", author: Optional[str] = None, committer: Optional[str] = None
) -> dict[str, Any]:
    commit: dict[str, Any] = {"sha": SHA, "commit": {"message": message}}
    if author:
        commit["author"] = {"login": author}
    if committer:
        commit["committer"] = {"login": committer}
    return commit


def _pull(number: int, ref: str, merged: bool = True) -> dict[str, Any]:
    return {
        "number": number,
        "head": {"ref": ref},
        "merged_at": "2026-10-26T10:00:00Z" if merged else None,
    }


def _detect(
    commit: Optional[dict[str, Any]] = None,
    pulls: Optional[list[dict[str, Any]]] = None,
    bot_login: Optional[str] = None,
    incident: Optional[Incident] = None,
    default_branch: Optional[str] = DEFAULT,
) -> Optional[RepeatAutofixDetection]:
    return detect_repeat_autofix(
        incident or _incident(), default_branch, commit, pulls or [], bot_login
    )


class TestDetectRepeatAutofix:
    def test_merge_commit_message(self) -> None:
        detection = _detect(_commit(f"Merge pull request #41 from {OWNER}/{FIX_BRANCH}\n\nfix"))
        assert detection is not None
        assert (detection.signal, detection.pr_number, detection.branch) == (
            "merge_message",
            41,
            FIX_BRANCH,
        )
        assert detection.default_branch == DEFAULT and detection.run_id == "777"

    def test_merge_branch_message_takes_pr_number_from_pulls(self) -> None:
        detection = _detect(
            _commit(f"Merge branch '{FIX_BRANCH}' into {DEFAULT}"), pulls=[_pull(41, FIX_BRANCH)]
        )
        assert detection is not None
        assert (detection.signal, detection.pr_number, detection.branch) == (
            "merge_message",
            41,
            FIX_BRANCH,
        )

    def test_merge_branch_message_without_pull(self) -> None:
        detection = _detect(_commit(f"Merge branch '{FIX_BRANCH}'"))
        assert detection is not None
        assert detection.pr_number is None and detection.branch == FIX_BRANCH

    def test_squash_merge_detected_through_associated_pull(self) -> None:
        detection = _detect(
            _commit("fix: remediation (#41)"),
            pulls=[_pull(40, "feature/other"), _pull(41, FIX_BRANCH)],
        )
        assert detection is not None
        assert (detection.signal, detection.pr_number, detection.branch) == (
            "associated_pull_request",
            41,
            FIX_BRANCH,
        )

    def test_unmerged_associated_pull_is_ignored(self) -> None:
        assert _detect(_commit("wip"), pulls=[_pull(41, FIX_BRANCH, merged=False)]) is None

    @pytest.mark.parametrize("role", ["author", "committer"])
    def test_bot_author_or_committer(self, role: str) -> None:
        commit = _commit("chore: update", **{role: BOT.upper()})
        detection = _detect(commit, bot_login=BOT)
        assert detection is not None
        assert detection.signal == "bot_author" and detection.bot_login == BOT.upper()
        assert _detect(commit, bot_login=None) is None

    def test_merge_of_non_autofix_branch_is_clear(self) -> None:
        assert _detect(_commit(f"Merge pull request #5 from {OWNER}/feature/x")) is None
        assert _detect(_commit("Merge branch 'feature/x'")) is None

    def test_prefix_must_start_the_branch_name(self) -> None:
        other = f"feature/{FIX_BRANCH}"
        assert _detect(_commit(f"Merge pull request #5 from {OWNER}/{other}")) is None

    def test_only_on_default_branch(self) -> None:
        merge = _commit(f"Merge pull request #41 from {OWNER}/{FIX_BRANCH}")
        assert _detect(merge, incident=_incident(branch="release")) is None
        assert _detect(merge, incident=_incident(branch=f"refs/heads/{DEFAULT}")) is not None
        assert _detect(merge, default_branch=None) is None

    def test_missing_commit_still_uses_pulls(self) -> None:
        detection = _detect(None, pulls=[_pull(41, FIX_BRANCH)])
        assert detection is not None and detection.signal == "associated_pull_request"


class TestFormatRepeatGuardWorkNote:
    def test_exact_template_with_pr_and_branch(self) -> None:
        detection = RepeatAutofixDetection(
            commit_sha=SHA,
            default_branch=DEFAULT,
            run_id="777",
            signal="merge_message",
            pr_number=41,
            branch=FIX_BRANCH,
        )
        assert format_repeat_guard_work_note(detection) == (
            f"[Auto-Fix Repeat Failure Guard] Failing commit 9f8e7d6 on 'trunk' is a merge of "
            f"auto-fix PR #41 (branch {FIX_BRANCH}). The previous automated remediation was "
            "ineffective and the workflow run 777 failed. Halting automated fix attempts to "
            "prevent retry loops. Requires human attention."
        )

    def test_branch_without_pr_number(self) -> None:
        detection = RepeatAutofixDetection(
            commit_sha=SHA,
            default_branch=DEFAULT,
            run_id="777",
            signal="merge_message",
            branch=FIX_BRANCH,
        )
        assert format_repeat_guard_work_note(detection) == (
            f"[Auto-Fix Repeat Failure Guard] Failing commit 9f8e7d6 on 'trunk' is a merge of "
            f"auto-fix branch {FIX_BRANCH} (no associated PR number found). The previous "
            "automated remediation was ineffective and the workflow run 777 failed. Halting "
            "automated fix attempts to prevent retry loops. Requires human attention."
        )

    def test_bot_author_without_branch(self) -> None:
        detection = RepeatAutofixDetection(
            commit_sha=SHA,
            default_branch=DEFAULT,
            run_id="777",
            signal="bot_author",
            bot_login=BOT,
        )
        assert format_repeat_guard_work_note(detection) == (
            f"[Auto-Fix Repeat Failure Guard] Failing commit 9f8e7d6 on 'trunk' was authored by "
            f"the auto-fix account {BOT}. The previous automated remediation was ineffective "
            "and the workflow run 777 failed. Halting automated fix attempts to prevent retry "
            "loops. Requires human attention."
        )

    def test_run_id_falls_back_to_external_id(self) -> None:
        detection = _detect(
            _commit(f"Merge branch '{FIX_BRANCH}'"), incident=_incident(run_id=None)
        )
        assert detection is not None and detection.run_id == "777"


def _writer(fake: FakeGitHub) -> FixPRWriter:
    config = BridgeConfig(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        github_token="ghp_test",
        github_api_url=API,
        autofix_enabled=True,
    )
    return FixPRWriter(
        config=config, http_client=httpx.Client(transport=httpx.MockTransport(fake.handler))
    )


class TestCheckRepeatAutofix:
    def test_reads_metadata_through_writer(self) -> None:
        fake = FakeGitHub()
        fake.repo_info = {"default_branch": DEFAULT}
        fake.commits[SHA] = _commit("fix (#41)")
        fake.commit_pulls[SHA] = [_pull(41, FIX_BRANCH)]
        detection = check_repeat_autofix(_incident(), _writer(fake), None)
        assert detection is not None and detection.pr_number == 41
        assert ("GET", f"/repos/{OWNER}/{REPO}/commits/{SHA}/pulls") in fake.calls

    def test_not_default_branch_skips_commit_lookups(self) -> None:
        fake = FakeGitHub()
        fake.repo_info = {"default_branch": DEFAULT}
        assert check_repeat_autofix(_incident(branch="release"), _writer(fake), None) is None
        assert all("/commits/" not in path for _, path in fake.calls)

    def test_default_branch_lookup_error_fails_open(self, caplog: pytest.LogCaptureFixture) -> None:
        fake = FakeGitHub()
        fake.lookup_errors.add("repo")
        with caplog.at_level(logging.WARNING):
            assert check_repeat_autofix(_incident(), _writer(fake), BOT) is None
        assert "lookup=default_branch result=error" in caplog.text

    def test_commit_lookup_error_uses_remaining_signals(self) -> None:
        fake = FakeGitHub()
        fake.repo_info = {"default_branch": DEFAULT}
        fake.lookup_errors.add("commit")
        fake.commit_pulls[SHA] = [_pull(41, FIX_BRANCH)]
        detection = check_repeat_autofix(_incident(), _writer(fake), BOT)
        assert detection is not None and detection.signal == "associated_pull_request"

    def test_pulls_lookup_error_uses_remaining_signals(self) -> None:
        fake = FakeGitHub()
        fake.repo_info = {"default_branch": DEFAULT}
        fake.lookup_errors.add("pulls")
        fake.commits[SHA] = _commit("chore", author=BOT)
        detection = check_repeat_autofix(_incident(), _writer(fake), BOT)
        assert detection is not None and detection.signal == "bot_author"

    def test_base_writer_without_lookups_is_clear(self) -> None:
        class Minimal(FixPRWriterBase):
            def create_fix_branch(self, *a: Any) -> str:
                raise NotImplementedError

            def commit_patch(self, *a: Any) -> str:
                raise NotImplementedError

            def create_pull_request(self, *a: Any) -> Any:
                raise NotImplementedError

            def execute_fix_proposal(self, *a: Any) -> Any:
                raise NotImplementedError

        assert check_repeat_autofix(_incident(), Minimal(), BOT) is None

    def test_missing_repo_or_sha_is_skipped(self) -> None:
        incident = _incident().model_copy(update={"github_head_sha": None})
        fake = FakeGitHub()
        assert check_repeat_autofix(incident, _writer(fake), BOT) is None
        assert fake.calls == []
