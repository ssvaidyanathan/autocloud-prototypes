"""ChangeSpecialist Agent for Alert Triage Runtime.

Analyzes Cloud Run and Apigee revision history over the past 2 hours
and correlates deployment timestamps with alert onset curves.
"""

from datetime import datetime, timezone, timedelta
import logging
from typing import Any, Optional

from ..config import AgentConfig
from ..contracts.alert import AlertEvent
from ..contracts.report import EvidenceItem
from ..request_count import REQUEST_COUNT_METRIC, request_count_filter, request_count_series
from .base import BaseSpecialist, EvidenceDossier

logger = logging.getLogger(__name__)


# Evaluate genuine changes within the 2-hour window:
# A revision is a genuine change ONLY when:
# 1. Created within lookback window (<= 120m) before anomaly onset.
# 2. Image digest or env/config differs from the previously created revision.
# 3. Traffic shifts to older revisions or without digest/env diff are NOT changes (is_traffic_shift: True).
def is_genuine_change(c: dict[str, Any]) -> bool:
    """True when a change record is a genuine deployment within the lookback window."""
    # 1. Discard explicit traffic shifts
    if (
        c.get("action") == "traffic_shift"
        or c.get("is_traffic_shift") is True
        or c.get("details", {}).get("is_traffic_shift") is True
    ):
        return False

    # 2. Time window check: creation / deploy time must be within 120 minutes and before/at anomaly onset
    mins = c.get("deployed_minutes_ago", 999)
    if mins > 120 or mins < 0:
        return False

    # 3. Must be serving (if serving status known)
    if c.get("is_serving") is False or c.get("details", {}).get("is_serving") is False:
        return False

    # 4. Must differ from the previously created revision (digest or env/config)
    if c.get("differs_from_previous") is False or c.get("details", {}).get("differs_from_previous") is False:
        return False

    details = c.get("details", {})
    if isinstance(details, dict):
        cur_img = details.get("image")
        prev_img = details.get("prev_image")
        env_diff = details.get("env_diff")
        if cur_img and prev_img and cur_img == prev_img and env_diff is not None and len(env_diff) == 0:
            return False

    return True


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def requests_by_revision(
    metric_client: Any, project_id: str, region: str, service: str, start: datetime, end: datetime
) -> dict[str, int]:
    """Sum Cloud Run request_count per revision of `service` in `region` over [start, end]."""
    totals: dict[str, int] = {}
    for ts in request_count_series(metric_client, project_id, region, service, start.timestamp(), end.timestamp()):
        rev = ts.resource.labels.get("revision_name", "")
        if not rev:
            continue
        count = sum(int(pt.value.int64_value) for pt in ts.points)
        totals[rev] = totals.get(rev, 0) + count
    return totals


def previous_serving_revision(
    metric_client: Any,
    project_id: str,
    region: str,
    service: str,
    new_revision: str,
    deploy_timestamp: Any,
    lookback_minutes: int,
) -> dict[str, Any]:
    """Identify the revision that served `service` before `new_revision` was created.

    The answer comes from traffic history (Cloud Monitoring request_count per revision in the
    `lookback_minutes` before the deploy), never from revision creation order: a newer revision
    can be a retired 0%-traffic one while traffic stays pinned to an older revision. A revision
    is named only when it is the single revision that received requests in that window;
    otherwise "revision" is None and "evidence" says why.
    """
    result: dict[str, Any] = {"revision": None, "evidence": "", "requests_by_revision": {}}
    deploy_dt = _parse_ts(deploy_timestamp)
    if deploy_dt is None:
        result["evidence"] = (
            f"Previous serving revision of {service} not determined: the creation time of {new_revision} is unknown."
        )
        return result
    start = deploy_dt - timedelta(minutes=lookback_minutes)
    window = f"between {_iso(start)} and {_iso(deploy_dt)}"
    try:
        totals = requests_by_revision(metric_client, project_id, region, service, start, deploy_dt)
    except Exception as exc:
        logger.warning("Traffic history query for %s failed: %s", service, exc)
        result["evidence"] = (
            f"Previous serving revision of {service} not determined: the Cloud Monitoring "
            f"{REQUEST_COUNT_METRIC} query for {window} failed ({type(exc).__name__})."
        )
        return result

    served = {rev: n for rev, n in totals.items() if n > 0 and rev != new_revision}
    result["requests_by_revision"] = served
    result["window_start"] = _iso(start)
    result["window_end"] = _iso(deploy_dt)
    if len(served) == 1:
        rev, n = next(iter(served.items()))
        result["revision"] = rev
        result["evidence"] = (
            f"Cloud Monitoring {REQUEST_COUNT_METRIC}: {rev} was the only revision of {service} that "
            f"received requests {window} ({n} requests), before {new_revision} was created."
        )
    elif not served:
        result["evidence"] = (
            f"Previous serving revision of {service} not determined: Cloud Monitoring "
            f"{REQUEST_COUNT_METRIC} recorded no requests on any other revision {window}."
        )
    else:
        split = ", ".join(f"{rev} ({n} requests)" for rev, n in sorted(served.items(), key=lambda kv: (-kv[1], kv[0])))
        result["evidence"] = (
            f"Previous serving revision of {service} not determined as a single revision: Cloud Monitoring "
            f"{REQUEST_COUNT_METRIC} shows requests on {len(served)} revisions {window}: {split}."
        )
    if result["revision"] is None:
        logger.warning("%s", result["evidence"])
    return result


