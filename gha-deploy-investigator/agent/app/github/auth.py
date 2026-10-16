import logging
import time
from typing import Optional

import httpx
import jwt
from agent.app.config import AgentConfig, get_settings

logger = logging.getLogger(__name__)


class GitHubAppAuth:
    """GitHub App authentication handling RS256 JWT generation and installation token caching."""

    def __init__(
        self,
        app_id: Optional[str] = None,
        private_key_pem: Optional[str] = None,
        config: Optional[AgentConfig] = None,
    ) -> None:
        self.config = config or get_settings()
        self.app_id = app_id or getattr(self.config, "github_app_id", None) or "12345"
        self.private_key_pem = private_key_pem or getattr(
            self.config, "github_app_private_key", None
        )
        self._token_cache: dict[int, tuple[str, float]] = {}

    def generate_jwt(self, expiration_seconds: int = 600) -> str:
        """Generate RS256 JWT signed with App Private Key (or test mock in local mode)."""
        if not self.private_key_pem or getattr(self.config, "is_local", False):
            return "mock-github-app-jwt"

        now = int(time.time())
        iat = now - 60
        exp = iat + min(expiration_seconds, 600)
        payload = {
            "iat": iat,
            "exp": exp,
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.private_key_pem, algorithm="RS256")

    def get_installation_token(
        self,
        installation_id: int,
        client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
    ) -> str:
        """Exchange JWT for installation access token with caching."""
        now = time.time()
        if installation_id in self._token_cache:
            token, expires_at = self._token_cache[installation_id]
            if now < expires_at - 60:
                return token

        if getattr(self.config, "is_local", False) and not self.private_key_pem:
            token = self.config.github_token or "ghs_mock_installation_token"
            self._token_cache[installation_id] = (token, now + 3600)
            return token

        api_url = (base_url or self.config.github_api_url or "https://api.github.com").rstrip("/")
        app_jwt = self.generate_jwt()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        }
        endpoint = f"{api_url}/app/installations/{installation_id}/access_tokens"

        owns_client = client is None
        c = client or httpx.Client(timeout=10.0)
        try:
            resp = c.post(endpoint, headers=headers)
            if resp.status_code in (200, 201):
                data = resp.json()
                token = data.get("token", "ghs_mock_installation_token")
                self._token_cache[installation_id] = (token, now + 3500)
                return token
            elif getattr(self.config, "is_local", False):
                token = "ghs_mock_installation_token"
                self._token_cache[installation_id] = (token, now + 3600)
                return token
            resp.raise_for_status()
            raise RuntimeError(
                f"Unexpected response from GitHub App token exchange: {resp.status_code}"
            )
        except Exception as exc:
            if getattr(self.config, "is_local", False):
                token = "ghs_mock_installation_token"
                self._token_cache[installation_id] = (token, now + 3600)
                return token
            logger.error("Failed to exchange installation token for %s: %s", installation_id, exc)
            raise
        finally:
            if owns_client:
                c.close()
