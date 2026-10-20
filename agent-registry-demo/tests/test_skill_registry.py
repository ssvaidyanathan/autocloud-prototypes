"""Unit and integration tests for Agent Registry skill client and minimal chat."""

import httpx
import pytest
from fastapi.testclient import TestClient
from google.adk.skills import models
from google.adk.tools.skill_toolset import SkillToolset

from app.config import Settings
from app.main import set_skills_loader
from app.registry.client import GCPSkillRegistryClient
from app.skills_loader import SkillsLoader


@pytest.mark.asyncio
async def test_agent_registry_downloads_and_unpacks(valid_skill_zip: bytes) -> None:
    """Verify that Agent Registry client downloads and unpacks ZIP bytes directly in-memory."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/calculator"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": "projects/cloud-assist-fde-7/locations/us-central1/skills/calculator",
                    "defaultRevision": (
                        "projects/cloud-assist-fde-7/locations/us-central1/"
                        "skills/calculator/revisions/r1"
                    ),
                },
            )
        elif "revisions/r1" in url_str and "alt=media" in url_str:
            return httpx.Response(
                status_code=200,
                content=valid_skill_zip,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404, json={"error": "Not Found"})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = GCPSkillRegistryClient(
            project_id="cloud-assist-fde-7",
            location="us-central1",
            http_client=mock_http,
        )

        skill = await client.get_skill(name="calculator")

        # Assert valid ADK models.Skill instance
        assert isinstance(skill, models.Skill)
        assert skill.name == "calculator"
        assert skill.frontmatter.description == (
            "Basic arithmetic calculation skill for addition and multiplication."
        )
        assert skill.frontmatter.allowed_tools is not None
        assert "add" in skill.frontmatter.allowed_tools
        assert "multiply" in skill.frontmatter.allowed_tools
        assert "references" in skill.resources.__dict__
        assert "arithmetic_rules.md" in skill.resources.references
        assert "Precision is mandatory" in skill.resources.references["arithmetic_rules.md"]
        assert skill._uri is not None and skill._uri.endswith("/revisions/r1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_name",
    [
        "../path-traversal",
        "../../etc/passwd",
        "UPPERCASE_NAME",
        "invalid/segment",
        "skill with space",
        "-leading-dash",
        "trailing-dash-",
        "double--dash",
        "",
    ],
)
async def test_agent_registry_invalid_skill_name(invalid_name: str) -> None:
    """Verify safe path segment validation rejects malicious or malformed skill resource IDs."""
    client = GCPSkillRegistryClient(project_id="cloud-assist-fde-7", location="us-central1")
    with pytest.raises(ValueError, match="(Invalid skill name|Skill name must be)"):
        await client.get_skill(name=invalid_name)


@pytest.mark.asyncio
async def test_agent_registry_rejects_forbidden_scripts(forbidden_scripts_zip: bytes) -> None:
    """Security Guardrail: Skill packages containing a scripts/ directory MUST be rejected."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/bad-skill"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": "projects/cloud-assist-fde-7/locations/us-central1/skills/bad-skill",
                    "defaultRevision": (
                        "projects/cloud-assist-fde-7/locations/us-central1/"
                        "skills/bad-skill/revisions/r1"
                    ),
                },
            )
        elif "revisions/r1" in url_str:
            return httpx.Response(
                status_code=200,
                content=forbidden_scripts_zip,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404, json={"error": "Not Found"})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = GCPSkillRegistryClient(
            project_id="cloud-assist-fde-7",
            location="us-central1",
            http_client=mock_http,
        )

        with pytest.raises(ValueError, match="forbidden 'scripts/' directory"):
            await client.get_skill(name="bad-skill")


@pytest.mark.asyncio
async def test_skills_loader_provides_toolset_and_resolves_skills(
    valid_skill_zip: bytes,
) -> None:
    """Verify SkillsLoader wires GCPSkillRegistryClient and produces valid ADK SkillToolset."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/calculator"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": "projects/cloud-assist-fde-7/locations/us-central1/skills/calculator",
                    "defaultRevision": (
                        "projects/cloud-assist-fde-7/locations/us-central1/"
                        "skills/calculator/revisions/r1"
                    ),
                },
            )
        elif "revisions/r1" in url_str:
            return httpx.Response(
                status_code=200,
                content=valid_skill_zip,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        registry = GCPSkillRegistryClient(
            project_id="cloud-assist-fde-7",
            location="us-central1",
            http_client=mock_http,
        )
        loader = SkillsLoader(registry=registry)

        # 1. Resolve skill directly
        skill = await loader.resolve_skill("calculator")
        assert skill.name == "calculator"

        # 2. Provide ADK SkillToolset
        toolset = loader.get_skill_toolset()
        assert isinstance(toolset, SkillToolset)
        tools = await toolset.get_tools()
        tool_names = [t.name for t in tools]
        assert "load_skill" in tool_names


def test_health_endpoint(test_client: TestClient) -> None:
    """Verify GET /health endpoint returns 200 OK and healthy status."""
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_chat_endpoint_minimal(test_client: TestClient, valid_skill_zip: bytes) -> None:
    """Verify POST /chat executes chat turn with dynamically loaded skill instructions."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/calculator"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": "projects/cloud-assist-fde-7/locations/us-central1/skills/calculator",
                    "defaultRevision": (
                        "projects/cloud-assist-fde-7/locations/us-central1/"
                        "skills/calculator/revisions/r1"
                    ),
                },
            )
        elif "revisions/r1" in url_str:
            return httpx.Response(
                status_code=200,
                content=valid_skill_zip,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(mock_handler)
    mock_http = httpx.AsyncClient(transport=transport)
    registry = GCPSkillRegistryClient(
        project_id="cloud-assist-fde-7",
        location="us-central1",
        http_client=mock_http,
    )
    loader = SkillsLoader(registry=registry)
    set_skills_loader(loader)

    payload = {"prompt": "Compute 15 multiplied by 4, then add 10."}
    response = test_client.post("/chat", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["skill_revision"] == "r1"
    assert "Result: 70" in data["response"]
    assert "multiply" in data["tools_invoked"]
    assert "add" in data["tools_invoked"]


def test_config_settings_defaults() -> None:
    """Verify default Settings values match Phase 1 requirements."""
    settings = Settings()
    assert settings.project_id == "cloud-assist-fde-7"
    assert settings.region == "us-central1"
    assert settings.agent_registry_endpoint == "https://agentregistry.googleapis.com/v1alpha"
    assert settings.port == 8080
    assert settings.log_level == "INFO"


def test_chat_endpoint_skill_not_found(test_client: TestClient) -> None:
    """Verify POST /chat returns 500 when requested skill cannot be resolved."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=404, json={"error": "Skill not found"})

    transport = httpx.MockTransport(mock_handler)
    mock_http = httpx.AsyncClient(transport=transport)
    registry = GCPSkillRegistryClient(
        project_id="cloud-assist-fde-7",
        location="us-central1",
        http_client=mock_http,
    )
    loader = SkillsLoader(registry=registry)
    set_skills_loader(loader)

    response = test_client.post("/chat", json={"prompt": "Hello"})
    assert response.status_code == 500
    assert "Failed to load skill" in response.json()["detail"]


def test_boot_without_local_skills_dir() -> None:
    """Verify that the prototype boots cleanly without any local skills directory."""
    from pathlib import Path

    local_skills_dir = Path("skills")
    assert not local_skills_dir.exists(), "Local skills directory must not exist"
    # App and loader instantiate without error
    loader = SkillsLoader()
    assert loader.registry is not None

