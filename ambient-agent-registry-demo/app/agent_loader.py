"""Agent Loader implementation dynamically resolving skills and tools from Agent Registry."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
from typing import Any, AsyncGenerator, Sequence

import httpx
from fastapi import HTTPException, status
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.cli.fast_api import BaseAgentLoader
from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.skills.models import Skill
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from app.config import get_settings
from app.skills_loader import SkillsLoader
from app.tools.registry import ToolRegistryManager

logger = logging.getLogger(__name__)


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



class MockGeminiModel(BaseLlm):
    """Mock Gemini model for offline testing without Vertex AI API credentials.

    Drives multi-turn ADK tool execution strictly through tools, never executing
    fallback local arithmetic.
    """

    instruction: str = ""
    declared_tools: list[str] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        contents = llm_request.contents or []
        # Find index of the most recent user prompt to isolate current turn
        last_user_idx = -1
        for idx in range(len(contents) - 1, -1, -1):
            c = contents[idx]
            if c.role == "user" and c.parts and any(p.text for p in c.parts):
                last_user_idx = idx
                break

        turn_contents = contents[last_user_idx:] if last_user_idx != -1 else contents
        user_text = ""
        for c in turn_contents:
            if c.role == "user" and c.parts:
                for p in c.parts:
                    if p.text:
                        user_text += p.text

        def _extract_scalar(resp_data: Any) -> float | None:
            if isinstance(resp_data, (int, float)):
                return float(resp_data)
            if isinstance(resp_data, dict):
                if "output" in resp_data:
                    unwrapped = _extract_scalar(resp_data["output"])
                    if unwrapped is not None:
                        return unwrapped
                structured = resp_data.get("structuredContent")
                if isinstance(structured, dict) and "result" in structured:
                    try:
                        return float(structured["result"])
                    except (ValueError, TypeError):
                        pass
                content_list = resp_data.get("content")
                if isinstance(content_list, list) and len(content_list) > 0:
                    first = content_list[0]
                    if isinstance(first, dict) and "text" in first:
                        try:
                            return float(first["text"])
                        except (ValueError, TypeError):
                            pass
                if "result" in resp_data:
                    try:
                        return float(resp_data["result"])
                    except (ValueError, TypeError):
                        pass
            try:
                return float(str(resp_data))
            except (ValueError, TypeError):
                return None

        # Collect executed tool results for current turn
        responses: dict[str, Any] = {}
        for c in turn_contents:
            if c.parts:
                for p in c.parts:
                    if p.function_response:
                        resp_data = p.function_response.response
                        val = _extract_scalar(resp_data)
                        responses[p.function_response.name] = val

        mult_add_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:multiplied by|\*)\s*(\d+(?:\.\d+)?).*?add\s*(\d+(?:\.\d+)?)",
            user_text,
            re.I,
        )
        power_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:to the power of|\^)\s*(\d+(?:\.\d+)?)",
            user_text,
            re.I,
        )

        if mult_add_match:
            a = float(mult_add_match.group(1))
            b = float(mult_add_match.group(2))
            c = float(mult_add_match.group(3))

            if "multiply" not in responses:
                if "multiply" not in self.declared_tools:
                    raise ValueError(
                        "Tool 'multiply' is not declared in adk_additional_tools"
                    )
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(name="multiply", args={"a": a, "b": b})
                        ],
                    )
                )
                return
            elif "add" not in responses:
                if "add" not in self.declared_tools:
                    raise ValueError(
                        "Tool 'add' is not declared in adk_additional_tools"
                    )
                mult_val = responses["multiply"]
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(name="add", args={"a": mult_val, "b": c})
                        ],
                    )
                )
                return
            else:
                final_val = responses.get("add")
                fmt_val = (
                    int(final_val)
                    if final_val is not None and int(final_val) == final_val
                    else (final_val if final_val is not None else 0)
                )
                if "CALCULATOR-V2" in self.instruction or "[CALCULATOR-V2]" in self.instruction:
                    text = f"🧮 [CALCULATOR-V2] Computed value: **{fmt_val}**"
                else:
                    text = f"Result: {fmt_val} (Computed via multiply, add)"
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part.from_text(text=text)])
                )
                return

        elif power_match:
            base = float(power_match.group(1))
            exp = float(power_match.group(2))
            if "power" not in responses:
                if "power" not in self.declared_tools:
                    raise ValueError(
                        "Tool 'power' is not declared in adk_additional_tools"
                    )
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name="power", args={"base": base, "exponent": exp}
                            )
                        ],
                    )
                )
                return
            else:
                p_val = responses.get("power")
                fmt_val = (
                    int(p_val)
                    if p_val is not None and int(p_val) == p_val
                    else (p_val if p_val is not None else 0)
                )
                if "CALCULATOR-V2" in self.instruction or "[CALCULATOR-V2]" in self.instruction:
                    text = f"🧮 [CALCULATOR-V2] Computed value: **{fmt_val}**"
                else:
                    text = f"Result: {fmt_val} (Computed via power)"
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part.from_text(text=text)])
                )
                return

        else:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=f"Response: {user_text}")],
                )
            )


class AmbientAgentLoader(BaseAgentLoader):
    """ADK Agent Loader subclass dynamically loading skills and tools from Agent Registry."""

    _EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def __init__(
        self,
        skills_loader: SkillsLoader | None = None,
        tool_registry_manager: ToolRegistryManager | None = None,
        model: Any | None = None,
    ) -> None:
        self._skills_loader = skills_loader
        self._tool_registry_manager = tool_registry_manager
        self._model = model
        self.last_resolved_skill: Skill | None = None

    @property
    def skills_loader(self) -> SkillsLoader:
        """Return active SkillsLoader."""
        if self._skills_loader is not None:
            return self._skills_loader
        from app.main import get_skills_loader

        return get_skills_loader()

    @property
    def tool_registry_manager(self) -> ToolRegistryManager:
        """Return active ToolRegistryManager."""
        if self._tool_registry_manager is not None:
            return self._tool_registry_manager
        from app.main import get_tool_registry_manager

        return get_tool_registry_manager()

    def list_agents(self) -> list[str]:
        """Lists all agents available in the agent loader in alphabetical order."""
        return ["ambient_agent"]

    def _sync_worker(self, coro: Any) -> Any:
        """Execute an asynchronous coroutine in a separate worker thread.

        Prevents 'event loop is already running' conflicts when called from inside
        an async server runner.
        """
        future = self._EXECUTOR.submit(asyncio.run, coro)
        return future.result()

    def load_agent(self, agent_name: str) -> BaseAgent:
        """Loads an instance of the ambient agent with dynamic skill and tools.

        Raises:
            ValueError: If agent_name is unrecognized or skill is not found.
            HTTPException: If upstream registry or MCP service fails.
        """
        settings = get_settings()

        valid_names = {"ambient_agent", "ambient-agent", "default", ""}
        if agent_name not in valid_names:
            raise ValueError(f"Agent '{agent_name}' not found. Available: {self.list_agents()}")

        async def _resolve() -> tuple[Skill, Sequence[BaseTool]]:
            target_skill_name = settings.skill_name

            # 1. Resolve skill from GCP Agent Registry
            try:
                skill = await self.skills_loader.resolve_skill(target_skill_name)
            except ValueError as exc:
                logger.warning("Invalid skill requested '%s': %s", target_skill_name, exc)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Skill '{target_skill_name}' not found in Agent Registry: {exc}",
                ) from exc
            except Exception as exc:
                cause = exc.__cause__ if exc.__cause__ is not None else exc
                exc_str = str(exc).lower()
                if (
                    isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 404
                ) or ("status 404" in exc_str or ("not found" in exc_str and "skill" in exc_str)):
                    logger.warning("Skill not found '%s': %s", target_skill_name, exc)
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Skill '{target_skill_name}' not found in Agent Registry: {exc}",
                    ) from exc
                logger.exception("Failed to resolve skill '%s'", target_skill_name)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Failed to load skill '{target_skill_name}' from Agent Registry: {exc}",
                ) from exc

            # 2. Extract declared tools from skill
            declared_tools = extract_declared_tools(skill)

            # 3. Discover remote FastMCP tools from Agent Registry / ToolRegistryManager
            try:
                all_tools = await self.tool_registry_manager.discover_tools(
                    settings.calculator_mcp_server
                )
            except Exception as exc:
                logger.error(
                    "Remote MCP discovery failure for server '%s': %s",
                    settings.calculator_mcp_server,
                    exc,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Remote MCP service failure: {exc}",
                ) from exc

            # 4. Filter tools strictly to skill's declared tools
            active_tools = [tool for tool in all_tools if tool.name in declared_tools]
            return skill, active_tools

        # Execute async resolution via thread worker to isolate event loops
        skill, active_tools = self._sync_worker(_resolve())
        self.last_resolved_skill = skill

        # Extract system instructions
        instruction = skill.instructions or skill.description or ""
        declared_tool_names = extract_declared_tools(skill)

        # 5. Model resolution
        if self._model is not None:
            model_instance = self._model
        elif settings.mock_registry:
            model_instance = MockGeminiModel(
                model=settings.model_name,
                instruction=instruction,
                declared_tools=declared_tool_names,
            )
        else:
            model_instance = Gemini(
                model=settings.model_name,
                client_kwargs={
                    "vertexai": True,
                    "project": settings.project_id,
                    "location": settings.region,
                },
            )

        # 6. Construct and return native ADK LlmAgent
        return LlmAgent(
            name="ambient_agent",
            model=model_instance,
            instruction=instruction,
            tools=list(active_tools),
        )
