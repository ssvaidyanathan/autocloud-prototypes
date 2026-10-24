"""ConsoleLinkBuilder for canonical Google Cloud Console URLs."""

import re
from typing import Optional
import urllib.parse


class ConsoleLinkBuilder:
    """Builds canonical Google Cloud Console URLs with proper URL encoding and project/region context."""

    def __init__(self, project_id: str = "cloud-assist-fde-2", region: str = "us-west1"):
        self.project_id = str(project_id or "cloud-assist-fde-2").strip()
        self.region = str(region or "us-west1").strip()

    def build_dashboard_url(self) -> str:
        """GCP Project Console dashboard: https://console.cloud.google.com/home/dashboard?project=<project>"""
        return f"https://console.cloud.google.com/home/dashboard?project={self.project_id}"

    def build_incident_url(self, incident_id: Optional[str]) -> str:
        """Monitoring incident: https://console.cloud.google.com/monitoring/alerting/incidents/<incident_id>?project=<project>"""
        clean_id = str(incident_id or "").strip()
        if not clean_id or clean_id.lower() in ["none", "n/a", "unknown"]:
            return f"https://console.cloud.google.com/monitoring/alerting?project={self.project_id}"
        if clean_id.startswith("http://") or clean_id.startswith("https://"):
            return clean_id
        if "incidents/" in clean_id:
            clean_id = clean_id.split("incidents/")[-1].split("?")[0].strip()
        if clean_id.startswith("grp_") or clean_id.startswith("grp-"):
            return f"https://console.cloud.google.com/monitoring/alerting?project={self.project_id}"
        enc_id = urllib.parse.quote(clean_id, safe=".")
        return f"https://console.cloud.google.com/monitoring/alerting/incidents/{enc_id}?project={self.project_id}"

    def build_policy_url(self, policy_id_or_name: Optional[str]) -> str:
        """Alert policy: https://console.cloud.google.com/monitoring/alerting/policies/<policy_numeric_id>?project=<project>"""
        clean_id = str(policy_id_or_name or "").strip()
        if not clean_id or clean_id.lower() in ["none", "n/a", "unknown"]:
            return f"https://console.cloud.google.com/monitoring/alerting/policies?project={self.project_id}"
        if clean_id.startswith("http://") or clean_id.startswith("https://"):
            return clean_id
        numeric_id = None
        if "alertPolicies/" in clean_id:
            m = re.search(r"alertPolicies/(\d+)", clean_id)
            if m:
                numeric_id = m.group(1)
            else:
                tail = clean_id.split("alertPolicies/")[-1].split("?")[0].strip()
                if tail.isdigit():
                    numeric_id = tail
        elif clean_id.isdigit():
            numeric_id = clean_id

        if numeric_id:
            enc_id = urllib.parse.quote(numeric_id, safe="")
            return f"https://console.cloud.google.com/monitoring/alerting/policies/{enc_id}?project={self.project_id}"
        return f"https://console.cloud.google.com/monitoring/alerting/policies?project={self.project_id}"

    def build_cloud_run_service_url(self, service_name: Optional[str], tab: str = "metrics") -> str:
        """Cloud Run service: https://console.cloud.google.com/run/detail/<region>/<service>/metrics?project=<project> (revisions: .../run/detail/<region>/<service>/revisions?project=)"""
        clean_svc = str(service_name or "").strip()
        if not clean_svc or clean_svc.lower() in ["none", "n/a", "unknown"]:
            return f"https://console.cloud.google.com/run?project={self.project_id}"
        if "/" in clean_svc:
            clean_svc = clean_svc.split("/")[-1]
        enc_svc = urllib.parse.quote(clean_svc, safe="")
        enc_reg = urllib.parse.quote(self.region, safe="")
        tab_name = "revisions" if tab == "revisions" else "metrics"
        return f"https://console.cloud.google.com/run/detail/{enc_reg}/{enc_svc}/{tab_name}?project={self.project_id}"

    def build_cloud_sql_instance_url(self, instance_name: Optional[str]) -> str:
        """Cloud SQL: https://console.cloud.google.com/sql/instances/<instance>/overview?project=<project>"""
        clean_inst = str(instance_name or "").strip()
        if not clean_inst or clean_inst.lower() in ["none", "n/a", "unknown"]:
            return f"https://console.cloud.google.com/sql/instances?project={self.project_id}"
        if ":" in clean_inst:
            clean_inst = clean_inst.split(":")[-1]
        if "/" in clean_inst:
            clean_inst = clean_inst.split("/")[-1]
        enc_inst = urllib.parse.quote(clean_inst, safe="")
        return f"https://console.cloud.google.com/sql/instances/{enc_inst}/overview?project={self.project_id}"

    def build_apigee_proxy_url(self, proxy_name: Optional[str], revision: Optional[str] = None) -> str:
        """Apigee proxy: https://console.cloud.google.com/apigee/proxies/<proxy>/overview?project=<project> (deployments: .../apigee/proxies/<proxy>/develop/<revision>?project=)"""
        clean_proxy = str(proxy_name or "").strip()
        if not clean_proxy or clean_proxy.lower() in ["none", "n/a", "unknown"]:
            return f"https://console.cloud.google.com/apigee/proxies?project={self.project_id}"
        if "/" in clean_proxy:
            clean_proxy = clean_proxy.split("/")[-1]
        enc_proxy = urllib.parse.quote(clean_proxy, safe="")
        if revision:
            enc_rev = urllib.parse.quote(str(revision).strip(), safe="")
            return f"https://console.cloud.google.com/apigee/proxies/{enc_proxy}/develop/{enc_rev}?project={self.project_id}"
        return f"https://console.cloud.google.com/apigee/proxies/{enc_proxy}/overview?project={self.project_id}"

    def build_logs_explorer_url(
        self,
        query: Optional[str],
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> str:
        """Logs Explorer with a query: https://console.cloud.google.com/logs/query;query=<urlencoded filter>;timeRange=<start>%2F<end>?project=<project>"""
        clean_query = str(query or "").strip()
        if not clean_query:
            return f"https://console.cloud.google.com/logs/query?project={self.project_id}"
        enc_query = urllib.parse.quote(clean_query, safe="")
        time_part = ""
        if start_time and end_time:
            enc_start = urllib.parse.quote(str(start_time).strip(), safe="")
            enc_end = urllib.parse.quote(str(end_time).strip(), safe="")
            time_part = f";timeRange={enc_start}%2F{enc_end}"
        return f"https://console.cloud.google.com/logs/query;query={enc_query}{time_part}?project={self.project_id}"

    def build_resource_url(self, resource_name: Optional[str], res_type: Optional[str] = None) -> str:
        """Resolve canonical console URL for resource based on type or name."""
        r_name = str(resource_name or "").strip()
        if not r_name or r_name.lower() in ["none", "n/a", "unknown"]:
            return self.build_dashboard_url()
        r_lower = r_name.lower()
        t_lower = str(res_type or "").lower()

        if "sql" in t_lower or "database" in t_lower or "db" in r_lower:
            return self.build_cloud_sql_instance_url(r_name)
        elif "apigee" in t_lower or "proxy" in t_lower or "apigee" in r_lower:
            return self.build_apigee_proxy_url(r_name)
        else:
            return self.build_cloud_run_service_url(r_name, tab="metrics")

    def build_metrics_explorer_url(self, resource_name: Optional[str], policy_id: Optional[str] = None) -> str:
        """Metrics Explorer: link to policy or service page rather than hand-built MQL."""
        if policy_id:
            return self.build_policy_url(policy_id)
        return self.build_resource_url(resource_name)
