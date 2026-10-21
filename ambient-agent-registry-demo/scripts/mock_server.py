"""Mock Agent Server for offline verification and local demonstration.

Exposes native ADK /run, /health, and /admin/cache/invalidate endpoints
to simulate live Agent Registry and FastMCP calculator behavior without
requiring live GCP infrastructure.
"""

from __future__ import annotations

import argparse
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

REVISION_FILE = Path("/tmp/agent_registry_mock_revision")

app = FastAPI(title="Ambient Agent Mock Server")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mock_server")


def get_current_mock_revision() -> str:
    """Read active mock revision from state file or default to r1."""
    if REVISION_FILE.exists():
        content = REVISION_FILE.read_text().strip()
        if content:
            return content
    return "r1"


def set_current_mock_revision(rev: str) -> None:
    """Set active mock revision."""
    REVISION_FILE.write_text(rev.strip())


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


@app.post("/admin/cache/invalidate")
async def invalidate_cache(request: Request) -> dict[str, Any]:
    """Administrative cache invalidation endpoint."""
    try:
        body = await request.json()
        skill_name = body.get("skill_name", "ambient-privilege-audit-skill")
    except Exception:
        skill_name = "ambient-privilege-audit-skill"

    logger.info("Flushed cache for skill: %s", skill_name)
    return {"status": "invalidated", "flushed": [skill_name]}


def make_event(
    author: str,
    role: str,
    parts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Construct an ADK-compliant Event dictionary."""
    return {
        "id": str(uuid.uuid4()),
        "author": author,
        "content": {
            "role": role,
            "parts": parts,
        },
        "actions": {
            "stateDelta": {},
            "artifactDelta": {},
            "requestedAuthConfigs": {},
            "requestedToolConfirmations": {},
        },
        "nodeInfo": {"path": ""},
        "invocationId": "",
        "timestamp": time.time(),
    }


@app.post("/run")
async def run_agent(request: Request) -> JSONResponse:
    """Simulate native ADK POST /run execution."""
    body = await request.json()
    new_message = body.get("new_message", {})
    parts = new_message.get("parts", [])
    prompt = ""
    for part in parts:
        if isinstance(part, dict) and "text" in part:
            prompt += part["text"] + " "
    prompt = prompt.strip()
    logger.info("Received /run request with prompt: %s", prompt)

    revision = get_current_mock_revision()
    events: list[dict[str, Any]] = []

    if "power" in prompt.lower() or revision == "r2":
        # Act 2: power calculation
        events = [
            make_event(
                author="ambient_agent",
                role="model",
                parts=[
                    {
                        "functionCall": {
                            "id": "call_p1",
                            "name": "power",
                            "args": {"base": 2.0, "exponent": 8.0},
                        }
                    }
                ],
            ),
            make_event(
                author="ambient_agent",
                role="user",
                parts=[
                    {
                        "functionResponse": {
                            "id": "call_p1",
                            "name": "power",
                            "response": {"result": 256.0},
                        }
                    }
                ],
            ),
            make_event(
                author="ambient_agent",
                role="model",
                parts=[{"text": "🧮 [CALCULATOR-V2] Computed value: **256**"}],
            ),
        ]
    else:
        # Act 1: multiply and add calculation
        events = [
            make_event(
                author="ambient_agent",
                role="model",
                parts=[
                    {
                        "functionCall": {
                            "id": "call_m1",
                            "name": "multiply",
                            "args": {"a": 15.0, "b": 4.0},
                        }
                    }
                ],
            ),
            make_event(
                author="ambient_agent",
                role="user",
                parts=[
                    {
                        "functionResponse": {
                            "id": "call_m1",
                            "name": "multiply",
                            "response": {"result": 60.0},
                        }
                    }
                ],
            ),
            make_event(
                author="ambient_agent",
                role="model",
                parts=[
                    {
                        "functionCall": {
                            "id": "call_a1",
                            "name": "add",
                            "args": {"a": 60.0, "b": 10.0},
                        }
                    }
                ],
            ),
            make_event(
                author="ambient_agent",
                role="user",
                parts=[
                    {
                        "functionResponse": {
                            "id": "call_a1",
                            "name": "add",
                            "response": {"result": 70.0},
                        }
                    }
                ],
            ),
            make_event(
                author="ambient_agent",
                role="model",
                parts=[{"text": "Result: 70 (Computed via multiply, add)"}],
            ),
        ]

    return JSONResponse(content=events)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Agent Server for offline demo")
    parser.add_argument("--port", type=int, default=8089, help="Port to listen on")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface")
    args = parser.parse_args()

    # Reset mock revision state
    set_current_mock_revision("r1")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
