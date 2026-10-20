"""Unit and integration tests for the FastMCP Calculator Tool Service.

Covers:
- GET /health liveness and readiness probe
- Direct tool operations (add, multiply, power)
- FastMCP JSON-RPC /mcp protocol handling:
  - tools/list endpoint returning add, multiply, power
  - tools/call endpoint for add, multiply, power
- Authentication enforcement and OIDC token validation
- Registration script dry-run validation
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from demo.calculator_service.server import (
    add,
    app,
    create_app,
    multiply,
    power,
)


@pytest.fixture
def unauthenticated_client():
    """Client for default unauthenticated application instance."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def auth_enforced_client():
    """Client for application with authentication enforcement enabled."""
    auth_app = create_app(enforce_auth=True, verify_oidc=False)
    with TestClient(auth_app) as client:
        yield client


# ==============================================================================
# 1. Health Probe Tests
# ==============================================================================


def test_health_check_returns_200(unauthenticated_client: TestClient) -> None:
    """Test that GET /health returns HTTP 200 OK with healthy status."""
    response = unauthenticated_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_health_check_bypasses_auth(auth_enforced_client: TestClient) -> None:
    """Test that GET /health succeeds without authentication even when auth is enforced."""
    response = auth_enforced_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_health_check_trailing_slash_bypasses_auth(auth_enforced_client: TestClient) -> None:
    """Test that GET /health/ succeeds without authentication when auth is enforced."""
    response = auth_enforced_client.get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}



# ==============================================================================
# 2. Direct Function Execution Tests
# ==============================================================================


def test_direct_add() -> None:
    """Test direct execution of add function."""
    assert add(10.5, 2.5) == 13.0
    assert add(-5.0, 5.0) == 0.0
    assert add(0.0, 0.0) == 0.0


def test_direct_multiply() -> None:
    """Test direct execution of multiply function."""
    assert multiply(6.0, 7.0) == 42.0
    assert multiply(-4.0, 5.0) == -20.0
    assert multiply(10.0, 0.0) == 0.0


def test_direct_power() -> None:
    """Test direct execution of power function."""
    assert power(2.0, 8.0) == 256.0
    assert power(3.0, 3.0) == 27.0
    assert power(5.0, 0.0) == 1.0
    assert power(4.0, 0.5) == 2.0


def test_direct_power_boundary_conditions() -> None:
    """Test that power raises ValueError on division-by-zero or complex results."""
    with pytest.raises(
        ValueError, match="Division by zero: zero cannot be raised to a negative exponent"
    ):
        power(0.0, -1.0)

    with pytest.raises(
        ValueError,
        match="Fractional exponent of a negative base produces a complex number",
    ):
        power(-4.0, 0.5)

    # Valid negative base with integer exponent succeeds
    assert power(-2.0, 3.0) == -8.0
    assert power(-4.0, 2.0) == 16.0



# ==============================================================================
# 3. FastMCP JSON-RPC /mcp Endpoint Tests
# ==============================================================================


