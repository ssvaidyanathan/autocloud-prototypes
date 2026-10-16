import json
from typing import Any

import httpx
import pytest
from agent.app.callback import send_callback_report
from agent.app.config import AgentConfig
from agent.app.contracts.request import InvestigationRequest
from agent.app.contracts.result import InvestigationResult
from agent.app.dispatcher import InvestigationDispatcher
from agent.app.tools.registry import default_query_cache
from agent.app.tools.runtime_tools import reset_runtime_state, set_mock_revision
from bridge.tests.fake_github_server import state as github_state


class TestInvestigationDispatcher:
    def test_dispatcher_investigates_and_delivers_callback(
        self,
        agent_settings: AgentConfig,
        fake_github_client: httpx.Client,
        sample_request: InvestigationRequest,
    ) -> None:
        default_query_cache.clear()
        recorded_callbacks: list[dict[str, Any]] = []

        def mock_callback_handler(request: httpx.Request) -> httpx.Response:
            data = json.loads(request.content.decode("utf-8"))
            recorded_callbacks.append(data)
            return httpx.Response(200, json={"status": "ok"})

        callback_transport = httpx.MockTransport(mock_callback_handler)
        callback_client = httpx.Client(transport=callback_transport)

        dispatcher = InvestigationDispatcher(
            config=agent_settings,
            github_client=fake_github_client,
            callback_client=callback_client,
        )

        result = dispatcher.investigate(sample_request)

        # Validate InvestigationResult contract
        assert result.status == "completed"
        assert result.incident_id == sample_request.incident.id
        assert result.incident_source == "github"
        assert result.report is not None

        # Validate synthesized report content
        assert "Run Ruff Lint" in result.report.summary
        assert result.report.probable_root_cause is not None
        assert "unused_token" in result.report.probable_root_cause.text
        assert "Automated fallback (LLM unavailable):" in result.report.probable_root_cause.text
        assert result.report.probable_root_cause.confidence > 0.8
        assert len(result.report.evidence_by_area.get("logs", [])) == 1

        # Validate that incident had its failing stage updated
        assert sample_request.incident.github_failing_stage == "lint"

        # Validate callback delivery to bridge
        assert len(recorded_callbacks) == 1
        delivered = recorded_callbacks[0]
        assert delivered["status"] == "completed"
        assert delivered["incident_id"] == sample_request.incident.id
        assert "report" in delivered

    def test_dispatcher_handles_job_failure_gracefully(
        self,
        agent_settings: AgentConfig,
        sample_request: InvestigationRequest,
    ) -> None:
        default_query_cache.clear()

        def mock_error_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        err_transport = httpx.MockTransport(mock_error_handler)
        err_client = httpx.Client(transport=err_transport)

        dispatcher = InvestigationDispatcher(
            config=agent_settings,
            github_client=err_client,
        )

        result = dispatcher.investigate(sample_request)
        assert result.status == "failed"
        assert result.error is not None
        assert "500" in result.error

    def test_callback_dynamic_token_provider(
        self,
        sample_request: InvestigationRequest,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        headers_recorded: list[dict[str, str]] = []

        def mock_callback(request: httpx.Request) -> httpx.Response:
            headers_recorded.append(dict(request.headers))
            return httpx.Response(200, json={"status": "ok"})

        client = httpx.Client(transport=httpx.MockTransport(mock_callback))
        res = InvestigationResult(
            investigation_id="inv-token-test",
            incident_id=sample_request.incident.id,
            incident_source=sample_request.incident.source,
            incident_external_id=sample_request.incident.external_id,
            incident_external_key=sample_request.incident.external_key,
            status="failed",
            error="error",
            incident=sample_request.incident,
        )

        prod_cfg = AgentConfig(app_env="production")

        # Custom token provider works
        send_callback_report(
            result=res,
            callback_url="http://testbridge.local/callback/report",
            client=client,
            config=prod_cfg,
            token_provider=lambda aud: "custom-gcp-oidc-token",
        )
        assert len(headers_recorded) == 1
        assert headers_recorded[0]["authorization"] == "Bearer custom-gcp-oidc-token"

        # Production environment without metadata server raises
        def mock_failing_get(self: Any, url: str, **kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("Metadata server unreachable")

        monkeypatch.setattr(httpx.Client, "get", mock_failing_get)
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("ENVIRONMENT", "production")

        with pytest.raises(RuntimeError, match="Failed to fetch OIDC ID token"):
            send_callback_report(
                result=res,
                callback_url="http://testbridge.local/callback/report",
                client=client,
                config=prod_cfg,
            )

        # Local environment falls back to mock token when metadata server fails
        local_cfg = AgentConfig(app_env="local")
        monkeypatch.setenv("APP_ENV", "local")
        monkeypatch.setenv("ENVIRONMENT", "local")
        headers_recorded.clear()

        send_callback_report(
            result=res,
            callback_url="http://testbridge.local/callback/report",
            client=client,
            config=local_cfg,
        )
        assert len(headers_recorded) == 1
        assert headers_recorded[0]["authorization"] == "Bearer mock-oidc-token-for-testing"

    def test_dispatcher_diagnoses_pytest_failure(
        self,
        agent_settings: AgentConfig,
        fake_github_client: httpx.Client,
        sample_request: InvestigationRequest,
    ) -> None:
        default_query_cache.clear()
        run_id = sample_request.incident.github_run_id or 1849201948

        # Configure fake jobs and logs for pytest failure
        github_state.jobs[run_id] = {
            "total_count": 1,
            "jobs": [
                {
                    "id": 202,
                    "run_id": run_id,
                    "name": "unit-tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "Checkout", "conclusion": "success", "number": 1},
                        {"name": "Run Pytest Suite", "conclusion": "failure", "number": 2},
                    ],
                }
            ],
        }
        github_state.logs[202] = (
            "tests/test_calculator.py:15: in test_addition\n"
            ">       assert add(2, 2) == 5\n"
            "E       AssertionError: assert 4 == 5\n"
            "FAILED tests/test_calculator.py::test_addition - AssertionError: assert 4 == 5\n"
        )

        dispatcher = InvestigationDispatcher(
            config=agent_settings,
            github_client=fake_github_client,
        )
        result = dispatcher.investigate(sample_request)

        assert result.status == "completed"
        assert result.report is not None
        assert sample_request.incident.github_failing_stage == "test"
        assert "test_addition" in result.report.probable_root_cause.text
        assert any("pytest" in act for act in result.report.recommended_actions)

    def test_dispatcher_diagnoses_docker_build_failure(
        self,
        agent_settings: AgentConfig,
        fake_github_client: httpx.Client,
        sample_request: InvestigationRequest,
    ) -> None:
        default_query_cache.clear()
        run_id = sample_request.incident.github_run_id or 1849201948

        github_state.jobs[run_id] = {
            "total_count": 1,
            "jobs": [
                {
                    "id": 303,
                    "run_id": run_id,
                    "name": "build-container",
                    "status": "completed",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "Checkout", "conclusion": "success", "number": 1},
                        {"name": "Docker Build & Push", "conclusion": "failure", "number": 2},
                    ],
                }
            ],
        }
        github_state.logs[303] = (
            "#5 [3/5] COPY non_existent_file.txt .\n"
            "#5 ERROR: failed to solve: failed to compute cache key: "
            "COPY failed: file not found in build context or excluded by .dockerignore\n"
        )

        dispatcher = InvestigationDispatcher(
            config=agent_settings,
            github_client=fake_github_client,
        )
        result = dispatcher.investigate(sample_request)

        assert result.status == "completed"
        assert result.report is not None
        assert sample_request.incident.github_failing_stage == "build"
        assert "COPY failed" in result.report.probable_root_cause.text
        assert any(
            ".dockerignore" in act or "Dockerfile" in act
            for act in result.report.recommended_actions
        )

    def test_dispatcher_diagnoses_cloud_run_deploy_failure_oom(
        self,
        agent_settings: AgentConfig,
        fake_github_client: httpx.Client,
        sample_request: InvestigationRequest,
    ) -> None:
        default_query_cache.clear()
        reset_runtime_state()
        run_id = sample_request.incident.github_run_id or 1849201948

        github_state.jobs[run_id] = {
            "total_count": 1,
            "jobs": [
                {
                    "id": 404,
                    "run_id": run_id,
                    "name": "deploy-cloudrun",
                    "status": "completed",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "Authenticate to GCP", "conclusion": "success", "number": 1},
                        {
                            "name": "Deploy Container to Cloud Run",
                            "conclusion": "failure",
                            "number": 2,
                        },
                    ],
                }
            ],
        }
        github_state.logs[404] = (
            "Deploying container to Cloud Run service [sample-app] in region [us-central1]...\n"
            "Deploying revision [sample-app-00005-oom]...\n"
            "ERROR: (gcloud.run.deploy) Container failed to start.\n"
        )

        set_mock_revision(
            "sample-app-00005-oom",
            {
                "service_name": "sample-app",
                "revision_name": "sample-app-00005-oom",
                "region": "us-central1",
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "False",
                        "reason": "ContainerTerminated",
                        "message": "Container terminated due to memory limit (exit code 137)",
                    }
                ],
                "container_statuses": [
                    {"name": "sample-app", "restart_count": 2, "last_exit_code": 137}
                ],
            },
        )

        dispatcher = InvestigationDispatcher(
            config=agent_settings,
            github_client=fake_github_client,
        )
        result = dispatcher.investigate(sample_request)

        assert result.status == "completed"
        assert result.report is not None
        assert sample_request.incident.github_failing_stage == "deploy"
        assert (
            "137" in result.report.probable_root_cause.text
            or "OOM" in result.report.probable_root_cause.text
            or "memory" in result.report.probable_root_cause.text.lower()
        )
        assert any("--memory 1Gi" in act for act in result.report.recommended_actions)
