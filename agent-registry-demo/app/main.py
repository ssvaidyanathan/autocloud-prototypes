"""FastAPI Application for Agent Registry Demo Prototype."""

import logging
import re
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, status
from google.adk.skills.models import Skill
from pydantic import BaseModel, Field

from app.config import get_settings
from app.skills_loader import SkillsLoader

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


# Pluggable loader & executor for dependency injection and testing
_skills_loader: SkillsLoader | None = None
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
            rev_part = uri_str.split("/revisions/")[-1]
            return rev_part.split("?")[0]
    if skill.frontmatter and skill.frontmatter.metadata:
        version = skill.frontmatter.metadata.get("version")
        if version:
            return str(version)
    return "r1"


async def default_chat_executor(prompt: str, skill: Skill) -> tuple[str, list[str]]:
    """Execute chat turn guided by dynamically loaded skill instructions.

    In Phase 1 vertical slice, simulates instruction following and tool sequencing
    in accordance with the active skill directives.
    """
    instructions = skill.instructions or ""
    tools_invoked: list[str] = []

    # Pattern recognition for arithmetic prompts
    mult_add_match = re.search(r"(\d+)\s*(?:multiplied by|\*)\s*(\d+).*?add\s*(\d+)", prompt, re.I)
    power_match = re.search(r"(\d+)\s*(?:to the power of|\^)\s*(\d+)", prompt, re.I)

    if mult_add_match:
        a, b, c = map(int, mult_add_match.groups())
        val = (a * b) + c
        tools_invoked = ["multiply", "add"]
        if "CALCULATOR-V2" in instructions:
            response_text = f"🧮 [CALCULATOR-V2] Computed value: **{val}**"
        else:
            response_text = f"Result: {val} (Computed via {', '.join(tools_invoked)})"
    elif power_match:
        base, exp = map(int, power_match.groups())
        val = base**exp
        tools_invoked = ["power"]
        if "CALCULATOR-V2" in instructions:
            response_text = f"🧮 [CALCULATOR-V2] Computed value: **{val}**"
        else:
            response_text = f"Result: {val} (Computed via {', '.join(tools_invoked)})"
    else:
        # Generic response reflecting loaded skill instructions
        response_text = f"Executed prompt with skill '{skill.name}': {prompt}"

    return response_text, tools_invoked


@app.get("/health", status_code=status.HTTP_200_OK)
def health() -> dict[str, str]:
    """Health and readiness probe."""
    return {"status": "healthy"}


@app.post("/chat", response_model=ChatResponse, status_code=status.HTTP_200_OK)
async def chat(request: ChatRequest) -> ChatResponse:
    """Execute a chat turn using dynamic skill instructions loaded from Agent Registry."""
    settings = get_settings()
    target_skill = request.skill_name or settings.skill_name

    loader = get_skills_loader()
    try:
        skill = await loader.resolve_skill(target_skill)
    except Exception as exc:
        logger.exception("Failed to resolve skill '%s'", target_skill)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load skill '{target_skill}' from Agent Registry: {exc}",
        ) from exc

    revision = extract_skill_revision(skill)

    executor = _chat_executor or default_chat_executor
    try:
        response_text, tools_invoked = await executor(request.prompt, skill)
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
