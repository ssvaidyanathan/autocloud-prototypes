"""Thread-safe TTL caching decorator for GCP Agent Registry Skills."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from google.adk.skills.models import Frontmatter, Skill
from google.adk.skills.skill_registry import SkillRegistry

logger = logging.getLogger(__name__)


def compute_skill_content_hash(skill: Skill) -> str:
    """Compute deterministic SHA-256 hash of skill content and metadata."""
    hasher = hashlib.sha256()

    def _feed(*values: str | bytes) -> None:
        for val in values:
            data = val.encode("utf-8") if isinstance(val, str) else val
            hasher.update(len(data).to_bytes(8, "big"))
            hasher.update(data)

    _feed(skill.name, skill.instructions or "")
    if skill.frontmatter:
        try:
            dumped = skill.frontmatter.model_dump(mode="json")
            _feed(json.dumps(dumped, sort_keys=True, default=str))
        except Exception:
            _feed(str(skill.frontmatter))

    if skill.resources:
        res = skill.resources
        for ref_name in sorted(res.list_references()):
            content = res.get_reference(ref_name)
            if content:
                _feed("references", ref_name, content)
        for asset_name in sorted(res.list_assets()):
            asset_bytes = res.get_asset(asset_name)
            if asset_bytes:
                _feed("assets", asset_name, asset_bytes)

    return hasher.hexdigest()


@dataclass
class CacheEntry:
    """In-memory cache entry for a loaded Skill package."""

    skill: Skill
    timestamp: float
    content_hash: str


class CachedSkillRegistry(SkillRegistry):
    """Decorates a SkillRegistry with thread-safe TTL caching and on-demand invalidation."""

    def __init__(
        self,
        target_registry: SkillRegistry,
        ttl_seconds: float = 60.0,
    ) -> None:
        self._target = target_registry
        self._ttl = ttl_seconds
        self._cache: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        """Configured TTL in seconds."""
        return self._ttl

    @property
    def target_registry(self) -> SkillRegistry:
        """Underlying target SkillRegistry."""
        return self._target

    def is_cached(self, name: str) -> bool:
        """Return True if skill exists in cache and has not expired."""
        entry = self._cache.get(name)
        if not entry:
            return False
        return (time.monotonic() - entry.timestamp) < self._ttl

    def get_cached_entry(self, name: str) -> CacheEntry | None:
        """Return cache entry if valid, else None."""
        entry = self._cache.get(name)
        if entry and (time.monotonic() - entry.timestamp) < self._ttl:
            return entry
        return None

    async def get_skill(
        self,
        name: str | None = None,
        *,
        skill_name: str | None = None,
        **kwargs: Any,
    ) -> Skill:
        """Fetch skill from cache (fast path) or underlying registry if expired/missed.

        Accepts both positional and keyword arguments for skill name to remain
        compatible with the ADK abstract interface and diverse caller conventions.
        """
        resolved_name = name or skill_name or kwargs.get("name")
        if not resolved_name or not isinstance(resolved_name, str):
            raise ValueError("Skill name must be a non-empty string.")

        # 1. Fast path: in-memory cache hit (< 1ms, lock-free read)
        now = time.monotonic()
        entry = self._cache.get(resolved_name)
        if entry and (now - entry.timestamp) < self._ttl:
            return entry.skill

        # 2. Cache miss or expired: serialize refresh via async lock
        async with self._lock:
            # Double-check inside lock in case another task refreshed it
            now = time.monotonic()
            entry = self._cache.get(resolved_name)
            if entry and (now - entry.timestamp) < self._ttl:
                return entry.skill

            # Fetch fresh skill from underlying target registry
            fresh_skill = await self._target.get_skill(name=resolved_name, **kwargs)

            # Compute content hash
            try:
                from google.adk.tools.skill_toolset import _skill_content_hash

                chash = _skill_content_hash(fresh_skill)
            except Exception:
                chash = compute_skill_content_hash(fresh_skill)

            self._cache[resolved_name] = CacheEntry(
                skill=fresh_skill,
                timestamp=time.monotonic(),
                content_hash=chash,
            )
            return fresh_skill

    async def search_skills(self, *, query: str, **kwargs: Any) -> list[Frontmatter]:
        """Search skills via the underlying registry."""
        return await self._target.search_skills(query=query, **kwargs)

    def invalidate(self, name: str | None = None) -> list[str]:
        """Atomically clear specific cached skill or entire cache.

        Args:
            name: Optional name of the skill to invalidate. If None, flushes all entries.

        Returns:
            List of skill names that were flushed from the cache.
        """
        if name is not None:
            if not name.strip():
                return []
            if name in self._cache:
                del self._cache[name]
                return [name]
            return []

        flushed = list(self._cache.keys())
        self._cache.clear()
        return flushed
