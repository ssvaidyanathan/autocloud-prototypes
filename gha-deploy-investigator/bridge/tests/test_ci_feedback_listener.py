import json
from typing import Any, Generator, Optional
from unittest.mock import MagicMock

import httpx
import pytest
from bridge.app.adapters.github import GitHubAdapter
from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig, get_settings
from bridge.app.core.tasks import DispatchResult
from bridge.app.fixer.ci_listener import (
    CIFeedbackListener,
    build_failure_note,
    build_inconclusive_note,
    build_success_note,
    parse_original_run_id,
)
from bridge.app.main import app, set_ci_feedback_listener, set_task_dispatcher
from starlette.testclient import TestClient

SN_URL = "https://example.service-now.com"
GH_API = "https://api.github.com"
REPO = "my-org/sample-app"
RUN_URL = f"https://github.com/{REPO}/actions/runs/999"


def _settings(**overrides: Any) -> BridgeConfig:
    values: dict[str, Any] = {
        "app_env": "test",
        "github_webhook_secret": None,
        "github_token": "ghp_test_token_xyz",
        "github_api_url": GH_API,
        "servicenow_instance_url": SN_URL,
        "servicenow_username": "admin",
        "servicenow_password": "password",
        "enable_github_writeback": False,
    }
    values.update(overrides)
    return BridgeConfig(_env_file=None, **values)  # type: ignore[call-arg]


def _workflow_run_payload(
    branch: str,
    conclusion: Optional[str] = "failure",
    action: str = "completed",
    pr_numbers: Optional[list[int]] = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "workflow_run": {
            "id": 999,
            "name": "CI",
            "head_branch": branch,
            "head_sha": "abc123def456",
            "conclusion": conclusion,
            "html_url": RUN_URL,
            "run_attempt": 1,
            "updated_at": "2026-10-24T10:00:00Z",
            "actor": {"login": "octocat"},
            "pull_requests": [{"number": n} for n in (pr_numbers or [])],
        },
        "repository": {"full_name": REPO, "name": "sample-app"},
    }


