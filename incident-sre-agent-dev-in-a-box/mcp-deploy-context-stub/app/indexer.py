"""In-memory indexer for alert context resolution and deployment events."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.models import (
    AlertContext,
    DeploymentEvent,
    GetRecentDeploymentsResponse,
    LookupAlertContextResponse,
)

logger = logging.getLogger(__name__)


def _parse_iso_timestamp(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into a timezone-aware datetime."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class DeploymentContextIndexer:
    """In-memory repository and indexer for alert contexts and deployments."""

    def __init__(self) -> None:
        self._alert_contexts: List[AlertContext] = []
        self._deployments: List[DeploymentEvent] = []

        # Secondary lookup indices
        self._by_check_id: Dict[str, AlertContext] = {}
        self._by_target_ip: Dict[str, AlertContext] = {}
        self._by_service_hint: Dict[str, AlertContext] = {}

    def clear(self) -> None:
        """Clear all indexed data."""
        self._alert_contexts.clear()
        self._deployments.clear()
        self._by_check_id.clear()
        self._by_target_ip.clear()
        self._by_service_hint.clear()

    def add_alert_context(self, ctx: AlertContext) -> None:
        """Index a single AlertContext."""
        self._alert_contexts.append(ctx)
        if ctx.check_id:
            self._by_check_id[ctx.check_id] = ctx
        if ctx.target_ip:
            self._by_target_ip[ctx.target_ip] = ctx
        if ctx.service_hint:
            self._by_service_hint[ctx.service_hint.lower()] = ctx
        if ctx.service_name:
            self._by_service_hint[ctx.service_name.lower()] = ctx
        if ctx.workload_name:
            self._by_service_hint[ctx.workload_name.lower()] = ctx

    def add_deployment_event(self, event: DeploymentEvent) -> None:
        """Index a single DeploymentEvent."""
        self._deployments.append(event)

    def load_data(self, data: Any) -> None:
        """Ingest raw dictionary or list of items into the indexer."""
        if isinstance(data, dict):
            # Check for alert contexts in standard keys
            for key in ("alert_contexts", "alerts", "contexts"):
                if key in data and isinstance(data[key], list):
                    for item in data[key]:
                        try:
                            self.add_alert_context(AlertContext.model_validate(item))
                        except Exception as e:
                            logger.warning("Failed to parse alert context: %s", e)

            # Check for deployment events in standard keys
            for key in ("deployments", "deployment_events", "events"):
                if key in data and isinstance(data[key], list):
                    for item in data[key]:
                        try:
                            self.add_deployment_event(DeploymentEvent.model_validate(item))
                        except Exception as e:
                            logger.warning("Failed to parse deployment event: %s", e)

            # If top-level dict itself looks like an alert context
            if "service_name" in data and "cluster_name" in data and "namespace" in data:
                try:
                    self.add_alert_context(AlertContext.model_validate(data))
                except Exception as e:
                    logger.debug("Dict not parsed as alert context: %s", e)

        elif isinstance(data, list):
            for item in data:
                self.load_data(item)

    def load_file(self, file_path: str | Path) -> None:
        """Load and index JSON data from a local file path."""
        p = Path(file_path)
        if not p.is_file():
            logger.warning("Fixture file not found: %s", file_path)
            return

        try:
            with open(p, "r", encoding="utf-8") as f:
                content = json.load(f)
            self.load_data(content)
            logger.info("Successfully loaded fixture from %s", file_path)
        except Exception as e:
            logger.error("Error reading fixture file %s: %s", file_path, e)

    def load_directory(self, dir_path: str | Path) -> None:
        """Load all JSON fixture files from a local directory."""
        p = Path(dir_path)
        if not p.is_dir():
            logger.warning("Fixture directory not found: %s", dir_path)
            return

        json_files = sorted(p.glob("*.json"))
        for jf in json_files:
            self.load_file(jf)

    def load_gcs_uri(self, gcs_uri: str) -> None:
        """Load fixture files dynamically from a Google Cloud Storage URI (gs://bucket/prefix)."""
        if not gcs_uri or not gcs_uri.startswith("gs://"):
            logger.warning("Invalid GCS URI: %s", gcs_uri)
            return

        try:
            from google.cloud import storage  # type: ignore[import-untyped]
        except ImportError:
            logger.error("google-cloud-storage is not installed; cannot load from %s", gcs_uri)
            return

        parsed = urlparse(gcs_uri)
        bucket_name = parsed.netloc
        prefix = parsed.path.lstrip("/")

        try:
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            blobs = list(bucket.list_blobs(prefix=prefix))
            loaded_count = 0
            for blob in blobs:
                if blob.name.endswith(".json"):
                    text_content = blob.download_as_text()
                    data = json.loads(text_content)
                    self.load_data(data)
                    loaded_count += 1
            logger.info("Loaded %d fixture file(s) from %s", loaded_count, gcs_uri)
        except Exception as e:
            logger.error("Failed to load fixtures from GCS %s: %s", gcs_uri, e)

    def lookup_alert_context(
        self,
        target_ip: Optional[str] = None,
        check_id: Optional[str] = None,
        service_hint: Optional[str] = None,
    ) -> LookupAlertContextResponse:
        """Resolve alert indicators to service topology and metadata."""
        # 1. Check by monitoring check ID
        if check_id and check_id in self._by_check_id:
            return LookupAlertContextResponse(found=True, context=self._by_check_id[check_id])

        # 2. Check by target IP address
        if target_ip and target_ip in self._by_target_ip:
            return LookupAlertContextResponse(found=True, context=self._by_target_ip[target_ip])

        # 3. Check by service hint or partial match
        if service_hint:
            hint_lower = service_hint.lower().strip()
            if hint_lower in self._by_service_hint:
                return LookupAlertContextResponse(
                    found=True,
                    context=self._by_service_hint[hint_lower],
                )

            # Substring scan across alert contexts
            for ctx in self._alert_contexts:
                if (
                    hint_lower in ctx.service_name.lower()
                    or hint_lower in ctx.workload_name.lower()
                    or (ctx.service_hint and hint_lower in ctx.service_hint.lower())
                ):
                    return LookupAlertContextResponse(found=True, context=ctx)

        # Unmatched query returns typed empty response
        return LookupAlertContextResponse(found=False, context=None)

    def get_recent_deployments(
        self,
        workload: Optional[str] = None,
        cluster: Optional[str] = None,
        namespace: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 5,
        workload_name: Optional[str] = None,
        cluster_name: Optional[str] = None,
    ) -> GetRecentDeploymentsResponse:
        """Fetch recent deployments filtered by workload, cluster, or namespace."""
        target_workload = workload or workload_name
        target_cluster = cluster or cluster_name

        matched: List[DeploymentEvent] = []

        for dep in self._deployments:
            # Filter by workload if provided
            if target_workload:
                w_lower = target_workload.lower()
                dep_w = (dep.workload_name or dep.workload or dep.service_name or "").lower()
                if w_lower != dep_w and w_lower not in dep_w:
                    continue

            # Filter by cluster if provided
            if target_cluster:
                c_lower = target_cluster.lower()
                dep_c = (dep.cluster_name or dep.cluster or "").lower()
                if c_lower != dep_c and c_lower not in dep_c:
                    continue

            # Filter by namespace if provided
            if namespace:
                n_lower = namespace.lower()
                dep_n = (dep.namespace or "").lower()
                if n_lower != dep_n:
                    continue

            # Filter by start_time (ISO-8601)
            if start_time:
                parsed_start = _parse_iso_timestamp(start_time)
                parsed_dep = _parse_iso_timestamp(dep.timestamp)
                if parsed_start and parsed_dep:
                    if parsed_dep < parsed_start:
                        continue
                elif dep.timestamp < start_time:
                    continue

            # Filter by end_time (ISO-8601)
            if end_time:
                parsed_end = _parse_iso_timestamp(end_time)
                parsed_dep = _parse_iso_timestamp(dep.timestamp)
                if parsed_end and parsed_dep:
                    if parsed_dep > parsed_end:
                        continue
                elif dep.timestamp > end_time:
                    continue

            matched.append(dep)

        # Sort descending by timestamp
        matched.sort(
            key=lambda d: (
                _parse_iso_timestamp(d.timestamp) or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=True,
        )

        total_count = len(matched)
        effective_limit = max(1, min(20, limit))
        paged = matched[:effective_limit]

        return GetRecentDeploymentsResponse(deployments=paged, total_count=total_count)
