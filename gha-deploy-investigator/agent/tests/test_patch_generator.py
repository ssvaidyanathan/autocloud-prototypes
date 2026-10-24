"""Unit tests for LLM patch extraction and pre-flight validation (strategy llm_patch)."""

import json
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import patch

import pytest
from agent.app.agent_config import get_agent_config, reset_agent_config
from agent.app.contracts.incident import Incident
from agent.app.contracts.report import FixProposal
from agent.app.patch_extractor import (
    MAX_CHANGED_FILES,
    MAX_LINE_DELTA,
    MIN_PATCH_CONFIDENCE,
    PatchValidationError,
    extract_fix_proposal,
    split_multi_file_diff,
    validate_unified_diff,
)
from agent.app.synthesizer import SynthesizerAgent

BASE = "feature/login"


def _diff(path: str = "tests/test_calc.py", added: int = 1, removed: int = 1) -> str:
    """Build a structurally valid single-file unified diff."""
    body = [" ctx_before"] + [f"-old line {i}" for i in range(removed)]
    body += [f"+new line {i}" for i in range(added)] + [" ctx_after"]
    header = [
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -10,{removed + 2} +10,{added + 2} @@",
    ]
    return "\n".join(header + body)


def _raw(patches: list[dict[str, Any]], confidence: float = 0.92, **extra: Any) -> dict:
    data: dict[str, Any] = {
        "status": "proposed",
        "confidence": confidence,
        "rationale": "Assertion compares the wrong literal",
        "patches": patches,
    }
    data.update(extra)
    return data


def _p(path: str = "tests/test_calc.py", **kw: Any) -> dict[str, Any]:
    return {"path": path, "action": "modify", "diff": _diff(path, **kw)}


class TestExtractionHappyPath:
    def test_structured_patch_json_becomes_proposed_llm_patch(self) -> None:
        proposal = extract_fix_proposal(_raw([_p()]), BASE)

        assert proposal.status == "proposed"
        assert proposal.strategy == "llm_patch"
        assert proposal.target_branch_base == BASE
        assert proposal.rationale == "Assertion compares the wrong literal"
        assert [p.path for p in proposal.patches] == ["tests/test_calc.py"]
        assert proposal.patches[0].action == "modify"
        assert proposal.patches[0].diff == _diff()

    def test_json_string_wrapped_in_markdown_fence_is_parsed(self) -> None:
        raw = "```json\n" + json.dumps(_raw([_p()])) + "\n```"

        proposal = extract_fix_proposal(raw, BASE)

        assert proposal.status == "proposed"
        assert len(proposal.patches) == 1

    def test_multi_file_unified_diff_field_is_split_per_file(self) -> None:
        multi = "\n".join(
            [
                "diff --git a/src/a.py b/src/a.py",
                "index 111..222 100644",
                _diff("src/a.py"),
                "diff --git a/Dockerfile b/Dockerfile",
                _diff("Dockerfile"),
            ]
        )
        raw = {"confidence": 0.9, "rationale": "r", "diff": multi}

        proposal = extract_fix_proposal(raw, BASE)

        assert proposal.status == "proposed"
        assert [p.path for p in proposal.patches] == ["src/a.py", "Dockerfile"]

    def test_new_file_diff_from_dev_null_is_classified_as_create(self) -> None:
        diff = "--- /dev/null\n+++ b/assets/logo.txt\n@@ -0,0 +1,1 @@\n+hello"

        proposal = extract_fix_proposal(_raw([{"path": "assets/logo.txt", "diff": diff}]), BASE)

        assert proposal.status == "proposed"
        assert proposal.patches[0].action == "create"

    def test_create_with_full_content_counts_content_lines(self) -> None:
        raw = _raw([{"path": "assets/x.txt", "action": "create", "content": "a\nb\n"}])

        proposal = extract_fix_proposal(raw, BASE)

        assert proposal.status == "proposed"
        assert proposal.patches[0].content == "a\nb\n"

    def test_missing_rationale_gets_generic_rationale(self) -> None:
        raw = _raw([_p()])
        del raw["rationale"]

        proposal = extract_fix_proposal(raw, BASE)

        assert proposal.status == "proposed"
        assert proposal.rationale


