from unittest.mock import MagicMock, patch

import pytest
from agent.app.config import AgentConfig
from agent.app.tools.github_tools import (
    _build_client,
    fetch_job_logs,
    fetch_workflow_run_jobs,
    get_commit_diff,
    get_file_content,
    reset_github_mock_state,
)
from agent.app.tools.registry import default_registry
from agent.app.tools.runtime_tools import (
    describe_cloud_run_revision,
    describe_service,
    list_service_revisions,
    query_cloud_logging,
    reset_runtime_state,
)


class TestToolClientAudit:
    """Comprehensive audit of EVERY tool in agent/app/tools for client instantiation."""

    def setup_method(self) -> None:
        reset_runtime_state()
        reset_github_mock_state()

    def test_registry_contains_all_expected_tools(self) -> None:
        """Verify the tool registry registers all 8 expected tools and all are read-only."""
        tool_names = set(default_registry.get_tool_names())
        expected_tools = {
            "describe_cloud_run_revision",
            "query_cloud_logging",
            "describe_service",
            "list_service_revisions",
            "fetch_workflow_run_jobs",
            "fetch_job_logs",
            "get_commit_diff",
            "get_file_content",
        }
        assert expected_tools.issubset(tool_names), f"Missing tools: {expected_tools - tool_names}"
        default_registry.assert_all_readonly()
        assert not default_registry.has_mutating_tools()

    def test_query_cloud_logging_constructs_real_client_or_raises_in_production(self) -> None:
        """Verify query_cloud_logging in production constructs real client and never falls back."""
        prod_config = AgentConfig(
            app_env="production",
            environment="production",
            gcp_project="test-prod-project",
        )
        # Mock google.cloud.logging.Client constructor to verify it is called
        with patch("google.cloud.logging.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.list_entries.return_value = [
                {
                    "timestamp": "2026-10-17T11:36:59Z",
                    "severity": "ERROR",
                    "text_payload": "Memory limit of 512 MiB exceeded with 1160 MiB used.",
                }
            ]
            mock_client_cls.return_value = mock_client_instance

            # Invoked WITHOUT client parameter
            results = query_cloud_logging(
                service_name="sample-pipeline-app",
                revision_name="sample-pipeline-app-00007-748",
                config=prod_config,
            )
            # Must construct google.cloud.logging.Client
            mock_client_cls.assert_called_once_with(project="test-prod-project")
            assert len(results) == 1
            assert "Memory limit of 512 MiB exceeded" in results[0]["text_payload"]

        # When client fails in production, it MUST raise RuntimeError, NEVER return []
        reset_runtime_state()
        with patch("google.cloud.logging.Client", side_effect=Exception("Auth failure")):
            with pytest.raises(RuntimeError) as exc_info:
                query_cloud_logging(
                    service_name="sample-pipeline-app",
                    revision_name="sample-pipeline-app-00007-748",
                    config=prod_config,
                )
            assert "Cloud Logging failed in production" in str(exc_info.value)

    def test_describe_cloud_run_revision_constructs_real_client_or_raises_in_production(
        self,
    ) -> None:
        """Verify describe_cloud_run_revision in production constructs RevisionsClient."""
        prod_config = AgentConfig(
            app_env="production",
            environment="production",
            gcp_project="test-prod-project",
        )
        with patch("google.cloud.run_v2.RevisionsClient") as mock_rev_cls:
            mock_rev_instance = MagicMock()
            mock_rev_obj = MagicMock()
            mock_cond = MagicMock()
            mock_cond.type_ = "Ready"
            mock_cond.state.name = "CONDITION_SUCCEEDED"
            mock_cond.reason.name = "RevisionReady"
            mock_cond.message = "Revision is ready."
            mock_rev_obj.conditions = [mock_cond]
            mock_container = MagicMock()
            mock_container.name = "sample-app-1"
            mock_container.image = "gcr.io/test/app:v1"
            mock_container.resources.limits = {"memory": "512Mi", "cpu": "1"}
            mock_rev_obj.containers = [mock_container]
            mock_rev_instance.get_revision.return_value = mock_rev_obj
            mock_rev_cls.return_value = mock_rev_instance

            # Invoked WITHOUT client parameter
            rev_data = describe_cloud_run_revision(
                service_name="sample-pipeline-app",
                revision_name="sample-pipeline-app-00007-748",
                config=prod_config,
            )
            mock_rev_cls.assert_called_once()
            assert rev_data["status"] == "Ready"
            assert len(rev_data["conditions"]) == 1
            assert rev_data["containers"][0]["resources"]["limits"]["memory"] == "512Mi"

        # When client fails in production, it MUST raise RuntimeError, NEVER return Unknown stub
        reset_runtime_state()
        with patch("google.cloud.run_v2.RevisionsClient", side_effect=Exception("API failure")):
            with pytest.raises(RuntimeError) as exc_info:
                describe_cloud_run_revision(
                    service_name="sample-pipeline-app",
                    revision_name="sample-pipeline-app-00007-748",
                    config=prod_config,
                )
            assert "Cloud Run revision lookup failed in production" in str(exc_info.value)

    def test_describe_service_constructs_real_client_or_raises_in_production(self) -> None:
        """Verify describe_service in production constructs ServicesClient."""
        prod_config = AgentConfig(
            app_env="production",
            environment="production",
            gcp_project="test-prod-project",
        )
        with patch("google.cloud.run_v2.ServicesClient") as mock_svc_cls:
            mock_svc_instance = MagicMock()
            mock_svc_obj = MagicMock()
            mock_svc_obj.uri = "https://sample-app.run.app"
            mock_svc_obj.latest_created_revision = "revisions/sample-pipeline-app-00008-xyz"
            mock_svc_obj.latest_ready_revision = "revisions/sample-pipeline-app-00008-xyz"
            mock_svc_obj.conditions = []
            mock_svc_instance.get_service.return_value = mock_svc_obj
            mock_svc_cls.return_value = mock_svc_instance

            svc_data = describe_service(
                service_name="sample-pipeline-app",
                config=prod_config,
            )
            mock_svc_cls.assert_called_once()
            assert svc_data["status"] == "Ready"
            assert svc_data["uri"] == "https://sample-app.run.app"

        # When client fails in production, it MUST raise RuntimeError
        reset_runtime_state()
        with patch("google.cloud.run_v2.ServicesClient", side_effect=Exception("Quota exceeded")):
            with pytest.raises(RuntimeError) as exc_info:
                describe_service(
                    service_name="sample-pipeline-app",
                    config=prod_config,
                )
            assert "Cloud Run service lookup failed in production" in str(exc_info.value)

    def test_list_service_revisions_constructs_real_client_or_raises_in_production(self) -> None:
        """Verify list_service_revisions in production constructs RevisionsClient."""
        prod_config = AgentConfig(
            app_env="production",
            environment="production",
            gcp_project="test-prod-project",
        )
        with patch("google.cloud.run_v2.RevisionsClient") as mock_rev_cls:
            mock_rev_instance = MagicMock()
            mock_rev_item = MagicMock()
            mock_rev_item.name = (
                "projects/test/locations/us-central1/services/sample-pipeline-app/revisions/rev-001"
            )
            mock_rev_item.create_time = "2026-10-17T11:00:00Z"
            mock_rev_item.conditions = []
            mock_rev_instance.list_revisions.return_value = [mock_rev_item]
            mock_rev_cls.return_value = mock_rev_instance

            revs = list_service_revisions(
                service_name="sample-pipeline-app",
                config=prod_config,
            )
            mock_rev_cls.assert_called_once()
            assert len(revs) == 1
            assert revs[0]["revision_name"] == "rev-001"

        # When client fails in production, it MUST raise RuntimeError
        reset_runtime_state()
        with patch("google.cloud.run_v2.RevisionsClient", side_effect=Exception("Network error")):
            with pytest.raises(RuntimeError) as exc_info:
                list_service_revisions(
                    service_name="sample-pipeline-app",
                    config=prod_config,
                )
            assert "Cloud Run list revisions failed in production" in str(exc_info.value)

    def test_github_tools_client_construction_in_production(self) -> None:
        """Verify GitHub tools build client with token and raise loudly in production."""
        prod_config_no_token = AgentConfig(
            app_env="production",
            environment="production",
            github_token=None,
            github_app_id="5352615",
        )
        # In production with github_app_id, failure to obtain installation token must raise
        with patch(
            "agent.app.github.auth.GitHubAppAuth.get_installation_token_for_repo",
            side_effect=Exception("JWT Error"),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                _build_client(config=prod_config_no_token, repo="my-org/sample-app")
            assert "Failed to obtain GitHub App installation token" in str(exc_info.value)

        # With token in production config, builds authenticated client
        prod_config_with_token = AgentConfig(
            app_env="production",
            environment="production",
            github_token="ghp_test_token_12345",
        )
        client, owns = _build_client(config=prod_config_with_token, repo="my-org/sample-app")
        try:
            assert owns is True
            assert client.headers["Authorization"] == "Bearer ghp_test_token_12345"
        finally:
            client.close()

    def test_github_tools_raise_in_production_on_api_error(self) -> None:
        """Verify GitHub API tools do not silently return empty data in production."""
        prod_config = AgentConfig(
            app_env="production",
            environment="production",
            github_token="ghp_test_token_12345",
        )
        with patch("httpx.Client.get", side_effect=Exception("GitHub API 500 Server Error")):
            with pytest.raises(Exception) as exc_info:
                fetch_workflow_run_jobs(
                    repo="my-org/sample-app",
                    run_id=12345,
                    config=prod_config,
                )
            assert "GitHub API 500" in str(exc_info.value)

            with pytest.raises(Exception):
                fetch_job_logs(
                    repo="my-org/sample-app",
                    job_id=999,
                    config=prod_config,
                )

            with pytest.raises(Exception):
                get_commit_diff(
                    repo="my-org/sample-app",
                    head_sha="abcdef123456",
                    config=prod_config,
                )

            with pytest.raises(Exception):
                get_file_content(
                    repo="my-org/sample-app",
                    path="app/main.py",
                    config=prod_config,
                )
