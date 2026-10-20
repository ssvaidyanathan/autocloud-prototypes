"""Mock Agent Server for offline verification and local demonstration."""

import argparse
import io
import os
import zipfile
from pathlib import Path

import httpx
import uvicorn

from app.main import app, set_skills_loader
from app.registry.client import GCPSkillRegistryClient
from app.skills_loader import SkillsLoader

REVISION_FILE = Path("/tmp/agent_registry_mock_revision")


def build_zip_from_dir(directory: Path) -> bytes:
    """Build a ZIP archive from a directory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(directory):
            if "scripts" in dirs:
                raise ValueError("Security violation: scripts directory in skill package")
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, directory)
                zf.write(full_path, rel_path)
    return buf.getvalue()


def get_current_mock_revision() -> str:
    """Read active mock revision from state file or default to r1."""
    if REVISION_FILE.exists():
        content = REVISION_FILE.read_text().strip()
        if content:
            return content
    return "r1"


def setup_mock_registry(root_dir: Path) -> None:
    """Configure app with mock GCP Agent Registry client."""
    v1_dir = root_dir / "demo" / "registry_packages" / "v1" / "calculator"
    v2_dir = root_dir / "demo" / "registry_packages" / "v2" / "calculator"

    r1_zip = build_zip_from_dir(v1_dir)
    r2_zip = build_zip_from_dir(v2_dir)

    def mock_registry_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        current_rev = get_current_mock_revision()

        if url_str.endswith("/skills/calculator"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": "projects/cloud-assist-fde-7/locations/us-central1/skills/calculator",
                    "defaultRevision": (
                        f"projects/cloud-assist-fde-7/locations/us-central1/"
                        f"skills/calculator/revisions/{current_rev}"
                    ),
                },
            )
        elif "revisions/" in url_str and "alt=media" in url_str:
            target_rev = url_str.split("revisions/")[1].split("?")[0]
            content = r1_zip if target_rev == "r1" else r2_zip
            return httpx.Response(
                status_code=200,
                content=content,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404, json={"error": "Not Found"})

    transport = httpx.MockTransport(mock_registry_handler)
    mock_http = httpx.AsyncClient(transport=transport)
    base_registry = GCPSkillRegistryClient(
        project_id="cloud-assist-fde-7",
        location="us-central1",
        http_client=mock_http,
    )
    loader = SkillsLoader(registry=base_registry, cache_ttl=60.0)
    set_skills_loader(loader)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Agent Server for offline demo")
    parser.add_argument("--port", type=int, default=8088, help="Port to listen on")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    setup_mock_registry(root_dir)

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
