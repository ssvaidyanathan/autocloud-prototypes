import httpx
from agent.app.config import AgentConfig
from agent.app.tools.github_tools import (
    _build_client,
    fetch_job_logs,
    fetch_workflow_run_jobs,
    get_commit_diff,
    get_file_content,
)
from agent.app.tools.registry import default_query_cache, default_registry
from bridge.tests.fake_github_server import state as github_state


class TestGitHubTools:
    def test_fetch_workflow_run_jobs(
        self,
        fake_github_client: httpx.Client,
        agent_settings: AgentConfig,
    ) -> None:
        default_query_cache.clear()
        jobs_data = fetch_workflow_run_jobs(
            repo="my-org/sample-app",
            run_id=1849201948,
            client=fake_github_client,
            config=agent_settings,
        )
        assert "jobs" in jobs_data
        assert len(jobs_data["jobs"]) == 1
        assert jobs_data["jobs"][0]["name"] == "lint"
        assert jobs_data["jobs"][0]["conclusion"] == "failure"

    def test_fetch_job_logs(
        self,
        fake_github_client: httpx.Client,
        agent_settings: AgentConfig,
    ) -> None:
        default_query_cache.clear()
        logs = fetch_job_logs(
            repo="my-org/sample-app",
            job_id=101,
            client=fake_github_client,
            config=agent_settings,
        )
        assert "app/main.py:42:5: F841" in logs
        assert "Process completed with exit code 1" in logs

    def test_readonly_tool_invariant(self) -> None:
        """Verify that 100% of registered tools in agent are strictly read-only."""
        default_registry.assert_all_readonly()
        assert not default_registry.has_mutating_tools()
        names = default_registry.get_tool_names()
        assert "fetch_workflow_run_jobs" in names
        assert "fetch_job_logs" in names
        assert "get_commit_diff" in names
        assert "get_file_content" in names
        assert "describe_cloud_run_revision" in names
        assert "query_cloud_logging" in names

    def test_get_commit_diff_and_file_content(
        self,
        fake_github_client: httpx.Client,
        agent_settings: AgentConfig,
    ) -> None:
        default_query_cache.clear()
        diff = get_commit_diff(
            repo="my-org/sample-app",
            head_sha="e4b2a8d39c01",
            client=fake_github_client,
            config=agent_settings,
        )
        assert diff["sha"] == "e4b2a8d39c01"
        assert len(diff["files"]) >= 1

        content = get_file_content(
            repo="my-org/sample-app",
            path="app/main.py",
            ref="main",
            client=fake_github_client,
            config=agent_settings,
        )
        assert "Content for app/main.py" in content

    def test_query_caching(
        self,
        fake_github_client: httpx.Client,
        agent_settings: AgentConfig,
    ) -> None:
        default_query_cache.clear()
        req_count_before = len(github_state.recorded_requests)

        # First call hits mock server
        res1 = fetch_job_logs(
            repo="my-org/sample-app",
            job_id=999,
            client=fake_github_client,
            config=agent_settings,
        )
        assert len(github_state.recorded_requests) == req_count_before + 1

        # Second identical call hits query cache
        res2 = fetch_job_logs(
            repo="my-org/sample-app",
            job_id=999,
            client=fake_github_client,
            config=agent_settings,
        )
        assert len(github_state.recorded_requests) == req_count_before + 1
        assert res1 == res2

    def test_build_client_resolves_installation_token_when_github_token_unset(
        self,
    ) -> None:
        cfg = AgentConfig(
            app_env="local",
            github_token=None,
            github_app_id="12345",
        )
        c, owns = _build_client(config=cfg, repo="my-org/sample-app")
        try:
            assert owns is True
            assert "Authorization" in c.headers
            assert "ghs_mock_installation_token" in c.headers["Authorization"]
        finally:
            c.close()

    def test_fetch_job_logs_follows_redirects(
        self,
        agent_settings: AgentConfig,
    ) -> None:
        default_query_cache.clear()

        def redirect_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/my-org/sample-app/actions/jobs/777/logs":
                return httpx.Response(
                    302,
                    headers={"Location": "https://blob-storage.local/raw-logs/job-777.txt"},
                )
            if request.url.path == "/raw-logs/job-777.txt":
                return httpx.Response(200, text="Runner execution logs from blob storage")
            return httpx.Response(404)

        transport = httpx.MockTransport(redirect_handler)
        mock_client = httpx.Client(transport=transport, follow_redirects=True)

        logs = fetch_job_logs(
            repo="my-org/sample-app",
            job_id=777,
            client=mock_client,
            config=agent_settings,
        )
        assert logs == "Runner execution logs from blob storage"

