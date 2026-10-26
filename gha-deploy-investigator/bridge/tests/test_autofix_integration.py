"""End-to-end integration tests for the auto-fix PR flow through the bridge.

webhook failure -> callback report with FixProposal -> FixPRWriter opens PR ->
ServiceNow work note -> Check Run carries the PR link.
"""

import json
from typing import Any, Generator, Optional
from unittest.mock import MagicMock

import httpx
import pytest
from agent.app.patch_extractor import extract_fix_proposal
from bridge.app.adapters.github import GitHubAdapter
from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig, get_settings
from bridge.app.contracts.findings import EvidenceItem
from bridge.app.contracts.report import FilePatch, FixProposal, ProbableRootCause, Report
from bridge.app.contracts.result import InvestigationResult
from bridge.app.core.dedup import get_dedup_registry
from bridge.app.core.tasks import DispatchResult
from bridge.app.fixer.ci_listener import CIFeedbackListener
from bridge.app.fixer.interface import FixPRWriterBase, build_autofix_branch_name
from bridge.app.fixer.writer import FixPRWriter
from bridge.app.main import (
    app,
    set_ci_feedback_listener,
    set_fix_pr_writer,
    set_task_dispatcher,
)
from bridge.tests.fake_github_server import state as github_state
from bridge.tests.fake_ticket_server import state as ticket_state
from bridge.tests.test_fixer_writer import OWNER, REPO, FakeGitHub
from starlette.testclient import TestClient

RUN_ID = 555
HEAD_SHA = "abc123"
SOURCE = "import os\nx = 1\n"
FIXED = "x = 1\n"


def _settings(autofix_enabled: bool) -> BridgeConfig:
    return BridgeConfig(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        github_webhook_secret=None,
        github_token="ghp_test_token_xyz",
        github_api_url="https://api.github.com",
        servicenow_instance_url="https://example.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        enable_github_writeback=True,
        autofix_enabled=autofix_enabled,
    )


def _webhook_payload(branch: str, conclusion: str = "failure") -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_run": {
            "id": RUN_ID,
            "name": "CI",
            "conclusion": conclusion,
            "head_sha": HEAD_SHA,
            "head_branch": branch,
            "html_url": f"https://github.com/{OWNER}/{REPO}/actions/runs/{RUN_ID}",
            "updated_at": "2026-10-24T10:00:00Z",
            "run_attempt": 1,
            "actor": {"login": "octocat"},
            "pull_requests": [],
        },
        "repository": {"name": REPO, "full_name": f"{OWNER}/{REPO}"},
    }


def _proposal(base: str = "main") -> FixProposal:
    return FixProposal(
        status="proposed",
        strategy="deterministic_linter",
        rationale="Remove unused import (F401)",
        patches=[
            FilePatch(
                path="app/main.py",
                action="modify",
                content=FIXED,
                diff="--- a/app/main.py\n+++ b/app/main.py\n@@ -1,2 +1 @@\n-import os\n x = 1\n",
            )
        ],
        target_branch_base=base,
    )