class _Recorder:
    """httpx MockTransport handler emulating ServiceNow + GitHub endpoints."""

    def __init__(
        self,
        incidents: Optional[list[dict[str, Any]]] = None,
        jobs: Optional[dict[str, list[dict[str, Any]]]] = None,
        jobs_status: int = 200,
    ) -> None:
        self.incidents = incidents if incidents is not None else []
        self.jobs = jobs if jobs is not None else {}
        self.jobs_status = jobs_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/api/now/table/incident":
            return httpx.Response(200, json={"result": self.incidents})
        if request.method == "GET" and path.endswith("/jobs") and "/actions/runs/" in path:
            if self.jobs_status != 200:
                return httpx.Response(self.jobs_status, json={"message": "server error"})
            run_id = path.split("/actions/runs/", 1)[1].split("/", 1)[0]
            return httpx.Response(200, json={"jobs": self.jobs.get(run_id, [])})
        if request.method == "PATCH" and path.startswith("/api/now/table/incident/"):
            return httpx.Response(200, json={"result": {"sys_id": path.rsplit("/", 1)[-1]}})
        if request.method == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404, json={"error": "not found"})

    def patches(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.method == "PATCH"]

    def comments(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


def _listener(settings: BridgeConfig, recorder: _Recorder) -> CIFeedbackListener:
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    return CIFeedbackListener(
        settings=settings,
        servicenow_adapter=ServiceNowAdapter(config=settings, http_client=client),
        github_adapter=GitHubAdapter(config=settings, http_client=client),
    )


ORIGINAL_INCIDENT = {
    "sys_id": "sn-sys-1",
    "number": "INC0010001",
    "correlation_id": f"gdi:{REPO}:123:1",
    "state": "2",
}


@pytest.fixture
def mock_dispatcher() -> Generator[MagicMock, None, None]:
    dispatcher = MagicMock()
    dispatcher.dispatch.return_value = DispatchResult(
        task_id="inv-test", task_name="tasks/inv-test", is_duplicate=False, payload={}
    )
    set_task_dispatcher(dispatcher)
    yield dispatcher
    set_task_dispatcher(None)
    set_ci_feedback_listener(None)
    app.dependency_overrides.clear()


def _client(settings: BridgeConfig) -> TestClient:
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def _post(client: TestClient, payload: dict[str, Any]) -> httpx.Response:
    return client.post(
        "/webhook/github",
        content=json.dumps(payload),
        headers={"X-GitHub-Event": "workflow_run", "Content-Type": "application/json"},
    )


def test_parse_original_run_id() -> None:
    assert parse_original_run_id("autofix/run-123") == "123"
    assert parse_original_run_id("autofix/run-123-retry") == "123"
    assert parse_original_run_id("autofix/other") is None
    assert parse_original_run_id("main") is None
    assert parse_original_run_id(None) is None


def test_autofix_branch_bypasses_investigation_enqueue(mock_dispatcher: MagicMock) -> None:
    settings = _settings()
    recorder = _Recorder(incidents=[ORIGINAL_INCIDENT])
    set_ci_feedback_listener(_listener(settings, recorder))

    resp = _post(_client(settings), _workflow_run_payload("autofix/run-123", "failure"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "autofix_verification_processed"
    assert body["branch"] == "autofix/run-123"
    assert body["conclusion"] == "failure"
    mock_dispatcher.dispatch.assert_not_called()


def test_autofix_branch_listener_error_still_returns_200(mock_dispatcher: MagicMock) -> None:
    settings = _settings()
    broken = MagicMock(spec=CIFeedbackListener)
    broken.handle_autofix_workflow_run.side_effect = RuntimeError("boom")
    set_ci_feedback_listener(broken)

    resp = _post(_client(settings), _workflow_run_payload("autofix/run-123", "success"))

    assert resp.status_code == 200
    assert resp.json()["status"] == "autofix_verification_processed"
    mock_dispatcher.dispatch.assert_not_called()


def test_ci_feedback_success_updates_servicenow() -> None:
    settings = _settings()
    recorder = _Recorder(incidents=[ORIGINAL_INCIDENT])
    listener = _listener(settings, recorder)

    outcome = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", "success"), settings
    )

    assert outcome.outcome == "verification_passed"
    assert outcome.work_note_posted is True
    assert outcome.incident_sys_id == "sn-sys-1"

    lookup = next(r for r in recorder.requests if r.url.path == "/api/now/table/incident")
    assert lookup.method == "GET"
    assert f"correlation_idSTARTSWITHgdi:{REPO}:123:" in lookup.url.params["sysparm_query"]

    patches = recorder.patches()
    assert patches == [{"work_notes": build_success_note("autofix/run-123", RUN_URL)}]
    assert "PASSED CI verification" in patches[0]["work_notes"]
    # Incident lifecycle: never close/resolve or change state.
    assert all("state" not in p for p in patches)
    assert recorder.requests[-1].url.path == "/api/now/table/incident/sn-sys-1"


def test_ci_feedback_failure_updates_servicenow(mock_dispatcher: MagicMock) -> None:
    settings = _settings()
    recorder = _Recorder(incidents=[ORIGINAL_INCIDENT])
    listener = _listener(settings, recorder)
    set_ci_feedback_listener(listener)

    for conclusion in ("failure", "timed_out"):
        recorder.requests.clear()
        payload = _workflow_run_payload("autofix/run-123", conclusion)
        payload["workflow_run"]["run_attempt"] = 1 if conclusion == "failure" else 2
        resp = _post(_client(settings), payload)
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "verification_failed"

        patches = recorder.patches()
        assert patches == [{"work_notes": build_failure_note("autofix/run-123", RUN_URL)}]
        note = patches[0]["work_notes"]
        assert "FAILED CI verification" in note
        assert "No further automated fix will be attempted" in note
        assert all("state" not in p for p in patches)

    # No re-trigger: neither a new investigation nor a new fix attempt.
    mock_dispatcher.dispatch.assert_not_called()


def test_ci_feedback_duplicate_delivery_posts_once() -> None:
    settings = _settings()
    recorder = _Recorder(incidents=[ORIGINAL_INCIDENT])
    listener = _listener(settings, recorder)
    payload = _workflow_run_payload("autofix/run-123", "failure")

    first = listener.handle_autofix_workflow_run(payload, settings)
    second = listener.handle_autofix_workflow_run(payload, settings)

    assert first.work_note_posted is True
    assert second.outcome == "duplicate_ignored"
    assert len(recorder.patches()) == 1


def test_ci_feedback_ignores_non_terminal_conclusions() -> None:
    settings = _settings()
    recorder = _Recorder(incidents=[ORIGINAL_INCIDENT])
    listener = _listener(settings, recorder)

    pending = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", None, action="in_progress"), settings
    )
    cancelled = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", "cancelled"), settings
    )

    assert pending.outcome == "pending"
    assert cancelled.outcome == "ignored_conclusion"
    assert recorder.requests == []


