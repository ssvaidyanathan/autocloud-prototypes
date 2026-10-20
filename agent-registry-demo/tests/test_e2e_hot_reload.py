"""End-to-End Integration Tests for Live Hot-Reload, Tool Guardrails & Demo Tooling."""

from __future__ import annotations

import io
import os
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import set_skills_loader
from app.registry.client import GCPSkillRegistryClient
from app.skills_loader import SkillsLoader

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_PACKAGES_DIR = REPO_ROOT / "demo" / "registry_packages"
SCRIPTS_DIR = REPO_ROOT / "scripts"


def package_directory_to_zip(source_dir: Path) -> bytes:
    """Read a skill package directory from disk and package into an in-memory ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(source_dir):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, source_dir)
                zf.write(full_path, rel_path)
    return buf.getvalue()


# ==============================================================================
# 1. Package Integrity & Manifest Tests
# ==============================================================================


def test_registry_packages_structure_and_no_scripts() -> None:
    """Verify that demo/registry_packages v1 and v2 exist and contain zero scripts directories."""
    v1_dir = DEMO_PACKAGES_DIR / "v1" / "calculator"
    v2_dir = DEMO_PACKAGES_DIR / "v2" / "calculator"

    assert v1_dir.exists(), f"v1 directory not found: {v1_dir}"
    assert v2_dir.exists(), f"v2 directory not found: {v2_dir}"

    assert (v1_dir / "SKILL.md").is_file(), "v1 SKILL.md missing"
    assert (v2_dir / "SKILL.md").is_file(), "v2 SKILL.md missing"

    # Strict security validation: zero executable scripts allowed
    assert not (v1_dir / "scripts").exists(), "Security violation: scripts directory found in v1"
    assert not (v2_dir / "scripts").exists(), "Security violation: scripts directory found in v2"

    # Validate v1 content
    v1_content = (v1_dir / "SKILL.md").read_text()
    assert "name: calculator" in v1_content
    assert "allowed-tools: add multiply" in v1_content
    assert "adk_additional_tools:" in v1_content
    assert "Result: <value> (Computed via <tools_used>)" in v1_content

    # Validate v2 content
    v2_content = (v2_dir / "SKILL.md").read_text()
    assert "name: calculator" in v2_content
    assert "allowed-tools: add multiply power" in v2_content
    assert "🧮 [CALCULATOR-V2] Computed value: **<value>**" in v2_content


# ==============================================================================
# 2. End-to-End Live Hot-Reload Scenario Test
# ==============================================================================


def test_e2e_hot_reload_lifecycle_with_real_packages(test_client: TestClient) -> None:
    """Test full hot reload sequence using real packages from demo/registry_packages.

    Scenario:
      1. Boot with v1 package (Revision r1).
      2. Act 1: Arithmetic query -> verifies multiply & add invoked with Revision 1 formatting.
      3. Live Registry Mutation: Revision r2 published, /admin/cache/invalidate called.
      4. Act 2: Exponentiation query -> verifies power tool invoked with Revision 2 formatting.
      5. Verifies continuous live operation without process restart.
    """
    v1_dir = DEMO_PACKAGES_DIR / "v1" / "calculator"
    v2_dir = DEMO_PACKAGES_DIR / "v2" / "calculator"

    v1_zip = package_directory_to_zip(v1_dir)
    v2_zip = package_directory_to_zip(v2_dir)

    active_revision = "r1"

    def mock_registry_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/calculator") or url_str.endswith("/skills/private-calculator"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/private-calculator"
                    ),
                    "defaultRevision": (
                        f"projects/demo-project-id/locations/us-central1/"
                        f"skills/private-calculator/revisions/{active_revision}"
                    ),
                },
            )
        elif f"revisions/{active_revision}" in url_str and "alt=media" in url_str:
            content = v1_zip if active_revision == "r1" else v2_zip
            return httpx.Response(
                status_code=200,
                content=content,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404, json={"error": "Not Found"})

    transport = httpx.MockTransport(mock_registry_handler)
    mock_http = httpx.AsyncClient(transport=transport)
    base_registry = GCPSkillRegistryClient(
        project_id="demo-project-id",
        location="us-central1",
        http_client=mock_http,
    )
    loader = SkillsLoader(registry=base_registry, cache_ttl=60.0)
    set_skills_loader(loader)

    # Health check verification
    health_res = test_client.get("/health")
    assert health_res.status_code == 200
    assert health_res.json() == {"status": "healthy"}

    # Act 1: Query under Revision 1
    act1_res = test_client.post(
        "/chat",
        json={"prompt": "Compute 15 multiplied by 4, then add 10."},
    )
    assert act1_res.status_code == 200
    act1_data = act1_res.json()
    assert act1_data["skill_revision"] == "r1"
    assert act1_data["response"] == "Result: 70 (Computed via multiply, add)"
    assert set(t["tool"] for t in act1_data["tools_invoked"]) == {"multiply", "add"}
    assert act1_data["skill"]["name"] == "calculator"
    assert act1_data["skill"]["revision"] == "r1"
    assert "revisions/r1" in act1_data["skill"]["resource_name"]

    # Live Registry Mutation: Promote r2 and flush cache
    active_revision = "r2"
    inv_res = test_client.post("/admin/cache/invalidate", json={"skill_name": "calculator"})
    assert inv_res.status_code == 200
    assert inv_res.json()["status"] == "invalidated"
    assert any("calculator" in s for s in inv_res.json()["flushed"])

    # Act 2: Immediate verification query under Revision 2
    act2_res = test_client.post(
        "/chat",
        json={"prompt": "Compute 2 to the power of 8."},
    )
    assert act2_res.status_code == 200
    act2_data = act2_res.json()
    assert act2_data["skill_revision"] == "r2"
    assert act2_data["response"] == "🧮 [CALCULATOR-V2] Computed value: **256**"
    assert [t["tool"] for t in act2_data["tools_invoked"]] == ["power"]
    assert act2_data["tools_invoked"][0]["args"] == {"base": 2.0, "exponent": 8.0}
    assert act2_data["tools_invoked"][0]["result"] == 256.0 or act2_data["tools_invoked"][0][
        "result"
    ] == {"result": 256.0}

    # Continuous live check: container never restarted
    final_health = test_client.get("/health")
    assert final_health.status_code == 200
    assert final_health.json() == {"status": "healthy"}


# ==============================================================================
# 3. Security Guardrails & Tool Access Control Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_security_guardrail_rejects_malicious_scripts_dir() -> None:
    """Verify that SkillsLoader rejects any skill package bundling a scripts/ directory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "SKILL.md",
            "---\nname: malicious\ndescription: Bad\nallowed-tools: add\n---\n# Bad Skill",
        )
        zf.writestr("scripts/exploit.sh", "#!/bin/bash\necho pwned")
    malicious_zip = buf.getvalue()

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/malicious"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": "projects/demo-project-id/locations/us-central1/skills/malicious",
                    "defaultRevision": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/malicious/revisions/r1"
                    ),
                },
            )
        elif "revisions/r1" in url_str and "alt=media" in url_str:
            return httpx.Response(
                status_code=200,
                content=malicious_zip,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404, json={"error": "Not Found"})

    transport = httpx.MockTransport(mock_handler)
    mock_http = httpx.AsyncClient(transport=transport)
    base_registry = GCPSkillRegistryClient(
        project_id="demo-project-id",
        location="us-central1",
        http_client=mock_http,
    )
    loader = SkillsLoader(registry=base_registry, cache_ttl=60.0)

    with pytest.raises(ValueError, match="Security violation.*forbidden 'scripts/' directory"):
        await loader.resolve_skill("malicious")


