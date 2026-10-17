from unittest.mock import MagicMock, patch

import pytest
from bridge.app.config import BridgeConfig
from bridge.app.contracts.incident import Incident
from bridge.app.core.tasks import TaskDispatcher, generate_task_id


def test_generate_task_id() -> None:
    task_id1 = generate_task_id("github", "12345")
    task_id2 = generate_task_id("github", "12345")
    task_id3 = generate_task_id("github", "67890")

    assert task_id1.startswith("inv-")
    assert task_id1 == task_id2
    assert task_id1 != task_id3


def test_task_dispatcher_local_mode(sample_incident: Incident) -> None:
    config = BridgeConfig(
        app_env="test",
        environment="test",
        gcp_project="test-project",
        tasks_queue="test-queue",
        tasks_location="us-central1",
        agent_service_url="https://agent.test.local",
    )
    dispatcher = TaskDispatcher(config=config)

    res = dispatcher.dispatch(
        sample_incident, callback_url="https://bridge.test.local/callback/report"
    )
    assert not res.is_duplicate
    assert res.task_id.startswith("inv-")
    assert res.task_id in res.task_name

    # Duplicate check in local mode
    res2 = dispatcher.dispatch(
        sample_incident, callback_url="https://bridge.test.local/callback/report"
    )
    assert res2.is_duplicate


def test_task_dispatcher_production_enqueues_cloud_tasks_with_oidc(
    sample_incident: Incident,
) -> None:
    config = BridgeConfig(
        app_env="production",
        environment="production",
        gcp_project="prod-project",
        tasks_queue="prod-queue",
        tasks_location="us-central1",
        tasks_invoker_sa="invoker@prod-project.iam.gserviceaccount.com",
        agent_service_url="https://gdi-agent.run.app",
    )
    mock_client = MagicMock()
    dispatcher = TaskDispatcher(config=config, tasks_client=mock_client)

    res = dispatcher.dispatch(
        sample_incident, callback_url="https://bridge.run.app/callback/report"
    )
    assert not res.is_duplicate
    assert mock_client.create_task.called

    call_kwargs = mock_client.create_task.call_args[1]
    assert call_kwargs["parent"] == "projects/prod-project/locations/us-central1/queues/prod-queue"
    task_def = call_kwargs["task"]
    assert task_def["http_request"]["url"] == "https://gdi-agent.run.app/investigate"
    assert "oidc_token" in task_def["http_request"]
    assert task_def["http_request"]["oidc_token"] == {
        "service_account_email": "invoker@prod-project.iam.gserviceaccount.com",
        "audience": "https://gdi-agent.run.app",
    }


def test_task_dispatcher_production_lazy_instantiates_client(
    sample_incident: Incident,
) -> None:
    config = BridgeConfig(
        app_env="production",
        environment="production",
        gcp_project="prod-project",
        tasks_queue="prod-queue",
        tasks_location="us-central1",
        tasks_invoker_sa="invoker@prod-project.iam.gserviceaccount.com",
        agent_service_url="https://gdi-agent.run.app",
    )
    mock_client = MagicMock()
    with patch("google.cloud.tasks_v2.CloudTasksClient", return_value=mock_client):
        dispatcher = TaskDispatcher(config=config)  # tasks_client is None
        res = dispatcher.dispatch(sample_incident)
        assert not res.is_duplicate
        assert mock_client.create_task.called


def test_task_dispatcher_production_fails_if_client_fails_to_init(
    sample_incident: Incident,
) -> None:
    config = BridgeConfig(
        app_env="production",
        environment="production",
        gcp_project="prod-project",
        tasks_queue="prod-queue",
        tasks_location="us-central1",
        tasks_invoker_sa="invoker@prod-project.iam.gserviceaccount.com",
        agent_service_url="https://gdi-agent.run.app",
    )
    with patch("google.cloud.tasks_v2.CloudTasksClient", side_effect=Exception("Auth error")):
        dispatcher = TaskDispatcher(config=config)
        with pytest.raises(RuntimeError, match="CloudTasksClient init failed"):
            dispatcher.dispatch(sample_incident)