def test_ci_feedback_incident_not_found_does_not_raise() -> None:
    settings = _settings()
    recorder = _Recorder(incidents=[])
    listener = _listener(settings, recorder)

    outcome = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", "success"), settings
    )

    assert outcome.work_note_posted is False
    assert outcome.detail == "incident not found"
    assert recorder.patches() == []


@pytest.mark.parametrize("correlation_id", ["", None, f"gdi:{REPO}:1234:1", "unrelated"])
def test_ci_feedback_rejects_records_without_matching_correlation_id(
    correlation_id: Optional[str],
) -> None:
    # Simulates an instance that ignores the STARTSWITH filter and returns other incidents.
    settings = _settings()
    record = {"sys_id": "sn-other", "number": "INC0099999", "correlation_id": correlation_id}
    recorder = _Recorder(incidents=[record])
    listener = _listener(settings, recorder)

    outcome = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", "success"), settings
    )

    assert outcome.work_note_posted is False
    assert outcome.detail == "incident not found"
    assert recorder.patches() == []


def test_ci_feedback_github_writeback_comments_on_pr() -> None:
    settings = _settings(enable_github_writeback=True)
    recorder = _Recorder(incidents=[ORIGINAL_INCIDENT])
    listener = _listener(settings, recorder)

    outcome = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", "success", pr_numbers=[42]), settings
    )

    assert outcome.github_writeback_posted is True
    comments = recorder.comments()
    assert len(comments) == 1
    assert comments[0].url.path == f"/repos/{REPO}/issues/42/comments"
    assert "PASSED" in json.loads(comments[0].content)["body"]
    assert comments[0].headers["Authorization"] == "Bearer ghp_test_token_xyz"


def test_ci_feedback_github_writeback_disabled() -> None:
    settings = _settings(enable_github_writeback=False)
    recorder = _Recorder(incidents=[ORIGINAL_INCIDENT])
    listener = _listener(settings, recorder)

    outcome = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", "failure", pr_numbers=[42]), settings
    )

    assert outcome.github_writeback_posted is False
    assert recorder.comments() == []


def test_non_autofix_branch_still_enqueues_investigation(mock_dispatcher: MagicMock) -> None:
    settings = _settings()
    listener = MagicMock(spec=CIFeedbackListener)
    set_ci_feedback_listener(listener)

    resp = _post(_client(settings), _workflow_run_payload("main", "failure"))

    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    mock_dispatcher.dispatch.assert_called_once()
    incident = mock_dispatcher.dispatch.call_args[0][0]
    assert incident.github_branch == "main"
    listener.handle_autofix_workflow_run.assert_not_called()


# ------------------------------------------------- original-vs-fix job comparison


def _job(name: str, conclusion: Optional[str]) -> dict[str, Any]:
    return {"name": name, "conclusion": conclusion, "status": "completed"}