@pytest.fixture
def harness(
    fake_github_client: httpx.Client,
    fake_ticket_client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    """Wire the bridge app to in-memory GitHub/ServiceNow fakes and a fake dispatcher."""
    get_dedup_registry().clear()
    dispatcher = MagicMock()
    dispatcher.dispatch.return_value = DispatchResult(
        task_id="inv-test", task_name="tasks/inv-test", is_duplicate=False, payload={}
    )
    set_task_dispatcher(dispatcher)

    adapters: dict[str, Any] = {}

    def configure(settings: BridgeConfig) -> TestClient:
        adapters["github"] = GitHubAdapter(config=settings, http_client=fake_github_client)
        adapters["servicenow"] = ServiceNowAdapter(config=settings, http_client=fake_ticket_client)
        app.dependency_overrides[get_settings] = lambda: settings
        return TestClient(app)

    def mock_get_adapter(source: str, config: Any = None) -> Any:
        try:
            return adapters[source.lower()]
        except KeyError as exc:
            raise ValueError(source) from exc

    monkeypatch.setattr("bridge.app.main.get_adapter", mock_get_adapter)

    yield {"configure": configure, "dispatcher": dispatcher, "adapters": adapters}

    set_task_dispatcher(None)
    set_ci_feedback_listener(None)
    set_fix_pr_writer(None)
    app.dependency_overrides.clear()
    get_dedup_registry().clear()


def _post_webhook(client: TestClient, payload: dict[str, Any]) -> httpx.Response:
    return client.post(
        "/webhook/github",
        content=json.dumps(payload),
        headers={"X-GitHub-Event": "workflow_run", "Content-Type": "application/json"},
    )


def _post_callback(
    client: TestClient, harness: dict[str, Any], branch: str, report: Report
) -> httpx.Response:
    incident = harness["adapters"]["github"].to_incident(_webhook_payload(branch))
    result = InvestigationResult(
        investigation_id="inv-afx-1",
        incident_id=incident.id,
        incident_source=incident.source,
        incident_external_id=incident.external_id,
        incident_external_key=incident.external_key,
        status="completed",
        report=report,
        incident=incident,
    )
    return client.post(
        "/callback/report",
        json=result.model_dump(mode="json"),
        headers={"Authorization": "Bearer mock-oidc-token-for-testing"},
    )


def _work_notes() -> str:
    return json.dumps([inc.get("work_notes") for inc in ticket_state.servicenow_incidents.values()])


class TestAutofixEndToEnd:
    def test_full_flow_opens_pr_notes_servicenow_and_links_check_run(
        self, harness: dict[str, Any]
    ) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"app/main.py": SOURCE})
        set_fix_pr_writer(
            FixPRWriter(
                config=settings,
                http_client=httpx.Client(transport=httpx.MockTransport(fake_gh.handler)),
            )
        )

        # 1. Failure webhook enqueues an investigation.
        resp = _post_webhook(client, _webhook_payload("main"))
        assert resp.status_code == 202
        harness["dispatcher"].dispatch.assert_called_once()

        # 2. Agent calls back with a proposed fix.
        report = Report(summary="Ruff F401 in app/main.py", fix_proposal=_proposal("main"))
        cb = _post_callback(client, harness, "main", report)
        assert cb.status_code == 200
        body = cb.json()
        assert body["status"] == "ok"
        pr_url = f"https://github.com/{OWNER}/{REPO}/pull/7"
        assert body["fix_pr_url"] == pr_url

        # 3. FixPRWriter created the autofix branch, committed the patch, opened the PR.
        assert fake_gh.refs == {f"refs/heads/autofix/run-{RUN_ID}": HEAD_SHA}
        assert fake_gh.files["app/main.py"] == FIXED
        assert len(fake_gh.pulls) == 1
        assert fake_gh.pulls[0]["head"]["ref"] == f"autofix/run-{RUN_ID}"
        assert fake_gh.pulls[0]["base"]["ref"] == "main"

        # 4. ServiceNow ticket was opened and received the fix-PR work note.
        assert len(ticket_state.servicenow_incidents) == 1
        notes = _work_notes()
        assert "[Auto-Fix Initiated]" in notes
        assert pr_url in notes
        assert "[Auto-Fix Hand-off]" not in notes

        # 5. Check Run links both the ticket and the fix PR.
        assert len(github_state.check_runs) == 1
        check_run_json = json.dumps(github_state.check_runs[0])
        assert body["servicenow_ticket"] in check_run_json
        assert pr_url in check_run_json

    def test_kill_switch_disabled_creates_no_pr(self, harness: dict[str, Any]) -> None:
        client = harness["configure"](_settings(autofix_enabled=False))
        writer = MagicMock(spec=FixPRWriterBase)
        set_fix_pr_writer(writer)

        report = Report(summary="Ruff F401", fix_proposal=_proposal("main"))
        cb = _post_callback(client, harness, "main", report)

        assert cb.status_code == 200
        assert "fix_pr_url" not in cb.json()
        writer.execute_fix_proposal.assert_not_called()
        assert len(ticket_state.servicenow_incidents) == 1
        assert "[Auto-Fix Initiated]" not in _work_notes()
        assert "/pull/" not in json.dumps(github_state.check_runs)
        assert "[Auto-Fix Hand-off]" not in _work_notes()

    @pytest.mark.parametrize("status", ["no_safe_fix", "unsupported"])
    def test_non_proposed_status_creates_no_pr(self, harness: dict[str, Any], status: str) -> None:
        client = harness["configure"](_settings(autofix_enabled=True))
        writer = MagicMock(spec=FixPRWriterBase)
        set_fix_pr_writer(writer)
        proposal = _proposal().model_copy(update={"status": status, "patches": []})

        cb = _post_callback(client, harness, "main", Report(summary="s", fix_proposal=proposal))

        assert cb.status_code == 200
        writer.execute_fix_proposal.assert_not_called()
        assert "[Auto-Fix Hand-off] No automated fix PR opened: Remove unused import (F401)" in (
            _work_notes()
        )

    def test_writer_error_does_not_block_ticket(self, harness: dict[str, Any]) -> None:
        client = harness["configure"](_settings(autofix_enabled=True))
        writer = MagicMock(spec=FixPRWriterBase)
        writer.execute_fix_proposal.side_effect = RuntimeError("github down")
        set_fix_pr_writer(writer)

        cb = _post_callback(client, harness, "main", Report(summary="s", fix_proposal=_proposal()))

        assert cb.status_code == 200
        assert cb.json()["status"] == "ok"
        assert "fix_pr_url" not in cb.json()
        assert len(ticket_state.servicenow_incidents) == 1
        assert len(github_state.check_runs) == 1
        notes = _work_notes()
        assert (
            "[Auto-Fix Hand-off] No automated fix PR opened: An automated fix was proposed "
            "but could not be applied: Remove unused import (F401). Requires human attention."
        ) in notes

    def test_no_proposal_posts_default_handoff_note(self, harness: dict[str, Any]) -> None:
        client = harness["configure"](_settings(autofix_enabled=True))
        set_fix_pr_writer(MagicMock(spec=FixPRWriterBase))

        cb = _post_callback(client, harness, "main", Report(summary="Unit tests failed"))

        assert cb.status_code == 200
        assert "fix_pr_url" not in cb.json()
        notes = _work_notes()
        assert "[Auto-Fix Hand-off] No automated fix could be safely proposed." in notes
        assert "Requires human attention." in notes

    def test_kill_switch_disabled_posts_no_handoff_note_without_proposal(
        self, harness: dict[str, Any]
    ) -> None:
        client = harness["configure"](_settings(autofix_enabled=False))

        cb = _post_callback(client, harness, "main", Report(summary="Unit tests failed"))

        assert cb.status_code == 200
        assert len(ticket_state.servicenow_incidents) == 1
        assert "[Auto-Fix Hand-off]" not in _work_notes()

    def test_handoff_note_failure_does_not_block_response(
        self, harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = harness["configure"](_settings(autofix_enabled=True))
        set_fix_pr_writer(MagicMock(spec=FixPRWriterBase))
        monkeypatch.setattr(
            harness["adapters"]["servicenow"],
            "post_fix_pr_handoff_note",
            MagicMock(side_effect=RuntimeError("servicenow down")),
        )

        cb = _post_callback(client, harness, "main", Report(summary="s"))

        assert cb.status_code == 200
        assert cb.json()["status"] == "ok"
        assert len(github_state.check_runs) == 1

    def test_autofix_branch_webhook_routes_to_listener_never_investigates(
        self, harness: dict[str, Any]
    ) -> None:
        client = harness["configure"](_settings(autofix_enabled=True))
        listener = MagicMock(spec=CIFeedbackListener)
        listener.handle_autofix_workflow_run.return_value = MagicMock(outcome="failure_noted")
        set_ci_feedback_listener(listener)
        writer = MagicMock(spec=FixPRWriterBase)
        set_fix_pr_writer(writer)

        resp = _post_webhook(client, _webhook_payload(f"autofix/run-{RUN_ID}", "failure"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "autofix_verification_processed"
        listener.handle_autofix_workflow_run.assert_called_once()
        harness["dispatcher"].dispatch.assert_not_called()
        writer.execute_fix_proposal.assert_not_called()


def _writer_for(settings: BridgeConfig, fake_gh: FakeGitHub) -> FixPRWriter:
    writer = FixPRWriter(
        config=settings,
        http_client=httpx.Client(transport=httpx.MockTransport(fake_gh.handler)),
    )
    set_fix_pr_writer(writer)
    return writer


def _agent_llm_proposal(raw: dict[str, Any], base: str = "main") -> FixProposal:
    """Run the agent's real extractor and carry its output across the wire contract."""
    agent_proposal = extract_fix_proposal(raw, base)
    return FixProposal.model_validate(agent_proposal.model_dump(mode="json"))


class TestAutofixRealProducers:
    """Drive proposals shaped exactly as each production producer emits them."""

    def test_agent_diff_only_proposal_is_applied_and_opens_pr(
        self, harness: dict[str, Any]
    ) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"app/main.py": SOURCE})
        _writer_for(settings, fake_gh)
        proposal = _agent_llm_proposal(
            {
                "status": "proposed",
                "confidence": 0.93,
                "rationale": "Remove the unused import flagged by the linter.",
                "diff": "--- a/app/main.py\n+++ b/app/main.py\n@@ -1,2 +1 @@\n-import os\n x = 1\n",
            }
        )
        assert proposal.status == "proposed"
        assert proposal.patches[0].content is None and proposal.patches[0].diff

        cb = _post_callback(client, harness, "main", Report(summary="s", fix_proposal=proposal))

        assert cb.status_code == 200
        assert cb.json()["fix_pr_url"] == f"https://github.com/{OWNER}/{REPO}/pull/7"
        assert fake_gh.files["app/main.py"] == FIXED
        assert fake_gh.refs == {f"refs/heads/autofix/run-{RUN_ID}": HEAD_SHA}

    def test_agent_diff_that_does_not_apply_opens_no_branch(self, harness: dict[str, Any]) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"app/main.py": "import sys\nx = 1\n"})
        _writer_for(settings, fake_gh)
        proposal = _agent_llm_proposal(
            {
                "status": "proposed",
                "confidence": 0.93,
                "rationale": "r",
                "diff": "--- a/app/main.py\n+++ b/app/main.py\n@@ -1,2 +1 @@\n-import os\n x = 1\n",
            }
        )

        cb = _post_callback(client, harness, "main", Report(summary="s", fix_proposal=proposal))

        assert cb.status_code == 200
        assert "fix_pr_url" not in cb.json()
        assert fake_gh.refs == {} and fake_gh.pulls == []
        assert len(ticket_state.servicenow_incidents) == 1

    def test_small_diff_with_large_content_is_rejected(self, harness: dict[str, Any]) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"app/main.py": SOURCE})
        _writer_for(settings, fake_gh)
        big = "".join(f"y{i} = {i}\n" for i in range(500))
        proposal = _proposal("main")
        proposal.patches[0] = proposal.patches[0].model_copy(update={"content": big})

        cb = _post_callback(client, harness, "main", Report(summary="s", fix_proposal=proposal))

        assert cb.status_code == 200
        assert "fix_pr_url" not in cb.json()
        assert fake_gh.refs == {} and fake_gh.pulls == []
        assert fake_gh.files["app/main.py"] == SOURCE

    def test_deterministic_lint_fixer_opens_pr_without_agent_proposal(
        self, harness: dict[str, Any]
    ) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"app/main.py": SOURCE})
        _writer_for(settings, fake_gh)
        lint_output = "app/main.py:1:8: F401 [*] `os` imported but unused"
        report = Report(
            summary="Lint step failed",
            evidence_by_area={"pipeline": [EvidenceItem(kind="log", excerpt=lint_output)]},
            fix_proposal=FixProposal(
                status="no_safe_fix",
                strategy="llm_patch",
                rationale="low",
                target_branch_base="main",
            ),
        )

        cb = _post_callback(client, harness, "main", report)

        assert cb.status_code == 200
        assert "fix_pr_url" in cb.json()
        assert fake_gh.files["app/main.py"] == FIXED
        assert "deterministic_linter" in fake_gh.pulls[0]["body"]

    def test_deterministic_memory_fixer_opens_pr_for_deploy_oom(
        self, harness: dict[str, Any]
    ) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        script = "#!/usr/bin/env bash\nSERVICE_MEMORY=384Mi\ngcloud run deploy svc\n"
        fake_gh = FakeGitHub(files={"deploy/run.sh": script})
        _writer_for(settings, fake_gh)
        report = Report(
            summary="Deploy failed: container terminated with exit code 137",
            probable_root_cause=ProbableRootCause(
                text="Memory limit in deploy/run.sh is too low (OOMKilled)", confidence=0.9
            ),
            metadata={"failing_stage": "deploy"},
        )

        cb = _post_callback(client, harness, "main", report)

        assert cb.status_code == 200
        assert "fix_pr_url" in cb.json()
        assert fake_gh.files["deploy/run.sh"] == script.replace("384Mi", "768Mi")
        assert "config_tuning" in fake_gh.pulls[0]["body"]

    def test_no_producer_applies_creates_no_pr(self, harness: dict[str, Any]) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"app/main.py": SOURCE})
        _writer_for(settings, fake_gh)

        cb = _post_callback(client, harness, "main", Report(summary="Unit tests failed"))

        assert cb.status_code == 200
        assert "fix_pr_url" not in cb.json()
        assert fake_gh.refs == {}


