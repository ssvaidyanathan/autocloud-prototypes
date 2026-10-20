"""GCP Agent Registry Skills client for dynamic in-memory skill package loading."""

import asyncio
import io
import logging
import zipfile
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx
from google.adk.integrations.skill_registry.gcp_skill_registry import (
    GCPSkillRegistry,
    _is_safe_registry_id,
)
from google.adk.skills import _utils, models

logger = logging.getLogger(__name__)


class GCPSkillRegistryClient(GCPSkillRegistry):
    """Client for Google Cloud Agent Registry Skills API (v1alpha).

    Downloads immutable skill ZIP packages directly via media download and
    unpacks them in memory into ADK Skill instances. Strictly enforces zero
    local disk persistence, pure Agent Registry APIs (no GCS fallback),
    and security guardrails prohibiting executable scripts.
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        location: str | None = None,
        base_url: str | None = None,
        credentials: Any = None,
        ssl_context: Any = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            project_id=project_id,
            location=location,
            credentials=credentials,
        )
        if base_url:
            self.base_url = base_url.rstrip("/")
        if ssl_context is not None:
            self._ssl_context = ssl_context
        self._custom_client = http_client

    @asynccontextmanager
    async def _get_client(self) -> AsyncIterator[httpx.AsyncClient]:
        """Provide an AsyncClient context, reusing custom client if provided."""
        if self._custom_client is not None:
            yield self._custom_client
        else:
            client = self._create_httpx_client()
            async with client:
                yield client

    async def _get_headers(self) -> dict[str, str]:
        """Retrieve auth headers, with graceful fallback for offline/test environments."""
        try:
            return await super()._get_headers()
        except RuntimeError as e:
            logger.warning(
                "Unable to obtain default GCP credentials (%s); using offline test headers", e
            )
            return {
                "Authorization": "Bearer mock-agent-registry-token",
                "Content-Type": "application/json",
            }

    async def get_skill(self, name: str | None = None, **kwargs: Any) -> models.Skill:
        """Fetch and unpack a skill package in memory from GCP Agent Registry.

        Args:
            name: Safe resource ID of the skill.
            **kwargs: Keyword arguments, optionally containing 'name'.

        Returns:
            An unpacked in-memory models.Skill object.

        Raises:
            ValueError: If the skill name is invalid or the package contains forbidden scripts.
        """
        resolved_name = name
        if not resolved_name or not isinstance(resolved_name, str):
            raise ValueError("Skill name must be a non-empty string.")

        # Guardrail: validate safe path segment to prevent path traversal
        if not _is_safe_registry_id(resolved_name):
            raise ValueError(
                f"Invalid skill name {resolved_name!r}: name must be a single safe path segment "
                "of at most 256 characters (lowercase letters, digits, and non-consecutive "
                "'.', '_', '-' separators), with no leading, trailing, or consecutive delimiters."
            )

        async with self._get_client() as client:
            # 1. Fetch Skill metadata from Agent Registry v1alpha
            skill_url = (
                f"{self.base_url}/projects/{self.project_id}/"
                f"locations/{self.location}/skills/{quote(resolved_name, safe='')}"
            )
            response = await self._make_request(client, skill_url)
            skill_data = response.json()

            default_revision = skill_data.get("defaultRevision") or skill_data.get(
                "default_revision"
            )
            if not default_revision:
                raise ValueError(
                    f"Skill '{resolved_name}' does not contain default revision."
                )

            # 2. Fetch immutable ZIP package bytes via direct media download
            revision_url = f"{self.base_url}/{default_revision}"
            media_response = await self._make_request(
                client, revision_url, params={"alt": "media"}
            )
            zip_bytes = media_response.content

        # Security guardrail: Scan raw ZIP entries for forbidden scripts/ directory
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
                for member in z.infolist():
                    if member.filename.startswith("scripts/") or "/scripts/" in member.filename:
                        raise ValueError(
                            f"Security violation: skill at '{resolved_name}' contains a forbidden "
                            "'scripts/' directory. Code execution inside skills is prohibited."
                        )
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Skill archive is malformed: {exc}") from exc

        # 3. In-memory package unpacking using ADK utility (never extract to disk)
        skill = await asyncio.to_thread(_utils._load_skill_from_zip_bytes, zip_bytes)

        # Additional security guardrail check on unpacked resources
        if skill.resources and skill.resources.scripts:
            raise ValueError(
                f"Security violation: skill at '{resolved_name}' contains a forbidden "
                "'scripts/' directory. Code execution inside skills is prohibited."
            )

        # Set revision URI on skill
        skill._uri = revision_url
        return skill
