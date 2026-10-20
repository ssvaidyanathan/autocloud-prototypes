"""Entrypoint for FastMCP Calculator Tool Service.

Runs the FastMCP HTTP server via Uvicorn on Cloud Run or local development.
"""

from __future__ import annotations

import os

import uvicorn

try:
    from .server import add, app, create_app, health_check, mcp, multiply, power
except ImportError:
    from server import (  # type: ignore[no-redef]
        add,
        app,
        create_app,
        health_check,
        mcp,
        multiply,
        power,
    )

__all__ = [
    "add",
    "app",
    "create_app",
    "health_check",
    "mcp",
    "multiply",
    "power",
]


def main() -> None:
    """Run the FastMCP Uvicorn server."""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()
    uvicorn.run(app, host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
