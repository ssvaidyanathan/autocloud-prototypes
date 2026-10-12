from datetime import datetime
from typing import Any, List, Optional

from google.adk.agents import LlmAgent
from google.adk.tools.function_tool import FunctionTool

from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident
from ..prompts import load_prompt
from ..tools.resources import (
    describe_cloud_run_service,
    describe_cloud_sql_instances,
    describe_compute_instances,
    describe_gke_clusters,
)
from .base import BaseSpecialist


class ResourcesSpecialist(BaseSpecialist):
    """Specialist evaluating compute and database resource configuration and health."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        client: Any = None,
        model: Optional[str] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[Any] = None,
    ) -> None:
        if not model:
            if config and hasattr(config, "model_specialist"):
                model = config.model_specialist
            else:
                try:
                    from ..config import get_settings

                    model = get_settings().model_specialist
                except Exception:
                    model = ""

        adk_tools: list[Any] = [
            FunctionTool(describe_cloud_run_service),
            FunctionTool(describe_gke_clusters),
            FunctionTool(describe_compute_instances),
            FunctionTool(describe_cloud_sql_instances),
        ]
        self.adk_tools = adk_tools
        instruction = load_prompt("resources")
        agent = llm_agent or LlmAgent(
            name="resources_specialist",
            model=model or "default",
            instruction=instruction,
            tools=adk_tools,
            output_schema=Findings,
        )
        super().__init__(
            name="ResourcesSpecialist",
            description="Inspects Cloud Run, GKE, Compute Engine, and Cloud SQL configurations.",
            project_id=project_id,
            client=client,
            llm_agent=agent,
            config=config,
        )

    def get_tools(self) -> List[Any]:
        return list(self.adk_tools)

    def run_investigation(
        self,
        incident: Incident,
        start_time: datetime,
        end_time: datetime,
        **kwargs: Any,
    ) -> Findings:
        queries_run: List[str] = []
        evidence: List[EvidenceItem] = []
        anomalies: List[str] = []
        errors: List[str] = []

        effective_service = incident.service_hint or (
            getattr(kwargs.get("plan"), "affected_service", None)
            if kwargs.get("plan")
            and getattr(kwargs.get("plan"), "affected_service", None) != "unknown-service"
            else None
        )
        service = effective_service or "service"

        # 1. Cloud Run service inspection if service hint provided
        if effective_service:
            try:
                from ..tools.common import get_candidate_service_names

                candidate_names = get_candidate_service_names(
                    effective_service, getattr(self.config, "name_prefix", None)
                )
                svc_desc = None
                resolved_cand = effective_service
                for cand in candidate_names:
                    try:
                        desc = describe_cloud_run_service(
                            project_id=self.project_id,
                            service_name=cand,
                            client=self.client,
                        )
                        if desc and (desc.get("uri") or desc.get("container_image")):
                            svc_desc = desc
                            resolved_cand = desc.get("service_name") or cand
                            break
                    except Exception:
                        pass

                if svc_desc is None:
                    svc_desc = describe_cloud_run_service(
                        project_id=self.project_id,
                        service_name=effective_service,
                        client=self.client,
                    )
                    resolved_cand = (
                        svc_desc.get("service_name") or effective_service or incident.service_hint
                    )

                queries_run.append(f"describe_cloud_run_service({resolved_cand})")

                # Check resource constraints
                mem_limit = svc_desc.get("memory_limit", "")
                if mem_limit and ("128M" in mem_limit or "256M" in mem_limit):
                    anomalies.append(
                        f"Constrained memory limit detected: {mem_limit} on {resolved_cand}"
                    )

                evidence.append(
                    self.create_evidence_item(
                        kind="cloud_run_service",
                        excerpt=(
                            f"Service {resolved_cand}: "
                            f"image={svc_desc.get('container_image')}, "
                            f"cpu={svc_desc.get('cpu_limit')}, mem={mem_limit}, "
                            f"env_vars={len(svc_desc.get('env_var_names', []))} vars configured"
                        ),
                        timestamp=start_time,
                        console_url=svc_desc.get("console_url"),
                    )
                )
            except Exception as e:
                errors.append(f"Failed to describe Cloud Run service: {str(e)}")

        # 2. GKE Clusters
        try:
            clusters = describe_gke_clusters(project_id=self.project_id, client=self.client)
            queries_run.append("describe_gke_clusters")
            for c in clusters:
                if c.get("status") not in ("RUNNING", "STATUS_UNSPECIFIED"):
                    anomalies.append(
                        f"GKE cluster '{c.get('name')}' is in non-running state: {c.get('status')}"
                    )
        except Exception as e:
            errors.append(f"Failed to describe GKE clusters: {str(e)}")

        # 3. Compute Engine Instances
        try:
            instances = describe_compute_instances(project_id=self.project_id, client=self.client)
            queries_run.append("describe_compute_instances")
            for inst in instances:
                if inst.get("status") not in ("RUNNING", "UNKNOWN"):
                    anomalies.append(
                        f"Compute VM '{inst.get('name')}' is in unexpected state: "
                        f"{inst.get('status')}"
                    )
        except Exception as e:
            errors.append(f"Failed to describe compute instances: {str(e)}")

        # 4. Cloud SQL Instances
        try:
            sql_instances = describe_cloud_sql_instances(
                project_id=self.project_id, client=self.client
            )
            queries_run.append("describe_cloud_sql_instances")
            for sql_inst in sql_instances:
                tier = sql_inst.get("tier", "")
                state = sql_inst.get("state", "")
                if state not in ("RUNNABLE", "STATE_UNSPECIFIED"):
                    anomalies.append(
                        f"Cloud SQL '{sql_inst.get('name')}' is in abnormal state: {state}"
                    )
                evidence.append(
                    self.create_evidence_item(
                        kind="cloud_sql",
                        excerpt=(
                            f"Cloud SQL {sql_inst.get('name')}: "
                            f"{sql_inst.get('database_version')}, "
                            f"tier={tier}, state={state}"
                        ),
                        timestamp=start_time,
                        console_url=sql_inst.get("console_url"),
                    )
                )
        except Exception as e:
            errors.append(f"Failed to describe Cloud SQL instances: {str(e)}")

        confidence = 0.70 if anomalies else 0.50
        summary = (
            f"Resources analysis for {service}: checked infrastructure, found "
            f"{len(anomalies)} anomalies and {len(evidence)} evidence items."
        )

        return self.create_findings(
            summary=summary,
            evidence=evidence,
            anomalies=anomalies,
            queries_run=queries_run,
            confidence=confidence,
            errors=errors,
        )
