"""Unit and integration tests for Agent Registry skill client and configuration."""

from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from google.adk.skills import models
from google.adk.tools.skill_toolset import SkillToolset

from app.config import Settings
from app.registry.client import GCPSkillRegistryClient
from app.skills_loader import SkillsLoader


@pytest.mark.asyncio
async def test_agent_registry_downloads_and_unpacks(valid_skill_zip: bytes) -> None:
    """Verify that Agent Registry client downloads and unpacks ZIP bytes directly in-memory."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/ambient-privilege-audit-skill"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/ambient-privilege-audit-skill"
                    ),
                    "defaultRevision": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/ambient-privilege-audit-skill/revisions/r1"
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
            project_id="demo-project-id",
            location="us-central1",
            http_client=mock_http,
        )

        skill = await client.get_skill(name="ambient-privilege-audit-skill")

        assert isinstance(skill, models.Skill)
        assert skill.name == "ambient-privilege-audit-skill"
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
    client = GCPSkillRegistryClient(project_id="demo-project-id", location="us-central1")
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
                    "name": "projects/demo-project-id/locations/us-central1/skills/bad-skill",
                    "defaultRevision": (
                        "projects/demo-project-id/locations/us-central1/"
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
            project_id="demo-project-id",
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
        if url_str.endswith("/skills/ambient-privilege-audit-skill"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/ambient-privilege-audit-skill"
                    ),
                    "defaultRevision": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/ambient-privilege-audit-skill/revisions/r1"
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
            project_id="demo-project-id",
            location="us-central1",
            http_client=mock_http,
        )
        loader = SkillsLoader(registry=registry)

        # 1. Resolve skill directly
        skill = await loader.resolve_skill("ambient-privilege-audit-skill")
        assert skill.name == "ambient-privilege-audit-skill"

        # 2. Provide ADK SkillToolset
        toolset = loader.get_skill_toolset()
        assert isinstance(toolset, SkillToolset)
        tools = await toolset.get_tools()
        tool_names = [t.name for t in tools]
        assert "load_skill" in tool_names


def test_health_endpoint(test_client: TestClient) -> None:
    """Verify GET /health endpoint returns 200 OK."""
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_config_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify default Settings values match requirements when env is unset."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    monkeypatch.delenv("REGION", raising=False)
    monkeypatch.delenv("MOCK_REGISTRY", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)

    settings = Settings()
    assert settings.project_id == "demo-project-id"
    assert settings.region == "us-central1"
    assert settings.agent_registry_endpoint == "https://agentregistry.googleapis.com/v1alpha"
    assert settings.port == 8080
    assert settings.log_level == "INFO"
    assert settings.service_name == "ambient-agent-service"
    assert settings.skill_name == "ambient-privilege-audit-skill"
    assert settings.calculator_mcp_server == "ambient-calculator"
    assert settings.mock_registry is False


def test_production_safety_guard_blocks_mock_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify application Settings refuses to instantiate mock_registry in production."""
    # Production project ID
    with pytest.raises(RuntimeError) as exc_info:
        Settings(project_id="real-production-project", mock_registry=True)
    assert "production safety guard" in str(exc_info.value).lower()

    # Cloud Run K_SERVICE set
    monkeypatch.setenv("K_SERVICE", "ambient-agent-service")
    with pytest.raises(RuntimeError) as exc_info2:
        Settings(project_id="demo-project-id", mock_registry=True)
    assert "production safety guard" in str(exc_info2.value).lower()


def test_config_settings_standard_gcp_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Settings recognizes standard GCP env vars GOOGLE_CLOUD_PROJECT/LOCATION."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "custom-gcp-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west1")
    settings = Settings()
    assert settings.project_id == "custom-gcp-project"
    assert settings.region == "europe-west1"


def test_config_settings_ambient_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Settings respects AMBIENT_ prefixed environment variables."""
    monkeypatch.setenv("AMBIENT_PROJECT_ID", "custom-ambient-project")
    monkeypatch.setenv("AMBIENT_REGION", "europe-west4")
    monkeypatch.setenv("AMBIENT_SKILL_LOCATION", "us-east1")
    monkeypatch.setenv("AMBIENT_PORT", "9090")
    monkeypatch.setenv("AMBIENT_SERVICE_NAME", "my-ambient-service")
    monkeypatch.setenv("AMBIENT_SKILL_NAME", "custom-skill")
    monkeypatch.setenv("AMBIENT_MODEL_NAME", "gemini-2.5-pro")
    monkeypatch.setenv("AMBIENT_CALCULATOR_SERVICE_URL", "https://calculator.internal/mcp")
    monkeypatch.setenv("AMBIENT_CALCULATOR_MCP_SERVER", "my-calculator")
    monkeypatch.setenv("AMBIENT_MOCK_REGISTRY", "false")

    settings = Settings()
    assert settings.project_id == "custom-ambient-project"
    assert settings.region == "europe-west4"
    assert settings.skill_location == "us-east1"
    assert settings.port == 9090
    assert settings.service_name == "my-ambient-service"
    assert settings.skill_name == "custom-skill"
    assert settings.model_name == "gemini-2.5-pro"
    assert settings.calculator_service_url == "https://calculator.internal/mcp"
    assert settings.calculator_mcp_server == "my-calculator"
    assert settings.mock_registry is False


@pytest.mark.asyncio
async def test_auth_headers_production_raises_and_test_fallback() -> None:
    """Verify production client raises on missing ADC, while test client uses fallback."""
    # 1. Production client (no http_client) re-raises RuntimeError
    prod_client = GCPSkillRegistryClient(project_id="p", location="l")
    with patch(
        "google.adk.integrations.skill_registry.gcp_skill_registry.GCPSkillRegistry._get_headers",
        side_effect=RuntimeError("ADC not found"),
    ):
        with pytest.raises(RuntimeError, match="ADC not found"):
            await prod_client._get_headers()

    # 2. Test client (with http_client) provides offline fallback
    async with httpx.AsyncClient() as mock_http:
        test_client = GCPSkillRegistryClient(project_id="p", location="l", http_client=mock_http)
        with patch(
            "google.adk.integrations.skill_registry.gcp_skill_registry.GCPSkillRegistry._get_headers",
            side_effect=RuntimeError("ADC not found"),
        ):
            headers = await test_client._get_headers()
            assert headers["Authorization"] == "Bearer mock-agent-registry-token"


@pytest.mark.asyncio
async def test_agent_registry_rejects_forbidden_scripts_windows_path() -> None:
    """Verify zip archive path separator normalization detects backslash scripts entries."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "SKILL.md",
            "---\nname: win-skill\ndescription: Windows path test\n---\ninstructions",
        )
        z.writestr("scripts\\nested\\run.bat", "echo bad")
    zip_bytes = buf.getvalue()

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/win-skill"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": "projects/p/locations/l/skills/win-skill",
                    "defaultRevision": "projects/p/locations/l/skills/win-skill/revisions/r1",
                },
            )
        elif "revisions/r1" in url_str:
            return httpx.Response(
                status_code=200,
                content=zip_bytes,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = GCPSkillRegistryClient(project_id="p", location="l", http_client=mock_http)
        with pytest.raises(ValueError, match="forbidden 'scripts/' directory"):
            await client.get_skill("win-skill")


def test_boot_without_local_skills_dir() -> None:
    """Verify that the prototype boots cleanly without any local skills directory."""
    from pathlib import Path

    local_skills_dir = Path("skills")
    assert not local_skills_dir.exists(), "Local skills directory must not exist"
    loader = SkillsLoader()
    assert loader.registry is not None
