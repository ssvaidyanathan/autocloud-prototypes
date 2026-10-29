import re
from datetime import datetime, timezone
from unittest.mock import MagicMock

from bridge.app.config import BridgeConfig
from bridge.app.contracts.incident import Incident
from bridge.app.core.tasks import TaskDispatcher, generate_task_id


def _sample_incident(source: str = "servicenow", external_id: str = "sys-12345") -> Incident:
    return Incident(
        id=f"{source}-{external_id}",
        source=source,  # type: ignore[arg-type]
        external_id=external_id,
        external_key="INC0010001",
        title="Sample incident",
        description="Sample description",
        severity="P2",
        opened_at=datetime.now(timezone.utc),
    )


def test_deterministic_task_id_format() -> None:
    task_id1 = generate_task_id("servicenow", "abc-123")
    task_id2 = generate_task_id("servicenow", "abc-123")
    task_id3 = generate_task_id("jira", "abc-123")
    task_id4 = generate_task_id("servicenow", "abc-456")

    # Identical inputs produce identical IDs
    assert task_id1 == task_id2

    # Different source or external_id produce different IDs
    assert task_id1 != task_id3
    assert task_id1 != task_id4

    # Matches inv-<48 hex chars>
    assert re.match(r"^inv-[a-f0-9]{48}$", task_id1) is not None
    assert len(task_id1) == 52  # 4 prefix chars + 48 hex chars


def test_task_dispatcher_local_deduplication() -> None:
    config = BridgeConfig(
        app_env="local",
        gcp_project="test-project",
        tasks_location="us-central1",
        tasks_queue="investigations",
        agent_service_url="https://sre-agent.example.com",
        callback_url="https://bridge.example.com/callback",
    )
    dispatcher = TaskDispatcher(config=config)
    incident = _sample_incident()

    # First dispatch -> created
    res1 = dispatcher.dispatch(incident)
    assert res1.is_duplicate is False
    assert res1.task_name.endswith(res1.task_id)
    assert "investigations" in res1.task_name
    assert res1.payload["incident"]["id"] == incident.id
    assert res1.payload["callback_url"] == config.callback_url

    # Second dispatch -> duplicate dropped
    res2 = dispatcher.dispatch(incident)
    assert res2.is_duplicate is True
    assert res2.task_id == res1.task_id


def test_task_dispatcher_cloud_tasks_client_dispatch_deadline_and_oidc() -> None:
    mock_client = MagicMock()
    config = BridgeConfig(
        gcp_project="test-project",
        tasks_location="us-central1",
        tasks_queue="investigations",
        tasks_invoker_sa="tasks-invoker@test-project.iam.gserviceaccount.com",
        agent_service_url="https://sre-agent.example.com",
        callback_url="https://bridge.example.com/callback/report",
    )
    dispatcher = TaskDispatcher(config=config, tasks_client=mock_client)
    incident = _sample_incident()

    res = dispatcher.dispatch(incident)
    assert res.is_duplicate is False
    assert mock_client.create_task.called

    call_kwargs = mock_client.create_task.call_args.kwargs
    task_arg = call_kwargs["task"]

    # Verify 900s dispatch deadline
    assert task_arg["dispatch_deadline"] == {"seconds": 900}

    # Verify HTTP request configuration
    http_req = task_arg["http_request"]
    assert http_req["url"] == "https://sre-agent.example.com/investigate"
    assert http_req["http_method"] == "POST"

    # Verify OIDC token configuration
    oidc = http_req["oidc_token"]
    assert oidc["service_account_email"] == "tasks-invoker@test-project.iam.gserviceaccount.com"
    assert oidc["audience"] == "https://sre-agent.example.com"


def test_task_dispatcher_catches_already_exists_from_google_api() -> None:
    mock_client = MagicMock()
    # Simulate Google API AlreadyExists error (HTTP 409)
    error = Exception("409 Requested entity already exists")
    mock_client.create_task.side_effect = error

    config = BridgeConfig(
        gcp_project="test-project",
        tasks_location="us-central1",
        tasks_queue="investigations",
        agent_service_url="https://sre-agent.example.com",
        callback_url="https://bridge.example.com/callback",
    )
    dispatcher = TaskDispatcher(config=config, tasks_client=mock_client)
    incident = _sample_incident()

    res = dispatcher.dispatch(incident)
    assert res.is_duplicate is True
    assert res.task_id == generate_task_id(incident.source, incident.external_id)
