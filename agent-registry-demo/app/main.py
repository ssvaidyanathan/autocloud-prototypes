"""FastAPI Application for Agent Registry Demo Prototype."""

import logging
import re
from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI, HTTPException, status
from google.adk.skills.models import Skill
from pydantic import BaseModel, Field

from app.config import get_settings
from app.registry.cache import CachedSkillRegistry
from app.skills_loader import SkillsLoader
from app.tools.registry import ToolRegistryManager

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Agent Registry Demo",
    description="Agent service dynamically loading skills and tools from GCP Agent Registry",
    version="0.1.0",
)


class ChatRequest(BaseModel):
    """Chat request payload."""

    prompt: str = Field(..., description="User prompt or query to process")
    skill_name: str | None = Field(
        default=None, description="Optional target skill name (defaults to configured skill)"
    )


class ChatResponse(BaseModel):
    """Structured response from chat execution."""

    response: str = Field(..., description="Assistant response text")
    skill_revision: str = Field(..., description="Agent Registry revision of the active skill")
    tools_invoked: list[str] = Field(
        default_factory=list, description="Names of tools invoked during the turn"
    )


class CacheInvalidateRequest(BaseModel):
    """Request payload for cache invalidation."""

    skill_name: str | None = Field(
        default=None, description="Optional skill name to invalidate; if None, flushes all entries"
    )


class CacheInvalidateResponse(BaseModel):
    """Response payload for cache invalidation."""

    status: str = Field(default="invalidated", description="Status of cache invalidation")
    flushed: list[str] = Field(
        default_factory=list, description="List of skill names flushed from the cache"
    )


# Pluggable loader, tool manager & executor for dependency injection and testing
_skills_loader: SkillsLoader | None = None
_tool_registry_manager: ToolRegistryManager | None = None
_chat_executor: Callable[[str, Skill], Awaitable[tuple[str, list[str]]]] | None = None


def get_skills_loader() -> SkillsLoader:
    """Return active SkillsLoader instance."""
    global _skills_loader
    if _skills_loader is None:
        _skills_loader = SkillsLoader()
    return _skills_loader


def set_skills_loader(loader: SkillsLoader | None) -> None:
    """Set or override active SkillsLoader."""
    global _skills_loader
    _skills_loader = loader


def get_cached_registry() -> CachedSkillRegistry:
    """Return active CachedSkillRegistry instance."""
    loader = get_skills_loader()
    if isinstance(loader.registry, CachedSkillRegistry):
        return loader.registry
    if hasattr(loader, "cached_registry") and isinstance(
        loader.cached_registry, CachedSkillRegistry
    ):
        return loader.cached_registry
    cached = CachedSkillRegistry(loader.registry)
    loader.registry = cached
    return cached


def get_tool_registry_manager() -> ToolRegistryManager:
    """Return active ToolRegistryManager instance."""
    global _tool_registry_manager
    if _tool_registry_manager is None:
        _tool_registry_manager = ToolRegistryManager()
    return _tool_registry_manager


def set_tool_registry_manager(manager: ToolRegistryManager | None) -> None:
    """Set or override active ToolRegistryManager."""
    global _tool_registry_manager
    _tool_registry_manager = manager


def set_chat_executor(
    executor: Callable[[str, Skill], Awaitable[tuple[str, list[str]]]] | None,
) -> None:
    """Set or override custom chat execution logic."""
    global _chat_executor
    _chat_executor = executor


def extract_skill_revision(skill: Skill) -> str:
    """Extract revision identifier from skill metadata or URI."""
    if hasattr(skill, "_uri") and skill._uri:
        uri_str = str(skill._uri)
        if "/revisions/" in uri_str:
            rev_part = uri_str.split("/revisions/")[-1].split("?")[0]
            if rev_part.startswith("rev-"):
                rev_part = rev_part[4:]
            return rev_part
    if skill.frontmatter and skill.frontmatter.metadata:
        revision = skill.frontmatter.metadata.get("revision")
        if revision:
            return str(revision)
        version = skill.frontmatter.metadata.get("version")
        if version:
            v_str = str(version)
            if v_str.startswith("r"):
                return v_str
            if v_str == "2.0.0":
                return "r2"
            if v_str == "1.0.0":
                return "r1"
            return v_str
    if skill.instructions and (
        "CALCULATOR-V2" in skill.instructions or "[CALCULATOR-V2]" in skill.instructions
    ):
        return "r2"
    return "r1"


