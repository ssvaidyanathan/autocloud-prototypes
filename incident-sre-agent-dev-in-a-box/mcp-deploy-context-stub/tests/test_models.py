"""Unit tests for Pydantic data models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import (
    AlertContext,
    DeploymentEvent,
    GetRecentDeploymentsRequest,
    GetRecentDeploymentsResponse,
    LookupAlertContextResponse,
)


def test_alert_context_sync_aliases() -> None:
    """Verify alias synchronization for cluster/cluster_name and workload/workload_name."""
    ctx = AlertContext(
        service_name="test-svc",
        cluster_name="test-cluster",
        namespace="test-ns",
        workload_name="test-workload",
        owner_team="test-team",
    )
    assert ctx.cluster == "test-cluster"
    assert ctx.workload == "test-workload"

    ctx2 = AlertContext(
        service_name="test-svc",
        cluster="test-cluster-2",
        cluster_name="",
        namespace="test-ns",
        workload="test-workload-2",
        workload_name="",
        owner_team="test-team",
    )
    assert ctx2.cluster_name == "test-cluster-2"
    assert ctx2.workload_name == "test-workload-2"


def test_lookup_alert_context_response_empty() -> None:
    """Verify empty response semantics when context is not found."""
    resp = LookupAlertContextResponse(found=False, context=None)
    dumped = resp.model_dump()
    assert dumped["found"] is False
    assert dumped["context"] is None


def test_deployment_event_sync_aliases() -> None:
    """Verify alias synchronization for author/tool and image/container_images."""
    evt = DeploymentEvent(
        event_id="evt-001",
        timestamp="2026-10-27T00:00:00Z",
        author="alice@example.com",
        deployment_tool="helm",
        image="gcr.io/sample-stg/app:v1.0.0",
    )
    assert evt.deployer == "alice@example.com"
    assert evt.tool == "helm"
    assert evt.container_images == ["gcr.io/sample-stg/app:v1.0.0"]

    evt2 = DeploymentEvent(
        event_id="evt-002",
        timestamp="2026-10-27T01:00:00Z",
        deployer="bob@example.com",
        tool="kubectl",
        container_images=["gcr.io/sample-stg/app:v2.0.0"],
    )
    assert evt2.author == "bob@example.com"
    assert evt2.deployment_tool == "kubectl"
    assert evt2.image == "gcr.io/sample-stg/app:v2.0.0"


def test_get_recent_deployments_request_limits() -> None:
    """Verify pagination limit validation boundaries (1-20)."""
    req = GetRecentDeploymentsRequest(limit=10)
    assert req.limit == 10

    # Default limit
    req_default = GetRecentDeploymentsRequest()
    assert req_default.limit == 5

    # Out of range limit should raise ValidationError
    with pytest.raises(ValidationError):
        GetRecentDeploymentsRequest(limit=0)

    with pytest.raises(ValidationError):
        GetRecentDeploymentsRequest(limit=25)


def test_get_recent_deployments_response_empty() -> None:
    """Verify empty response semantics for deployment queries."""
    resp = GetRecentDeploymentsResponse()
    dumped = resp.model_dump()
    assert dumped["deployments"] == []
    assert dumped["total_count"] == 0
