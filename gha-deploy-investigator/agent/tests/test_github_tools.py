import httpx
from agent.app.config import AgentConfig
from agent.app.tools.github_tools import fetch_job_logs, fetch_workflow_run_jobs
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