SCRIPT_GOOD = "#!/usr/bin/env bash\nSERVICE_MEMORY=384Mi\nRUN_MODE=normal\nlaunch svc\n"
SCRIPT_BAD = "#!/usr/bin/env bash\nSERVICE_MEMORY=192Mi\nRUN_MODE=normal\nlaunch svc\n"
SCRIPT_PATCH = (
    "@@ -1,4 +1,4 @@ heading text\n #!/usr/bin/env bash\n-SERVICE_MEMORY=384Mi\n"
    "+SERVICE_MEMORY=192Mi\n RUN_MODE=normal\n launch svc"
)


def _failing_commit(files: list[dict[str, Any]], message: str = "Tune runtime") -> dict[str, Any]:
    return {
        "sha": HEAD_SHA,
        "parents": [{"sha": "parent1"}],
        "commit": {"message": message},
        "files": files,
    }


def _oom_report_for(path: str, agent: Optional[FixProposal] = None) -> Report:
    return Report(
        summary="Deploy failed: container terminated with exit code 137",
        probable_root_cause=ProbableRootCause(
            text=f"The failing commit lowered the memory setting in {path} (OOMKilled)",
            confidence=0.9,
        ),
        metadata={"failing_stage": "deploy"},
        fix_proposal=agent,
    )


