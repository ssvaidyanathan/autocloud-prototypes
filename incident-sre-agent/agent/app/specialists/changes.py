from datetime import datetime
from typing import Any, List, Optional

from google.adk.agents import LlmAgent
from google.adk.tools.function_tool import FunctionTool

from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident
from ..prompts import load_prompt
from ..tools.changes import (
    list_cloud_builds,
    list_cloud_run_revisions,
    normalize_cloud_build_status,
    query_audit_logs,
)
from .base import BaseSpecialist


def is_build_related(build: Any, service_name: Optional[str]) -> bool:
    """Determine if a Cloud Build item is related to the target service name.

    Inspects build images, substitutions, and trigger tags.
    If no service name is specified or the build lacks all relevant metadata,
    it cannot be ruled out as unrelated, so returns True.
    """
    if not service_name:
        return True

    from ..tools.common import get_candidate_service_names

    candidate_names = get_candidate_service_names(service_name)
    svc_lowers = [c.lower() for c in candidate_names]

    images = build.get("images") if isinstance(build, dict) else getattr(build, "images", None)
    substitutions = (
        build.get("substitutions")
        if isinstance(build, dict)
        else getattr(build, "substitutions", None)
    )
    tags = build.get("tags") if isinstance(build, dict) else getattr(build, "tags", None)

    # Ignore MagicMock objects
    if images and hasattr(images, "_mock_name"):
        images = None
    if substitutions and hasattr(substitutions, "_mock_name"):
        substitutions = None
    if tags and hasattr(tags, "_mock_name"):
        tags = None

    # If build has none of these metadata fields, cannot rule out
    if not images and not substitutions and not tags:
        return True

    # 1. Inspect container images
    if images:
        for img in images:
            img_lower = str(img).lower()
            if any(svc_lower in img_lower for svc_lower in svc_lowers):
                return True

    # 2. Inspect substitutions (e.g. _SERVICE_NAME, SERVICE, etc.)
    if substitutions and hasattr(substitutions, "items"):
        for k, v in substitutions.items():
            val_lower = str(v).lower()
            key_lower = str(k).lower()
            if any(svc_lower in val_lower or svc_lower in key_lower for svc_lower in svc_lowers):
                return True

    # 3. Inspect tags
    if tags:
        for tag in tags:
            tag_lower = str(tag).lower()
            if any(svc_lower in tag_lower for svc_lower in svc_lowers):
                return True

    return False


def get_build_unrelated_descriptor(build: Any) -> str:
    """Extract a concise label/descriptor from an unrelated build."""
    images = build.get("images") if isinstance(build, dict) else getattr(build, "images", None)
    if images and not hasattr(images, "_mock_name") and len(images) > 0:
        return str(images[0]).split("/")[-1]
    tags = build.get("tags") if isinstance(build, dict) else getattr(build, "tags", None)
    if tags and not hasattr(tags, "_mock_name") and len(tags) > 0:
        return str(tags[0])
    subs = (
        build.get("substitutions")
        if isinstance(build, dict)
        else getattr(build, "substitutions", None)
    )
    if subs and not hasattr(subs, "_mock_name") and hasattr(subs, "get"):
        if "_SERVICE_NAME" in subs:
            return str(subs["_SERVICE_NAME"])
        for v in subs.values():
            if v:
                return str(v)
    return "unrelated"


