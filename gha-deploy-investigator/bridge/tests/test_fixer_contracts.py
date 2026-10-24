import pytest
from bridge.app.config import BridgeConfig
from bridge.app.contracts import (
    FilePatch,
    FixProposal,
    InvestigationResult,
    PullRequestRef,
    Report,
)
from bridge.app.fixer import (
    AUTOFIX_BRANCH_PREFIX,
    MAX_CHANGED_FILES,
    MAX_LINE_DELTA,
    FixPRWriterBase,
    build_autofix_branch_name,
    is_autofix_branch,
    validate_fix_guardrails,
)
from pydantic import ValidationError


def _diff(added: int, removed: int = 0) -> str:
    header = ["--- a/src/app.py", "+++ b/src/app.py", "@@ -1,1 +1,1 @@", " context"]
    return "\n".join(header + ["-old"] * removed + ["+new"] * added)


def _proposal(patches: list[FilePatch], base: str = "feature/x") -> FixProposal:
    return FixProposal(
        status="proposed",
        strategy="deterministic_linter",
        rationale="Fix lint error",
        patches=patches,
        target_branch_base=base,
    )


def _pr_ref() -> PullRequestRef:
    return PullRequestRef(
        pr_number=42,
        pr_url="https://github.com/owner/repo/pull/42",
        branch_name="autofix/run-123",
        head_sha="abc123",
        status="opened",
    )


class TestFixerContracts:
    def test_file_patch_round_trip(self) -> None:
        patch = FilePatch(path="src/app.py", action="modify", diff=_diff(1, 1))
        restored = FilePatch.model_validate_json(patch.model_dump_json())
        assert restored == patch
        assert restored.content is None

    def test_file_patch_rejects_extra_and_bad_action(self) -> None:
        with pytest.raises(ValidationError):
            FilePatch(path="a.py", action="modify", extra_field="x")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            FilePatch(path="a.py", action="rename")  # type: ignore[arg-type]

    def test_fix_proposal_round_trip(self) -> None:
        proposal = _proposal([FilePatch(path="src/app.py", action="create", content="x = 1\n")])
        restored = FixProposal.model_validate(proposal.model_dump())
        assert restored == proposal
        assert restored.patches[0].action == "create"

    def test_fix_proposal_defaults_and_forbid_extra(self) -> None:
        proposal = FixProposal(
            status="no_safe_fix", strategy="llm_patch", rationale="n/a", target_branch_base="dev"
        )
        assert proposal.patches == []
        with pytest.raises(ValidationError):
            FixProposal(
                status="proposed",
                strategy="llm_patch",
                rationale="r",
                target_branch_base="dev",
                bogus=True,  # type: ignore[call-arg]
            )
        with pytest.raises(ValidationError):
            FixProposal(
                status="proposed",
                strategy="magic",  # type: ignore[arg-type]
                rationale="r",
                target_branch_base="dev",
            )

    def test_pull_request_ref_round_trip_and_forbid_extra(self) -> None:
        ref = _pr_ref()
        assert PullRequestRef.model_validate_json(ref.model_dump_json()) == ref
        with pytest.raises(ValidationError):
            PullRequestRef(**ref.model_dump(), extra="x")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            PullRequestRef(**{**ref.model_dump(), "status": "merged"})

    def test_report_fix_proposal(self) -> None:
        assert Report(summary="s").fix_proposal is None
        proposal = _proposal([FilePatch(path="src/app.py", action="modify", diff=_diff(1))])
        report = Report(summary="s", fix_proposal=proposal)
        restored = Report.model_validate_json(report.model_dump_json())
        assert restored.fix_proposal == proposal

    def test_investigation_result_pull_request(self) -> None:
        result = InvestigationResult(
            incident_id="gha-1",
            incident_source="github",
            status="completed",
            report=Report(summary="s"),
        )
        assert result.pull_request is None
        result = InvestigationResult(
            incident_id="gha-1",
            incident_source="github",
            status="completed",
            report=Report(summary="s"),
            pull_request=_pr_ref(),
        )
        restored = InvestigationResult.model_validate_json(result.model_dump_json())
        assert restored.pull_request == _pr_ref()

    def test_bridge_config_autofix_disabled_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AUTOFIX_ENABLED", raising=False)
        assert BridgeConfig(_env_file=None).autofix_enabled is False  # type: ignore[call-arg]


