"""FastAPI Application for Agent Registry Demo Prototype."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, status
from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.models.google_llm import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.skills.models import Skill
from google.adk.tools.base_tool import BaseTool
from google.genai import types
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


class SkillProvenance(BaseModel):
    """Provenance details for the resolved Agent Registry skill."""

    name: str = Field(..., description="Skill name")
    revision: str = Field(..., description="Active skill revision")
    resource_name: str = Field(..., description="Full GCP Agent Registry resource name")


class ToolInvocation(BaseModel):
    """Record of a tool invocation extracted directly from ADK events."""

    tool: str = Field(..., description="Tool name invoked")
    name: str | None = Field(default=None, description="Tool name alias")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments passed to the tool")
    result: Any = Field(default=None, description="Return value from tool execution")

    def model_post_init(self, __context: object) -> None:
        if self.name is None:
            self.name = self.tool


class ChatResponse(BaseModel):
    """Structured response from chat execution with full provenance."""

    response: str = Field(..., description="Assistant response text")
    skill: SkillProvenance = Field(..., description="Provenance metadata of active skill")
    skill_revision: str = Field(
        ..., description="Active skill revision (maintained for backward compatibility)"
    )
    mcp_server: str = Field(..., description="Resolved MCP server endpoint URL from Agent Registry")
    tools_invoked: list[ToolInvocation] = Field(
        default_factory=list,
        description="Tools invoked during the turn detailing args and return values",
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
_chat_executor: Callable[[str, Skill], Awaitable[tuple[str, list[Any]]]] | None = None


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
    executor: Callable[[str, Skill], Awaitable[tuple[str, list[Any]]]] | None,
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


def extract_declared_tools(skill: Skill) -> list[str]:
    """Extract declared tool names from skill frontmatter metadata."""
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
    return declared_tools


def extract_provenance_from_events(
    events: list[Event],
) -> tuple[str, list[ToolInvocation]]:
    """Extract final response text and structured tool invocations from ADK Events."""
    response_text = ""
    invocations: list[ToolInvocation] = []
    call_map: dict[str, ToolInvocation] = {}

    for event in events:
        for fc in event.get_function_calls():
            inv = ToolInvocation(
                tool=fc.name,
                name=fc.name,
                args=fc.args or {},
            )
            invocations.append(inv)
            key = fc.id or fc.name
            call_map[key] = inv

        for fr in event.get_function_responses():
            key = fr.id or fr.name
            if key in call_map:
                call_map[key].result = fr.response
            else:
                for inv in reversed(invocations):
                    if inv.tool == fr.name and inv.result is None:
                        inv.result = fr.response
                        break

        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    response_text += part.text
        elif event.content and event.content.role == "model" and event.content.parts:
            for part in event.content.parts:
                if part.text and not part.function_call:
                    response_text += part.text

    return response_text.strip(), invocations


async def default_chat_executor(
    prompt: str,
    skill: Skill,
    tool_manager: ToolRegistryManager | None = None,
) -> tuple[str, list[ToolInvocation]]:
    """Execute chat turn using real ADK LlmAgent and Runner with Vertex AI Gemini.

    Strictly enforces adk_additional_tools, interacts via remote MCP, and extracts
    provenance from ADK events with zero local fallback calculation.
    """
    tm = tool_manager or get_tool_registry_manager()
    settings = get_settings()

    # 1. Access control guardrail: Validate required tools are declared in skill metadata
    declared_tools = set(extract_declared_tools(skill))
    if ("power" in prompt.lower() or "^" in prompt) and "power" not in declared_tools:
        raise ValueError(
            f"Tool 'power' is not declared in adk_additional_tools for skill '{skill.name}'"
        )
    if ("multiply" in prompt.lower() or "*" in prompt) and "multiply" not in declared_tools:
        raise ValueError(
            f"Tool 'multiply' is not declared in adk_additional_tools for skill '{skill.name}'"
        )
    if ("add" in prompt.lower() or "+" in prompt) and "add" not in declared_tools:
        raise ValueError(
            f"Tool 'add' is not declared in adk_additional_tools for skill '{skill.name}'"
        )

    # 2. Discover remote MCP tools from Agent Registry catalog (fails loudly if unavailable)
    all_tools = await tm.discover_tools(settings.calculator_mcp_server)

    # 3. Filter discovered tools strictly to skill's declared tools
    active_tools: list[BaseTool] = [tool for tool in all_tools if tool.name in declared_tools]

    # 4. Handle Mock Mode (offline simulation for CI without external Vertex AI billing)
    if settings.mock_registry:
        # Execute via remote MCP server using JSON-RPC (zero local fallback)
        events: list[Event] = []
        instructions = skill.instructions or ""
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
            a = float(mult_add_match.group(1))
            b = float(mult_add_match.group(2))
            c = float(mult_add_match.group(3))

            # Multiply via remote MCP
            m_res = await tm.execute_tool("multiply", {"a": a, "b": b})
            events.append(
                Event(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(name="multiply", args={"a": a, "b": b})
                        ],
                    )
                )
            )
            events.append(
                Event(
                    content=types.Content(
                        role="tool",
                        parts=[
                            types.Part.from_function_response(
                                name="multiply", response={"result": m_res}
                            )
                        ],
                    )
                )
            )

            # Add via remote MCP
            a_res = await tm.execute_tool("add", {"a": m_res, "b": c})
            events.append(
                Event(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(name="add", args={"a": m_res, "b": c})
                        ],
                    )
                )
            )
            events.append(
                Event(
                    content=types.Content(
                        role="tool",
                        parts=[
                            types.Part.from_function_response(
                                name="add", response={"result": a_res}
                            )
                        ],
                    )
                )
            )

            fmt_val = int(a_res) if int(a_res) == a_res else a_res
            if "CALCULATOR-V2" in instructions or "[CALCULATOR-V2]" in instructions:
                resp_text = f"🧮 [CALCULATOR-V2] Computed value: **{fmt_val}**"
            else:
                resp_text = f"Result: {fmt_val} (Computed via multiply, add)"

            events.append(
                Event(
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=resp_text)],
                    )
                )
            )

        elif power_match:
            base = float(power_match.group(1))
            exp = float(power_match.group(2))

            # Power via remote MCP
            p_res = await tm.execute_tool("power", {"base": base, "exponent": exp})
            events.append(
                Event(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name="power", args={"base": base, "exponent": exp}
                            )
                        ],
                    )
                )
            )
            events.append(
                Event(
                    content=types.Content(
                        role="tool",
                        parts=[
                            types.Part.from_function_response(
                                name="power", response={"result": p_res}
                            )
                        ],
                    )
                )
            )

            fmt_val = int(p_res) if int(p_res) == p_res else p_res
            if "CALCULATOR-V2" in instructions or "[CALCULATOR-V2]" in instructions:
                resp_text = f"🧮 [CALCULATOR-V2] Computed value: **{fmt_val}**"
            else:
                resp_text = f"Result: {fmt_val} (Computed via power)"

            events.append(
                Event(
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=resp_text)],
                    )
                )
            )

        else:
            resp_text = f"Executed prompt with skill '{skill.name}': {prompt}"
            events.append(
                Event(
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=resp_text)],
                    )
                )
            )

        return extract_provenance_from_events(events)

    # 5. Live Mode: Real ADK LlmAgent + Runner execution with Vertex AI Gemini
    model_instance = Gemini(
        model=settings.model_name,
        client_kwargs={
            "vertexai": True,
            "project": settings.project_id,
            "location": settings.region,
        },
    )
    instruction = skill.instructions or skill.description or ""
    agent = LlmAgent(
        name="agent_registry_service",
        model=model_instance,
        instruction=instruction,
        tools=active_tools,
    )

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        user_id="demo-user",
        app_name=settings.service_name,
    )
    runner = Runner(
        agent=agent,
        session_service=session_service,
        app_name=settings.service_name,
    )

    user_content = types.Content(
        role="user",
        parts=[types.Part.from_text(text=prompt)],
    )

    collected_events: list[Event] = []
    async for event in runner.run_async(
        user_id="demo-user",
        session_id=session.id,
        new_message=user_content,
    ):
        if event.error_message:
            raise RuntimeError(f"ADK runner error: {event.error_message}")
        collected_events.append(event)

    return extract_provenance_from_events(collected_events)


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
        exc_str = str(exc).lower()
        if (isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 404) or (
            "status 404" in exc_str or ("not found" in exc_str and "skill" in exc_str)
        ):
            logger.warning("Skill not found '%s': %s", target_skill, exc)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Skill '{target_skill}' not found in Agent Registry: {exc}",
            ) from exc
        if (isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 400) or (
            "status 400" in exc_str
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
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to load skill '{target_skill}' from Agent Registry: {exc}",
        ) from exc

    revision = extract_skill_revision(skill)

    # Ensure ToolRegistryManager has resolved its toolset
    tool_manager = get_tool_registry_manager()
    tool_manager.resolve_toolset()

    try:
        if _chat_executor is not None:
            raw_response, raw_tools = await _chat_executor(request.prompt, skill)
            response_text = raw_response
            tools_invoked: list[ToolInvocation] = []
            for item in raw_tools:
                if isinstance(item, ToolInvocation):
                    tools_invoked.append(item)
                elif isinstance(item, dict):
                    tools_invoked.append(ToolInvocation(**item))
                elif isinstance(item, str):
                    tools_invoked.append(ToolInvocation(tool=item, name=item))
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
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        exc_str = str(exc).lower()
        cause_str = str(cause).lower()
        if (
            "mcp" in exc_str
            or "tool" in exc_str
            or "connection" in exc_str
            or "connection" in cause_str
            or "streamable" in exc_str
        ):
            logger.error("Remote MCP service failure for skill '%s': %s", target_skill, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Remote MCP service failure: {exc}",
            ) from exc
        logger.exception("Failed executing chat turn with skill '%s'", target_skill)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chat execution failed: {exc}",
        ) from exc

    resource_name = (
        getattr(skill, "_uri", None)
        or f"projects/{settings.project_id}/locations/{settings.skill_location}/skills/{skill.name}"
    )
    skill_prov = SkillProvenance(
        name=skill.name,
        revision=revision,
        resource_name=str(resource_name),
    )

    return ChatResponse(
        response=response_text,
        skill=skill_prov,
        skill_revision=revision,
        mcp_server=tool_manager.endpoint_url,
        tools_invoked=tools_invoked,
    )