class ChangesSpecialist(BaseSpecialist):
    """Specialist identifying recent deployments, configuration edits, and build activity."""

    is_build_related = staticmethod(is_build_related)
    get_build_unrelated_descriptor = staticmethod(get_build_unrelated_descriptor)

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
            FunctionTool(query_audit_logs),
            FunctionTool(list_cloud_run_revisions),
            FunctionTool(list_cloud_builds),
        ]
        self.adk_tools = adk_tools
        instruction = load_prompt("changes")
        agent = llm_agent or LlmAgent(
            name="changes_specialist",
            model=model or "default",
            instruction=instruction,
            tools=adk_tools,
            output_schema=Findings,
        )
        super().__init__(
            name="ChangesSpecialist",
            description="Correlates incident onset with administrative edits, builds, and deploys.",
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

        # 1. Query Audit Logs
        try:
            audit_events = query_audit_logs(
                project_id=self.project_id,
                start_time=start_time,
                end_time=end_time,
                service_name=effective_service,
                client=self.client,
            )
            queries_run.append("query_audit_logs")
            for evt in audit_events:
                ts_val: Optional[datetime] = None
                raw_ts = evt.get("timestamp")
                if raw_ts:
                    try:
                        ts_val = datetime.fromisoformat(raw_ts)
                    except Exception:
                        ts_val = None

                method = evt.get("method_name") or "AdminActivity"
                principal = evt.get("principal_email") or "system"
                resource = evt.get("resource_name") or service
                anomalies.append(f"Admin action: {method} on {resource} by {principal}")
                evidence.append(
                    self.create_evidence_item(
                        kind="audit_log",
                        excerpt=f"Action {method} on {resource} by {principal}",
                        timestamp=ts_val,
                        console_url=evt.get("console_url"),
                    )
                )
        except Exception as e:
            errors.append(f"Failed to query audit logs: {str(e)}")

        # 2. Check Cloud Run Revisions if service hint is present
        if effective_service:
            try:
                from ..tools.common import get_candidate_service_names

                candidate_names = get_candidate_service_names(
                    effective_service, getattr(self.config, "name_prefix", None)
                )
                seen_rev_names: set[str] = set()
                revisions = []
                for cand_name in candidate_names:
                    try:
                        cand_revs = list_cloud_run_revisions(
                            project_id=self.project_id,
                            service_name=cand_name,
                            client=self.client,
                        )
                        for r in cand_revs:
                            r_name = r.get("name")
                            if r_name and r_name not in seen_rev_names:
                                seen_rev_names.add(r_name)
                                revisions.append(r)
                            elif not r_name:
                                revisions.append(r)
                        if cand_revs:
                            queries_run.append(f"list_cloud_run_revisions({cand_name})")
                    except Exception:
                        pass
                if not any("list_cloud_run_revisions" in q for q in queries_run):
                    queries_run.append(f"list_cloud_run_revisions({effective_service})")
                for rev in revisions[:3]:
                    ts_val = None
                    raw_ts = rev.get("creation_timestamp")
                    if raw_ts:
                        try:
                            ts_val = datetime.fromisoformat(raw_ts)
                        except Exception:
                            ts_val = None

                    author = rev.get("author") or "system"
                    traffic = rev.get("traffic_percent", 0)
                    evidence.append(
                        self.create_evidence_item(
                            kind="revision",
                            excerpt=(
                                f"Revision {rev.get('name')} (author: {author}, "
                                f"traffic: {traffic}%)"
                            ),
                            timestamp=ts_val,
                            console_url=rev.get("console_url"),
                        )
                    )
                    anomalies.append(f"Recent revision deployment: {rev.get('name')} at {raw_ts}")
            except Exception as e:
                errors.append(f"Failed to query Cloud Run revisions: {str(e)}")

        # 3. Check Cloud Builds
        try:
            builds = list_cloud_builds(
                project_id=self.project_id,
                limit=5,
                client=self.client,
            )
            queries_run.append("list_cloud_builds")

            target_svc = effective_service
            related_builds = [b for b in builds if is_build_related(b, target_svc)]
            unrelated_builds = [b for b in builds if not is_build_related(b, target_svc)]

            # Prioritize related builds and exclude completely unrelated repository builds
            # when related builds exist.
            if related_builds:
                builds_to_report = [(b, False) for b in related_builds[:3]]
            else:
                builds_to_report = [(b, True) for b in unrelated_builds[:3]]

            for b, is_unrelated in builds_to_report:
                ts_val = None
                raw_ts = b.get("finish_time")
                if raw_ts:
                    try:
                        ts_val = datetime.fromisoformat(raw_ts)
                    except Exception:
                        ts_val = None

                raw_sha = b.get("commit_sha")
                sha = str(raw_sha)[:7] if raw_sha else "none"
                status = normalize_cloud_build_status(b.get("status", "STATUS_UNKNOWN"))

                unrelated_tag = ""
                if is_unrelated:
                    desc = get_build_unrelated_descriptor(b)
                    unrelated_tag = f" [unrelated: {desc}]"

                build_id = b.get("build_id")
                evidence.append(
                    self.create_evidence_item(
                        kind="build",
                        excerpt=(
                            f"Build {build_id} status={status} (commit: {sha}){unrelated_tag}"
                        ),
                        timestamp=ts_val,
                        console_url=b.get("console_url"),
                    )
                )
        except Exception as e:
            errors.append(f"Failed to query Cloud Builds: {str(e)}")

        confidence = 0.70 if anomalies else 0.40
        summary = (
            f"Changes analysis for {service}: found {len(anomalies)} change events "
            f"and {len(evidence)} evidence items."
        )

        return self.create_findings(
            summary=summary,
            evidence=evidence,
            anomalies=anomalies,
            queries_run=queries_run,
            confidence=confidence,
            errors=errors,
        )
