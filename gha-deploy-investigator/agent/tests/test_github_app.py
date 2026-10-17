import jwt
import pytest
from agent.app.config import AgentConfig
from agent.app.github.auth import GitHubAppAuth
from agent.app.github.client import GitHubRestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient


@pytest.fixture
def rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


class TestGitHubApp:
    def test_github_app_auth_local_fallback(self) -> None:
        cfg = AgentConfig(app_env="local")
        auth = GitHubAppAuth(config=cfg)
        assert auth.generate_jwt() == "mock-github-app-jwt"
        token = auth.get_installation_token(12345)
        assert token in ("ghs_mock_installation_token", "ghp_test_token_123")

    def test_github_app_auth_rs256_generation(self, rsa_private_key_pem: str) -> None:
        cfg = AgentConfig(app_env="production")
        auth = GitHubAppAuth(
            app_id="9999",
            private_key_pem=rsa_private_key_pem,
            config=cfg,
        )
        token_str = auth.generate_jwt(expiration_seconds=300)
        assert token_str != "mock-github-app-jwt"

        # Verify decoded payload
        unverified = jwt.decode(token_str, options={"verify_signature": False})
        assert unverified["iss"] == "9999"
        assert unverified["exp"] > unverified["iat"]
        assert unverified["exp"] - unverified["iat"] == 300
        assert unverified["exp"] - unverified["iat"] <= 600

        # Verify capped at 600 seconds even when expiration_seconds is large
        token_large = auth.generate_jwt(expiration_seconds=1200)
        unverified_large = jwt.decode(token_large, options={"verify_signature": False})
        assert unverified_large["exp"] - unverified_large["iat"] <= 600

    def test_github_app_token_exchange_and_caching(
        self,
        fake_github_client: TestClient,
    ) -> None:
        auth = GitHubAppAuth(
            app_id="12345",
            config=AgentConfig(app_env="production"),
        )
        # First call triggers exchange with fake server
        token1 = auth.get_installation_token(
            installation_id=5829104,
            client=fake_github_client,
            base_url="https://api.github.com",
        )
        assert token1 == "ghs_fake_token_for_inst_5829104"

        # Second call returns from cache
        token2 = auth.get_installation_token(
            installation_id=5829104,
            client=fake_github_client,
            base_url="https://api.github.com",
        )
        assert token2 == token1

    def test_github_rest_client_methods(
        self,
        fake_github_client: TestClient,
    ) -> None:
        rest_client = GitHubRestClient(
            token="ghs_test_token",
            base_url="https://api.github.com",
            client=fake_github_client,
        )

        jobs = rest_client.get_workflow_run_jobs(repo="my-org/sample-app", run_id=1849201948)
        assert "jobs" in jobs
        assert len(jobs["jobs"]) >= 1

        logs = rest_client.get_job_logs(repo="my-org/sample-app", job_id=101)
        assert "Run ruff check" in logs

        commit = rest_client.get_commit_diff(repo="my-org/sample-app", head_sha="e4b2a8d39c01")
        assert commit["sha"] == "e4b2a8d39c01"
        assert len(commit["files"]) >= 1

        content = rest_client.get_file_content(repo="my-org/sample-app", path="app/main.py")
        assert "Content for app/main.py" in content

    def test_get_installation_token_for_repo(
        self,
        fake_github_client: TestClient,
    ) -> None:
        auth = GitHubAppAuth(
            app_id="12345",
            config=AgentConfig(app_env="production"),
        )
        token1 = auth.get_installation_token_for_repo(
            repo="my-org/sample-app",
            client=fake_github_client,
            base_url="https://api.github.com",
        )
        assert token1 == "ghs_fake_token_for_inst_12345"

        # Verify repo installation caching
        token2 = auth.get_installation_token_for_repo(
            repo="my-org/sample-app",
            client=fake_github_client,
            base_url="https://api.github.com",
        )
        assert token2 == token1

