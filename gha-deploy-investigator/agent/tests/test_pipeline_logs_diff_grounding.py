"""Tests that ADK-runner findings are grounded with git_diff evidence when resolvable."""

from datetime import datetime, timezone
from typing import Any

import pytest
from agent.app.contracts.findings import EvidenceItem, Findings
from agent.app.contracts.incident import Incident
from agent.app.specialists import pipeline_logs
from agent.app.specialists.pipeline_logs import PipelineLogsSpecialist
from agent.app.tools.github_tools import reset_github_mock_state, set_mock_diff
from agent.app.tools.registry import default_query_cache

HEAD_SHA = "0f1e2d3c4b5a"


def _make_incident(**overrides: Any) -> Incident:
    fields: dict[str, Any] = {
        "id": "gha-test-diff-grounding",
        "source": "github",
        "external_id": "777",
        "external_key": "example-org/example-app#777",
        "external_url": "https://github.com/example-org/example-app/actions/runs/777",
        "title": "[CI/CD Failure] example-app: build",
        "description": "Workflow run 777 failed",
        "severity": "P2",
        "service_hint": "example-app",
        "opened_at": datetime.now(timezone.utc),
        "github_repo": "example-org/example-app",
        "github_run_id": 777,
        "github_head_sha": HEAD_SHA,
        "github_branch": "main",
        "github_failing_stage": "build",
    }
    fields.update(overrides)
    return Incident(**fields)


def _findings(evidence: list[EvidenceItem]) -> Findings:
    return Findings(summary="Build failed in compile step", evidence=evidence, confidence=0.8)


def _to_result(specialist: PipelineLogsSpecialist, findings: Findings, incident: Incident) -> Any:
    return specialist._findings_to_diagnostic_result(
        findings,
        incident,
        {"name": "build-job"},
        {"name": "Build image"},
        "error: compile failed",
        "build",
    )


@pytest.fixture(autouse=True)
def _reset_mocks() -> Any:
    reset_github_mock_state()
    default_query_cache.clear()
    yield
    reset_github_mock_state()
    default_query_cache.clear()


def test_git_diff_appended_when_findings_omit_it() -> None:
    set_mock_diff(
        HEAD_SHA,
        {"files": [{"filename": "src/app.py", "patch": "@@ -1 +1,2 @@\n+import missing_mod"}]},
    )
    runner_log = EvidenceItem(kind="github_runner_log", excerpt="error: compile failed")

    res = _to_result(PipelineLogsSpecialist(), _findings([runner_log]), _make_incident())

    diffs = [e for e in res.evidence_items if e.kind == "git_diff"]
    assert len(diffs) == 1
    assert "src/app.py" in diffs[0].excerpt
    assert "+import missing_mod" in diffs[0].excerpt
    assert res.evidence_items[0].kind == "github_runner_log"


def test_git_diff_not_duplicated_when_present() -> None:
    set_mock_diff(HEAD_SHA, {"files": [{"filename": "src/other.py", "patch": "+x = 1"}]})
    existing = EvidenceItem(kind="git_diff", excerpt="Commit diff touched 1 file(s): src/app.py")
    calls: list[Any] = []
    specialist = PipelineLogsSpecialist()
    original = specialist._resolve_diff_data

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return original(*args, **kwargs)

    specialist._resolve_diff_data = spy  # type: ignore[method-assign]

    res = _to_result(specialist, _findings([existing]), _make_incident())

    diffs = [e for e in res.evidence_items if e.kind == "git_diff"]
    assert diffs == [existing]
    assert calls == []


def test_no_error_when_diff_fetch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs: Any) -> Any:
        raise RuntimeError("github unavailable")

    monkeypatch.setattr(pipeline_logs, "get_commit_diff", boom)

    res = _to_result(PipelineLogsSpecialist(), _findings([]), _make_incident())

    assert [e.kind for e in res.evidence_items] == ["github_runner_log"]


def test_no_error_when_incident_has_no_commit() -> None:
    res = _to_result(
        PipelineLogsSpecialist(),
        _findings([]),
        _make_incident(github_repo=None, github_head_sha=None),
    )

    assert [e.kind for e in res.evidence_items] == ["github_runner_log"]


def test_no_diff_evidence_when_diff_has_no_files() -> None:
    set_mock_diff(HEAD_SHA, {"files": []})

    res = _to_result(PipelineLogsSpecialist(), _findings([]), _make_incident())

    assert all(e.kind != "git_diff" for e in res.evidence_items)