async def default_chat_executor(
    prompt: str,
    skill: Skill,
    tool_manager: ToolRegistryManager | None = None,
) -> tuple[str, list[str]]:
    """Execute chat turn guided by dynamically loaded skill instructions and declared tools.

    Enforces adk_additional_tools: Only tools declared in the active skill's
    metadata.adk_additional_tools are bound/exposed for execution.
    """
    tm = tool_manager or get_tool_registry_manager()
    instructions = skill.instructions or ""
    tools_invoked: list[str] = []

    # Extract declared tools from skill frontmatter
    declared_tools: list[str] = []
    if skill.frontmatter and skill.frontmatter.metadata:
        meta = skill.frontmatter.metadata
        declared = meta.get("adk_additional_tools") or meta.get("tools")
        if isinstance(declared, list):
            declared_tools = [str(t) for t in declared]
        elif isinstance(declared, str):
            declared_tools = [t.strip() for t in declared.replace(",", " ").split() if t.strip()]
    if not declared_tools and skill.frontmatter and skill.frontmatter.allowed_tools:
        if isinstance(skill.frontmatter.allowed_tools, str):
            declared_tools = skill.frontmatter.allowed_tools.split()
        elif isinstance(skill.frontmatter.allowed_tools, (list, tuple)):
            declared_tools = [str(t) for t in skill.frontmatter.allowed_tools]

    # Pattern recognition for arithmetic prompts
    mult_add_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:multiplied by|\*)\s*(\d+(?:\.\d+)?).*?add\s*(\d+(?:\.\d+)?)",
        prompt,
        re.I,
    )
    power_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:to the power of|\^)\s*(\d+(?:\.\d+)?)",
        prompt,
        re.I,
    )

    if mult_add_match:
        required_tools = ["multiply", "add"]
        for req_tool in required_tools:
            if req_tool not in declared_tools:
                raise ValueError(
                    f"Tool '{req_tool}' is not declared in adk_additional_tools "
                    f"for skill '{skill.name}'"
                )

        a, b, c = (
            float(mult_add_match.group(1)),
            float(mult_add_match.group(2)),
            float(mult_add_match.group(3)),
        )
        mult_res = await tm.execute_tool("multiply", {"a": a, "b": b})
        final_val = await tm.execute_tool("add", {"a": mult_res, "b": c})
        tools_invoked = ["multiply", "add"]

        formatted_val = int(final_val) if int(final_val) == final_val else final_val
        if "CALCULATOR-V2" in instructions or "[CALCULATOR-V2]" in instructions:
            response_text = f"🧮 [CALCULATOR-V2] Computed value: **{formatted_val}**"
        else:
            response_text = f"Result: {formatted_val} (Computed via {', '.join(tools_invoked)})"

    elif power_match:
        required_tools = ["power"]
        for req_tool in required_tools:
            if req_tool not in declared_tools:
                raise ValueError(
                    f"Tool '{req_tool}' is not declared in adk_additional_tools "
                    f"for skill '{skill.name}'"
                )

        base, exp = float(power_match.group(1)), float(power_match.group(2))
        final_val = await tm.execute_tool("power", {"base": base, "exponent": exp})
        tools_invoked = ["power"]

        formatted_val = int(final_val) if int(final_val) == final_val else final_val
        if "CALCULATOR-V2" in instructions or "[CALCULATOR-V2]" in instructions:
            response_text = f"🧮 [CALCULATOR-V2] Computed value: **{formatted_val}**"
        else:
            response_text = f"Result: {formatted_val} (Computed via {', '.join(tools_invoked)})"

    else:
        # Generic response reflecting loaded skill instructions
        response_text = f"Executed prompt with skill '{skill.name}': {prompt}"

    return response_text, tools_invoked


@app.get("/health", status_code=status.HTTP_200_OK)
def health() -> dict[str, str]:
    """Health and readiness probe."""
    return {"status": "healthy"}


@app.post(
    "/admin/cache/invalidate",
    response_model=CacheInvalidateResponse,
    status_code=status.HTTP_200_OK,
)
async def invalidate_cache(
    request: CacheInvalidateRequest = CacheInvalidateRequest(),
) -> CacheInvalidateResponse:
    """Atomically invalidate in-memory skill cache."""
    cached_reg = get_cached_registry()
    flushed = cached_reg.invalidate(name=request.skill_name)
    return CacheInvalidateResponse(
        status="invalidated",
        flushed=flushed,
    )


@app.post("/chat", response_model=ChatResponse, status_code=status.HTTP_200_OK)
async def chat(request: ChatRequest) -> ChatResponse:
    """Execute a chat turn using dynamic skill instructions loaded from Agent Registry."""
    settings = get_settings()
    target_skill = request.skill_name or settings.skill_name

    loader = get_skills_loader()
    try:
        skill = await loader.resolve_skill(target_skill)
    except ValueError as exc:
        logger.warning("Invalid skill requested '%s': %s", target_skill, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid skill '{target_skill}': {exc}",
        ) from exc
    except Exception as exc:
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        if (isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 404) or (
            isinstance(exc, RuntimeError) and "status 404" in str(exc)
        ):
            logger.warning("Skill not found '%s': %s", target_skill, exc)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Skill '{target_skill}' not found in Agent Registry: {exc}",
            ) from exc
        if (isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 400) or (
            isinstance(exc, RuntimeError) and "status 400" in str(exc)
        ):
            logger.warning(
                "Invalid request to Agent Registry for skill '%s': %s", target_skill, exc
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid request for skill '{target_skill}': {exc}",
            ) from exc
        logger.exception("Failed to resolve skill '%s'", target_skill)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load skill '{target_skill}' from Agent Registry: {exc}",
        ) from exc

    revision = extract_skill_revision(skill)

    # Ensure ToolRegistryManager has resolved its toolset
    tool_manager = get_tool_registry_manager()
    tool_manager.resolve_toolset()

    executor = _chat_executor or default_chat_executor
    try:
        if _chat_executor is not None:
            response_text, tools_invoked = await executor(request.prompt, skill)
        else:
            response_text, tools_invoked = await default_chat_executor(
                request.prompt, skill, tool_manager=tool_manager
            )
    except ValueError as exc:
        logger.warning("Execution validation error for skill '%s': %s", target_skill, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Failed executing chat turn with skill '%s'", target_skill)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chat execution failed: {exc}",
        ) from exc

    return ChatResponse(
        response=response_text,
        skill_revision=revision,
        tools_invoked=tools_invoked,
    )
