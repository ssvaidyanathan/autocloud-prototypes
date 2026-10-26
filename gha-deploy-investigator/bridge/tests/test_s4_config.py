"""Unit tests for the S4 deploy-config memory tuning fixer (strategy config_tuning)."""

from datetime import datetime, timezone
from typing import Any

import pytest
from bridge.app.contracts import Report
from bridge.app.contracts.findings import EvidenceItem
from bridge.app.contracts.report import ProbableRootCause
from bridge.app.fixer import validate_fix_guardrails
from bridge.app.fixer.s4_config import (
    DEFAULT_MAX_MEMORY_MIB,
    find_memory_edits,
    generate_memory_config_fix,
    is_editable_path,
    is_oom_failure,
)

BASE = "feature/deploy"
OOM_TEXT = (
    "Container terminated: Memory limit of 256 MiB exceeded with 300 MiB used (exit code 137)"
)


def _report(stage: str = "deploy", text: str = OOM_TEXT, evidence: str = "") -> Report:
    evidence_by_area: dict[str, Any] = {}
    if evidence:
        evidence_by_area["logs"] = [
            EvidenceItem(
                kind="cloud_run_container_log",
                excerpt=evidence,
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        ]
    return Report(
        summary="Deploy failed",
        probable_root_cause=ProbableRootCause(text=text, confidence=0.9),
        evidence_by_area=evidence_by_area,
        metadata={"failing_stage": stage},
    )


DEPLOY_SH = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    'SERVICE_MEMORY="256Mi"\n'
    "gcloud run deploy svc --memory=\"${SERVICE_MEMORY}\" --region=us-central1\n"
)


class TestOomDetection:
    @pytest.mark.parametrize(
        "text",
        [
            OOM_TEXT,
            "Process exited with code 137",
            "Container was OOMKilled",
            "fatal: out of memory",
            "worker received SIGKILL",
        ],
    )
    def test_deploy_stage_memory_signals_are_detected(self, text: str) -> None:
        assert is_oom_failure(_report(text=text))

    def test_signal_in_evidence_excerpt_is_detected(self) -> None:
        assert is_oom_failure(_report(text="Deploy failed", evidence="exit code 137"))

    @pytest.mark.parametrize("stage", ["lint", "test", "build", ""])
    def test_non_deploy_stage_is_not_oom_failure(self, stage: str) -> None:
        assert not is_oom_failure(_report(stage=stage))

    def test_deploy_failure_without_memory_signal_is_not_oom(self) -> None:
        assert not is_oom_failure(_report(text="Permission denied on service account"))

    def test_exit_code_1370_is_not_mistaken_for_137(self) -> None:
        assert not is_oom_failure(_report(text="exit code 1370"))


class TestMemoryTuning:
    def test_env_var_in_deploy_script_is_doubled(self) -> None:
        proposal = generate_memory_config_fix(_report(), {"scripts/deploy.sh": DEPLOY_SH}, BASE)

        assert proposal.status == "proposed"
        assert proposal.strategy == "config_tuning"
        assert proposal.target_branch_base == BASE
        assert len(proposal.patches) == 1
        patch = proposal.patches[0]
        assert patch.path == "scripts/deploy.sh"
        assert patch.action == "modify"
        assert 'SERVICE_MEMORY="512Mi"' in patch.content
        assert "-SERVICE_MEMORY=\"256Mi\"" in patch.diff
        assert "+SERVICE_MEMORY=\"512Mi\"" in patch.diff
        assert "256Mi -> 512Mi" in proposal.rationale

    def test_only_the_memory_line_changes(self) -> None:
        proposal = generate_memory_config_fix(_report(), {"scripts/deploy.sh": DEPLOY_SH}, BASE)

        before = DEPLOY_SH.splitlines()
        after = proposal.patches[0].content.splitlines()
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert changed == [2]
        assert len(before) == len(after)
        assert proposal.patches[0].content.endswith("\n")

    def test_proposal_passes_bridge_guardrails(self) -> None:
        proposal = generate_memory_config_fix(_report(), {"scripts/deploy.sh": DEPLOY_SH}, BASE)

        assert validate_fix_guardrails(proposal) == (True, "")

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("MEMORY_LIMIT=256Mi", "MEMORY_LIMIT=512Mi"),
            ("export APP_MEM='512Mi'", "export APP_MEM='1024Mi'"),
            ("MEM_LIMIT=1Gi  # tuned", "MEM_LIMIT=2Gi  # tuned"),
            ("MEMORY=1536Mi", "MEMORY=2048Mi"),  # capped
            ('MEMORY="${MEMORY:-256Mi}"', 'MEMORY="${MEMORY:-512Mi}"'),
            ("export SVC_MEM=${SVC_MEM-1Gi}  # d", "export SVC_MEM=${SVC_MEM-2Gi}  # d"),
        ],
    )
    def test_env_assignment_variants(self, line: str, expected: str) -> None:
        edits = find_memory_edits({"scripts/deploy.sh": line + "\n"})

        assert [e.new_line for e in edits] == [expected]

    def test_yaml_memory_key_is_tuned_preserving_indent_and_quotes(self) -> None:
        yaml_text = 'spec:\n  resources:\n    limits:\n      memory: "512Mi"\n      cpu: "1"\n'

        proposal = generate_memory_config_fix(_report(), {"deploy/service.yaml": yaml_text}, BASE)

        assert proposal.status == "proposed"
        assert '      memory: "1024Mi"\n' in proposal.patches[0].content
        assert '      cpu: "1"\n' in proposal.patches[0].content

    def test_increase_is_capped_at_max(self) -> None:
        edits = find_memory_edits(
            {"d.sh": "MEMORY=1536Mi\n"}, max_memory_mib=DEFAULT_MAX_MEMORY_MIB
        )

        assert edits[0].new_mib == DEFAULT_MAX_MEMORY_MIB

    def test_custom_lower_cap_is_honoured(self) -> None:
        edits = find_memory_edits({"d.sh": "MEMORY=768Mi\n"}, max_memory_mib=1024)

        assert edits[0].new_mib == 1024


