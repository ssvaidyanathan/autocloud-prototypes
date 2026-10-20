"""Pytest configuration and shared fixtures for agent-registry-demo."""

import io
import zipfile
from collections.abc import Iterator
from typing import Callable

import pytest
from fastapi.testclient import TestClient

from app.main import app, set_chat_executor, set_skills_loader


def create_skill_zip(
    name: str = "calculator",
    description: str = "Precision arithmetic calculation skill.",
    instructions: str = (
        "# Calculator Skill\n\n"
        "Instructions:\n"
        "1. For arithmetic operations, you MUST call the registered calculator tools.\n"
        "2. Format output strictly as: 'Result: <value> (Computed via <tools_used>)'\n"
    ),
    allowed_tools: str = "add multiply",
    version: str = "1.0.0",
    references: dict[str, str] | None = None,
    assets: dict[str, str] | None = None,
    include_scripts: bool = False,
    script_files: dict[str, str] | None = None,
) -> bytes:
    """Build an in-memory ZIP archive matching the GCP Agent Registry skill package format."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        frontmatter = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"allowed-tools: {allowed_tools}\n"
            f"metadata:\n"
            f"  version: '{version}'\n"
            f"  adk_additional_tools:\n"
        )
        for tool in allowed_tools.split():
            frontmatter += f"    - {tool}\n"
        frontmatter += f"---\n{instructions}\n"

        z.writestr("SKILL.md", frontmatter)

        if references:
            for filename, content in references.items():
                z.writestr(f"references/{filename}", content)

        if assets:
            for filename, content in assets.items():
                z.writestr(f"assets/{filename}", content)

        if include_scripts:
            scripts = script_files or {"helper.sh": "#!/bin/bash\necho bad"}
            for filename, content in scripts.items():
                z.writestr(f"scripts/{filename}", content)

    return buf.getvalue()


@pytest.fixture
def mock_zip_builder() -> Callable[..., bytes]:
    """Factory fixture to create in-memory skill ZIP packages."""
    return create_skill_zip


@pytest.fixture
def valid_skill_zip() -> bytes:
    """Fixture providing a standard valid calculator skill ZIP package."""
    return create_skill_zip(
        name="calculator",
        description="Basic arithmetic calculation skill for addition and multiplication.",
        references={"arithmetic_rules.md": "# Arithmetic Rules\nPrecision is mandatory."},
    )


@pytest.fixture
def forbidden_scripts_zip() -> bytes:
    """Fixture providing a skill ZIP package containing a forbidden scripts/ directory."""
    return create_skill_zip(
        name="malicious-skill",
        description="A skill that illicitly bundles executable scripts.",
        include_scripts=True,
    )


@pytest.fixture
def test_client() -> Iterator[TestClient]:
    """Fixture providing a FastAPI TestClient."""
    # Reset global overrides
    set_skills_loader(None)
    set_chat_executor(None)
    client = TestClient(app)
    yield client
    set_skills_loader(None)
    set_chat_executor(None)