def test_tool_guardrail_blocks_undeclared_power_tool_in_v1(test_client: TestClient) -> None:
    """Verify that in Revision 1, attempting to execute the undeclared 'power' tool is blocked."""
    v1_dir = DEMO_PACKAGES_DIR / "v1" / "calculator"
    v1_zip = package_directory_to_zip(v1_dir)

    def mock_registry_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/calculator") or url_str.endswith("/skills/private-calculator"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/private-calculator"
                    ),
                    "defaultRevision": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/private-calculator/revisions/r1"
                    ),
                },
            )
        elif "revisions/r1" in url_str and "alt=media" in url_str:
            return httpx.Response(
                status_code=200,
                content=v1_zip,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404, json={"error": "Not Found"})

    transport = httpx.MockTransport(mock_registry_handler)
    mock_http = httpx.AsyncClient(transport=transport)
    base_registry = GCPSkillRegistryClient(
        project_id="demo-project-id",
        location="us-central1",
        http_client=mock_http,
    )
    loader = SkillsLoader(registry=base_registry, cache_ttl=60.0)
    set_skills_loader(loader)

    # Exponentiation prompt requires 'power', which is not in Revision 1 metadata
    response = test_client.post(
        "/chat",
        json={"prompt": "Compute 2 to the power of 8."},
    )
    assert response.status_code == 400
    assert "Tool 'power' is not declared in adk_additional_tools" in response.json()["detail"]


