import json
from typing import Any, Optional

import httpx
import pytest
from agent.app.config import AgentConfig
from agent.app.contracts.incident import Incident
from agent.app.llm import generate_gemini_synthesis, parse_synthesis_payload
from agent.app.synthesizer import synthesize_report


class TestVertexAIGemini:
    def test_generate_gemini_synthesis_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = AgentConfig(
            app_env="production",
            gcp_project="test-proj-123",
            vertex_ai_location="us-central1",
            gemini_model="test-gemini-model",
        )

        raw_json_text = (
            '```json\n'
            '{\n'
            '  "root_cause": "Cloud Run container failed due to missing storage permissions.",\n'
            '  "remediation_actions": ["Grant storage.admin to worker SA."],\n'
            '  "confidence": 0.96\n'
            '}\n'
            '```'
        )
        mock_response_data = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": raw_json_text
                            }
                        ]
                    }
                }
            ]
        }

        # Mock google.auth.default
        class MockCreds:
            valid = True
            token = "mock-vertex-bearer-token"

            def refresh(self, req: Any) -> None:
                pass

        monkeypatch.setattr(
            "google.auth.default",
            lambda scopes: (MockCreds(), "test-proj-123"),
        )

        def mock_post(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            req = httpx.Request("POST", str(url))
            return httpx.Response(200, request=req, json=mock_response_data)

        client = httpx.Client()
        client.post = mock_post  # type: ignore[method-assign]

        res = generate_gemini_synthesis(
            failing_job={"name": "deploy"},
            failing_step={"name": "Deploy Step"},
            log_text="ERROR: (gcloud.run.deploy) PERMISSION_DENIED",
            config=cfg,
            client=client,
        )

        assert res is not None
        assert "missing storage permissions" in res["root_cause"]
        assert len(res["remediation_actions"]) == 1
        assert res["confidence"] == 0.96

    def test_synthesizer_uses_gemini_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        incident = Incident(
            id="inc-1",
            source="github",
            external_id="1",
            external_key="repo#1",
            title="Deploy failure",
            description="Failed step",
            severity="P2",
            opened_at="2026-10-17T00:00:00Z",
            github_repo="owner/app",
        )

        monkeypatch.setattr(
            "agent.app.llm.generate_gemini_synthesis",
            lambda **kwargs: {
                "root_cause": "Grounded Gemini diagnosis: Missing IAM permission.",
                "remediation_actions": ["Grant IAM role."],
                "confidence": 0.98,
            },
        )

        report = synthesize_report(
            incident=incident,
            failed_job={"name": "deploy-job"},
            failed_step={"name": "deploy-step"},
            log_text="Deploy error line",
        )

        assert report.probable_root_cause is not None
        expected_cause = "Grounded Gemini diagnosis: Missing IAM permission."
        assert report.probable_root_cause.text == expected_cause
        assert report.probable_root_cause.confidence == 0.98
        assert "Automated fallback" not in report.probable_root_cause.text
        assert "Grant IAM role." in report.recommended_actions

    def test_generate_gemini_synthesis_token_budget_and_mime_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = AgentConfig(
            app_env="production",
            gcp_project="test-proj-123",
            vertex_ai_location="us-central1",
            gemini_model="test-gemini-model",
        )

        class MockCreds:
            valid = True
            token = "mock-vertex-bearer-token"

            def refresh(self, req: Any) -> None:
                pass

        monkeypatch.setattr("google.auth.default", lambda scopes: (MockCreds(), "test-proj-123"))

        captured_request: dict[str, Any] = {}

        def mock_post(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal captured_request
            captured_request = kwargs.get("json", {})
            return httpx.Response(
                200,
                request=httpx.Request("POST", str(url)),
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": (
                                    '{"root_cause": "All good", "remediation_actions": [], '
                                    '"confidence": 0.9}'
                                )
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        client = httpx.Client()
        client.post = mock_post  # type: ignore[method-assign]

        res = generate_gemini_synthesis(
            failing_job={"name": "deploy"},
            failing_step={"name": "Deploy Step"},
            log_text="Deploy error log",
            config=cfg,
            client=client,
        )

        assert res is not None
        gen_cfg = captured_request.get("generationConfig", {})
        assert gen_cfg.get("maxOutputTokens") == 8192
        assert gen_cfg.get("responseMimeType") == "application/json"
        assert gen_cfg.get("thinkingConfig") == {"thinkingBudget": 1024}

    def test_generate_gemini_synthesis_truncated_json_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = AgentConfig(
            app_env="production",
            gcp_project="test-proj-123",
            vertex_ai_location="us-central1",
            gemini_model="test-gemini-model",
        )

        class MockCreds:
            valid = True
            token = "mock-vertex-bearer-token"

            def refresh(self, req: Any) -> None:
                pass

        monkeypatch.setattr("google.auth.default", lambda scopes: (MockCreds(), "test-proj-123"))

        # Truncated JSON response simulating max token cutoff
        truncated_text = (
            '{\n  "root_cause": "The deployment failed because permission was denied on '
            'the service account gha-creds-7b90aa6c361'
        )

        def mock_post(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("POST", str(url)),
                json={"candidates": [{"content": {"parts": [{"text": truncated_text}]}}]},
            )

        client = httpx.Client()
        client.post = mock_post  # type: ignore[method-assign]

        res = generate_gemini_synthesis(
            failing_job={"name": "deploy"},
            failing_step={"name": "Deploy Step"},
            log_text="ERROR: PERMISSION_DENIED",
            config=cfg,
            client=client,
        )

        # Truncated JSON is invalid JSON: no heuristic extraction, no fallback.
        assert res is None

    def test_generate_gemini_synthesis_includes_structured_specialist_findings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = AgentConfig(
            app_env="production",
            gcp_project="test-proj-123",
            vertex_ai_location="us-central1",
            gemini_model="test-gemini-model",
        )

        class MockCreds:
            valid = True
            token = "mock-vertex-bearer-token"

            def refresh(self, req: Any) -> None:
                pass

        monkeypatch.setattr("google.auth.default", lambda scopes: (MockCreds(), "test-proj-123"))

        captured_request: dict[str, Any] = {}

        def mock_post(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal captured_request
            captured_request = kwargs.get("json", {})
            return httpx.Response(
                200,
                request=httpx.Request("POST", str(url)),
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": (
                                            '{"root_cause": "Linter error", '
                                            '"remediation_actions": [], "confidence": 0.95}'
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        client = httpx.Client()
        client.post = mock_post  # type: ignore[method-assign]

        specialist_findings = {
            "probable_cause": "Linter detected 5 errors in 'service.py': 3x W291, 1x E501, 1x B006",
            "findings": ["Error 1", "Error 2"],
            "diff_info": "Commit diff touched 1 file(s): src/service.py",
            "structured_errors": [
                {
                    "code": "B006",
                    "file": "src/service.py",
                    "line": 1,
                    "message": "Do not use mutable default arguments",
                },
                {
                    "code": "W291",
                    "file": "src/service.py",
                    "line": 5,
                    "message": "Trailing whitespace",
                },
            ],
        }
        diagnostic_summary = "Specialist diagnostic summary text"

        res = generate_gemini_synthesis(
            failing_job={"name": "test"},
            failing_step={"name": "Run linter"},
            log_text="Linter output...",
            config=cfg,
            client=client,
            specialist_findings=specialist_findings,
            diagnostic_summary=diagnostic_summary,
        )

        assert res is not None
        prompt_text = captured_request["contents"][0]["parts"][0]["text"]
        assert "Specialist Analysis & Findings:" in prompt_text
        assert "3x W291, 1x E501, 1x B006" in prompt_text
        assert "Git Diff Context:\nCommit diff touched 1 file(s): src/service.py" in prompt_text
        assert "[B006] src/service.py:1:" in prompt_text
        assert "Diagnostic Summary: Specialist diagnostic summary text" in prompt_text
        assert "Do NOT guess, approximate, or misattribute error counts" in prompt_text
        assert "Correlate findings with git diff hunks" in prompt_text
        assert "consequential violations" in prompt_text
        assert "Diff-grounded test failure remediation" in prompt_text
        assert "Warning segregation" in prompt_text
        assert "Deploy & runtime failure correlation" in prompt_text

    def test_generate_gemini_synthesis_includes_container_logs_and_telemetry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = AgentConfig(
            app_env="production",
            gcp_project="test-proj-123",
            vertex_ai_location="us-central1",
            gemini_model="test-gemini-model",
        )

        captured_request: dict[str, Any] = {}

        class MockCreds:
            valid = True
            token = "mock-vertex-bearer-token"

            def refresh(self, req: Any) -> None:
                pass

        monkeypatch.setattr(
            "google.auth.default",
            lambda scopes: (MockCreds(), "test-proj-123"),
        )

        def mock_post(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal captured_request
            captured_request = kwargs.get("json", {})
            raw_json_text = json.dumps(
                {
                    "root_cause": "Container crashed due to OOM.",
                    "remediation_actions": ["Increase memory limit."],
                    "confidence": 0.99,
                }
            )
            mock_response_data = {
                "candidates": [{"content": {"parts": [{"text": raw_json_text}]}}]
            }
            req = httpx.Request("POST", str(url))
            return httpx.Response(200, request=req, json=mock_response_data)

        client = httpx.Client()
        client.post = mock_post  # type: ignore[method-assign]

        spec_findings = {
            "probable_cause": "OOM during startup",
            "diff_info": (
                "Commit diff touched 1 file(s): scripts/deploy.sh\n\n"
                "Diff Hunks:\n--- scripts/deploy.sh\n+++ scripts/deploy.sh\n"
                "-MEMORY=512Mi\n+MEMORY=256Mi\n+EXTRA_ENV_VARS=CACHE_PREWARM_BUFFER_MB=2048"
            ),
            "container_logs": "Memory limit of 256 MiB exceeded with 584 MiB used.",
            "telemetry_info": "Cloud Run revision container terminated with exit code 137 (OOM).",
        }

        res = generate_gemini_synthesis(
            failing_job={"name": "deploy"},
            failing_step={"name": "Deploy Step"},
            log_text="Deploy error log",
            config=cfg,
            client=client,
            specialist_findings=spec_findings,
        )

        assert res is not None
        prompt_text = captured_request["contents"][0]["parts"][0]["text"]
        assert "Container Logs Before Termination:" in prompt_text
        assert "Memory limit of 256 MiB exceeded with 584 MiB used." in prompt_text
        assert "Runtime Telemetry:" in prompt_text
        assert "Cloud Run revision container terminated with exit code 137 (OOM)." in prompt_text
        assert "Diff Hunks:" in prompt_text
        assert "+MEMORY=256Mi" in prompt_text
        assert "+EXTRA_ENV_VARS=CACHE_PREWARM_BUFFER_MB=2048" in prompt_text

    def test_synthesizer_passes_container_logs_and_diff_hunks_to_llm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime, timezone

        from agent.app.contracts.findings import EvidenceItem
        from agent.app.contracts.incident import Incident
        from agent.app.specialists.base import DiagnosticResult, format_git_diff_excerpt
        from agent.app.synthesizer import SynthesizerAgent

        incident = Incident(
            id="inc-oom",
            source="github",
            external_id="39043854698",
            external_key="repo#39043854698",
            title="Deploy failure",
            description="Failed deploy",
            severity="P2",
            opened_at="2026-10-17T00:00:00Z",
            github_repo="owner/app",
        )

        now = datetime.now(timezone.utc)
        diff_payload = {
            "files": [
                {
                    "filename": "scripts/deploy.sh",
                    "patch": "@@ -5,2 +5,3 @@\n-MEMORY=512Mi\n+MEMORY=256Mi",
                }
            ]
        }
        diff_text = format_git_diff_excerpt(diff_payload)
        diag = DiagnosticResult(
            stage="deploy",
            specialist_name="runtime_health",
            summary="Cloud Run deploy failed",
            probable_cause="Container terminated with exit code 137 (OOM)",
            confidence=0.95,
            evidence_items=[
                EvidenceItem(kind="git_diff", excerpt=diff_text, timestamp=now),
                EvidenceItem(
                    kind="cloud_run_telemetry",
                    excerpt="Terminated with exit code 137 (OOM)",
                    timestamp=now,
                ),
                EvidenceItem(
                    kind="cloud_run_container_log",
                    excerpt="Memory limit of 256 MiB exceeded with 584 MiB used.",
                    timestamp=now,
                ),
            ],
        )

        captured_spec_findings: dict[str, Any] = {}

        def mock_generate_synthesis(**kwargs: Any) -> dict[str, Any]:
            nonlocal captured_spec_findings
            captured_spec_findings = kwargs.get("specialist_findings", {})
            return {
                "root_cause": "OOM due to lowered memory in deploy.sh",
                "remediation_actions": ["Increase memory"],
                "confidence": 0.99,
            }

        monkeypatch.setattr(
            "agent.app.llm.generate_gemini_synthesis",
            mock_generate_synthesis,
        )

        cfg = AgentConfig(app_env="production", gcp_project="test-proj")
        agent = SynthesizerAgent(config=cfg)
        report = agent.synthesize(
            incident=incident,
            failed_job={"name": "deploy-job"},
            failed_step={"name": "deploy-step"},
            log_text="gcloud deploy error",
            diagnostic_result=diag,
        )

        assert report.probable_root_cause is not None
        expected_log = "Memory limit of 256 MiB exceeded with 584 MiB used."
        assert captured_spec_findings.get("container_logs") == expected_log
        assert (
            captured_spec_findings.get("telemetry_info")
            == "Terminated with exit code 137 (OOM)"
        )
        assert "Diff Hunks:" in captured_spec_findings.get("diff_info", "")
        assert "+MEMORY=256Mi" in captured_spec_findings.get("diff_info", "")

    def test_format_git_diff_excerpt_empty_and_populated(self) -> None:
        from agent.app.specialists.base import format_git_diff_excerpt

        assert format_git_diff_excerpt(None) == ""
        assert format_git_diff_excerpt({}) == ""
        assert format_git_diff_excerpt({"files": []}) == ""

        data_no_patch = {"files": [{"filename": "app/main.py"}]}
        expected_no_patch = "Commit diff touched 1 file(s): app/main.py"
        assert format_git_diff_excerpt(data_no_patch) == expected_no_patch

        data_with_patch = {
            "files": [
                {
                    "filename": "scripts/deploy.sh",
                    "patch": "@@ -1,2 +1,2 @@\n-old\n+new",
                }
            ]
        }
        res = format_git_diff_excerpt(data_with_patch)
        assert "Commit diff touched 1 file(s): scripts/deploy.sh" in res
        assert "Diff Hunks:" in res
        assert "--- scripts/deploy.sh\n+++ scripts/deploy.sh\n@@ -1,2 +1,2 @@\n-old\n+new" in res

    def test_generate_gemini_synthesis_prompt_rules_and_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = AgentConfig(
            app_env="production",
            gcp_project="test-proj-123",
            vertex_ai_location="us-central1",
            gemini_model="test-gemini-model",
        )

        class MockCreds:
            valid = True
            token = "mock-vertex-bearer-token"

            def refresh(self, req: Any) -> None:
                pass

        monkeypatch.setattr("google.auth.default", lambda scopes: (MockCreds(), "test-proj-123"))

        captured_request: dict[str, Any] = {}

        def mock_post(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal captured_request
            captured_request = kwargs.get("json", {})
            return httpx.Response(
                200,
                request=httpx.Request("POST", str(url)),
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": (
                                            '{"root_cause": "Test logic error", '
                                            '"causal_remediation": ["Fix test assertion"], '
                                            '"unrelated_observations": ["Library warning"], '
                                            '"confidence": 0.95}'
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        client = httpx.Client()
        client.post = mock_post  # type: ignore[method-assign]

        res = generate_gemini_synthesis(
            failing_job={"name": "test"},
            failing_step={"name": "Run unit tests"},
            log_text="FAILED tests/test_calc.py::test_calc - AssertionError: 10 != 20",
            config=cfg,
            client=client,
        )

        assert res is not None
        prompt_text = captured_request["contents"][0]["parts"][0]["text"]

        # Assert structured JSON keys in prompt
        assert "'root_cause' (string)" in prompt_text
        assert "'causal_remediation' (list of strings)" in prompt_text
        assert "'unrelated_observations' (list of strings)" in prompt_text

        # Assert generic test-only diff rule in prompt
        assert (
            "When the commit diff touches only test files and no application code changed, "
            "causal remediation must target the test"
        ) in prompt_text
        assert (
            "Put non-causal warnings (e.g. deprecation or runtime warnings not on the fatal path) "
            "into unrelated_observations, NEVER into causal_remediation"
        ) in prompt_text

        # Assert parser extracts both fields
        assert res["causal_remediation"] == ["Fix test assertion"]
        assert res["unrelated_observations"] == ["Library warning"]
        assert res["remediation_actions"] == ["Fix test assertion"]

    def test_generate_gemini_synthesis_parses_legacy_remediation_actions_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = AgentConfig(
            app_env="production",
            gcp_project="test-proj-123",
            vertex_ai_location="us-central1",
            gemini_model="test-gemini-model",
        )

        class MockCreds:
            valid = True
            token = "mock-vertex-bearer-token"

            def refresh(self, req: Any) -> None:
                pass

        monkeypatch.setattr("google.auth.default", lambda scopes: (MockCreds(), "test-proj-123"))

        def mock_post(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("POST", str(url)),
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": (
                                            '{"root_cause": "Legacy response", '
                                            '"remediation_actions": ["Legacy action 1"], '
                                            '"confidence": 0.88}'
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        client = httpx.Client()
        client.post = mock_post  # type: ignore[method-assign]

        res = generate_gemini_synthesis(
            failing_job={"name": "deploy"},
            failing_step={"name": "deploy"},
            log_text="error",
            config=cfg,
            client=client,
        )

        assert res is not None
        assert res["causal_remediation"] == ["Legacy action 1"]
        assert res["remediation_actions"] == ["Legacy action 1"]
        assert res["unrelated_observations"] == []

    def test_synthesizer_populates_causal_remediation_and_unrelated_observations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        incident = Incident(
            id="inc-structured",
            source="github",
            external_id="42",
            external_key="repo#42",
            title="Test failure",
            description="Failed test",
            severity="P3",
            opened_at="2026-10-17T00:00:00Z",
            github_repo="owner/app",
        )

        monkeypatch.setattr(
            "agent.app.llm.generate_gemini_synthesis",
            lambda **kwargs: {
                "root_cause": "Test assertion mismatched expected value.",
                "causal_remediation": ["Correct assertion in test_calc.py."],
                "unrelated_observations": ["Warning: module xyz deprecated."],
                "confidence": 0.97,
            },
        )

        report = synthesize_report(
            incident=incident,
            failed_job={"name": "test-job"},
            failed_step={"name": "test-step"},
            log_text="AssertionError: 10 != 20",
        )

        assert report.causal_remediation == ["Correct assertion in test_calc.py."]
        assert report.unrelated_observations == ["Warning: module xyz deprecated."]
        assert report.recommended_actions == ["Correct assertion in test_calc.py."]



_FIX_PROPOSAL = {
    "summary": "Pin the base image tag.",
    "files": [{"path": "Dockerfile", "patch": "-FROM python\n+FROM python:3.12-slim\n"}],
}


def _synthesis_json(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "root_cause": "  Build failed because the base image tag was not found.  ",
        "causal_remediation": ["Pin the base image tag.", "  ", ""],
        "unrelated_observations": ["Deprecated action warning."],
        "confidence": 0.88,
        "fix_proposal": _FIX_PROPOSAL,
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestParseSynthesisPayload:
    def test_parse_synthesis_payload_plain_json_with_fix_proposal(self) -> None:
        res = parse_synthesis_payload(_synthesis_json())
        assert res is not None
        assert res["root_cause"] == "Build failed because the base image tag was not found."
        assert res["causal_remediation"] == ["Pin the base image tag."]
        assert res["remediation_actions"] == ["Pin the base image tag."]
        assert res["unrelated_observations"] == ["Deprecated action warning."]
        assert res["confidence"] == 0.88
        assert res["fix_proposal"] == _FIX_PROPOSAL

    def test_parse_synthesis_payload_markdown_fenced_json_with_fix_proposal(self) -> None:
        raw = "```JSON\n" + _synthesis_json() + "\n```"
        res = parse_synthesis_payload(raw)
        assert res is not None
        assert res["fix_proposal"] == _FIX_PROPOSAL

    def test_parse_synthesis_payload_wrapped_with_preamble_and_postamble(self) -> None:
        raw = "Here is the analysis:\n" + _synthesis_json() + "\nHope this helps!"
        res = parse_synthesis_payload(raw)
        assert res is not None
        assert res["root_cause"] == "Build failed because the base image tag was not found."
        assert res["fix_proposal"] == _FIX_PROPOSAL

    def test_parse_synthesis_payload_invalid_json_returns_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = '{"root_cause": "Build failed", "fix_proposal": {"summary": "x",}'
        with caplog.at_level("WARNING", logger="agent.app.llm"):
            res = parse_synthesis_payload(raw)
        assert res is None
        assert any("Failed to parse synthesis response" in r.message for r in caplog.records)

    def test_parse_synthesis_payload_no_json_object_returns_none(self) -> None:
        assert parse_synthesis_payload("The build failed, sorry.") is None
        assert parse_synthesis_payload("") is None
        assert parse_synthesis_payload("   ") is None

    def test_parse_synthesis_payload_without_fix_proposal(self) -> None:
        payload = json.loads(_synthesis_json())
        del payload["fix_proposal"]
        del payload["causal_remediation"]
        payload["remediation_actions"] = ["Use legacy key."]
        res = parse_synthesis_payload(json.dumps(payload))
        assert res is not None
        assert "fix_proposal" not in res
        assert res["causal_remediation"] == ["Use legacy key."]

    def test_parse_synthesis_payload_null_fix_proposal_is_omitted(self) -> None:
        res = parse_synthesis_payload(_synthesis_json(fix_proposal=None))
        assert res is not None
        assert "fix_proposal" not in res

    def test_parse_synthesis_payload_missing_root_cause_returns_none(self) -> None:
        payload = json.loads(_synthesis_json())
        del payload["root_cause"]
        assert parse_synthesis_payload(json.dumps(payload)) is None


def _gemini_cfg(**overrides: Any) -> AgentConfig:
    fields: dict[str, Any] = {
        "app_env": "production",
        "gcp_project": "test-proj-123",
        "vertex_ai_location": "us-central1",
        "gemini_model": "test-gemini-model",
    }
    fields.update(overrides)
    return AgentConfig(**fields)


class _MockCreds:
    valid = True
    token = "mock-vertex-bearer-token"

    def refresh(self, req: Any) -> None:
        pass


def _mock_gemini_client(
    monkeypatch: pytest.MonkeyPatch,
    candidate: dict[str, Any],
    captured: Optional[dict[str, Any]] = None,
) -> httpx.Client:
    monkeypatch.setattr("google.auth.default", lambda scopes: (_MockCreds(), "test-proj-123"))

    def mock_post(url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
        if captured is not None:
            captured.update(kwargs.get("json", {}))
        return httpx.Response(
            200,
            request=httpx.Request("POST", str(url)),
            json={"candidates": [candidate]},
        )

    client = httpx.Client()
    client.post = mock_post  # type: ignore[method-assign]
    return client


def _prompt_text(request_body: dict[str, Any]) -> str:
    return str(request_body["contents"][0]["parts"][0]["text"])


def _cleanup_tail(min_chars: int) -> str:
    block = [
        "##[group]Post job cleanup.",
        "[command]/usr/bin/git version",
        "Removing credentials config",
        "Cleaning up orphan processes",
        "##[endgroup]",
    ]
    lines: list[str] = []
    while len("\n".join(lines)) < min_chars:
        lines.extend(block)
    return "\n".join(lines)


class TestGeminiSynthesisGeneration:
    def test_generation_budget_comes_from_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        client = _mock_gemini_client(
            monkeypatch,
            {"content": {"parts": [{"text": _synthesis_json()}]}},
            captured,
        )
        cfg = _gemini_cfg(synthesis_max_output_tokens=12345, synthesis_thinking_budget=321)

        res = generate_gemini_synthesis(
            failing_job={"name": "build"},
            failing_step={"name": "Build"},
            log_text="error: build failed",
            config=cfg,
            client=client,
        )

        assert res is not None
        gen_cfg = captured["generationConfig"]
        assert gen_cfg["maxOutputTokens"] == 12345
        assert gen_cfg["thinkingConfig"] == {"thinkingBudget": 321}

    def test_generation_budget_read_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYNTHESIS_MAX_OUTPUT_TOKENS", "16000")
        monkeypatch.setenv("SYNTHESIS_THINKING_BUDGET", "0")

        cfg = _gemini_cfg()

        assert cfg.synthesis_max_output_tokens == 16000
        assert cfg.synthesis_thinking_budget == 0

    def test_multi_part_response_concatenated_excluding_thoughts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = _synthesis_json()
        split = len(payload) // 2
        client = _mock_gemini_client(
            monkeypatch,
            {
                "content": {
                    "parts": [
                        {"text": "Reasoning about the failure {not json", "thought": True},
                        {"text": payload[:split]},
                        {"text": payload[split:]},
                    ]
                },
                "finishReason": "STOP",
            },
        )
        status: dict[str, Any] = {}

        res = generate_gemini_synthesis(
            failing_job={"name": "build"},
            failing_step={"name": "Build"},
            log_text="error: build failed",
            config=_gemini_cfg(),
            client=client,
            synthesis_status=status,
        )

        assert res is not None
        assert res["root_cause"] == "Build failed because the base image tag was not found."
        assert status == {"outcome": "ok", "finish_reason": "STOP"}

    def test_max_tokens_truncation_logged_and_reported(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _mock_gemini_client(
            monkeypatch,
            {
                "content": {"parts": [{"text": _synthesis_json()[:120]}]},
                "finishReason": "MAX_TOKENS",
            },
        )
        status: dict[str, Any] = {}

        with caplog.at_level("WARNING", logger="agent.app.llm"):
            res = generate_gemini_synthesis(
                failing_job={"name": "build"},
                failing_step={"name": "Build"},
                log_text="error: build failed",
                config=_gemini_cfg(),
                client=client,
                synthesis_status=status,
            )

        assert res is None
        assert status == {"outcome": "truncated", "finish_reason": "MAX_TOKENS"}
        assert any("MAX_TOKENS" in r.getMessage() for r in caplog.records)

    def test_unparseable_response_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _mock_gemini_client(
            monkeypatch,
            {"content": {"parts": [{"text": "not a json payload"}]}, "finishReason": "STOP"},
        )
        status: dict[str, Any] = {}

        res = generate_gemini_synthesis(
            failing_job={"name": "build"},
            failing_step={"name": "Build"},
            log_text="error: build failed",
            config=_gemini_cfg(),
            client=client,
            synthesis_status=status,
        )

        assert res is None
        assert status == {"outcome": "unparseable", "finish_reason": "STOP"}

    def test_log_excerpt_prefers_runner_log_excerpt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        client = _mock_gemini_client(
            monkeypatch, {"content": {"parts": [{"text": _synthesis_json()}]}}, captured
        )
        runner_excerpt = "error: specialist-selected decisive excerpt"

        generate_gemini_synthesis(
            failing_job={"name": "build"},
            failing_step={"name": "Build"},
            log_text="raw step log body that should not be the excerpt",
            config=_gemini_cfg(),
            client=client,
            specialist_findings={"runner_log_excerpt": runner_excerpt},
        )

        prompt = _prompt_text(captured)
        assert runner_excerpt in prompt
        assert "raw step log body that should not be the excerpt" not in prompt

    def test_log_excerpt_uses_decisive_excerpt_not_cleanup_tail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        client = _mock_gemini_client(
            monkeypatch, {"content": {"parts": [{"text": _synthesis_json()}]}}, captured
        )
        decisive = "ERROR: failed to solve: module resolution failed for src/app.py"
        tail = _cleanup_tail(6000)
        log_text = "\n".join(["Step 3/7 : RUN make build", decisive, "Error: exit code 1", tail])
        assert decisive not in log_text[-4000:]

        generate_gemini_synthesis(
            failing_job={"name": "build"},
            failing_step={"name": "Build"},
            log_text=log_text,
            config=_gemini_cfg(),
            client=client,
        )

        prompt = _prompt_text(captured)
        assert decisive in prompt
        assert "Cleaning up orphan processes" not in prompt


def _synth_incident() -> Incident:
    return Incident(
        id="inc-synth-status",
        source="github",
        external_id="2",
        external_key="repo#2",
        title="Build failure",
        description="Failed step",
        severity="P2",
        opened_at="2026-10-17T00:00:00Z",
        github_repo="owner/app",
    )


class TestSynthesisFailureVisibility:
    def _run(
        self, monkeypatch: pytest.MonkeyPatch, outcome: Optional[str], result: Any
    ) -> Any:
        def fake(**kwargs: Any) -> Any:
            status = kwargs.get("synthesis_status")
            if status is not None and outcome is not None:
                status["outcome"] = outcome
                status["finish_reason"] = "MAX_TOKENS" if outcome == "truncated" else "STOP"
            return result

        monkeypatch.setattr("agent.app.llm.generate_gemini_synthesis", fake)
        return synthesize_report(
            incident=_synth_incident(),
            failed_job={"name": "build-job"},
            failed_step={"name": "build-step"},
            log_text="error: build failed",
        )

    def test_truncated_synthesis_surfaces_note_and_heuristic_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = self._run(monkeypatch, "truncated", None)

        assert any("synthesis truncated" in c for c in report.caveats)
        assert any(
            "Heuristic diagnosis because LLM synthesis failed (response truncated" in c
            for c in report.caveats
        )
        assert report.metadata["synthesizer_mode"] == "heuristic_fallback"
        assert report.metadata["heuristic_fallback"] is True
        assert report.metadata["synthesis_outcome"] == "truncated"
        assert report.metadata["synthesis_finish_reason"] == "MAX_TOKENS"

    def test_unparseable_synthesis_surfaces_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = self._run(monkeypatch, "unparseable", None)

        assert any("synthesis unparseable" in c for c in report.caveats)
        assert any(
            "Heuristic diagnosis because LLM synthesis failed (response could not be parsed)"
            in c
            for c in report.caveats
        )
        assert report.metadata["heuristic_fallback"] is True

    def test_unavailable_synthesis_marks_report_heuristic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = self._run(monkeypatch, None, None)

        assert report.metadata["heuristic_fallback"] is True
        assert [
            c
            for c in report.caveats
            if "Heuristic diagnosis because LLM synthesis was unavailable" in c
        ]
        assert not [c for c in report.caveats if "truncated" in c or "unparseable" in c]

    def test_successful_synthesis_has_no_failure_caveats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = self._run(
            monkeypatch,
            "ok",
            {"root_cause": "LLM root cause", "remediation_actions": [], "confidence": 0.9},
        )

        assert report.caveats == []
        assert report.metadata["synthesizer_mode"] == "llm"
        assert report.metadata["heuristic_fallback"] is False
        assert report.metadata["synthesis_outcome"] == "ok"

    def test_truncated_but_parsed_synthesis_still_noted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = self._run(
            monkeypatch,
            "truncated",
            {"root_cause": "LLM root cause", "remediation_actions": [], "confidence": 0.9},
        )

        assert report.metadata["synthesizer_mode"] == "llm"
        assert any("synthesis truncated" in c for c in report.caveats)
        assert not [c for c in report.caveats if "Heuristic diagnosis because" in c]


def test_heuristic_caveat_reaches_servicenow_work_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: failed synthesis -> agent report caveat -> rendered ServiceNow note."""
    from bridge.app.contracts.report import Report as BridgeReport
    from bridge.app.templates.renderer import chunk_servicenow_work_note, render_servicenow_html

    def fake(**kwargs: Any) -> None:
        kwargs["synthesis_status"].update(outcome="unparseable", finish_reason="STOP")
        return None

    monkeypatch.setattr("agent.app.llm.generate_gemini_synthesis", fake)
    report = synthesize_report(
        incident=_synth_incident(),
        failed_job={"name": "build-job"},
        failed_step={"name": "build-step"},
        log_text="error: build failed",
    )
    bridge_report = BridgeReport.model_validate(report.model_dump(mode="json"))

    note = chunk_servicenow_work_note(render_servicenow_html(bridge_report))[0]

    assert bridge_report.metadata["heuristic_fallback"] is True
    assert "Heuristic diagnosis because LLM synthesis failed" in note
    assert "LLM synthesis unparseable" in note
