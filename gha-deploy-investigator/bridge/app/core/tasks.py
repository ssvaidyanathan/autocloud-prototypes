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

        # Build Cloud Tasks HTTP request with OIDC token for authenticated internal invocation
        http_request_dict: dict[str, Any] = {
            "http_method": "POST",
            "url": target_url,
            "headers": {"Content-Type": "application/json"},
            "body": body_bytes,
        }

        # In production or when invoker SA is configured, require OIDC token
        if self.config.tasks_invoker_sa:
            http_request_dict["oidc_token"] = {
                "service_account_email": self.config.tasks_invoker_sa,
                "audience": self.config.agent_service_url,
            }

        task_def: dict[str, Any] = {
            "name": task_name,
            "dispatch_deadline": {"seconds": 900},
            "http_request": http_request_dict,
        }

        # Resolve Cloud Tasks client
        client = self._tasks_client
        if client is None and not _is_local_dev_env(self.config):
            try:
                from google.cloud import tasks_v2

                client = tasks_v2.CloudTasksClient()
                self._tasks_client = client
            except Exception as exc:
                logger.error(
                    "CRITICAL: Failed to instantiate CloudTasksClient in env (%s): %s",
                    self.config.environment,
                    exc,
                )
                raise RuntimeError(
                    f"CloudTasksClient init failed in {self.config.environment}: {exc}"
                ) from exc

        # If we have a Cloud Tasks client, perform real GCP enqueue
        if client is not None:
            parent = (
                f"projects/{self.config.gcp_project}/"
                f"locations/{self.config.tasks_location}/"
                f"queues/{self.config.tasks_queue}"
            )
            try:
                client.create_task(parent=parent, task=task_def)
                logger.info("Successfully enqueued Cloud Tasks task %s to %s", task_id, parent)
                return DispatchResult(
                    task_id=task_id,
                    task_name=task_name,
                    is_duplicate=False,
                    payload=req.model_dump(mode="json"),
                )
            except Exception as exc:
                err_str = str(exc).lower()
                if "already_exists" in err_str or "409" in err_str or "already exists" in err_str:
                    logger.info("Task %s already exists in Cloud Tasks queue (deduped)", task_id)
                    return DispatchResult(
                        task_id=task_id,
                        task_name=task_name,
                        is_duplicate=True,
                        payload=req.model_dump(mode="json"),
                    )
                logger.error("Failed to enqueue task %s in Cloud Tasks: %s", task_id, exc)
                raise

        # Hermetic local/dev environment fallback
        if not _is_local_dev_env(self.config):
            logger.error(
                "CRITICAL: Task %s accepted but not enqueued in %s env!",
                task_id,
                self.config.environment,
            )
            raise RuntimeError(
                f"Cannot use local in-memory fallback in non-local env: {self.config.environment}"
            )

        if task_id in self._local_tasks:
            logger.info("Local duplicate task detected: %s. Dropping.", task_id)
            return DispatchResult(
                task_id=task_id,
                task_name=task_name,
                is_duplicate=True,
                payload=req.model_dump(mode="json"),
            )

        self._local_tasks.add(task_id)
        logger.info("Enqueued task %s locally (dev/test mode)", task_id)
        return DispatchResult(
            task_id=task_id,
            task_name=task_name,
            is_duplicate=False,
            payload=req.model_dump(mode="json"),
        )
