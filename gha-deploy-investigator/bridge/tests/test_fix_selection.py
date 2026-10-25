from datetime import datetime, timezone
from typing import Optional

import pytest
from bridge.app.contracts.findings import EvidenceItem
from bridge.app.contracts.incident import Incident
from bridge.app.contracts.report import (
    CheckRunAnnotation,
    FilePatch,
    FixProposal,
    ProbableRootCause,
    Report,
)
from bridge.app.fixer import selection
from bridge.app.fixer.selection import (
    MAX_CONFIG_CANDIDATES,
    candidate_config_paths,
    select_fix_proposal,
)

SOURCE = "import os\nx = 1\n"
LINT = "app/main.py:1:8: F401 [*] `os` imported but unused"


def _incident() -> Incident:
    return Incident(
        id="gha-1",
        source="github",
        external_id="1",
        external_key="acme/widgets#1",
        title="CI failure",
        description="d",
        severity="P3",
        opened_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        github_repo="acme/widgets",
        github_run_id=1,
        github_head_sha="abc",
        github_branch="main",
    )


def _files(files: dict[str, str]):
    calls: list[str] = []

    def fetch(path: str) -> Optional[str]:
        calls.append(path)
        return files.get(path)

    return fetch, calls


def _oom_report(**extra: object) -> Report:
    return Report(
        summary="Deploy failed with exit code 137",
        probable_root_cause=ProbableRootCause(
            text="Memory in deploy/run.sh too low", confidence=0.9
        ),
        metadata={"failing_stage": "deploy"},
        **extra,  # type: ignore[arg-type]
    )


def _agent_proposal(status: str = "proposed") -> FixProposal:
    return FixProposal(
        status=status,  # type: ignore[arg-type]
        strategy="llm_patch",
        rationale="r",
        patches=[FilePatch(path="a.py", action="modify", diff="d")] if status == "proposed" else [],
        target_branch_base="main",
    )


class TestSelectFixProposal:
    def test_lint_fixer_takes_precedence_over_agent_proposal(self) -> None:
        agent = _agent_proposal()
        fetch, _ = _files({"app/main.py": SOURCE})
        report = Report(
            summary="s",
            fix_proposal=agent,
            evidence_by_area={"x": [EvidenceItem(kind="log", excerpt=LINT)]},
        )
        proposal = select_fix_proposal(_incident(), report, fetch)
        assert proposal is not None
        assert proposal is not agent
        assert proposal.strategy == "deterministic_linter"
        assert proposal.patches[0].content == "x = 1\n"

    def test_memory_fixer_takes_precedence_over_agent_proposal(self) -> None:
        agent = _agent_proposal()
        fetch, calls = _files({"deploy/run.sh": "SERVICE_MEMORY=384Mi\n"})
        proposal = select_fix_proposal(_incident(), _oom_report(fix_proposal=agent), fetch)
        assert proposal is not None
        assert proposal is not agent
        assert proposal.strategy == "config_tuning"
        assert proposal.patches[0].content == "SERVICE_MEMORY=768Mi\n"
        assert calls == ["deploy/run.sh"]

    def test_agent_proposal_chosen_when_no_deterministic_fixer_applies(self) -> None:
        agent = _agent_proposal()
        fetch, calls = _files({})
        report = Report(summary="tests failed: assert 1 == 2", fix_proposal=agent)
        assert select_fix_proposal(_incident(), report, fetch) is agent
        assert calls == []

    def test_agent_proposal_chosen_when_deterministic_fixer_cannot_produce_patch(self) -> None:
        # OOM is diagnosed but no tunable memory setting exists in the referenced files,
        # so config_tuning does not yield a proposed fix and the agent patch is used.
        agent = _agent_proposal()
        fetch, calls = _files({"deploy/run.sh": "echo deploying\n"})
        proposal = select_fix_proposal(_incident(), _oom_report(fix_proposal=agent), fetch)
        assert proposal is agent
        assert calls == ["deploy/run.sh"]

    def test_agent_proposal_chosen_when_deterministic_fixer_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*_: object) -> FixProposal:
            raise RuntimeError("fixer bug")

        monkeypatch.setattr(selection, "_lint_proposal", explode)
        agent = _agent_proposal()
        fetch, _ = _files({"app/main.py": SOURCE})
        report = Report(
            summary="s",
            fix_proposal=agent,
            evidence_by_area={"x": [EvidenceItem(kind="log", excerpt=LINT)]},
        )
        assert select_fix_proposal(_incident(), report, fetch) is agent

    def test_lint_fixer_chosen_when_agent_declined(self) -> None:
        fetch, _ = _files({"app/main.py": SOURCE})
        report = Report(
            summary="s",
            evidence_by_area={"x": [EvidenceItem(kind="log", excerpt=LINT)]},
            fix_proposal=_agent_proposal("no_safe_fix"),
        )
        proposal = select_fix_proposal(_incident(), report, fetch)
        assert proposal is not None
        assert proposal.strategy == "deterministic_linter"

    def test_memory_fixer_chosen_without_agent_proposal(self) -> None:
        fetch, calls = _files({"deploy/run.sh": "SERVICE_MEMORY=384Mi\n"})
        proposal = select_fix_proposal(_incident(), _oom_report(), fetch)
        assert proposal is not None
        assert proposal.strategy == "config_tuning"
        assert calls == ["deploy/run.sh"]

    def test_none_when_nothing_applies(self) -> None:
        fetch, calls = _files({})
        assert select_fix_proposal(_incident(), Report(summary="tests failed"), fetch) is None
        assert calls == []

    def test_none_when_agent_declined_and_no_deterministic_fixer_applies(self) -> None:
        fetch, _ = _files({})
        report = Report(summary="tests failed", fix_proposal=_agent_proposal("no_safe_fix"))
        assert select_fix_proposal(_incident(), report, fetch) is None

    def test_fetch_errors_and_non_strings_are_tolerated(self) -> None:
        def boom(path: str) -> Optional[str]:
            raise RuntimeError("github down")

        assert select_fix_proposal(_incident(), _oom_report(), boom) is None
        assert select_fix_proposal(_incident(), _oom_report(), lambda p: object()) is None  # type: ignore[arg-type,return-value]

    def test_fetch_errors_fall_through_to_agent_proposal(self) -> None:
        def boom(path: str) -> Optional[str]:
            raise RuntimeError("github down")

        agent = _agent_proposal()
        assert select_fix_proposal(_incident(), _oom_report(fix_proposal=agent), boom) is agent


class TestCandidateConfigPaths:
    def test_collects_editable_config_paths_and_skips_pipelines(self) -> None:
        report = _oom_report(
            causal_remediation=[
                "Raise memory in ./deploy/run.sh or k8s/service.yaml",
                "Do not edit .github/workflows/ci.yml or secrets/.env.yaml",
            ],
            annotations=[
                CheckRunAnnotation(path="config/app.toml", start_line=1, end_line=1, message="m")
            ],
        )
        assert candidate_config_paths(report) == [
            "config/app.toml",
            "deploy/run.sh",
            "k8s/service.yaml",
        ]

    def test_candidate_count_is_bounded(self) -> None:
        text = " ".join(f"c{i}.yaml" for i in range(MAX_CONFIG_CANDIDATES + 5))
        assert len(candidate_config_paths(Report(summary=text))) == MAX_CONFIG_CANDIDATES