class TestNoSafeFixOnConfidence:
    @pytest.mark.parametrize("conf", [0.0, 0.5, 0.849])
    def test_confidence_below_threshold_yields_no_safe_fix(self, conf: float) -> None:
        proposal = extract_fix_proposal(_raw([_p()], confidence=conf), BASE)

        assert proposal.status == "no_safe_fix"
        assert proposal.patches == []
        assert "below threshold" in proposal.rationale

    def test_confidence_exactly_at_threshold_is_accepted(self) -> None:
        proposal = extract_fix_proposal(_raw([_p()], confidence=MIN_PATCH_CONFIDENCE), BASE)

        assert proposal.status == "proposed"

    @pytest.mark.parametrize("conf", [None, "high", True, 1.5, -0.1, float("nan")])
    def test_missing_or_invalid_confidence_yields_no_safe_fix(self, conf: Any) -> None:
        raw = _raw([_p()])
        raw["confidence"] = conf

        proposal = extract_fix_proposal(raw, BASE)

        assert proposal.status == "no_safe_fix"
        assert "confidence" in proposal.rationale.lower()

    def test_bare_unified_diff_without_confidence_yields_no_safe_fix(self) -> None:
        proposal = extract_fix_proposal(_diff(), BASE)

        assert proposal.status == "no_safe_fix"
        assert "confidence" in proposal.rationale.lower()


class TestNoSafeFixOnForbiddenPaths:
    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            "config/.env",
            ".env.production",
            "deploy/credentials.json",
            "home/.ssh/id_rsa",
            "certs/server.pem",
        ],
    )
    def test_forbidden_path_yields_no_safe_fix(self, path: str) -> None:
        proposal = extract_fix_proposal(_raw([_p(path)]), BASE)

        assert proposal.status == "no_safe_fix"
        assert proposal.patches == []
        assert "forbidden path" in proposal.rationale

    def test_one_forbidden_path_among_safe_paths_rejects_whole_proposal(self) -> None:
        raw = _raw([_p("src/a.py"), _p(".github/workflows/deploy.yml")])

        proposal = extract_fix_proposal(raw, BASE)

        assert proposal.status == "no_safe_fix"

    @pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py", "src/../../x.py"])
    def test_absolute_or_traversal_path_yields_no_safe_fix(self, path: str) -> None:
        proposal = extract_fix_proposal(_raw([_p(path)]), BASE)

        assert proposal.status == "no_safe_fix"

    def test_declared_path_must_match_diff_header(self) -> None:
        raw = _raw([{"path": "src/safe.py", "action": "modify", "diff": _diff(".github/x.yml")}])

        proposal = extract_fix_proposal(raw, BASE)

        assert proposal.status == "no_safe_fix"
        assert "does not match" in proposal.rationale