class TestCommitHunkRevertFlow:
    def test_revert_opens_pr_restoring_the_offending_hunk(self, harness: dict[str, Any]) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"deploy/run.sh": SCRIPT_BAD, "app/core.py": "a = 2\n"})
        fake_gh.commits[HEAD_SHA] = _failing_commit(
            [
                {"filename": "deploy/run.sh", "status": "modified", "patch": SCRIPT_PATCH},
                {
                    "filename": "app/core.py",
                    "status": "modified",
                    "patch": "@@ -1 +1 @@\n-a = 1\n+a = 2",
                },
            ]
        )
        _writer_for(settings, fake_gh)

        cb = _post_callback(client, harness, "main", _oom_report_for("deploy/run.sh"))

        assert cb.status_code == 200
        assert "fix_pr_url" in cb.json()
        assert fake_gh.files["deploy/run.sh"] == SCRIPT_GOOD
        assert fake_gh.files["app/core.py"] == "a = 2\n"
        assert "commit_hunk_revert" in fake_gh.pulls[0]["body"]
        assert "[Auto-Fix Initiated]" in _work_notes()

    def test_apply_failure_hands_off_without_trying_another_candidate(
        self, harness: dict[str, Any]
    ) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        # The file at the fix base no longer matches the failing commit's hunk.
        drifted = SCRIPT_BAD.replace("RUN_MODE=normal", "RUN_MODE=other")
        fake_gh = FakeGitHub(files={"deploy/run.sh": drifted})
        fake_gh.commits[HEAD_SHA] = _failing_commit(
            [{"filename": "deploy/run.sh", "status": "modified", "patch": SCRIPT_PATCH}]
        )
        _writer_for(settings, fake_gh)

        cb = _post_callback(
            client, harness, "main", _oom_report_for("deploy/run.sh", agent=_proposal("main"))
        )

        assert cb.status_code == 200
        assert "fix_pr_url" not in cb.json()
        assert fake_gh.refs == {} and fake_gh.pulls == []
        assert fake_gh.files["deploy/run.sh"] == drifted
        assert not any(m == "PUT" for m, _ in fake_gh.calls)
        notes = _work_notes()
        assert "[Auto-Fix Hand-off]" in notes
        assert "could not be applied" in notes and "Reverting the hunk(s)" in notes


