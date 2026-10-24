import base64
import json
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import pytest
from bridge.app.adapters.base import IncidentRef
from bridge.app.adapters.github import GitHubAdapter, format_fix_pr_markdown
from bridge.app.adapters.servicenow import ServiceNowAdapter, format_fix_pr_work_note
from bridge.app.config import BridgeConfig
from bridge.app.contracts.incident import Incident
from bridge.app.contracts.report import FilePatch, FixProposal, PullRequestRef, Report
from bridge.app.fixer.writer import FixBranchExistsError, FixPRWriter
from bridge.app.templates.renderer import render_standalone_html

OWNER, REPO = "acme", "widgets"
API = "https://api.github.com"
PREFIX = f"/repos/{OWNER}/{REPO}"


class FakeGitHub:
    """Minimal in-memory GitHub REST API for refs, contents, and pulls."""

    def __init__(self, files: Optional[dict[str, str]] = None) -> None:
        self.refs: dict[str, str] = {}
        self.files: dict[str, str] = dict(files or {})
        self.pulls: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_put = False
        self.deleted_refs: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else {}

        if method == "POST" and path == f"{PREFIX}/git/refs":
            ref = body["ref"]
            if ref in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[ref] = body["sha"]
            return httpx.Response(201, json={"ref": ref, "object": {"sha": body["sha"]}})

        if method == "DELETE" and path.startswith(f"{PREFIX}/git/refs/heads/"):
            ref = "refs/" + path.split("/git/refs/", 1)[1]
            self.refs.pop(ref, None)
            self.deleted_refs.append(ref)
            return httpx.Response(204)

        if path.startswith(f"{PREFIX}/contents/"):
            file_path = path.split("/contents/", 1)[1]
            if method == "GET":
                if file_path not in self.files:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, json={"sha": f"blob-{file_path}", "path": file_path})
            if method == "PUT":
                if self.fail_put:
                    return httpx.Response(409, json={"message": "conflict"})
                self.files[file_path] = base64.b64decode(body["content"]).decode()
                return httpx.Response(200, json={"commit": {"sha": f"commit-{len(self.calls)}"}})
            if method == "DELETE":
                self.files.pop(file_path, None)
                return httpx.Response(200, json={"commit": {"sha": "commit-del"}})

        if path == f"{PREFIX}/pulls":
            if method == "POST":
                number = len(self.pulls) + 7
                pr = {
                    "number": number,
                    "html_url": f"https://github.com/{OWNER}/{REPO}/pull/{number}",
                    "head": {"ref": body["head"], "sha": "headsha123"},
                    "base": {"ref": body["base"]},
                    "title": body["title"],
                    "body": body["body"],
                }
                self.pulls.append(pr)
                return httpx.Response(201, json=pr)
            if method == "GET":
                head = request.url.params.get("head", "")
                branch = head.split(":", 1)[-1]
                return httpx.Response(
                    200, json=[p for p in self.pulls if p["head"]["ref"] == branch]
                )

        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})


@pytest.fixture
def fake_gh() -> FakeGitHub:
    return FakeGitHub(files={"app/main.py": "import os\nx = 1\n"})


def _config(enabled: bool = True) -> BridgeConfig:
    return BridgeConfig(
        app_env="test",
        github_token="ghp_test",
        github_api_url=API,
        servicenow_instance_url="https://example.service-now.com",
        autofix_enabled=enabled,
    )


def _writer(fake: FakeGitHub, enabled: bool = True) -> FixPRWriter:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return FixPRWriter(config=_config(enabled), http_client=client)


def _incident(branch: str = "feature/login") -> Incident:
    return Incident(
        id="gha-555",
        source="github",
        external_id="555",
        external_key=f"{OWNER}/{REPO}#555",
        external_url=f"https://github.com/{OWNER}/{REPO}/actions/runs/555",
        title="CI failure",
        description="lint failed",
        severity="P3",
        opened_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        github_repo=f"{OWNER}/{REPO}",
        github_run_id=555,
        github_head_sha="abc123",
        github_branch=branch,
    )


def _proposal(**overrides: Any) -> FixProposal:
    data: dict[str, Any] = {
        "status": "proposed",
        "strategy": "deterministic_linter",
        "rationale": "Remove unused import",
        "patches": [
            FilePatch(
                path="app/main.py",
                action="modify",
                content="x = 1\n",
                diff="--- a/app/main.py\n+++ b/app/main.py\n@@ -1,2 +1 @@\n-import os\n x = 1\n",
            )
        ],
        "target_branch_base": "feature/login",
    }
    data.update(overrides)
    return FixProposal(**data)


def _report() -> Report:
    return Report(summary="Lint failed")


