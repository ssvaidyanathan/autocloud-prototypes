"""Application entrypoint for Deployment Context MCP Server."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn

from app.config import get_settings
from app.indexer import DeploymentContextIndexer
from app.server import create_app

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mcp-deploy-context-stub")

settings = get_settings()
indexer = DeploymentContextIndexer()

# 1. Load bundled fixtures from local directory
fixtures_dir = Path(settings.FIXTURES_DIR)
if not fixtures_dir.is_absolute():
    # Check relative to module root or CWD
    module_root = Path(__file__).resolve().parent.parent
    candidate = module_root / fixtures_dir
    if candidate.is_dir():
        fixtures_dir = candidate

if fixtures_dir.is_dir():
    logger.info("Loading bundled fixtures from %s", fixtures_dir)
    indexer.load_directory(fixtures_dir)
else:
    logger.warning("Bundled fixtures directory not found: %s", fixtures_dir)

# 2. Sync dynamic fixtures from Google Cloud Storage if configured
if settings.FIXTURES_GCS_URI:
    logger.info("Syncing dynamic incident fixtures from GCS: %s", settings.FIXTURES_GCS_URI)
    indexer.load_gcs_uri(settings.FIXTURES_GCS_URI)

# 3. Build ASGI Starlette application
app = create_app(indexer=indexer, settings=settings)

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        reload=False,
    )
