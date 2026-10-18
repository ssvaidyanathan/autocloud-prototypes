import json
from typing import Any

import httpx
import pytest
from agent.app.config import AgentConfig
from agent.app.contracts.incident import Incident
from agent.app.llm import generate_gemini_synthesis
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
        assert gen_cfg.get("maxOutputTokens") == 4096
        assert gen_cfg.get("responseMimeType") == "application/json"

    def test_generate_gemini_synthesis_defensive_json_truncation_fallback(
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

        assert res is not None
        # Root cause must be clean prose, not containing JSON syntax or unparsed brackets
        assert not res["root_cause"].startswith("{")
        assert '"root_cause"' not in res["root_cause"]
        assert "The deployment failed because permission was denied on" in res["root_cause"]

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

