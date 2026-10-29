import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import BridgeConfig, get_settings
from ..contracts.incident import Incident
from ..contracts.request import InvestigationRequest
from .auth import _is_local_dev_env

logger = logging.getLogger(__name__)


def generate_task_id(source: str, external_id: str) -> str:
    """Generate a deterministic task ID based on incident source and external ID."""
    digest = hashlib.sha256(f"{source}:{external_id}".encode("utf-8")).hexdigest()[:48]
    return f"inv-{digest}"


@dataclass
class DispatchResult:
    task_id: str
    task_name: str
    is_duplicate: bool
    payload: dict[str, Any]


class TaskDispatcher:
    """Dispatches investigation requests to Cloud Tasks with deterministic deduplication."""

    def __init__(
        self,
        config: Optional[BridgeConfig] = None,
        tasks_client: Optional[Any] = None,
    ) -> None:
        self.config = config or get_settings()
        self._tasks_client = tasks_client
        # In-memory registry for local testing or fallback deduplication
        self._local_tasks: set[str] = set()

    def get_task_name(self, task_id: str) -> str:
        """Construct full resource path for the Cloud Tasks task."""
        return (
            f"projects/{self.config.gcp_project}/"
            f"locations/{self.config.tasks_location}/"
            f"queues/{self.config.tasks_queue}/"
            f"tasks/{task_id}"
        )

    def dispatch(
        self,
        incident: Incident,
        callback_url: Optional[str] = None,
    ) -> DispatchResult:
        """Enqueue an investigation task into Cloud Tasks.

        Returns DispatchResult indicating whether task was created or deduplicated.
        """
        task_id = generate_task_id(incident.source, incident.external_id)
        task_name = self.get_task_name(task_id)

        target_callback = callback_url or self.config.callback_url
        req = InvestigationRequest(
            incident=incident,
            callback_url=target_callback,
            requested_at=datetime.now(timezone.utc),
        )
        body_bytes = json.dumps(req.model_dump(mode="json")).encode("utf-8")

        target_url = f"{self.config.agent_service_url.rstrip('/')}/investigate"

        # Build task dictionary matching Cloud Tasks API schema
        task_def: dict[str, Any] = {
            "name": task_name,
            "dispatch_deadline": {"seconds": 900},
            "http_request": {
                "http_method": "POST",
                "url": target_url,
                "headers": {
                    "Content-Type": "application/json",
                },
                "body": body_bytes,
                "oidc_token": {
                    "service_account_email": self.config.tasks_invoker_sa,
                    "audience": self.config.agent_service_url,
                },
            },
        }

        # Check if tasks_client is provided or google-cloud-tasks is available
        client = self._tasks_client
        if client is None:
            if not _is_local_dev_env(self.config):
                try:
                    from google.cloud import tasks_v2

                    client = tasks_v2.CloudTasksClient()
                except (ImportError, Exception) as exc:
                    logger.debug(
                        "CloudTasksClient not available (%s), using in-memory local dispatcher",
                        exc,
                    )
                    client = None

        if client is not None:
            parent = (
                f"projects/{self.config.gcp_project}/"
                f"locations/{self.config.tasks_location}/"
                f"queues/{self.config.tasks_queue}"
            )
            try:
                # Call create_task
                client.create_task(parent=parent, task=task_def)  # type: ignore[arg-type]
                logger.info("Successfully enqueued Cloud Tasks task %s", task_name)
                return DispatchResult(
                    task_id=task_id,
                    task_name=task_name,
                    is_duplicate=False,
                    payload=req.model_dump(mode="json"),
                )
            except Exception as exc:
                # Check for AlreadyExists (status code 409 or grpc StatusCode.ALREADY_EXISTS)
                exc_str = str(exc)
                is_already_exists = (
                    "409" in exc_str
                    or "ALREADY_EXISTS" in exc_str
                    or "AlreadyExists" in type(exc).__name__
                    or getattr(exc, "code", None) == 409
                )
                if is_already_exists:
                    logger.info(
                        "Task %s already exists (duplicate webhook received). Dropping.",
                        task_name,
                    )
                    return DispatchResult(
                        task_id=task_id,
                        task_name=task_name,
                        is_duplicate=True,
                        payload=req.model_dump(mode="json"),
                    )
                logger.error("Failed to enqueue task %s: %s", task_name, exc)
                raise

        # Local in-memory deduplication path
        if task_name in self._local_tasks:
            logger.info("Local duplicate task detected: %s. Dropping.", task_name)
            return DispatchResult(
                task_id=task_id,
                task_name=task_name,
                is_duplicate=True,
                payload=req.model_dump(mode="json"),
            )

        self._local_tasks.add(task_name)
        logger.info("Enqueued task %s locally", task_name)
        return DispatchResult(
            task_id=task_id,
            task_name=task_name,
            is_duplicate=False,
            payload=req.model_dump(mode="json"),
        )
