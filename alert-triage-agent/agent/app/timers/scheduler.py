"""Cloud Tasks Delayed Timer Scheduler for Alert Triage Agent.

Schedules delayed wakeups to evaluate settle, max-wait, and cool-down deadlines
on the dedicated Cloud Tasks queue: ata-timer-queue.
"""

from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Callable, Optional

from google.api_core import exceptions
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

from ..contracts.timers import TimerWakeRequest

logger = logging.getLogger(__name__)


class TimerScheduleError(RuntimeError):
    pass


# Replay test / mock hook type: Callable[[project_id, location, queue_name, target_url, group_id, deadline_type, deadline_dt, sa_email], str]
_timer_scheduler_hook: Optional[Callable[..., str]] = None


def set_timer_scheduler_hook(hook: Optional[Callable[..., str]]) -> None:
    """Register a custom hook or mock for schedule_delayed_timer (e.g. for virtual clock replay)."""
    global _timer_scheduler_hook
    _timer_scheduler_hook = hook


def get_timer_scheduler_hook() -> Optional[Callable[..., str]]:
    """Return currently active timer scheduler hook."""
    return _timer_scheduler_hook


def schedule_delayed_timer(
    project_id: str,
    location: str,
    queue_name: str,
    target_url: str,
    group_id: str,
    deadline_type: str,
    deadline_dt: datetime,
    service_account_email: str,
) -> str:
    """Schedule a delayed HTTP POST to {target_url}/timer/wake via Cloud Tasks.
    
    Generates deterministic task id: ata-timer-{safe_group}-{deadline_type}-{epoch_target}.
    Catches google.api_core.exceptions.AlreadyExists and returns existing task name.
    """
    if _timer_scheduler_hook is not None:
        return _timer_scheduler_hook(
            project_id=project_id,
            location=location,
            queue_name=queue_name,
            target_url=target_url,
            group_id=group_id,
            deadline_type=deadline_type,
            deadline_dt=deadline_dt,
            service_account_email=service_account_email,
        )

    if deadline_dt.tzinfo is None:
        deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
    epoch_target = int(deadline_dt.timestamp())

    safe_group = re.sub(r"[^a-zA-Z0-9_-]", "", group_id)[:30]
    task_id = f"ata-timer-{safe_group}-{deadline_type}-{epoch_target}"
    parent = f"projects/{project_id}/locations/{location}/queues/{queue_name}"
    task_name = f"{parent}/tasks/{task_id}"

    wake_req = TimerWakeRequest(
        group_id=group_id,
        deadline_type=deadline_type,
        scheduled_epoch=epoch_target,
    )
    payload_bytes = wake_req.model_dump_json().encode("utf-8")
    wake_url = f"{target_url.rstrip('/')}/timer/wake"

    task = {
        "name": task_name,
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": wake_url,
            "headers": {"Content-Type": "application/json"},
            "body": payload_bytes,
            "oidc_token": {"service_account_email": service_account_email},
        },
        "schedule_time": timestamp_pb2.Timestamp(seconds=epoch_target),
    }

    try:
        client = tasks_v2.CloudTasksClient()
        response = client.create_task(request={"parent": parent, "task": task})
        logger.info(
            "Scheduled Cloud Tasks delayed timer %s (group=%s, deadline_type=%s, target_epoch=%s)",
            response.name,
            group_id,
            deadline_type,
            epoch_target,
        )
        return response.name
    except exceptions.AlreadyExists:
        logger.info(
            "Cloud Tasks timer %s already exists for group %s; returning existing task name",
            task_name,
            group_id,
        )
        return task_name
    except Exception as exc:
        logger.error(
            "Failed to schedule Cloud Tasks timer for group %s (%s): %s",
            group_id,
            deadline_type,
            exc,
            exc_info=True,
        )
        raise TimerScheduleError(
            f"Failed to schedule Cloud Tasks timer for group {group_id} ({deadline_type}): {exc}"
        ) from exc