class TestRepeatFixGuardFlow:
    def _merge_setup(self, fake_gh: FakeGitHub, default_branch: str = "main") -> str:
        branch = build_autofix_branch_name(41)
        fake_gh.repo_info = {"default_branch": default_branch}
        fake_gh.commits[HEAD_SHA] = _failing_commit(
            [{"filename": "deploy/run.sh", "status": "modified", "patch": SCRIPT_PATCH}],
            message=f"Merge pull request #12 from {OWNER}/{branch}",
        )
        return branch

    def test_repeat_failure_skips_fix_and_posts_guard_note(self, harness: dict[str, Any]) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"deploy/run.sh": SCRIPT_BAD})
        branch = self._merge_setup(fake_gh)
        _writer_for(settings, fake_gh)

        cb = _post_callback(
            client, harness, "main", _oom_report_for("deploy/run.sh", agent=_proposal("main"))
        )

        assert cb.status_code == 200
        body = cb.json()
        assert body["autofix_outcome"] == "repeat_autofix_detected"
        assert "fix_pr_url" not in body
        assert fake_gh.refs == {} and fake_gh.pulls == []
        assert not any("/contents/" in p for _, p in fake_gh.calls)
        expected = (
            f"[Auto-Fix Repeat Failure Guard] Failing commit {HEAD_SHA[:7]} on 'main' is a "
            f"merge of auto-fix PR #12 (branch {branch}). The previous automated remediation "
            f"was ineffective and the workflow run {RUN_ID} failed. Halting automated fix "
            "attempts to prevent retry loops. Requires human attention."
        )
        (incident,) = ticket_state.servicenow_incidents.values()
        assert expected in incident["work_notes"]
        assert "[Auto-Fix Hand-off]" not in _work_notes()
        assert "[Auto-Fix Initiated]" not in _work_notes()
        assert len(ticket_state.servicenow_attachments) == 1
        assert expected in github_state.check_runs[0]["output"]["text"]
        assert expected in github_state.comments[0]["body"]

    def test_guard_not_applied_off_default_branch(self, harness: dict[str, Any]) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"deploy/run.sh": SCRIPT_BAD})
        self._merge_setup(fake_gh, default_branch="trunk")
        _writer_for(settings, fake_gh)

        cb = _post_callback(client, harness, "main", Report(summary="s", fix_proposal=_proposal()))

        assert cb.status_code == 200
        assert "autofix_outcome" not in cb.json()
        assert "Repeat Failure Guard" not in _work_notes()

    def test_guard_lookup_errors_do_not_block_pipeline(self, harness: dict[str, Any]) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"app/main.py": SOURCE})
        fake_gh.lookup_errors.update({"repo", "commit", "pulls"})
        _writer_for(settings, fake_gh)

        cb = _post_callback(client, harness, "main", Report(summary="s", fix_proposal=_proposal()))

        assert cb.status_code == 200
        assert "fix_pr_url" in cb.json()
        assert "autofix_outcome" not in cb.json()

    def test_guard_writer_exception_does_not_block_pipeline(
        self, harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(autofix_enabled=True)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"app/main.py": SOURCE})
        _writer_for(settings, fake_gh)

        def boom(*_: Any, **__: Any) -> None:
            raise RuntimeError("guard exploded")

        monkeypatch.setattr("bridge.app.main.check_repeat_autofix", boom)
        cb = _post_callback(client, harness, "main", Report(summary="s", fix_proposal=_proposal()))

        assert cb.status_code == 200
        assert "fix_pr_url" in cb.json()

    def test_guard_skipped_when_autofix_disabled(self, harness: dict[str, Any]) -> None:
        settings = _settings(autofix_enabled=False)
        client = harness["configure"](settings)
        fake_gh = FakeGitHub(files={"deploy/run.sh": SCRIPT_BAD})
        self._merge_setup(fake_gh)
        _writer_for(settings, fake_gh)

        cb = _post_callback(client, harness, "main", Report(summary="s"))

        assert cb.status_code == 200
        assert "autofix_outcome" not in cb.json()
        assert fake_gh.calls == []