class TestFixPRWriterPrimitives:
    def test_create_fix_branch_posts_ref(self, fake_gh: FakeGitHub) -> None:
        branch = _writer(fake_gh).create_fix_branch(OWNER, REPO, "abc123", 555)
        assert branch == "autofix/run-555"
        assert fake_gh.refs == {"refs/heads/autofix/run-555": "abc123"}

    def test_create_fix_branch_existing_ref_raises(self, fake_gh: FakeGitHub) -> None:
        fake_gh.refs["refs/heads/autofix/run-555"] = "old"
        with pytest.raises(FixBranchExistsError):
            _writer(fake_gh).create_fix_branch(OWNER, REPO, "abc123", 555)
        assert fake_gh.refs["refs/heads/autofix/run-555"] == "old"

    def test_commit_patch_modify_sends_sha_and_content(self, fake_gh: FakeGitHub) -> None:
        captured: list[dict[str, Any]] = []
        original = fake_gh.handler

        def spy(request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                captured.append(json.loads(request.content))
            return original(request)

        client = httpx.Client(transport=httpx.MockTransport(spy))
        writer = FixPRWriter(config=_config(), http_client=client)
        sha = writer.commit_patch(
            OWNER, REPO, "autofix/run-1", _proposal().patches[0], "fix: lint"
        )
        assert sha.startswith("commit-")
        assert captured[0]["sha"] == "blob-app/main.py"
        assert captured[0]["branch"] == "autofix/run-1"
        assert base64.b64decode(captured[0]["content"]).decode() == "x = 1\n"
        assert fake_gh.files["app/main.py"] == "x = 1\n"

    def test_commit_patch_requires_content(self, fake_gh: FakeGitHub) -> None:
        patch = FilePatch(path="app/main.py", action="modify", diff="-a\n+b\n")
        with pytest.raises(ValueError):
            _writer(fake_gh).commit_patch(OWNER, REPO, "b", patch, "m")

    def test_create_pull_request_returns_ref(self, fake_gh: FakeGitHub) -> None:
        pr = _writer(fake_gh).create_pull_request(
            OWNER, REPO, "t", "b", "autofix/run-555", "feature/login"
        )
        assert pr.pr_number == 7
        assert pr.pr_url.endswith("/pull/7")
        assert pr.branch_name == "autofix/run-555"
        assert pr.head_sha == "headsha123"
        assert pr.status == "opened"


class TestExecuteFixProposal:
    def test_happy_path_opens_pr(self, fake_gh: FakeGitHub) -> None:
        pr = _writer(fake_gh).execute_fix_proposal(_incident(), _report(), _proposal())
        assert pr is not None
        assert pr.branch_name == "autofix/run-555"
        assert fake_gh.refs["refs/heads/autofix/run-555"] == "abc123"
        assert fake_gh.files["app/main.py"] == "x = 1\n"
        created = fake_gh.pulls[0]
        assert created["base"]["ref"] == "feature/login"
        assert created["title"] == "fix: automated remediation for run 555"
        assert f"{OWNER}/{REPO}#555" in created["body"]

    def test_kill_switch_disabled_makes_no_calls(self, fake_gh: FakeGitHub) -> None:
        result = _writer(fake_gh, enabled=False).execute_fix_proposal(
            _incident(), _report(), _proposal()
        )
        assert result is None
        assert fake_gh.calls == []

    def test_duplicate_attempt_returns_existing_pr_without_commits(
        self, fake_gh: FakeGitHub
    ) -> None:
        writer = _writer(fake_gh)
        first = writer.execute_fix_proposal(_incident(), _report(), _proposal())
        calls_before = len(fake_gh.calls)
        second = writer.execute_fix_proposal(_incident(), _report(), _proposal())
        assert first is not None and second is not None
        assert second.pr_number == first.pr_number
        assert len(fake_gh.pulls) == 1
        new_calls = fake_gh.calls[calls_before:]
        assert not any(m == "PUT" for m, _ in new_calls)

    def test_duplicate_attempt_without_pr_aborts(self, fake_gh: FakeGitHub) -> None:
        fake_gh.refs["refs/heads/autofix/run-555"] = "old"
        assert _writer(fake_gh).execute_fix_proposal(_incident(), _report(), _proposal()) is None
        assert not any(m == "PUT" for m, _ in fake_gh.calls)

    def test_commit_failure_cleans_up_branch(self, fake_gh: FakeGitHub) -> None:
        fake_gh.fail_put = True
        assert _writer(fake_gh).execute_fix_proposal(_incident(), _report(), _proposal()) is None
        assert fake_gh.deleted_refs == ["refs/heads/autofix/run-555"]
        assert fake_gh.pulls == []

    @pytest.mark.parametrize("base", ["main", "master", ""])
    def test_refuses_default_branch_target(self, fake_gh: FakeGitHub, base: str) -> None:
        result = _writer(fake_gh).execute_fix_proposal(
            _incident(), _report(), _proposal(target_branch_base=base)
        )
        assert result is None
        assert fake_gh.calls == []

    @pytest.mark.parametrize(
        "path", [".github/workflows/ci.yml", "config/.env.prod", "keys/server.pem"]
    )
    def test_refuses_forbidden_paths(self, fake_gh: FakeGitHub, path: str) -> None:
        patch = FilePatch(path=path, action="modify", content="x\n", diff="-a\n+x\n")
        result = _writer(fake_gh).execute_fix_proposal(
            _incident(), _report(), _proposal(patches=[patch])
        )
        assert result is None
        assert fake_gh.calls == []

    def test_refuses_too_many_files(self, fake_gh: FakeGitHub) -> None:
        patches = [
            FilePatch(path=f"app/m{i}.py", action="modify", content="x\n", diff="-a\n+x\n")
            for i in range(4)
        ]
        result = _writer(fake_gh).execute_fix_proposal(
            _incident(), _report(), _proposal(patches=patches)
        )
        assert result is None
        assert fake_gh.calls == []

    def test_refuses_oversized_diff(self, fake_gh: FakeGitHub) -> None:
        diff = "".join(f"+line{i}\n" for i in range(51))
        patch = FilePatch(path="app/main.py", action="modify", content="x\n", diff=diff)
        result = _writer(fake_gh).execute_fix_proposal(
            _incident(), _report(), _proposal(patches=[patch])
        )
        assert result is None
        assert fake_gh.calls == []

    @pytest.mark.parametrize("status", ["no_safe_fix", "unsupported"])
    def test_non_proposed_status_is_noop(self, fake_gh: FakeGitHub, status: str) -> None:
        result = _writer(fake_gh).execute_fix_proposal(
            _incident(), _report(), _proposal(status=status, patches=[])
        )
        assert result is None
        assert fake_gh.calls == []

    def test_refuses_failure_on_autofix_branch(self, fake_gh: FakeGitHub) -> None:
        result = _writer(fake_gh).execute_fix_proposal(
            _incident(branch="autofix/run-1"), _report(), _proposal()
        )
        assert result is None
        assert fake_gh.calls == []


_PR = PullRequestRef(
    pr_number=7,
    pr_url=f"https://github.com/{OWNER}/{REPO}/pull/7",
    branch_name="autofix/run-555",
    head_sha="headsha123",
    status="opened",
)
_SN_REF = IncidentRef(
    id="sn-abc",
    source="servicenow",
    external_id="abc",
    external_key="INC0010001",
    external_url="https://example.service-now.com/nav_to.do?uri=incident.do?sys_id=abc",
)


class TestLinkInjection:
    def test_servicenow_work_note_format(self) -> None:
        assert format_fix_pr_work_note(_PR) == (
            "[Auto-Fix Initiated] Pull Request #7 created on branch autofix/run-555 "
            f"to remediate failure: https://github.com/{OWNER}/{REPO}/pull/7"
        )

    def test_servicenow_post_fix_pr_note(self) -> None:
        captured: list[tuple[str, dict[str, Any]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"result": {}})

        adapter = ServiceNowAdapter(
            config=_config(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        adapter.post_fix_pr_note(_SN_REF, _PR)
        assert captured == [
            ("/api/now/table/incident/abc", {"work_notes": format_fix_pr_work_note(_PR)})
        ]

    def test_github_markdown_format(self) -> None:
        assert format_fix_pr_markdown(_PR) == (
            f"* 🛠️ **Automated Fix PR:** [#7](https://github.com/{OWNER}/{REPO}/pull/7) "
            "has been opened with proposed remediation."
        )

    def test_github_check_run_and_comment_include_pr_link(self) -> None:
        bodies: dict[str, dict[str, Any]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            bodies[request.url.path] = json.loads(request.content)
            return httpx.Response(201, json={"id": 1})

        adapter = GitHubAdapter(
            config=_config(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        adapter.post_report_link(_incident(), _report(), _SN_REF, pull_request=_PR)
        link = format_fix_pr_markdown(_PR)
        check = bodies[f"{PREFIX}/check-runs"]
        assert link in check["output"]["text"]
        comment = bodies[f"{PREFIX}/commits/abc123/comments"]
        assert link in comment["body"]

    def test_github_check_run_without_pr_unchanged(self) -> None:
        bodies: dict[str, dict[str, Any]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            bodies[request.url.path] = json.loads(request.content)
            return httpx.Response(201, json={"id": 1})

        adapter = GitHubAdapter(
            config=_config(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        adapter.post_report_link(_incident(), _report(), _SN_REF)
        assert bodies[f"{PREFIX}/check-runs"]["output"]["text"] == "Lint failed"
        assert "Automated Fix PR" not in bodies[f"{PREFIX}/commits/abc123/comments"]["body"]

    def test_standalone_dossier_renders_fix_section(self) -> None:
        report = Report(summary="Lint failed", fix_proposal=_proposal())
        html_out = render_standalone_html(
            report, incident_ref=_SN_REF, incident=_incident(), pull_request=_PR
        )
        assert "Automated Fix PR" in html_out
        assert _PR.pr_url in html_out
        assert "autofix/run-555" in html_out
        assert "-import os" in html_out

    def test_standalone_dossier_omits_fix_section_when_absent(self) -> None:
        html_out = render_standalone_html(_report(), incident=_incident())
        assert "Automated Fix PR" not in html_out