def test_mcp_tools_list(unauthenticated_client: TestClient) -> None:
    """Test FastMCP /mcp handles tools/list returning add, multiply, and power."""
    payload = {
        "jsonrpc": "2.0",
        "id": "list-1",
        "method": "tools/list",
        "params": {},
    }
    response = unauthenticated_client.post("/mcp", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data.get("jsonrpc") == "2.0"
    assert data.get("id") == "list-1"
    assert "result" in data
    assert "tools" in data["result"]

    tools = data["result"]["tools"]
    tool_names = [tool["name"] for tool in tools]
    assert "add" in tool_names
    assert "multiply" in tool_names
    assert "power" in tool_names

    # Verify input schemas
    tool_map = {tool["name"]: tool for tool in tools}
    assert "a" in tool_map["add"]["inputSchema"]["properties"]
    assert "b" in tool_map["add"]["inputSchema"]["properties"]
    assert "a" in tool_map["multiply"]["inputSchema"]["properties"]
    assert "b" in tool_map["multiply"]["inputSchema"]["properties"]
    assert "base" in tool_map["power"]["inputSchema"]["properties"]
    assert "exponent" in tool_map["power"]["inputSchema"]["properties"]


def test_mcp_tools_call_add(unauthenticated_client: TestClient) -> None:
    """Test FastMCP /mcp tools/call executes add tool accurately."""
    payload = {
        "jsonrpc": "2.0",
        "id": "call-add-1",
        "method": "tools/call",
        "params": {
            "name": "add",
            "arguments": {"a": 15.5, "b": 26.5},
        },
    }
    response = unauthenticated_client.post("/mcp", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data.get("jsonrpc") == "2.0"
    assert data.get("id") == "call-add-1"
    assert "result" in data
    result = data["result"]
    assert result.get("isError") is False
    assert result["structuredContent"]["result"] == 42.0
    assert result["content"][0]["text"] == "42.0"


def test_mcp_tools_call_multiply(unauthenticated_client: TestClient) -> None:
    """Test FastMCP /mcp tools/call executes multiply tool accurately."""
    payload = {
        "jsonrpc": "2.0",
        "id": "call-mul-1",
        "method": "tools/call",
        "params": {
            "name": "multiply",
            "arguments": {"a": 7.0, "b": 6.0},
        },
    }
    response = unauthenticated_client.post("/mcp", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data.get("jsonrpc") == "2.0"
    assert data.get("id") == "call-mul-1"
    assert "result" in data
    result = data["result"]
    assert result.get("isError") is False
    assert result["structuredContent"]["result"] == 42.0
    assert result["content"][0]["text"] == "42.0"


def test_mcp_tools_call_power(unauthenticated_client: TestClient) -> None:
    """Test FastMCP /mcp tools/call executes power tool accurately."""
    payload = {
        "jsonrpc": "2.0",
        "id": "call-pow-1",
        "method": "tools/call",
        "params": {
            "name": "power",
            "arguments": {"base": 2.0, "exponent": 10.0},
        },
    }
    response = unauthenticated_client.post("/mcp", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data.get("jsonrpc") == "2.0"
    assert data.get("id") == "call-pow-1"
    assert "result" in data
    result = data["result"]
    assert result.get("isError") is False
    assert result["structuredContent"]["result"] == 1024.0
    assert result["content"][0]["text"] == "1024.0"


def test_mcp_tools_call_unknown_tool(unauthenticated_client: TestClient) -> None:
    """Test FastMCP /mcp handles unknown tool gracefully."""
    payload = {
        "jsonrpc": "2.0",
        "id": "call-unknown",
        "method": "tools/call",
        "params": {
            "name": "divide",
            "arguments": {"a": 10.0, "b": 2.0},
        },
    }
    response = unauthenticated_client.post("/mcp", json=payload)
    assert response.status_code == 200
    data = response.json()
    # FastMCP returns an error or tool result with isError=True
    assert "error" in data or data.get("result", {}).get("isError") is True


def test_mcp_tools_call_power_boundary_errors(unauthenticated_client: TestClient) -> None:
    """Test that FastMCP /mcp returns tool error on power boundary violations."""
    # Zero to negative power
    payload_zero = {
        "jsonrpc": "2.0",
        "id": "pow-zero-neg",
        "method": "tools/call",
        "params": {"name": "power", "arguments": {"base": 0.0, "exponent": -2.0}},
    }
    response_zero = unauthenticated_client.post("/mcp", json=payload_zero)
    assert response_zero.status_code == 200
    res_zero = response_zero.json().get("result", {})
    assert res_zero.get("isError") is True
    assert "Division by zero" in res_zero["content"][0]["text"]

    # Negative base with fractional exponent
    payload_complex = {
        "jsonrpc": "2.0",
        "id": "pow-complex",
        "method": "tools/call",
        "params": {"name": "power", "arguments": {"base": -9.0, "exponent": 0.5}},
    }
    response_complex = unauthenticated_client.post("/mcp", json=payload_complex)
    assert response_complex.status_code == 200
    res_complex = response_complex.json().get("result", {})
    assert res_complex.get("isError") is True
    assert "Fractional exponent of a negative base" in res_complex["content"][0]["text"]



# ==============================================================================
# 4. Authentication Enforcement Tests
# ==============================================================================


def test_auth_rejected_when_token_missing(auth_enforced_client: TestClient) -> None:
    """Test that requests to /mcp without Authorization header are rejected with 401."""
    payload = {
        "jsonrpc": "2.0",
        "id": "auth-1",
        "method": "tools/list",
        "params": {},
    }
    response = auth_enforced_client.post("/mcp", json=payload)
    assert response.status_code == 401
    assert response.json()["error"] == "Unauthorized"
    assert "Missing or invalid Authorization header" in response.json()["detail"]


def test_auth_rejected_when_token_malformed(auth_enforced_client: TestClient) -> None:
    """Test that requests with non-Bearer or empty tokens are rejected with 401."""
    payload = {
        "jsonrpc": "2.0",
        "id": "auth-2",
        "method": "tools/list",
        "params": {},
    }

    # Non-bearer scheme
    res_basic = auth_enforced_client.post(
        "/mcp",
        json=payload,
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert res_basic.status_code == 401

    # Empty token
    res_empty = auth_enforced_client.post(
        "/mcp",
        json=payload,
        headers={"Authorization": "Bearer    "},
    )
    assert res_empty.status_code == 401


def test_auth_accepted_with_valid_bearer_token(auth_enforced_client: TestClient) -> None:
    """Test that requests with valid Bearer token succeed when auth is enforced."""
    payload = {
        "jsonrpc": "2.0",
        "id": "auth-3",
        "method": "tools/list",
        "params": {},
    }
    response = auth_enforced_client.post(
        "/mcp",
        json=payload,
        headers={"Authorization": "Bearer valid-test-token-123"},
    )
    assert response.status_code == 200
    assert "tools" in response.json()["result"]


def test_auth_with_oidc_verification_success() -> None:
    """Test OIDC verification when valid Google ID token claims are returned."""
    mock_claims: dict[str, Any] = {
        "email": "agent-sa@cloud-assist-fde-7.iam.gserviceaccount.com",
        "aud": "https://calculator-tool-service-uc.a.run.app",
    }

    oidc_app = create_app(
        enforce_auth=True,
        verify_oidc=True,
        expected_audience="https://calculator-tool-service-uc.a.run.app",
        allowed_service_accounts=["agent-sa@cloud-assist-fde-7.iam.gserviceaccount.com"],
    )

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=mock_claims):
        with TestClient(oidc_app) as client:
            payload = {
                "jsonrpc": "2.0",
                "id": "oidc-1",
                "method": "tools/list",
                "params": {},
            }
            response = client.post(
                "/mcp",
                json=payload,
                headers={"Authorization": "Bearer mock-google-id-token"},
            )
            assert response.status_code == 200


def test_auth_with_oidc_verification_unauthorized_service_account() -> None:
    """Test OIDC rejection when token email does not match allowed service accounts."""
    mock_claims: dict[str, Any] = {
        "email": "unauthorized-sa@evil.iam.gserviceaccount.com",
        "aud": "https://calculator-tool-service-uc.a.run.app",
    }

    oidc_app = create_app(
        enforce_auth=True,
        verify_oidc=True,
        expected_audience="https://calculator-tool-service-uc.a.run.app",
        allowed_service_accounts=["agent-sa@cloud-assist-fde-7.iam.gserviceaccount.com"],
    )

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=mock_claims):
        with TestClient(oidc_app) as client:
            payload = {
                "jsonrpc": "2.0",
                "id": "oidc-2",
                "method": "tools/list",
                "params": {},
            }
            response = client.post(
                "/mcp",
                json=payload,
                headers={"Authorization": "Bearer mock-google-id-token"},
            )
            assert response.status_code == 403
            assert response.json()["error"] == "Forbidden"


def test_auth_with_oidc_verification_invalid_token() -> None:
    """Test OIDC rejection when verify_oauth2_token raises an error."""
    oidc_app = create_app(
        enforce_auth=True,
        verify_oidc=True,
        expected_audience="https://calculator-tool-service-uc.a.run.app",
    )

    with patch(
        "google.oauth2.id_token.verify_oauth2_token",
        side_effect=ValueError("Token expired"),
    ):
        with TestClient(oidc_app) as client:
            payload = {
                "jsonrpc": "2.0",
                "id": "oidc-3",
                "method": "tools/list",
                "params": {},
            }
            response = client.post(
                "/mcp",
                json=payload,
                headers={"Authorization": "Bearer expired-token"},
            )
            assert response.status_code == 401
            assert response.json()["error"] == "Unauthorized"
            assert "Token verification failed" in response.json()["detail"]


def test_auth_oidc_uses_pooled_auth_request() -> None:
    """Test that AuthMiddleware reuses a single GoogleAuthRequest instance."""
    from google.auth.transport.requests import Request as GoogleAuthRequest

    oidc_app = create_app(
        enforce_auth=True,
        verify_oidc=True,
        expected_audience="https://calculator-tool-service-uc.a.run.app",
        allowed_service_accounts=["agent-sa@cloud-assist-fde-7.iam.gserviceaccount.com"],
    )

    mock_claims: dict[str, Any] = {
        "email": "agent-sa@cloud-assist-fde-7.iam.gserviceaccount.com",
        "aud": "https://calculator-tool-service-uc.a.run.app",
    }

    with patch(
        "google.oauth2.id_token.verify_oauth2_token", return_value=mock_claims
    ) as mock_verify:
        with TestClient(oidc_app) as client:
            payload = {
                "jsonrpc": "2.0",
                "id": "pool-1",
                "method": "tools/list",
                "params": {},
            }
            res = client.post(
                "/mcp",
                json=payload,
                headers={"Authorization": "Bearer mock-google-id-token"},
            )
            assert res.status_code == 200
            assert mock_verify.called
            # Verify the request adapter passed was an instance of GoogleAuthRequest
            passed_request_adapter = mock_verify.call_args[0][1]
            assert isinstance(passed_request_adapter, GoogleAuthRequest)



# ==============================================================================
# 5. Registration Script Dry-Run Tests
# ==============================================================================


def test_register_tool_script_dry_run() -> None:
    """Test that scripts/register_tool.sh --dry-run prints correct gcloud syntax."""
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "register_tool.sh"
    assert script_path.exists(), f"Registration script not found at {script_path}"

    result = subprocess.run(
        [
            str(script_path),
            "--dry-run",
            "calculator",
            "https://calculator-tool-service-12345-uc.a.run.app",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"Script failed with output: {result.stderr}"
    stdout = result.stdout
    assert "gcloud agent-registry services create calculator" in stdout
    assert '--project="cloud-assist-fde-7"' in stdout
    assert '--location="us-central1"' in stdout
    assert '--display-name="Precision Calculator Service"' in stdout
    assert (
        '--description="Provides verified arithmetic operations (add, multiply, power)"'
        in stdout
    )
    assert "--mcp-server-spec-type=no-spec" in stdout
    expected_iface = (
        '--interfaces="protocolBinding=jsonrpc,'
        'url=https://calculator-tool-service-12345-uc.a.run.app/mcp"'
    )
    assert expected_iface in stdout
    assert "gcloud agent-registry services update calculator" in stdout


def test_register_tool_script_space_separated_flags() -> None:
    """Test that register_tool.sh correctly parses space-separated flags."""
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "register_tool.sh"
    assert script_path.exists(), f"Registration script not found at {script_path}"

    result = subprocess.run(
        [
            str(script_path),
            "--dry-run",
            "--project",
            "custom-project-123",
            "--location",
            "europe-west1",
            "--tool-url",
            "https://calc-custom.a.run.app",
            "custom-calc",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"Script failed with output: {result.stderr}"
    stdout = result.stdout
    assert "gcloud agent-registry services create custom-calc" in stdout
    assert '--project="custom-project-123"' in stdout
    assert '--location="europe-west1"' in stdout
    expected_iface = (
        '--interfaces="protocolBinding=jsonrpc,'
        'url=https://calc-custom.a.run.app/mcp"'
    )
    assert expected_iface in stdout


# ==============================================================================
# 6. Dockerfile & Packaging Manifest Validation Tests
# ==============================================================================


def test_dockerfile_copies_source_files() -> None:
    """Test that calculator_service Dockerfile copies source code in builder stage."""
    dockerfile_path = (
        Path(__file__).resolve().parent.parent
        / "demo"
        / "calculator_service"
        / "Dockerfile"
    )
    assert dockerfile_path.exists(), f"Dockerfile not found at {dockerfile_path}"
    content = dockerfile_path.read_text()
    assert "COPY pyproject.toml /app/" in content
    assert "COPY __init__.py main.py server.py /app/" in content
    assert "RUN pip install --no-cache-dir --prefix=/install ." in content


def test_pyproject_contains_hatchling_wheel_packages() -> None:
    """Test that calculator_service pyproject.toml configures Hatchling wheel packages."""
    pyproject_path = (
        Path(__file__).resolve().parent.parent
        / "demo"
        / "calculator_service"
        / "pyproject.toml"
    )
    assert pyproject_path.exists(), f"pyproject.toml not found at {pyproject_path}"
    content = pyproject_path.read_text()
    assert "[tool.hatch.build.targets.wheel]" in content
    assert 'packages = ["."]' in content