class TestNoSafeFixAndUnsupported:
    def test_non_oom_report_is_unsupported(self) -> None:
        proposal = generate_memory_config_fix(
            _report(text="Image not found"), {"scripts/deploy.sh": DEPLOY_SH}, BASE
        )

        assert proposal.status == "unsupported"
        assert proposal.patches == []

    def test_non_deploy_stage_is_unsupported(self) -> None:
        proposal = generate_memory_config_fix(
            _report(stage="build"), {"scripts/deploy.sh": DEPLOY_SH}, BASE
        )

        assert proposal.status == "unsupported"

    @pytest.mark.parametrize("value", ["2Gi", "2048Mi", "4Gi"])
    def test_memory_at_or_above_cap_yields_no_safe_fix(self, value: str) -> None:
        proposal = generate_memory_config_fix(
            _report(), {"scripts/deploy.sh": f"MEMORY={value}\n"}, BASE
        )

        assert proposal.status == "no_safe_fix"
        assert proposal.patches == []
        assert "cap" in proposal.rationale

    def test_no_memory_setting_yields_no_safe_fix(self) -> None:
        proposal = generate_memory_config_fix(_report(), {"scripts/deploy.sh": "echo hi\n"}, BASE)

        assert proposal.status == "no_safe_fix"

    def test_empty_file_set_yields_no_safe_fix(self) -> None:
        assert generate_memory_config_fix(_report(), {}, BASE).status == "no_safe_fix"

    def test_guardrail_failure_on_forbidden_base_branch_yields_no_safe_fix(self) -> None:
        proposal = generate_memory_config_fix(
            _report(), {"scripts/deploy.sh": DEPLOY_SH}, "autofix/run-9"
        )

        assert proposal.status == "no_safe_fix"
        assert "Guardrail" in proposal.rationale

    def test_more_than_three_files_yields_no_safe_fix(self) -> None:
        files = {f"deploy/s{i}.sh": "MEMORY=256Mi\n" for i in range(4)}

        proposal = generate_memory_config_fix(_report(), files, BASE)

        assert proposal.status == "no_safe_fix"
        assert "exceeding limit" in proposal.rationale


class TestRejectsUnsafeScriptModifications:
    @pytest.mark.parametrize(
        "line",
        [
            'gcloud run deploy svc --memory=256Mi',
            'gcloud run deploy svc --memory 256Mi',
            'MEMORY=256Mi; rm -rf /',
            'MEMORY=256Mi && ./deploy',
            'MEMORY=$(echo 256Mi)',
            'MEMORY=`echo 256Mi`',
            'MEMORY=256Mi | tee out',
            'MEMORY=256Mi > /tmp/x',
            'eval MEMORY=256Mi',
            'MEMORY=256Mi ./run.sh',
        ],
    )
    def test_command_lines_are_never_edited(self, line: str) -> None:
        assert find_memory_edits({"scripts/deploy.sh": line + "\n"}) == []

    @pytest.mark.parametrize(
        "line",
        [
            "TIMEOUT=256Mi",  # name not memory-related
            "cpu: 256Mi",
            "MEMORY=256",  # no unit
            "MEMORY=256MB",  # unsupported unit
            "MEMORY=64Mi",  # below minimum sane value
            "# MEMORY=256Mi",  # comment
            'MEMORY="${OTHER_MEM:-256Mi}"',  # default taken from a different variable
            'MEMORY="${MEMORY:-$(probe)256Mi}"',  # command substitution in default
        ],
    )
    def test_non_memory_or_ambiguous_lines_are_not_edited(self, line: str) -> None:
        assert find_memory_edits({"scripts/deploy.sh": line + "\n"}) == []

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/deploy.yml",
            "ci/.gitlab-ci.yml",
            ".gitlab-ci.yml",
            "Jenkinsfile",
            "cloudbuild.yaml",
            "deploy/cloudbuild-prod.yml",
            "azure-pipelines.yml",
            ".circleci/config.yml",
            "Makefile",
            ".env",
            "config/prod.env",
            "secrets/credentials.yaml",
            "/abs/deploy.sh",
            "../deploy.sh",
        ],
    )
    def test_pipeline_definitions_and_forbidden_paths_are_not_editable(self, path: str) -> None:
        assert not is_editable_path(path)
        assert find_memory_edits({path: "memory: 256Mi\nMEMORY=256Mi\n"}) == []

    def test_safe_file_is_patched_while_pipeline_file_is_ignored(self) -> None:
        files = {
            ".github/workflows/deploy.yml": "      memory: 256Mi\n",
            "scripts/deploy.sh": DEPLOY_SH,
        }

        proposal = generate_memory_config_fix(_report(), files, BASE)

        assert proposal.status == "proposed"
        assert [p.path for p in proposal.patches] == ["scripts/deploy.sh"]

    @pytest.mark.parametrize("path", ["scripts/deploy.sh", "deploy/service.yaml"])
    def test_editable_paths(self, path: str) -> None:
        assert is_editable_path(path)
