"""Pytest fixtures for Deployment Context MCP Server tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from app.config import Settings
from app.indexer import DeploymentContextIndexer
from app.models import AlertContext, DeploymentEvent


@pytest.fixture
def sample_alert_context_dict() -> Dict[str, Any]:
    """Return a sanitized sample alert context dictionary."""
    return {
        "target_ip": "10.10.0.1",
        "port": 8080,
        "check_id": "sample-check-1",
        "service_hint": "sample-workload",
        "service_name": "sample-workload",
        "environment": "production",
        "platform": "gke",
        "cluster_name": "customer-cluster-1",
        "cluster": "customer-cluster-1",
        "namespace": "sample-app-ns",
        "workload_name": "sample-workload",
        "workload": "sample-workload",
        "workload_type": "Deployment",
        "release": "sample-release",
        "owner_team": "sample-devops-team",
        "repo_url": "https://git.example.internal/repos/sample-workload",
        "runbook_url": "https://wiki.example.internal/runbooks/sample-workload",
    }


@pytest.fixture
def sample_deployment_event_dict() -> Dict[str, Any]:
    """Return a sanitized sample deployment event dictionary."""
    return {
        "event_id": "evt-helm-deploy-089",
        "timestamp": "2026-10-27T02:15:00Z",
        "service_name": "sample-workload",
        "workload_name": "sample-workload",
        "workload": "sample-workload",
        "environment": "production",
        "cluster_name": "customer-cluster-1",
        "cluster": "customer-cluster-1",
        "namespace": "sample-app-ns",
        "author": "sample-deployer@example.com",
        "deployer": "sample-deployer@example.com",
        "deployment_tool": "helm",
        "tool": "helm",
        "release": "sample-release",
        "revision": "89",
        "status": "SUCCESS",
        "image": "gcr.io/sample-stg/sample-workload:v1.2.0",
        "container_images": ["gcr.io/sample-stg/sample-workload:v1.2.0"],
        "pipeline_id": "pipe-1089",
        "git_commit": "abc89def012345",
        "git_commit_message": "feat: bump image to v1.2.0",
        "change_summary": "Helm release sample-release updated to revision 89",
    }


@pytest.fixture
def populated_indexer(
    sample_alert_context_dict: Dict[str, Any],
    sample_deployment_event_dict: Dict[str, Any],
) -> DeploymentContextIndexer:
    """Return an indexer populated with sample data."""
    idx = DeploymentContextIndexer()
    idx.add_alert_context(AlertContext.model_validate(sample_alert_context_dict))
    idx.add_deployment_event(DeploymentEvent.model_validate(sample_deployment_event_dict))
    return idx


@pytest.fixture
def bundled_fixtures_path() -> Path:
    """Return path to the bundled fixtures directory."""
    return Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def test_settings() -> Settings:
    """Return test settings with auth enabled and known authorized caller."""
    return Settings(
        AUTH_DISABLED=False,
        AUTHORIZED_CALLER_SERVICE_ACCOUNTS="authorized-caller@example-project-id.iam.gserviceaccount.com,second-caller@example-project-id.iam.gserviceaccount.com",
        EXPECTED_AUDIENCE="https://mcp-deploy-stub.example.internal",
    )