class TestAutofixBranches:
    def test_build_autofix_branch_name(self) -> None:
        assert build_autofix_branch_name(123) == "autofix/run-123"
        assert build_autofix_branch_name("456") == "autofix/run-456"
        assert build_autofix_branch_name(1).startswith(AUTOFIX_BRANCH_PREFIX)

    @pytest.mark.parametrize(
        ("branch", "expected"),
        [
            ("autofix/run-1", True),
            ("autofix/anything", True),
            ("main", False),
            ("feature/autofix/run-1", False),
            ("", False),
            (None, False),
        ],
    )
    def test_is_autofix_branch(self, branch: str | None, expected: bool) -> None:
        assert is_autofix_branch(branch) is expected


class TestGuardrails:
    def test_valid_proposal(self) -> None:
        ok, reason = validate_fix_guardrails(
            _proposal([FilePatch(path="src/app.py", action="modify", diff=_diff(2, 2))])
        )
        assert ok is True
        assert reason == ""

    @pytest.mark.parametrize(
        "base",
        ["", "   ", "refs/heads/", "autofix/run-1", "refs/heads/autofix/run-1"],
    )
    def test_forbidden_target_branch(self, base: str) -> None:
        ok, reason = validate_fix_guardrails(_proposal([], base=base))
        assert ok is False
        assert "target branch" in reason

    @pytest.mark.parametrize(
        "base", ["main", "master", "refs/heads/main", "release/1.x", "feature/autofix/x"]
    )
    def test_pr_into_default_or_feature_branch_allowed(self, base: str) -> None:
        patch = FilePatch(path="src/app.py", action="modify", diff=_diff(1, 1))
        assert validate_fix_guardrails(_proposal([patch], base=base)) == (True, "")

    @pytest.mark.parametrize(
        "path",
        [
            "./.github/workflows/ci.yml",
            "src/../.github/workflows/ci.yml",
            ".GITHUB/workflows/ci.yml",
            "config/.ENV",
            "certs/server.PEM",
            ".//.github/x.yml",
        ],
    )
    def test_forbidden_path_after_normalisation(self, path: str) -> None:
        ok, reason = validate_fix_guardrails(_proposal([FilePatch(path=path, action="modify")]))
        assert ok is False
        assert "forbidden path" in reason or "Invalid patch path" in reason

    @pytest.mark.parametrize(
        ("path", "why"),
        [
            ("", "empty"),
            ("   ", "empty"),
            ("/.github/workflows/ci.yml", "absolute"),
            ("/etc/passwd", "absolute"),
            ("../outside.py", "traversal"),
            ("src/../../x.py", "traversal"),
            ("src\\app.py", "backslash"),
            ("src/app\x00.py", "NUL"),
            (".", "resolve"),
            ("./", "resolve"),
        ],
    )
    def test_invalid_path_rejected(self, path: str, why: str) -> None:
        ok, reason = validate_fix_guardrails(_proposal([FilePatch(path=path, action="modify")]))
        assert ok is False
        assert "Invalid patch path" in reason
        assert why in reason

    def test_normalised_safe_path_allowed(self) -> None:
        patch = FilePatch(path="./src/app.py", action="modify", diff=_diff(1))
        assert validate_fix_guardrails(_proposal([patch]))[0] is True

    def test_duplicate_paths_rejected(self) -> None:
        patches = [
            FilePatch(path="src/a.py", action="modify", diff=_diff(1)),
            FilePatch(path="./src/a.py", action="modify", diff=_diff(1)),
        ]
        ok, reason = validate_fix_guardrails(_proposal(patches))
        assert ok is False
        assert "Duplicate" in reason

    def test_create_content_counted(self) -> None:
        at_limit = FilePatch(path="src/n.py", action="create", content="x\n" * MAX_LINE_DELTA)
        over = FilePatch(path="src/n.py", action="create", content="x\n" * (MAX_LINE_DELTA + 1))
        assert validate_fix_guardrails(_proposal([at_limit]))[0] is True
        ok, reason = validate_fix_guardrails(_proposal([over]))
        assert ok is False
        assert "exceeds maximum" in reason

    def test_modify_content_only_counted(self) -> None:
        patch = FilePatch(path="src/a.py", action="modify", content="x\n" * 1000)
        ok, reason = validate_fix_guardrails(_proposal([patch]))
        assert ok is False
        assert "exceeds maximum" in reason

    def test_modify_prefers_diff_when_present(self) -> None:
        patch = FilePatch(path="src/a.py", action="modify", content="x\n" * 1000, diff=_diff(2, 2))
        assert validate_fix_guardrails(_proposal([patch]))[0] is True

    def test_mixed_create_and_modify_sum_toward_limit(self) -> None:
        patches = [
            FilePatch(path="src/n.py", action="create", content="x\n" * 30),
            FilePatch(path="src/a.py", action="modify", diff=_diff(MAX_LINE_DELTA - 30 + 1)),
        ]
        assert validate_fix_guardrails(_proposal(patches))[0] is False

    def test_removed_lines_starting_with_dashes_counted(self) -> None:
        header = ["--- a/q.sql", "+++ b/q.sql", "@@ -1,60 +1,0 @@"]
        diff = "\n".join(header + ["--- comment"] * (MAX_LINE_DELTA + 1))
        patch = FilePatch(path="db/q.sql", action="modify", diff=diff)
        ok, reason = validate_fix_guardrails(_proposal([patch]))
        assert ok is False
        assert "exceeds maximum" in reason

    def test_delete_patch_rejected(self) -> None:
        ok, reason = validate_fix_guardrails(
            _proposal([FilePatch(path="src/a.py", action="delete")])
        )
        assert ok is False
        assert "Delete" in reason

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            ".env",
            "config/.env.production",
            "secrets/credentials.json",
            "keys/id_rsa.pub",
            "certs/server.pem",
        ],
    )
    def test_forbidden_path(self, path: str) -> None:
        ok, reason = validate_fix_guardrails(_proposal([FilePatch(path=path, action="modify")]))
        assert ok is False
        assert "forbidden path" in reason

    def test_file_count_limit(self) -> None:
        patches = [
            FilePatch(path=f"src/f{i}.py", action="modify") for i in range(MAX_CHANGED_FILES + 1)
        ]
        ok, reason = validate_fix_guardrails(_proposal(patches))
        assert ok is False
        assert "exceeding limit" in reason

    def test_file_count_at_limit_ok(self) -> None:
        patches = [
            FilePatch(path=f"src/f{i}.py", action="modify") for i in range(MAX_CHANGED_FILES)
        ]
        assert validate_fix_guardrails(_proposal(patches))[0] is True

    def test_line_delta_limit(self) -> None:
        half = MAX_LINE_DELTA // 2
        patches = [
            FilePatch(path="src/a.py", action="modify", diff=_diff(half)),
            FilePatch(path="src/b.py", action="modify", diff=_diff(MAX_LINE_DELTA - half + 1)),
        ]
        ok, reason = validate_fix_guardrails(_proposal(patches))
        assert ok is False
        assert "exceeds maximum" in reason

    def test_line_delta_at_limit_ignores_headers(self) -> None:
        patch = FilePatch(path="src/a.py", action="modify", diff=_diff(MAX_LINE_DELTA))
        assert validate_fix_guardrails(_proposal([patch]))[0] is True


class TestFixPRWriterBase:
    def test_cannot_instantiate_abstract_base(self) -> None:
        with pytest.raises(TypeError):
            FixPRWriterBase()  # type: ignore[abstract]
