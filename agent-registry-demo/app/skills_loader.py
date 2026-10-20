"""SkillsLoader integration layer bridging GCP Agent Registry and ADK SkillToolset."""

import logging
from typing import Any, Sequence

from google.adk.skills.models import Skill
from google.adk.skills.skill_registry import SkillRegistry
from google.adk.tools.skill_toolset import SkillToolset

from app.config import get_settings
from app.registry.cache import CachedSkillRegistry
from app.registry.client import GCPSkillRegistryClient

logger = logging.getLogger(__name__)


class SkillsLoader:
    """Manages skill resolution and ADK SkillToolset wiring backed by Agent Registry.

    Ensures zero local file system dependencies for skills and strictly enforces
    security guardrails rejecting any skill package containing executable scripts.
    """

    def __init__(
        self,
        registry: SkillRegistry | None = None,
        *,
        project_id: str | None = None,
        location: str | None = None,
        base_url: str | None = None,
        cache_ttl: float | None = None,
    ) -> None:
        """Initialize the SkillsLoader with a Cached Agent Registry client."""
        settings = get_settings()
        ttl = cache_ttl if cache_ttl is not None else settings.skill_cache_ttl

        if registry is not None:
            if isinstance(registry, CachedSkillRegistry):
                self.cached_registry = registry
            else:
                self.cached_registry = CachedSkillRegistry(registry, ttl_seconds=ttl)
        else:
            base_client = GCPSkillRegistryClient(
                project_id=project_id or settings.project_id,
                location=location or settings.region,
                base_url=base_url or settings.agent_registry_endpoint,
            )
            self.cached_registry = CachedSkillRegistry(base_client, ttl_seconds=ttl)

        self.registry = self.cached_registry

    def invalidate(self, name: str | None = None) -> list[str]:
        """Atomically invalidate specific cached skill or all cached skills."""
        return self.cached_registry.invalidate(name=name)

    async def resolve_skill(self, name: str) -> Skill:
        """Fetch and return an ADK Skill instance from the registry.

        Raises:
            ValueError: If a security violation or malformed skill package is detected.
        """
        skill = await self.registry.get_skill(name=name)
        if skill.resources and skill.resources.scripts:
            raise ValueError(
                f"Security violation: skill at '{name}' contains a forbidden 'scripts/' directory. "
                "Code execution inside skills is prohibited."
            )
        return skill

    async def get_skill(self, name: str) -> Skill:
        """Alias for resolve_skill."""
        return await self.resolve_skill(name)

    def get_skill_toolset(
        self,
        *,
        additional_tools: Sequence[Any] | None = None,
        tool_filter: Any = None,
        lifecycle_config: Any = None,
    ) -> SkillToolset:
        """Construct an ADK SkillToolset bound to the Agent Registry client.

        Args:
            additional_tools: Optional tool definitions (e.g. MCP tools or FunctionTools)
                that can be dynamically unlocked by loaded skills.
            tool_filter: Optional filter restricting visible tools.
            lifecycle_config: Optional SkillLifecycleConfig for revalidation across turns.

        Returns:
            An instantiated SkillToolset configured with the remote registry.
        """
        return SkillToolset(
            registry=self.registry,
            additional_tools=additional_tools,
            tool_filter=tool_filter,
            lifecycle_config=lifecycle_config,
        )