def test_ci_feedback_inconclusive_when_failed_job_skipped_on_fix_branch() -> None:
    settings = _settings(enable_github_writeback=True)
    recorder = _Recorder(
        incidents=[ORIGINAL_INCIDENT],
        jobs={
            "123": [_job("build", "success"), _job("release-step", "failure")],
            "999": [_job("build", "success"), _job("release-step", "skipped")],
        },
    )
    listener = _listener(settings, recorder)

    outcome = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", "success", pr_numbers=[42]), settings
    )

    assert outcome.outcome == "verification_inconclusive"
    assert outcome.work_note_posted is True
    expected_note = build_inconclusive_note("autofix/run-123", RUN_URL, ["release-step"])
    assert recorder.patches() == [{"work_notes": expected_note}]
    note = recorder.patches()[0]["work_notes"]
    assert "INCONCLUSIVE" in note
    assert "'release-step'" in note
    assert RUN_URL in note
    assert "requires human validation" in note
    assert "PASSED" not in note

    assert outcome.github_writeback_posted is True
    comments = recorder.comments()
    assert len(comments) == 1
    body = json.loads(comments[0].content)["body"]
    assert body.startswith("### Auto-Fix Verification: INCONCLUSIVE")
    assert "'release-step'" in body
    assert "did not run on the fix branch; requires human validation." in body
    assert "PASSED" not in body

    job_paths = {r.url.path for r in recorder.requests if r.url.path.endswith("/jobs")}
    assert job_paths == {
        f"/repos/{REPO}/actions/runs/123/jobs",
        f"/repos/{REPO}/actions/runs/999/jobs",
    }


def test_ci_feedback_inconclusive_when_failed_job_missing_on_fix_branch() -> None:
    settings = _settings()
    recorder = _Recorder(
        incidents=[ORIGINAL_INCIDENT],
        jobs={
            "123": [_job("lint", "success"), _job("integration", "timed_out")],
            "999": [_job("lint", "success")],
        },
    )
    listener = _listener(settings, recorder)

    outcome = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", "success"), settings
    )

    assert outcome.outcome == "verification_inconclusive"
    assert recorder.patches() == [
        {"work_notes": build_inconclusive_note("autofix/run-123", RUN_URL, ["integration"])}
    ]


def test_ci_feedback_passed_when_failed_job_succeeds_on_fix_branch() -> None:
    settings = _settings(enable_github_writeback=True)
    recorder = _Recorder(
        incidents=[ORIGINAL_INCIDENT],
        jobs={
            "123": [_job("release-step", "failure")],
            "999": [_job("release-step", "success")],
        },
    )
    listener = _listener(settings, recorder)

    outcome = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", "success", pr_numbers=[42]), settings
    )

    assert outcome.outcome == "verification_passed"
    assert recorder.patches() == [{"work_notes": build_success_note("autofix/run-123", RUN_URL)}]
    comments = recorder.comments()
    assert len(comments) == 1
    body = json.loads(comments[0].content)["body"]
    assert "Auto-Fix Verification: PASSED" in body
    assert "INCONCLUSIVE" not in body


def test_ci_feedback_job_listing_error_falls_back_to_passed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(enable_github_writeback=True)
    recorder = _Recorder(incidents=[ORIGINAL_INCIDENT], jobs_status=500)
    listener = _listener(settings, recorder)

    with caplog.at_level("WARNING", logger="bridge.app.fixer.ci_listener"):
        outcome = listener.handle_autofix_workflow_run(
            _workflow_run_payload("autofix/run-123", "success", pr_numbers=[42]), settings
        )

    assert outcome.outcome == "verification_passed"
    assert outcome.work_note_posted is True
    assert recorder.patches() == [{"work_notes": build_success_note("autofix/run-123", RUN_URL)}]
    assert outcome.github_writeback_posted is True
    assert "Failed to fetch workflow run jobs" in caplog.text


def test_ci_feedback_job_listing_network_failure_falls_back_to_passed() -> None:
    settings = _settings()
    recorder = _Recorder(incidents=[ORIGINAL_INCIDENT])
    listener = _listener(settings, recorder)
    github = MagicMock(spec=GitHubAdapter)
    github.get_workflow_run_jobs.side_effect = httpx.ConnectError("network down")
    listener._github_adapter = github

    outcome = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", "success"), settings
    )

    assert outcome.outcome == "verification_passed"
    assert recorder.patches() == [{"work_notes": build_success_note("autofix/run-123", RUN_URL)}]


def test_ci_feedback_failure_conclusion_skips_job_comparison() -> None:
    settings = _settings()
    recorder = _Recorder(incidents=[ORIGINAL_INCIDENT])
    listener = _listener(settings, recorder)

    outcome = listener.handle_autofix_workflow_run(
        _workflow_run_payload("autofix/run-123", "failure"), settings
    )

    assert outcome.outcome == "verification_failed"
    assert not any(r.url.path.endswith("/jobs") for r in recorder.requests)
