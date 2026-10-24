from datetime import datetime, timezone
from typing import Optional

from bridge.app.contracts.findings import EvidenceItem
from bridge.app.contracts.incident import Incident
from bridge.app.contracts.report import (
    CheckRunAnnotation,
    FilePatch,
    FixProposal,
    ProbableRootCause,
    Report,
)
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


class TestSelectFixProposal:
    def test_agent_proposal_wins_without_fetching(self) -> None:
        agent = FixProposal(
            status="proposed",
            strategy="llm_patch",
            rationale="r",
            patches=[FilePatch(path="a.py", action="modify", diff="d")],
            target_branch_base="main",
        )
        fetch, calls = _files({"app/main.py": SOURCE})
        report = Report(
            summary="s",
            fix_proposal=agent,
            evidence_by_area={"x": [EvidenceItem(kind="log", excerpt=LINT)]},
        )
        assert select_fix_proposal(_incident(), report, fetch) is agent
        assert calls == []

    def test_lint_fallback_when_agent_declined(self) -> None:
        fetch, _ = _files({"app/main.py": SOURCE})
        report = Report(
            summary="s",
            evidence_by_area={"x": [EvidenceItem(kind="log", excerpt=LINT)]},
            fix_proposal=FixProposal(
                status="no_safe_fix", strategy="llm_patch", rationale="r", target_branch_base="main"
            ),
        )
        proposal = select_fix_proposal(_incident(), report, fetch)
        assert proposal is not None
        assert proposal.strategy == "deterministic_linter"
        assert proposal.patches[0].content == "x = 1\n"

    def test_memory_fallback_for_deploy_oom(self) -> None:
        fetch, calls = _files({"deploy/run.sh": "SERVICE_MEMORY=384Mi\n"})
        proposal = select_fix_proposal(_incident(), _oom_report(), fetch)
        assert proposal is not None
        assert proposal.strategy == "config_tuning"
        assert proposal.patches[0].content == "SERVICE_MEMORY=768Mi\n"
        assert calls == ["deploy/run.sh"]

    def test_none_when_nothing_applies(self) -> None:
        fetch, calls = _files({})
        assert select_fix_proposal(_incident(), Report(summary="tests failed"), fetch) is None
        assert calls == []

    def test_fetch_errors_and_non_strings_are_tolerated(self) -> None:
        def boom(path: str) -> Optional[str]:
            raise RuntimeError("github down")

        assert select_fix_proposal(_incident(), _oom_report(), boom) is None
        assert select_fix_proposal(_incident(), _oom_report(), lambda p: object()) is None  # type: ignore[arg-type,return-value]


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