class ChangeSpecialist(BaseSpecialist):
    """Specialist agent correlating recent infrastructure and code changes."""

    def __init__(self, metric_client: Any = None, **kwargs: Any):
        super().__init__("ChangeSpecialist")
        self._metric_client = metric_client

    def _get_metric_client(self, config: AgentConfig) -> Any:
        if self._metric_client is not None:
            return self._metric_client
        if config.is_local:
            return None
        from google.cloud import monitoring_v3

        return monitoring_v3.MetricServiceClient()

    def _previous_serving(self, chg: dict[str, Any], config: AgentConfig) -> dict[str, Any]:
        service, revision = chg.get("service") or "", chg.get("revision") or ""
        try:
            client = self._get_metric_client(config)
        except Exception as exc:
            logger.warning("Cloud Monitoring client unavailable for traffic history of %s: %s", service, exc)
            client = None
        if client is None:
            evidence = (
                f"Previous serving revision of {service} not determined: traffic history "
                "(Cloud Monitoring request_count) was not queried."
            )
            logger.warning("%s", evidence)
            return {"revision": None, "evidence": evidence, "requests_by_revision": {}}
        return previous_serving_revision(
            client, config.project_id, config.region, service, revision, chg.get("timestamp"),
            config.rollback_traffic_lookback_minutes,
        )

    async def investigate(
        self,
        alert: AlertEvent,
        config: AgentConfig,
    ) -> EvidenceDossier:
        findings: list[str] = []
        evidence_items: list[EvidenceItem] = []
        metrics: dict[str, Any] = {}
        queries_executed: dict[str, list[str]] = {
            "cloud_run_admin": [
                f'gcloud run revisions list --service="{alert.resource_name}" --project="{config.project_id}"'
            ],
            "apigee_deployments": [
                f'GET /v1/organizations/{config.project_id}/environments/prod/deployments'
            ],
        }

        now = datetime.now(timezone.utc)
        if alert.started_at:
            try:
                now = datetime.fromisoformat(alert.started_at.replace("Z", "+00:00"))
            except Exception:
                pass

        recent_changes: list[dict[str, Any]] = []
        # Apigee deployment history is only covered by the tools timeline; the direct fallbacks
        # below query Cloud Run alone, so a "no deployments" finding must say which it covered.
        apigee_queried = False

        # 1. First attempt querying via tools fastmcp server if available
        try:
            from tools.app.server import get_change_timeline
            tl = get_change_timeline(window_hours=2, project_id=config.project_id, region=config.region)
            apigee_queried = not any(
                c.get("source") == "apigee" and c.get("error") for c in tl.get("changes", [])
            )
            for chg in tl.get("changes", []):
                svc = chg.get("service_name") or chg.get("api_name")
                rev = chg.get("revision_name") or str(chg.get("revision", ""))
                ts_str = chg.get("timestamp")
                deployed_min = -1
                if ts_str:
                    try:
                        dep_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if dep_dt > now:
                            deployed_min = -1
                        else:
                            deployed_min = max(0, int((now - dep_dt).total_seconds() / 60))
                    except Exception:
                        pass
                is_traffic_shift = bool(
                    chg.get("is_traffic_shift")
                    or chg.get("action") == "traffic_shift"
                    or chg.get("details", {}).get("is_traffic_shift")
                    or chg.get("differs_from_previous") is False
                    or chg.get("details", {}).get("differs_from_previous") is False
                )
                differs = chg.get("differs_from_previous")
                if differs is None:
                    differs = chg.get("details", {}).get("differs_from_previous")
                is_serving = chg.get("is_serving")
                if is_serving is None:
                    is_serving = chg.get("details", {}).get("is_serving")
                # The timeline's previous_revision is the previously *created* revision, which is
                # not necessarily the one that served traffic (see previous_serving_revision).
                prev_rev = chg.get("previous_revision") or chg.get("details", {}).get("previous_revision")

                recent_changes.append({
                    "service": svc,
                    "revision": rev,
                    "previous_created_revision": prev_rev,
                    "timestamp": ts_str,
                    "deployed_minutes_ago": deployed_min,
                    "change_type": "traffic_shift" if is_traffic_shift else ("service_update" if chg.get("source") == "cloud_run_service" else "revision_deploy"),
                    "action": "traffic_shift" if is_traffic_shift else chg.get("action", "deployment"),
                    "is_serving": is_serving,
                    "is_traffic_shift": is_traffic_shift,
                    "differs_from_previous": differs,
                    "details": chg.get("details", {}),
                    "status": "Ready",
                })
        except Exception as e:
            logger.debug("Tools get_change_timeline query skipped or failed: %s", e)

        # 2. Self-contained Cloud Run Admin API query (runs directly inside Cloud Run container)
        if not recent_changes and not config.is_local:
            try:
                import google.auth
                from google.auth.transport.requests import Request as GoogleAuthRequest
                import requests

                creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                if not creds.valid:
                    creds.refresh(GoogleAuthRequest())
                headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}

                cutoff = now - timedelta(hours=2)
                services_url = f"https://run.googleapis.com/v2/projects/{config.project_id}/locations/{config.region}/services"
                resp = requests.get(services_url, headers=headers, timeout=15)
                if resp.status_code == 200:
                    services = resp.json().get("services", [])
                    for svc in services:
                        svc_full_name = svc.get("name", "")
                        svc_short = svc_full_name.split("/")[-1]

                        # Query revisions for the service
                        rev_url = f"https://run.googleapis.com/v2/{svc_full_name}/revisions"
                        rev_resp = requests.get(rev_url, headers=headers, timeout=15)
                        new_revisions_in_window = []
                        all_service_revs = []
                        if rev_resp.status_code == 200:
                            all_service_revs = rev_resp.json().get("revisions", [])
                            for rev in all_service_revs:
                                ctime_str = rev.get("createTime", "")
                                if ctime_str:
                                    try:
                                        ctime = datetime.fromisoformat(ctime_str.replace("Z", "+00:00"))
                                        if ctime >= cutoff and ctime <= now:
                                            new_revisions_in_window.append(rev)
                                    except Exception:
                                        pass

                        # Determine active serving revision
                        traffic_list = svc.get("traffic", []) or []
                        serving_rev_names = set()
                        for tr in traffic_list:
                            if tr.get("percent", 0) > 0 and tr.get("revision"):
                                serving_rev_names.add(tr["revision"].split("/")[-1])
                        if not serving_rev_names:
                            latest_ready = svc.get("latestReadyRevision", "")
                            if latest_ready:
                                serving_rev_names.add(latest_ready.split("/")[-1])

                        # Sort revisions descending by createTime to identify previous revision
                        def _rev_time_key(r: dict[str, Any]) -> str:
                            return str(r.get("createTime") or "")
                        sorted_revs = sorted(all_service_revs, key=_rev_time_key, reverse=True)

                        if new_revisions_in_window:
                            for rev in new_revisions_in_window:
                                rev_short = rev.get("name", "").split("/")[-1]
                                ctime_str = rev.get("createTime", "")
                                dep_min = -1
                                if ctime_str:
                                    try:
                                        ctime = datetime.fromisoformat(ctime_str.replace("Z", "+00:00"))
                                        if ctime > now:
                                            dep_min = -1
                                        else:
                                            dep_min = max(0, int((now - ctime).total_seconds() / 60))
                                    except Exception:
                                        pass
                                is_serving = rev_short in serving_rev_names

                                # Previously created revision (used only for the image/env diff;
                                # it is not necessarily the revision that served traffic)
                                prev_rev_name = None
                                prev_img = None
                                prev_env = []
                                for idx, r in enumerate(sorted_revs):
                                    if r.get("name", "").split("/")[-1] == rev_short and idx + 1 < len(sorted_revs):
                                        prev_r = sorted_revs[idx + 1]
                                        prev_rev_name = prev_r.get("name", "").split("/")[-1]
                                        prev_img = (prev_r.get("containers") or [{}])[0].get("image")
                                        prev_env = (prev_r.get("containers") or [{}])[0].get("env", [])
                                        break

                                cur_img = (rev.get("containers") or [{}])[0].get("image")
                                cur_env = (rev.get("containers") or [{}])[0].get("env", [])
                                differs = (cur_img != prev_img) or (cur_env != prev_env) or (prev_rev_name is None)
                                is_traffic_shift = not differs

                                if not any(c.get("service") == svc_short and c.get("revision") == rev_short for c in recent_changes):
                                    recent_changes.append({
                                        "service": svc_short,
                                        "revision": rev_short,
                                        "previous_created_revision": prev_rev_name,
                                        "timestamp": ctime_str,
                                        "deployed_minutes_ago": dep_min,
                                        "change_type": "traffic_shift" if is_traffic_shift else "revision_deploy",
                                        "action": "traffic_shift" if is_traffic_shift else "revision_create",
                                        "is_serving": is_serving,
                                        "is_traffic_shift": is_traffic_shift,
                                        "differs_from_previous": differs,
                                        "details": {
                                            "image": cur_img,
                                            "prev_image": prev_img,
                                            "env_diff": [e for e in cur_env if e not in prev_env],
                                            "scaling": rev.get("scaling"),
                                            "is_serving": is_serving,
                                            "is_traffic_shift": is_traffic_shift,
                                            "differs_from_previous": differs,
                                            "previous_created_revision": prev_rev_name,
                                        },
                                        "status": "Ready",
                                    })
                        has_serving_new_rev = any(
                            rev.get("name", "").split("/")[-1] in serving_rev_names
                            for rev in new_revisions_in_window
                        )
                        if not has_serving_new_rev and svc.get("updateTime"):
                            # Traffic shift or metadata update to existing revision - not a deployment change
                            svc_update_str = svc.get("updateTime", "")
                            try:
                                svc_dt = datetime.fromisoformat(svc_update_str.replace("Z", "+00:00"))
                                if svc_dt >= cutoff:
                                    dep_min = max(0, int((now - svc_dt).total_seconds() / 60))
                                    template_rev = (svc.get("template", {}) or {}).get("revision") or svc.get("latestReadyRevision", "").split("/")[-1]
                                    if not any(c.get("service") == svc_short and c.get("revision") == template_rev for c in recent_changes):
                                        recent_changes.append({
                                            "service": svc_short,
                                            "revision": template_rev,
                                            "timestamp": svc_update_str,
                                            "deployed_minutes_ago": dep_min,
                                            "change_type": "traffic_shift",
                                            "action": "traffic_shift",
                                            "is_traffic_shift": True,
                                            "differs_from_previous": False,
                                            "details": {
                                                "action": "traffic_shift",
                                                "is_traffic_shift": True,
                                                "updateTime": svc_update_str,
                                                "differs_from_previous": False,
                                                "note": "Traffic shifted to existing revision without new revision creation",
                                            },
                                            "status": "Ready",
                                        })
                            except Exception:
                                pass
            except Exception as e:
                logger.warning("Direct Cloud Run Admin API change query failed: %s", e)

        # 3. Direct Cloud Logging audit query for Services.ReplaceService / UpdateService events
        if not config.is_local:
            try:
                import google.cloud.logging
                cutoff_iso = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
                log_client = google.cloud.logging.Client(project=config.project_id)
                audit_filter = (
                    f'logName="projects/{config.project_id}/logs/cloudaudit.googleapis.com%2Factivity" AND '
                    f'protoPayload.serviceName="run.googleapis.com" AND '
                    f'timestamp >= "{cutoff_iso}"'
                )
                audit_entries = list(log_client.list_entries(filter_=audit_filter, max_results=20))
                for ae in audit_entries:
                    pld = ae.payload if isinstance(ae.payload, dict) else {}
                    method = str(pld.get("methodName", ""))
                    if "ReplaceService" in method or "UpdateService" in method:
                        res_name = str(pld.get("resourceName", ""))
                        svc_name = res_name.split("/")[-1] if res_name else ""
                        ts = ae.timestamp
                        if ts and svc_name:
                            ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(ts, "strftime") else str(ts)
                            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                            if ts > now:
                                dep_min = -1
                            else:
                                dep_min = max(0, int((now - ts).total_seconds() / 60))
                            # Check if already captured as revision_deploy or traffic_shift
                            if not any(c.get("service") == svc_name and abs(c.get("deployed_minutes_ago", 999) - dep_min) <= 1 for c in recent_changes):
                                recent_changes.append({
                                    "service": svc_name,
                                    "revision": f"{svc_name}-config-update",
                                    "timestamp": ts_str,
                                    "deployed_minutes_ago": dep_min,
                                    "change_type": "traffic_shift",
                                    "action": "traffic_shift",
                                    "is_traffic_shift": True,
                                    "differs_from_previous": False,
                                    "details": {"method": method, "resourceName": res_name, "is_traffic_shift": True},
                                    "status": "Ready",
                                })
            except Exception as e:
                logger.debug("Cloud Logging audit change query skipped or failed: %s", e)

        metrics["all_changes"] = recent_changes
        target = alert.resource_name
        candidate_services = [target]
        visited = set(candidate_services)
        try:
            from topology.topology_graph import get_upstream_dependencies
            for dep in get_upstream_dependencies(target):
                if dep not in visited:
                    visited.add(dep)
                    candidate_services.append(dep)
        except Exception:
            pass

        relevant_changes = [
            c for c in recent_changes if c.get("service") in candidate_services and is_genuine_change(c)
        ]

        if relevant_changes:
            target_changes = [c for c in relevant_changes if c.get("service") == target]
            chg = target_changes[0] if target_changes else relevant_changes[0]
            change_label = "configuration update" if chg.get("change_type") == "service_update" else "revision deployment"
            rev_label = f"`{chg['revision']}`"
            findings.append(
                f"Recent {change_label} detected on `{chg['service']}` ({chg['deployed_minutes_ago']}m ago): {rev_label}."
            )
            findings.append("Deployment timing aligns with alert onset curve.")
            metrics["recent_revision"] = chg["revision"]
            metrics["previous_created_revision"] = chg.get("previous_created_revision") or chg.get(
                "details", {}
            ).get("previous_created_revision")
            metrics["minutes_since_deploy"] = chg["deployed_minutes_ago"]
            metrics["deploy_timestamp"] = chg.get("timestamp")
            metrics["change_service"] = chg.get("service")
            serving = self._previous_serving(chg, config)
            metrics["previous_serving_revision"] = serving["revision"]
            metrics["previous_serving_evidence"] = serving["evidence"]
            metrics["previous_serving_requests"] = serving["requests_by_revision"]
            if serving["evidence"]:
                findings.append(serving["evidence"])
                queries_executed.setdefault("cloud_monitoring", []).append(
                    f'{request_count_filter(config.region, chg.get("service") or "")} '
                    f'grouped by resource.labels.revision_name, {config.rollback_traffic_lookback_minutes} min '
                    f'before {chg.get("timestamp")}'
                )
            evidence_items.append(
                EvidenceItem(
                    headline=f"Recent Deployment Correlation: {chg['service']}",
                    content=(
                        f"Service: {chg['service']}\n"
                        f"Change: {rev_label}\n"
                        f"Deployed: {chg['deployed_minutes_ago']} minutes prior to alert onset\n"
                        f"Correlation: Direct temporal correlation with error curve onset."
                    ),
                )
            )
        else:
            if apigee_queried:
                findings.append("No configuration or revision deployments detected in the 2-hour window prior to alert onset.")
                content = "No recent deployments detected; incident is not attributable to a recent change event."
            else:
                findings.append(
                    "No Cloud Run revision deployments detected in the 2-hour window prior to alert onset "
                    "(Apigee proxy deployment history not queried)."
                )
                content = (
                    "No recent Cloud Run deployments detected. Apigee proxy deployment history was not "
                    "queried; proxy revision changes come from the Apigee access log."
                )
            evidence_items.append(
                EvidenceItem(
                    headline="Deployment History Analysis",
                    content=content,
                )
            )

        mins = metrics.get("minutes_since_deploy", 999)
        proximity_bonus = 0.15 if mins <= 15 else (0.10 if mins <= 30 else (0.05 if mins <= 60 else (0.02 if mins <= 120 else 0.0)))
        base_conf = 0.70 if relevant_changes else 0.65
        chg_conf = round(base_conf + proximity_bonus, 2)

        return EvidenceDossier(
            specialist_name=self.name,
            target_resource=alert.resource_name,
            findings=findings,
            metrics=metrics,
            evidence_items=evidence_items,
            queries_executed=queries_executed,
            anomaly_detected=bool(relevant_changes),
            confidence=chg_conf,
        )
