"""FastAPI Application for Ambient Agent Registry Demo Prototype."""

from __future__ import annotations

import json
import logging
import os

import httpx
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from google.adk.cli.api_server import ApiServer
from google.adk.cli.fast_api import get_fast_api_app
from pydantic import BaseModel, Field

from app.agent_loader import AmbientAgentLoader
from app.config import get_settings
from app.registry.cache import CachedSkillRegistry
from app.skills_loader import SkillsLoader
from app.tools.registry import ToolRegistryManager

logger = logging.getLogger(__name__)

# Default agent name for native ADK runtime
os.environ.setdefault("ADK_DEFAULT_APP_NAME", "ambient_agent")


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


# Pluggable loader & tool manager for dependency injection and testing
_agent_loader: AmbientAgentLoader | None = None
_skills_loader: SkillsLoader | None = None
_tool_registry_manager: ToolRegistryManager | None = None


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
    if "app" in globals():
        api_server = getattr(globals()["app"].state, "api_server", None)
        if api_server is not None:
            api_server.runner_dict.clear()


def get_cached_registry() -> CachedSkillRegistry:
    """Return active CachedSkillRegistry instance."""
    loader = get_skills_loader()
    return loader.cached_registry


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
    if "app" in globals():
        api_server = getattr(globals()["app"].state, "api_server", None)
        if api_server is not None:
            api_server.runner_dict.clear()


def get_agent_loader() -> AmbientAgentLoader:
    """Return active AmbientAgentLoader instance."""
    global _agent_loader
    if _agent_loader is None:
        _agent_loader = AmbientAgentLoader()
    return _agent_loader


def set_agent_loader(loader: AmbientAgentLoader | None) -> None:
    """Set or override active AmbientAgentLoader."""
    global _agent_loader
    _agent_loader = loader
    if "app" in globals():
        api_server = getattr(globals()["app"].state, "api_server", None)
        if api_server is not None:
            api_server.agent_loader = loader or get_agent_loader()
            api_server.runner_dict.clear()


def _find_api_server(fastapi_app: FastAPI) -> ApiServer | None:
    """Inspect FastAPI route closures to extract the underlying ADK ApiServer instance."""
    for route in fastapi_app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint and getattr(endpoint, "__closure__", None):
            for cell in endpoint.__closure__:
                if isinstance(cell.cell_contents, ApiServer):
                    return cell.cell_contents
    return None


def create_app() -> FastAPI:
    """Instantiate and configure the native ADK FastAPI application."""
    settings = get_settings()

    # Production safety startup guard prohibiting mock registry in production
    is_production = bool(os.getenv("K_SERVICE")) or (settings.project_id != "demo-project-id")
    if is_production and settings.mock_registry:
        raise RuntimeError(
            "Production safety guard: mock_registry must not be enabled in production "
            f"mode (project_id='{settings.project_id}', K_SERVICE='{os.getenv('K_SERVICE')}')."
        )

    loader = get_agent_loader()

    # Native ADK Runtime via get_fast_api_app providing POST /run, POST /run_sse, and GET /health
    fastapi_app = get_fast_api_app(
        agents_dir=".",
        agent_loader=loader,
        auto_create_session=True,
        web=False,
        use_local_storage=False,
    )

    # Locate underlying ApiServer and link to app state for dynamic cache management
    api_server = _find_api_server(fastapi_app)
    if api_server:
        api_server.default_app_name = "ambient_agent"
        fastapi_app.state.api_server = api_server
    else:
        logger.warning(
            "Could not extract ADK ApiServer instance from FastAPI routes. "
            "Runner cache eviction during hot reload may be impaired."
        )

    # Middleware to enrich /run responses with provenance metadata (skill, revision, MCP server)
    @fastapi_app.middleware("http")
    async def attach_provenance_middleware(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/run" and response.status_code == 200:
            try:
                body = b"".join([chunk async for chunk in response.body_iterator])
                payload = json.loads(body.decode("utf-8"))
                if isinstance(payload, list) and payload:
                    loader = get_agent_loader()
                    skill = getattr(loader, "last_resolved_skill", None)
                    mcp_url = getattr(loader.tool_registry_manager, "endpoint_url", "")
                    skill_meta = {}
                    skill_rev = "unknown"
                    skill_uri = ""
                    if skill is not None:
                        skill_uri = getattr(skill, "_uri", "") or ""
                        if "/revisions/" in skill_uri:
                            skill_rev = skill_uri.split("/revisions/")[-1].replace("rev-", "")
                        fm = getattr(skill, "frontmatter", None)
                        fm_name = fm.name if fm else ""
                        skill_name = getattr(skill, "name", "") or fm_name or settings.skill_name
                        skill_meta = {
                            "name": skill_name,
                            "revision": skill_rev,
                            "resource_name": skill_uri,
                        }

                    provenance = {
                        "skill": skill_meta,
                        "skill_revision": skill_rev,
                        "mcp_server": mcp_url,
                    }
                    for item in payload:
                        if isinstance(item, dict):
                            existing_meta = item.get("custom_metadata") or {}
                            existing_meta.update(provenance)
                            item["custom_metadata"] = existing_meta

                    new_body = json.dumps(payload).encode("utf-8")
                    headers = dict(response.headers)
                    headers.pop("content-length", None)
                    return Response(
                        content=new_body,
                        status_code=response.status_code,
                        headers=headers,
                        media_type="application/json",
                    )
            except Exception as exc:
                logger.warning("Failed to enrich /run response with provenance: %s", exc)
        return response

    # Custom administrative route for cache invalidation & hot-reload
    @fastapi_app.post(
        "/admin/cache/invalidate",
        response_model=CacheInvalidateResponse,
        status_code=status.HTTP_200_OK,
    )
    async def invalidate_cache(
        request: CacheInvalidateRequest = CacheInvalidateRequest(),
    ) -> CacheInvalidateResponse:
        """Atomically invalidate in-memory skill cache and evict cached agent runners."""
        active_loader = get_agent_loader()
        flushed = active_loader.skills_loader.invalidate(name=request.skill_name)

        # Evict cached runner to force fresh agent loading on next turn
        app_api_server = getattr(fastapi_app.state, "api_server", None)
        if app_api_server is not None:
            app_api_server.runner_dict.clear()

        return CacheInvalidateResponse(
            status="invalidated",
            flushed=flushed,
        )

    # Fail-fast error handlers for zero fallback guarantee
    @fastapi_app.exception_handler(RuntimeError)
    async def runtime_error_handler(request: Request, exc: RuntimeError) -> JSONResponse:
        exc_str = str(exc).lower()
        if "mcp" in exc_str or "tool" in exc_str or "connection" in exc_str:
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={"detail": f"Remote MCP service failure: {exc}"},
            )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": str(exc)},
        )

    @fastapi_app.exception_handler(httpx.HTTPError)
    async def httpx_error_handler(request: Request, exc: httpx.HTTPError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": f"Upstream service connection failure: {exc}"},
        )

    @fastapi_app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    return fastapi_app


app = create_app()
