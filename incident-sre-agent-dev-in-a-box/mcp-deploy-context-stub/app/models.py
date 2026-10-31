"""Pydantic data models for Deployment Context MCP Server."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class AlertContext(BaseModel):
    """Contextual metadata mapping alert indicators to service topology."""

    service_name: str = Field(description="Canonical service name")
    cluster_name: str = Field(description="Cluster identifier")
    namespace: str = Field(description="Kubernetes namespace or project scope")
    workload_name: str = Field(description="Workload name")
    owner_team: str = Field(description="Owning team or group")

    # Optional / alias fields
    cluster: Optional[str] = Field(default=None, description="Alias for cluster_name")
    workload: Optional[str] = Field(default=None, description="Alias for workload_name")
    environment: Optional[str] = Field(default="production", description="Deployment environment")
    platform: Optional[str] = Field(default="gke", description="Runtime platform")
    workload_type: Optional[str] = Field(default="Deployment", description="Workload type")
    release: Optional[str] = Field(default=None, description="Release or package identifier")
    target_ip: Optional[str] = Field(default=None, description="Associated target IP address")
    port: Optional[int] = Field(default=None, description="Service port")
    check_id: Optional[str] = Field(default=None, description="Monitoring check identifier")
    service_hint: Optional[str] = Field(default=None, description="Service name hint")
    repo_url: Optional[str] = Field(default=None, description="Source repository URL")
    runbook_url: Optional[str] = Field(default=None, description="Incident runbook URL")

    @model_validator(mode="after")
    def sync_aliases(self) -> AlertContext:
        if not self.cluster and self.cluster_name:
            self.cluster = self.cluster_name
        elif not self.cluster_name and self.cluster:
            self.cluster_name = self.cluster

        if not self.workload and self.workload_name:
            self.workload = self.workload_name
        elif not self.workload_name and self.workload:
            self.workload_name = self.workload

        return self


class LookupAlertContextRequest(BaseModel):
    """Query parameters to resolve alert indicators to service context."""

    target_ip: Optional[str] = Field(default=None, description="Target IPv4 or IPv6 address")
    check_id: Optional[str] = Field(default=None, description="Monitoring or uptime check ID")
    service_hint: Optional[str] = Field(default=None, description="Service name hint from alert")


class LookupAlertContextResponse(BaseModel):
    """Response containing resolved alert context or empty result."""

    found: bool = Field(description="Whether alert context was resolved")
    context: Optional[AlertContext] = Field(
        default=None,
        description="Resolved alert context or null if not found",
    )


class DeploymentEvent(BaseModel):
    """Record of a workload deployment, release, or rollout event."""

    event_id: str = Field(description="Unique identifier for deployment event")
    timestamp: str = Field(description="ISO-8601 UTC timestamp of deployment")
    status: str = Field(default="SUCCESS", description="Deployment outcome status")

    service_name: Optional[str] = Field(default=None, description="Service name")
    workload_name: Optional[str] = Field(default=None, description="Workload name")
    workload: Optional[str] = Field(default=None, description="Alias for workload_name")
    cluster_name: Optional[str] = Field(default=None, description="Cluster identifier")
    cluster: Optional[str] = Field(default=None, description="Alias for cluster_name")
    namespace: Optional[str] = Field(default=None, description="Kubernetes namespace")
    environment: Optional[str] = Field(default="production", description="Deployment environment")

    deployment_tool: Optional[str] = Field(default=None, description="Deployment tool used")
    tool: Optional[str] = Field(default=None, description="Alias for deployment_tool")
    author: Optional[str] = Field(default=None, description="Deployer identity")
    deployer: Optional[str] = Field(default=None, description="Alias for author")
    revision: Optional[str] = Field(default=None, description="Revision or rollout version number")
    release: Optional[str] = Field(default=None, description="Release or package name")

    image: Optional[str] = Field(default=None, description="Deployed container image")
    container_images: List[str] = Field(
        default_factory=list,
        description="List of container images in the deployment",
    )
    pipeline_id: Optional[str] = Field(default=None, description="CI/CD pipeline run ID")
    git_commit: Optional[str] = Field(default=None, description="Git commit SHA")
    git_commit_message: Optional[str] = Field(default=None, description="Git commit message")
    change_summary: Optional[str] = Field(default=None, description="Summary of the change")

    @model_validator(mode="after")
    def sync_aliases(self) -> DeploymentEvent:
        if not self.cluster and self.cluster_name:
            self.cluster = self.cluster_name
        elif not self.cluster_name and self.cluster:
            self.cluster_name = self.cluster

        if not self.workload and self.workload_name:
            self.workload = self.workload_name
        elif not self.workload_name and self.workload:
            self.workload_name = self.workload

        if not self.tool and self.deployment_tool:
            self.tool = self.deployment_tool
        elif not self.deployment_tool and self.tool:
            self.deployment_tool = self.tool

        if not self.deployer and self.author:
            self.deployer = self.author
        elif not self.author and self.deployer:
            self.author = self.deployer

        if self.image and not self.container_images:
            self.container_images = [self.image]
        elif self.container_images and not self.image:
            self.image = self.container_images[0]

        return self


class GetRecentDeploymentsRequest(BaseModel):
    """Query parameters to fetch recent deployments for a workload."""

    workload: Optional[str] = Field(default=None, description="Target workload name")
    workload_name: Optional[str] = Field(default=None, description="Alias for workload")
    cluster: Optional[str] = Field(default=None, description="Cluster identifier")
    cluster_name: Optional[str] = Field(default=None, description="Alias for cluster")
    namespace: Optional[str] = Field(default=None, description="Kubernetes namespace")
    start_time: Optional[str] = Field(default=None, description="ISO-8601 start timestamp filter")
    end_time: Optional[str] = Field(default=None, description="ISO-8601 end timestamp filter")
    limit: int = Field(default=5, ge=1, le=20, description="Max events to return (1-20)")


class GetRecentDeploymentsResponse(BaseModel):
    """Response containing list of deployment events."""

    deployments: List[DeploymentEvent] = Field(
        default_factory=list,
        description="List of deployment events matching filters",
    )
    total_count: int = Field(
        default=0,
        description="Total count of matching events before pagination limit",
    )
