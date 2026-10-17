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
            '  "root_cause": "Cloud Run container failed due to IAM actAs denial.",\n'
            '  "remediation_actions": ["Grant roles/iam.serviceAccountUser to deployment SA."],\n'
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
        assert "IAM actAs denial" in res["root_cause"]
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
