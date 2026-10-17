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