class TestBoundsEnforcement:
    def test_exactly_max_files_is_accepted(self) -> None:
        raw = _raw([_p(f"src/m{i}.py") for i in range(MAX_CHANGED_FILES)])

        assert extract_fix_proposal(raw, BASE).status == "proposed"

    def test_more_than_max_files_yields_no_safe_fix(self) -> None:
        raw = _raw([_p(f"src/m{i}.py") for i in range(MAX_CHANGED_FILES + 1)])

        proposal = extract_fix_proposal(raw, BASE)

        assert proposal.status == "no_safe_fix"
        assert "exceeding limit of 3" in proposal.rationale

    def test_exactly_max_line_delta_is_accepted(self) -> None:
        raw = _raw([_p(added=MAX_LINE_DELTA // 2, removed=MAX_LINE_DELTA // 2)])

        assert extract_fix_proposal(raw, BASE).status == "proposed"

    def test_line_delta_over_max_across_files_yields_no_safe_fix(self) -> None:
        raw = _raw([_p("src/a.py", added=20, removed=5), _p("src/b.py", added=20, removed=6)])

        proposal = extract_fix_proposal(raw, BASE)

        assert proposal.status == "no_safe_fix"
        assert "51 lines" in proposal.rationale

    def test_create_content_counts_toward_line_delta(self) -> None:
        content = "\n".join(f"line {i}" for i in range(MAX_LINE_DELTA + 1))
        raw = _raw([{"path": "src/new.py", "action": "create", "content": content}])

        assert extract_fix_proposal(raw, BASE).status == "no_safe_fix"


class TestInvalidPatchSyntax:
    @pytest.mark.parametrize(
        "diff",
        [
            "just some prose, not a diff",
            "+++ b/src/a.py\n@@ -1,1 +1,1 @@\n-a\n+b",  # missing ---
            "--- a/src/a.py\n@@ -1,1 +1,1 @@\n-a\n+b",  # missing +++
            "--- a/src/a.py\n+++ b/src/a.py\n",  # no hunks
            "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,3 @@\n-a\n+b",  # counts mismatch
            "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,1 +1,1 @@\n-a\n*b",  # bad body line
        ],
    )
    def test_malformed_diff_yields_no_safe_fix(self, diff: str) -> None:
        proposal = extract_fix_proposal(_raw([{"path": "src/a.py", "diff": diff}]), BASE)

        assert proposal.status == "no_safe_fix"
        assert proposal.patches == []

    def test_invalid_json_string_yields_no_safe_fix(self) -> None:
        proposal = extract_fix_proposal('{"confidence": 0.9, "patches": [', BASE)

        assert proposal.status == "no_safe_fix"
        assert "not valid JSON" in proposal.rationale

    @pytest.mark.parametrize("raw", [42, ["a"], {"confidence": 0.9, "patches": "nope"}])
    def test_wrong_shapes_yield_no_safe_fix(self, raw: Any) -> None:
        assert extract_fix_proposal(raw, BASE).status == "no_safe_fix"

    def test_empty_patches_yield_no_safe_fix(self) -> None:
        assert extract_fix_proposal(_raw([]), BASE).status == "no_safe_fix"

    def test_none_yields_no_safe_fix(self) -> None:
        assert extract_fix_proposal(None, BASE).status == "no_safe_fix"

    @pytest.mark.parametrize("action", ["delete", "rename", "chmod"])
    def test_disallowed_actions_yield_no_safe_fix(self, action: str) -> None:
        raw = _raw([{"path": "src/a.py", "action": action, "diff": _diff("src/a.py")}])

        assert extract_fix_proposal(raw, BASE).status == "no_safe_fix"

    def test_diff_deleting_file_yields_no_safe_fix(self) -> None:
        diff = "--- a/src/a.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-a"

        proposal = extract_fix_proposal(_raw([{"path": "src/a.py", "diff": diff}]), BASE)

        assert proposal.status == "no_safe_fix"

    def test_modify_with_full_content_only_yields_no_safe_fix(self) -> None:
        raw = _raw([{"path": "src/a.py", "action": "modify", "content": "x = 1\n"}])

        assert extract_fix_proposal(raw, BASE).status == "no_safe_fix"

    def test_duplicate_file_patches_yield_no_safe_fix(self) -> None:
        assert extract_fix_proposal(_raw([_p(), _p()]), BASE).status == "no_safe_fix"


class TestDeclinedAndContext:
    def test_llm_declared_no_safe_fix_is_honoured_with_its_rationale(self) -> None:
        raw = {"status": "no_safe_fix", "rationale": "Evidence is ambiguous"}

        proposal = extract_fix_proposal(raw, BASE)

        assert proposal.status == "no_safe_fix"
        assert "Evidence is ambiguous" in proposal.rationale

    @pytest.mark.parametrize("base", [None, "", "   "])
    def test_missing_target_branch_yields_no_safe_fix(self, base: Any) -> None:
        proposal = extract_fix_proposal(_raw([_p()]), base)

        assert proposal.status == "no_safe_fix"
        assert "target branch" in proposal.rationale


class TestDiffHelpers:
    def test_validate_unified_diff_counts_added_and_removed_lines(self) -> None:
        old, new, delta = validate_unified_diff(_diff("src/a.py", added=3, removed=2))

        assert (old, new, delta) == ("src/a.py", "src/a.py", 5)

    def test_validate_unified_diff_tolerates_no_newline_marker(self) -> None:
        diff = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b"

        assert validate_unified_diff(diff)[2] == 2

    def test_validate_unified_diff_rejects_multiple_file_sections(self) -> None:
        with pytest.raises(PatchValidationError):
            validate_unified_diff(_diff("a.py") + "\n" + _diff("b.py"))

    def test_split_multi_file_diff_returns_one_section_per_file(self) -> None:
        assert len(split_multi_file_diff(_diff("a.py") + "\n" + _diff("b.py"))) == 2


# --------------------------------------------------------------------------------------
# Prompt configuration and synthesizer integration
# --------------------------------------------------------------------------------------


@pytest.fixture
def clean_config() -> Generator[None, None, None]:
    reset_agent_config()
    yield
    reset_agent_config()


@pytest.mark.usefixtures("clean_config")
class TestPatchInstructionConfig:
    def test_synthesizer_patch_instruction_lives_in_agent_yaml(self) -> None:
        text = get_agent_config().get_patch_instruction("synthesizer")

        assert "fix_proposal" in text
        assert "no_safe_fix" in text
        assert "0.85" in text
        assert "at most 3 files" in text and "50" in text

    def test_patch_instruction_is_appended_to_synthesis_prompt(self) -> None:
        from agent.app import llm

        captured: dict[str, Any] = {}

        class _Resp:
            status_code = 200
            text = ""

            def json(self) -> dict:
                payload = {
                    "root_cause": "assert failed",
                    "confidence": 0.9,
                    "fix_proposal": {"status": "no_safe_fix", "rationale": "x"},
                }
                return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}

        class _Client:
            def post(self, url: str, json: dict, headers: dict) -> _Resp:
                captured.update(json)
                return _Resp()

        class _Creds:
            valid = True
            token = "tok"

        cfg = type("Cfg", (), {"gcp_project": "p", "gemini_model": "m"})()
        with patch.object(llm.google.auth, "default", return_value=(_Creds(), "p")):
            result = llm.generate_gemini_synthesis(
                {"name": "j"}, {"name": "s"}, "log", config=cfg, client=_Client()
            )

        prompt_text = captured["contents"][0]["parts"][0]["text"]
        assert get_agent_config().get_patch_instruction("synthesizer") in prompt_text
        assert result is not None
        assert result["fix_proposal"] == {"status": "no_safe_fix", "rationale": "x"}


class _StubLLM:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    def synthesize(self, **_: Any) -> dict[str, Any]:
        return self.result


def _incident(branch: str | None = BASE) -> Incident:
    return Incident(
        id="gha-1",
        source="github",
        external_id="1",
        external_key="org/repo#1",
        title="CI failure",
        description="Workflow failed",
        severity="P2",
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        github_repo="org/repo",
        github_branch=branch,
    )


@pytest.mark.usefixtures("clean_config")
class TestSynthesizerIntegration:
    def _synth(self, llm_result: dict[str, Any], branch: str | None = BASE) -> Any:
        agent = SynthesizerAgent(llm_client=_StubLLM(llm_result), llm_agent=object())
        return agent.synthesize(incident=_incident(branch), log_text="FAILED test")

    def test_valid_llm_patch_is_attached_to_report(self) -> None:
        report = self._synth(
            {"root_cause": "wrong literal", "confidence": 0.9, "fix_proposal": _raw([_p()])}
        )

        assert isinstance(report.fix_proposal, FixProposal)
        assert report.fix_proposal.status == "proposed"
        assert report.fix_proposal.strategy == "llm_patch"
        assert report.fix_proposal.target_branch_base == BASE

    def test_unsafe_llm_patch_is_attached_as_no_safe_fix(self) -> None:
        report = self._synth(
            {
                "root_cause": "wrong literal",
                "fix_proposal": _raw([_p(".github/workflows/ci.yml")]),
            }
        )

        assert report.fix_proposal is not None
        assert report.fix_proposal.status == "no_safe_fix"

    def test_report_has_no_fix_proposal_when_llm_omits_it(self) -> None:
        report = self._synth({"root_cause": "wrong literal", "confidence": 0.9})

        assert report.fix_proposal is None
