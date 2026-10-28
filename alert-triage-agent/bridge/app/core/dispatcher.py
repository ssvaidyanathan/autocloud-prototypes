from dataclasses import dataclass
import json
import logging
import time
from typing import Any, Optional

from ..config import BridgeConfig, get_settings
from ..contracts.alert import AlertEvent, slugify

logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    task_id: str
    task_name: str
    is_duplicate: bool
    payload: dict[str, Any]


class TaskDispatcher:
    """Dispatches alert triage and investigation tasks to Cloud Tasks with group-level deduplication."""

    def __init__(
        self,
        config: Optional[BridgeConfig] = None,
        tasks_client: Optional[Any] = None,
    ) -> None:
        self.config = config or get_settings()
        self._tasks_client = tasks_client
        self._local_tasks: set[str] = set()
        self._local_payloads: dict[str, dict[str, Any]] = {}

    @property
    def local_payloads(self) -> dict[str, dict[str, Any]]:
        return self._local_payloads

    def get_task_id(
        self,
        current_time: Optional[float] = None,
        prefix: Optional[str] = None,
        group_id: Optional[str] = None,
        incident_id: Optional[str] = None,
        attempt: int = 1,
    ) -> str:
        """Calculate unique task ID per alert or group attempt (default prefix {NAME_PREFIX}-alert)."""
        alert_prefix = f"{self.config.name_prefix}-alert"
        prefix = prefix or alert_prefix
        if group_id and incident_id:
            safe_inc = slugify(incident_id)
            return f"{prefix}-{group_id}-{safe_inc}-{attempt}"
        if incident_id:
            safe_inc = slugify(incident_id)
            attempt_ts = int((current_time if current_time is not None else time.time()) * 1000)
            return f"{alert_prefix}-{safe_inc}-{attempt_ts}"
        if group_id:
            return f"{prefix}-{group_id}-{attempt}"
        ts = current_time if current_time is not None else time.time()
        return f"{prefix}-{int(ts * 1000)}"

    def get_task_name(self, task_id: str) -> str:
        parent = (
            f"projects/{self.config.project_id}/"
            f"locations/{self.config.effective_tasks_location}/"
            f"queues/{self.config.effective_tasks_queue}"
        )
        return f"{parent}/tasks/{task_id}"

    def dispatch_alert(
        self,
        alert: AlertEvent,
        group: Optional[Any] = None,
        current_time: Optional[float] = None,
        attempt: int = 1,
    ) -> DispatchResult:
        """Enqueue triage task to Cloud Tasks serialized queue."""
        group_id = getattr(group, "group_id", None) if group else None
        filename = getattr(group, "filename", None) if group else None
        task_id = self.get_task_id(
            current_time=current_time,
            prefix=None if not group_id else "triage",
            group_id=group_id,
            incident_id=alert.incident_id,
            attempt=attempt,
        )
        task_name = self.get_task_name(task_id)
        target_url = f"{self.config.agent_url.rstrip('/')}/triage"

        body_dict: dict[str, Any] = {
            "alert": alert.model_dump(mode="json") if hasattr(alert, "model_dump") else alert,
        }
        if group_id:
            body_dict["group_id"] = group_id
        if filename:
            body_dict["filename"] = filename
        if group and hasattr(group, "alerts") and group.alerts:
            body_dict["member_alerts"] = [
                a.model_dump(mode="json") if hasattr(a, "model_dump") else a
                for a in group.alerts.values()
            ]

        return self._enqueue_task(task_name, task_id, target_url, body_dict)

    def _enqueue_task(
        self,
        task_name: str,
        task_id: str,
        target_url: str,
        body_dict: dict[str, Any],
    ) -> DispatchResult:
        """Helper to enqueue a task to Cloud Tasks or local registry."""
        body_bytes = json.dumps(body_dict).encode("utf-8")

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
            },
        }

        # Attach OIDC token if not local dev
        if not self.config.is_local and self.config.effective_tasks_invoker_sa:
            task_def["http_request"]["oidc_token"] = {
                "service_account_email": self.config.effective_tasks_invoker_sa,
                "audience": self.config.agent_url,
            }

        client = self._tasks_client
        if client is None and not self.config.is_local:
            try:
                from google.cloud import tasks_v2
                client = tasks_v2.CloudTasksClient()
            except Exception as exc:
                logger.debug("CloudTasksClient initialization fallback: %s", exc)
                client = None

        if client is not None:
            parent = (
                f"projects/{self.config.project_id}/"
                f"locations/{self.config.effective_tasks_location}/"
                f"queues/{self.config.effective_tasks_queue}"
            )
            try:
                client.create_task(parent=parent, task=task_def)
                logger.info("Successfully enqueued Cloud Tasks task %s", task_name)
                return DispatchResult(
                    task_id=task_id,
                    task_name=task_name,
                    is_duplicate=False,
                    payload=body_dict,
                )
            except Exception as exc:
                exc_str = str(exc)
                if "409" in exc_str or "ALREADY_EXISTS" in exc_str or "AlreadyExists" in type(exc).__name__:
                    logger.info("Cloud Tasks task %s already exists (deduplicated).", task_name)
                    return DispatchResult(
                        task_id=task_id,
                        task_name=task_name,
                        is_duplicate=True,
                        payload=body_dict,
                    )
                logger.error("Failed to enqueue task %s: %s", task_name, exc)
                raise

        # Local in-memory deduplication fallback
        if task_name in self._local_tasks:
            logger.info("Local deduplication dropped task %s", task_name)
            return DispatchResult(
                task_id=task_id,
                task_name=task_name,
                is_duplicate=True,
                payload=body_dict,
            )

        self._local_tasks.add(task_name)
        self._local_payloads[task_name] = body_dict
        logger.info("Local dispatcher registered task %s", task_name)
        return DispatchResult(
            task_id=task_id,
            task_name=task_name,
            is_duplicate=False,
            payload=body_dict,
        )

    def clear(self) -> None:
        """Clear local in-memory deduplication registry."""
        self._local_tasks.clear()
        self._local_payloads.clear()
        logger.info("TaskDispatcher local task registry cleared")