# ==============================================================================
# 4. Automation Scripts CLI Execution Tests
# ==============================================================================


def test_publish_skill_script_dry_run() -> None:
    """Test publish_skill.sh execution in --dry-run mode."""
    script_path = SCRIPTS_DIR / "publish_skill.sh"
    assert script_path.exists(), f"publish_skill.sh not found at {script_path}"

    result = subprocess.run(
        [
            str(script_path),
            "--dry-run",
            "--project",
            "test-proj",
            "--location",
            "us-central1",
            "calculator",
            "r1",
            str(DEMO_PACKAGES_DIR / "v1" / "calculator"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"Script failed: {result.stderr}"
    assert (
        "[dry-run] Validation passed: zero forbidden 'scripts/' directories detected."
        in result.stdout
    )
    assert "gcloud alpha agent-registry skills revisions create r1" in result.stdout
    assert "gcloud alpha agent-registry skills update calculator" in result.stdout


def test_publish_skill_script_rejects_scripts_dir(tmp_path: Path) -> None:
    """Test publish_skill.sh security guardrail rejecting scripts directory."""
    script_path = SCRIPTS_DIR / "publish_skill.sh"

    # Create dummy skill package with a forbidden scripts directory
    skill_dir = tmp_path / "bad_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: bad\n---\n# Bad")
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "test.sh").write_text("#!/bin/bash\necho bad")

    result = subprocess.run(
        [
            str(script_path),
            "--dry-run",
            "bad_skill",
            "r1",
            str(skill_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Security violation" in result.stderr
    assert "forbidden 'scripts/' directory" in result.stderr


def test_run_demo_script_dry_run() -> None:
    """Test run_demo.sh execution in --dry-run mode."""
    script_path = SCRIPTS_DIR / "run_demo.sh"
    assert script_path.exists(), f"run_demo.sh not found at {script_path}"

    result = subprocess.run(
        [str(script_path), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"Script failed: {result.stderr}"
    assert "AGENT REGISTRY HOT-RELOAD DEMO (DRY-RUN MODE)" in result.stdout
    assert "Act 1 - Query Under Skill Revision 1" in result.stdout
    assert "Live Registry Mutation & In-Memory Cache Invalidation" in result.stdout
    assert "Act 2 - Immediate Verification Query Under Revision 2" in result.stdout
    assert "DRY-RUN VALIDATION COMPLETE: ALL SCENARIOS VERIFIED" in result.stdout


def test_run_demo_script_mock_mode() -> None:
    """Test run_demo.sh execution in --mock-mode."""
    script_path = SCRIPTS_DIR / "run_demo.sh"
    assert script_path.exists(), f"run_demo.sh not found at {script_path}"

    result = subprocess.run(
        [str(script_path), "--mock-mode"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PROJECT_ID": "demo-project-id"},
    )

    assert result.returncode == 0, f"Script failed: {result.stderr}\nOutput: {result.stdout}"
    assert "STARTING MOCK AGENT SERVER FOR OFFLINE DEMO" in result.stdout
    assert "Act 1: Initial Query Under Skill Revision 1" in result.stdout
    assert "Active skill revision verified as 'r1'" in result.stdout
    assert "Live Registry Mutation & Instant Invalidation" in result.stdout
    assert "Act 2: Immediate Verification Query Under Revision 2" in result.stdout
    assert "Active skill revision verified as 'r2'" in result.stdout
    assert "Zero Container Restarts & Continuous Liveness Verification" in result.stdout
    assert "HOT-RELOAD DEMONSTRATION SUMMARY: ALL ASSERTIONS PASSED" in result.stdout
