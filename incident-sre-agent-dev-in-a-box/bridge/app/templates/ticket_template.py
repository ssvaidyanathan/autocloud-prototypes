"""Incident ticket description template loading and rendering with GCS and local support."""

from __future__ import annotations

import logging
import os
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from jinja2 import Template

logger = logging.getLogger(__name__)

_DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parent / "incident_template.md"
_template_cache: dict[str, str] = {}
_cache_lock = threading.Lock()


def clear_template_cache() -> None:
    """Clear in-memory template cache (for tests and dynamic reloads)."""
    with _cache_lock:
        _template_cache.clear()


def _load_gcs_template(gcs_uri: str) -> str:
    """Download template text from Google Cloud Storage URI (gs://bucket/object)."""
    path_without_scheme = gcs_uri[5:]
    if "/" not in path_without_scheme:
        raise ValueError(f"Invalid GCS URI: {gcs_uri}")

    bucket_name, blob_name = path_without_scheme.split("/", 1)

    # 1. Try google.cloud.storage client
    try:
        from google.cloud import storage  # type: ignore[attr-defined]

        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        content = blob.download_as_text(encoding="utf-8")
        logger.info("Successfully loaded template from GCS URI %s via storage client", gcs_uri)
        return str(content)
    except Exception as exc:
        logger.debug(
            "google.cloud.storage download failed for %s (%s), trying HTTP fallback", gcs_uri, exc
        )

    # 2. HTTP API fallback using Google Application Default Credentials
    import google.auth
    import httpx
    from google.auth.transport.requests import Request as GoogleRequest

    creds, _ = google.auth.default(
        scopes=[
            "https://www.googleapis.com/auth/devstorage.read_only",
            "https://www.googleapis.com/auth/cloud-platform",
        ]
    )
    creds.refresh(GoogleRequest())  # type: ignore[no-untyped-call]

    encoded_blob = urllib.parse.quote(blob_name, safe="")
    storage_url = (
        f"https://storage.googleapis.com/storage/v1/b/{bucket_name}/o/{encoded_blob}?alt=media"
    )
    headers = {"Authorization": f"Bearer {creds.token}"}

    with httpx.Client(timeout=10.0) as http_client:
        resp = http_client.get(storage_url, headers=headers)
        resp.raise_for_status()
        logger.info("Successfully loaded template from GCS URI %s via HTTP API", gcs_uri)
        return resp.text


def get_ticket_template_content(custom_path: Optional[str] = None) -> str:
    """Load ticket template markdown from custom_path, TICKET_TEMPLATE_PATH, or bundled file.

    Supports:
      - gs://bucket/path (Google Cloud Storage)
      - local filesystem path
      - bundled default incident_template.md
    Caches loaded template text in memory.
    """
    path_to_try = (custom_path or os.environ.get("TICKET_TEMPLATE_PATH") or "").strip()
    if not path_to_try:
        return _DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")

    with _cache_lock:
        if path_to_try in _template_cache:
            return _template_cache[path_to_try]

    content: Optional[str] = None
    if path_to_try.startswith("gs://"):
        try:
            content = _load_gcs_template(path_to_try)
        except Exception as exc:
            logger.error(
                "Failed to load template from %s: %s; falling back to bundled default",
                path_to_try,
                exc,
            )
            return _DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")
    else:
        p = Path(path_to_try)
        if p.is_file():
            content = p.read_text(encoding="utf-8")
        else:
            logger.warning(
                "Template path '%s' not found; falling back to bundled default", path_to_try
            )
            return _DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")

    if content is not None:
        with _cache_lock:
            _template_cache[path_to_try] = content
        return content

    return _DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")


def render_ticket_description(
    data: Optional[dict[str, Any]] = None,
    template_path: Optional[str] = None,
) -> str:
    """Render incident ticket markdown description from template using supplied dictionary."""
    template_content = get_ticket_template_content(template_path)
    template = Template(template_content)
    context = data or {}
    rendered = template.render(**context)
    return str(rendered).strip()
