from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent.app.tools.changes import (
    diff_revisions,
    list_cloud_builds,
    list_cloud_run_revisions,
    query_audit_logs,
)
from agent.app.tools.common import get_candidate_service_names
from agent.app.tools.logs import (
    _is_probe_or_infra_noise,
    aggregate_requests,
    list_log_names,
    query_logs,
    query_request_logs,
    summarize_error_groups,
)
from agent.app.tools.metrics import (
    compare_to_baseline,
    list_alert_policies,
    list_open_alerts,
    query_time_series,
)
from agent.app.tools.resources import (
    describe_cloud_run_service,
    describe_cloud_sql_instances,
    describe_compute_instances,
    describe_gke_clusters,
    get_platform_events,
)

# ============================================================================
# Logs Tools Tests
# ============================================================================


def test_query_logs_limit_clamping() -> None:
    # Test that max_entries > 200 is clamped to 200
    res = query_logs(
        filter_str="severity >= ERROR",
        resource_names=["projects/test-proj"],
        max_entries=500,
    )
    assert res["limit_applied"] == 200
    assert "https://console.cloud.google.com/logs/query" in res["console_url"]
    assert "test-proj" in res["console_url"]


def test_query_logs_with_mock_client() -> None:
    mock_client = MagicMock()
    entry1 = MagicMock()
    entry1.timestamp = datetime(2026, 10, 9, 1, 0, 0, tzinfo=timezone.utc)
    entry1.severity = "ERROR"
    entry1.payload = "A" * 300  # Longer than 256 chars, should be truncated
    entry1.log_name = "projects/test-proj/logs/app"
    entry1.resource.type = "cloud_run_revision"

    mock_client.list_entries.return_value = [entry1]

    start = datetime(2026, 10, 9, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)

    res = query_logs(
        filter_str="httpRequest.status=500",
        resource_names=["projects/test-proj"],
        max_entries=20,
        start_time=start,
        end_time=end,
        client=mock_client,
    )

    assert res["total_returned"] == 1
    assert len(res["entries"]) == 1
    assert len(res["entries"][0]["payload_excerpt"]) <= 256
    assert res["entries"][0]["payload_excerpt"].endswith("...")
    assert res["entries"][0]["severity"] == "ERROR"


def test_summarize_error_groups() -> None:
    mock_client = MagicMock()
    grp = MagicMock()
    grp.group_id = "grp-123"
    grp.message = "NullPointerException in catalog handler"
    grp.count = 42
    grp.affected_services = ["shop-api"]
    grp.first_seen = "2026-10-09T01:00:00Z"
    grp.last_seen = "2026-10-09T02:00:00Z"

    mock_client.list_group_stats.return_value = [grp]

    res = summarize_error_groups(project_id="test-proj", time_window_hours=2, client=mock_client)
    assert res["total_groups"] == 1
    assert res["error_groups"][0]["group_id"] == "grp-123"
    assert res["error_groups"][0]["count"] == 42
    assert "https://console.cloud.google.com/errors?project=test-proj" in res["console_url"]


def test_list_log_names() -> None:
    mock_client = MagicMock()
    mock_client.list_logs.return_value = [
        "cloudaudit.googleapis.com%2Factivity",
        "run.googleapis.com%2Fstdout",
    ]
    logs = list_log_names(project_id="test-proj", client=mock_client)
    assert len(logs) == 2
    assert "cloudaudit.googleapis.com%2Factivity" in logs


# ============================================================================
# Metrics Tools Tests
# ============================================================================


def test_query_time_series() -> None:
    mock_client = MagicMock()
    ts = MagicMock()
    ts.metric.type = "run.googleapis.com/request_count"
    pt = MagicMock()
    pt.interval.end_time = datetime(2026, 10, 9, 1, 30, 0, tzinfo=timezone.utc)
    pt.value.double_value = 15.5
    ts.points = [pt]
    mock_client.list_time_series.return_value = [ts]

    start = datetime(2026, 10, 9, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)

    res = query_time_series(
        filter_str='metric.type = "run.googleapis.com/request_count"',
        project_id="test-proj",
        start_time=start,
        end_time=end,
        client=mock_client,
    )

    assert res["total_points"] == 1
    assert res["points"][0]["value"] == 15.5
    assert "monitoring/metrics-explorer" in res["console_url"]


def test_compare_to_baseline() -> None:
    mock_client = MagicMock()

    # Current period: high values [100.0, 120.0, 110.0]
    ts_curr = MagicMock()
    pts_curr = []
    for v in [100.0, 120.0, 110.0]:
        p = MagicMock()
        p.value.double_value = v
        pts_curr.append(p)
    ts_curr.points = pts_curr

    # Baseline period: low values [10.0, 12.0, 11.0]
    ts_base = MagicMock()
    pts_base = []
    for v in [10.0, 12.0, 11.0]:
        p = MagicMock()
        p.value.double_value = v
        pts_base.append(p)
    ts_base.points = pts_base

    mock_client.list_time_series.side_effect = [[ts_curr], [ts_base]]

    t1 = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 9, 3, 0, 0, tzinfo=timezone.utc)
    b1 = datetime(2026, 10, 9, 1, 0, 0, tzinfo=timezone.utc)
    b2 = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)

    res = compare_to_baseline(
        filter_str="metric.type=run.googleapis.com/request_count",
        project_id="test-proj",
        current_start=t1,
        current_end=t2,
        baseline_start=b1,
        baseline_end=b2,
        client=mock_client,
    )

    assert res["is_anomalous"] is True
    assert res["mean_change_percent"] > 800.0
    assert res["current_period"]["mean"] == 110.0
    assert res["baseline_period"]["mean"] == 11.0


def test_list_alert_policies_and_open_alerts() -> None:
    mock_client = MagicMock()
    pol = MagicMock()
    pol.name = "projects/test-proj/alertPolicies/123"
    pol.display_name = "HTTP 5xx Spike"
    pol.enabled = True
    cond = MagicMock()
    cond.name = "cond-1"
    cond.display_name = "5xx error rate > 5%"
    cond.condition_threshold.filter = "metric.type=run.googleapis.com/request_count"
    pol.conditions = [cond]
    mock_client.list_alert_policies.return_value = [pol]

    policies = list_alert_policies(project_id="test-proj", client=mock_client)
    assert len(policies) == 1
    assert policies[0]["display_name"] == "HTTP 5xx Spike"
    assert policies[0]["conditions"][0]["display_name"] == "5xx error rate > 5%"

    inc = MagicMock()
    inc.incident_id = "inc-456"
    inc.policy_name = "HTTP 5xx Spike"
    inc.condition_name = "5xx error rate > 5%"
    inc.started_at = datetime(2026, 10, 9, 2, 15, 0, tzinfo=timezone.utc)
    inc.summary = "5xx error rate exceeded threshold"
    inc.state = "OPEN"
    mock_client.list_incidents.return_value = [inc]

    alerts = list_open_alerts(project_id="test-proj", client=mock_client)
    assert len(alerts) == 1
    assert alerts[0]["incident_id"] == "inc-456"
    assert alerts[0]["state"] == "OPEN"


# ============================================================================
# Changes Tools Tests
# ============================================================================


def test_query_audit_logs() -> None:
    mock_client = MagicMock()
    entry = MagicMock()
    entry.timestamp = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    entry.proto_payload = {
        "methodName": "google.cloud.run.v1.Services.ReplaceService",
        "authenticationInfo": {"principalEmail": "deployer@example.com"},
        "resourceName": "services/shop-api",
        "status": {"code": 0},
    }
    mock_client.list_entries.return_value = [entry]

    start = datetime(2026, 10, 9, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 9, 3, 0, 0, tzinfo=timezone.utc)

    logs = query_audit_logs(
        project_id="test-proj",
        start_time=start,
        end_time=end,
        service_name="shop-api",
        client=mock_client,
    )
    assert len(logs) == 1
    assert logs[0]["method_name"] == "google.cloud.run.v1.Services.ReplaceService"
    assert logs[0]["principal_email"] == "deployer@example.com"
    assert logs[0]["status_code"] == 0


def test_list_cloud_run_revisions() -> None:
    mock_client = MagicMock()
    rev = MagicMock()
    rev.name = "projects/test-proj/locations/us-central1/services/shop-api/revisions/shop-api-00042"
    rev.create_time = datetime(2026, 10, 9, 2, 5, 0, tzinfo=timezone.utc)
    rev.author = "ci-bot@example.com"
    rev.traffic_percent = 100
    rev.template.containers = [MagicMock(image="gcr.io/test-proj/shop-api:v2.1")]
    mock_client.list_revisions.return_value = [rev]

    revs = list_cloud_run_revisions(
        project_id="test-proj",
        service_name="shop-api",
        client=mock_client,
    )
    assert len(revs) == 1
    assert revs[0]["name"] == "shop-api-00042"
    assert revs[0]["traffic_percent"] == 100
    assert revs[0]["image"] == "gcr.io/test-proj/shop-api:v2.1"


def test_list_cloud_builds() -> None:
    mock_client = MagicMock()
    b = MagicMock()
    b.id = "build-abc-123"
    b.status = "SUCCESS"
    b.create_time = datetime(2026, 10, 9, 1, 50, 0, tzinfo=timezone.utc)
    b.finish_time = datetime(2026, 10, 9, 1, 55, 0, tzinfo=timezone.utc)
    b.source_provenance.resolved_repo_source.commit_sha = "abcd1234efgh"
    b.log_url = "https://storage.googleapis.com/logs/build-abc-123.txt"
    mock_client.list_builds.return_value = [b]

    builds = list_cloud_builds(project_id="test-proj", limit=5, client=mock_client)
    assert len(builds) == 1
    assert builds[0]["build_id"] == "build-abc-123"
    assert builds[0]["commit_sha"] == "abcd1234efgh"
    assert builds[0]["status"] == "SUCCESS"


# ============================================================================
# Resources Tools Tests (CRITICAL Security Rule: Redacting env values)
# ============================================================================


def test_describe_cloud_run_service_redacts_env_values() -> None:
    mock_client = MagicMock()
    svc = MagicMock()
    svc.uri = "https://shop-api-xyz.a.run.app"
    svc.ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

    template = MagicMock()
    template.service_account = "agent-sa@test-proj.iam.gserviceaccount.com"
    template.max_instance_request_concurrency = 80

    container = MagicMock()
    container.image = "gcr.io/test-proj/shop-api:v2.1"
    container.resources.limits = {"cpu": "2", "memory": "1Gi"}

    # Container env contains secret/config VALUES that MUST NEVER be exposed!
    container.env = [
        {"name": "DATABASE_URL", "value": "postgres://user:SUPER_SECRET_PASSWORD@db:5432/db"},
        {"name": "API_SECRET_KEY", "value": "sk_live_very_secret_token_12345"},
        {"name": "PORT", "value": "8080"},
    ]
    template.containers = [container]
    svc.template = template
    mock_client.get_service.return_value = svc

    res = describe_cloud_run_service(
        project_id="test-proj",
        service_name="shop-api",
        client=mock_client,
    )

    # Assert variable NAMES are preserved
    assert res["env_var_names"] == ["DATABASE_URL", "API_SECRET_KEY", "PORT"]

    # CRITICAL: Assert secret VALUES do NOT exist anywhere in the output!
    res_str = str(res)
    assert "SUPER_SECRET_PASSWORD" not in res_str
    assert "sk_live_very_secret_token_12345" not in res_str
    assert "postgres://" not in res_str
    assert "value" not in res


def test_describe_gke_compute_and_sql_instances() -> None:
    mock_client = MagicMock()

    # GKE
    cluster = MagicMock()
    cluster.name = "prod-cluster"
    cluster.status = "RUNNING"
    cluster.current_node_count = 6
    cluster.location = "us-central1"
    mock_client.list_clusters.return_value = [cluster]

    clusters = describe_gke_clusters(project_id="test-proj", client=mock_client)
    assert len(clusters) == 1
    assert clusters[0]["name"] == "prod-cluster"
    assert clusters[0]["status"] == "RUNNING"

    # Compute Engine
    inst = MagicMock()
    inst.name = "worker-node-1"
    inst.status = "RUNNING"
    inst.machine_type = "zones/us-central1-a/machineTypes/e2-standard-4"
    mock_client.list_instances.return_value = [inst]

    instances = describe_compute_instances(project_id="test-proj", client=mock_client)
    assert len(instances) == 1
    assert instances[0]["name"] == "worker-node-1"
    assert instances[0]["machine_type"] == "e2-standard-4"

    # Cloud SQL
    sql_inst = MagicMock()
    sql_inst.name = "shop-db"
    sql_inst.database_version = "POSTGRES_15"
    sql_inst.settings.tier = "db-custom-4-16384"
    sql_inst.state = "RUNNABLE"
    sql_inst.region = "us-central1"
    mock_client.list_instances.return_value = [sql_inst]

    sql_instances = describe_cloud_sql_instances(project_id="test-proj", client=mock_client)
    assert len(sql_instances) == 1
    assert sql_instances[0]["name"] == "shop-db"
    assert sql_instances[0]["tier"] == "db-custom-4-16384"
    assert sql_instances[0]["state"] == "RUNNABLE"


def test_get_candidate_service_names() -> None:
    assert get_candidate_service_names(None) == []
    assert get_candidate_service_names("") == []
    assert get_candidate_service_names("shop-api") == ["shop-api"]
    assert get_candidate_service_names("shop-api", prefix="acme") == [
        "acme-shop-api",
        "shop-api",
    ]
    assert get_candidate_service_names("acme-shop-api", prefix="acme") == [
        "acme-shop-api",
        "shop-api",
    ]


def test_summarize_error_groups_filters_infra_noise() -> None:
    mock_client = MagicMock()

    # Noise error groups
    grp_k8s = MagicMock()
    grp_k8s.group_id = "grp-k8s"
    grp_k8s.message = (
        "Get http://localnodeip:10255/api/v1/namespaces/kube-system/services: dial tcp error"
    )
    grp_k8s.count = 100
    grp_k8s.affected_services = ["kube-system"]
    grp_k8s.first_seen = "2026-10-09T01:00:00Z"
    grp_k8s.last_seen = "2026-10-09T02:00:00Z"

    grp_prom = MagicMock()
    grp_prom.group_id = "grp-prom"
    grp_prom.message = "Scrape failed: prometheus server unreachable in gmp-system"
    grp_prom.count = 50
    grp_prom.affected_services = ["gmp-system"]
    grp_prom.first_seen = "2026-10-09T01:00:00Z"
    grp_prom.last_seen = "2026-10-09T02:00:00Z"

    # App error group
    grp_app = MagicMock()
    grp_app.group_id = "grp-app"
    grp_app.message = "DatabaseConnectionError: pool exhausted on /checkout"
    grp_app.count = 25
    grp_app.affected_services = ["acme-shop-api"]
    grp_app.first_seen = "2026-10-09T01:00:00Z"
    grp_app.last_seen = "2026-10-09T02:00:00Z"

    mock_client.list_group_stats.return_value = [grp_k8s, grp_prom, grp_app]

    res = summarize_error_groups(
        project_id="test-proj",
        candidate_services=["acme-shop-api", "shop-api"],
        client=mock_client,
    )
    assert res["total_groups"] == 1
    assert res["error_groups"][0]["group_id"] == "grp-app"
    assert res["error_groups"][0]["count"] == 25


def test_get_candidate_service_names_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAME_PREFIX", "acme")
    assert get_candidate_service_names("shop-api") == ["acme-shop-api", "shop-api"]
    assert get_candidate_service_names("acme-shop-api") == ["acme-shop-api", "shop-api"]
    # Explicit prefix overrides env
    assert get_candidate_service_names("shop-api", prefix="novartis") == [
        "novartis-shop-api",
        "shop-api",
    ]


def test_is_probe_or_infra_noise_keywords() -> None:
    keywords = [
        "cni_network_config",
        "containerd",
        "podsandbox",
        "containerstatus",
        "shim",
        "reconciling subsys=sfc",
        "gke-system-balloon",
        "cni",
        "cri",
        "kube",
        "gke",
        "cilium",
        "leases",
        "coordination.k8s.io",
        "localnodeip",
        "subsys=sfc",
        "probe",
        "healthz",
    ]
    for kw in keywords:
        assert _is_probe_or_infra_noise(f"Prefix message with {kw} and suffix") is True
        assert _is_probe_or_infra_noise(f"Prefix message with {kw.upper()} and suffix") is True


def test_summarize_error_groups_unrelated_errors_and_generic_error() -> None:
    mock_client = MagicMock()

    # Unrelated service error with generic "error" in message
    grp_unrelated = MagicMock()
    grp_unrelated.group_id = "grp-unrelated"
    grp_unrelated.message = "Unhandled error occurred in auth-service handler"
    grp_unrelated.count = 40
    grp_unrelated.affected_services = ["auth-service"]
    grp_unrelated.first_seen = "2026-10-09T01:00:00Z"
    grp_unrelated.last_seen = "2026-10-09T02:00:00Z"

    # App error matching candidate service
    grp_app = MagicMock()
    grp_app.group_id = "grp-app"
    grp_app.message = "DatabaseConnectionError: pool exhausted on /checkout"
    grp_app.count = 25
    grp_app.affected_services = ["acme-shop-api"]
    grp_app.first_seen = "2026-10-09T01:00:00Z"
    grp_app.last_seen = "2026-10-09T02:00:00Z"

    mock_client.list_group_stats.return_value = [grp_unrelated, grp_app]

    res = summarize_error_groups(
        project_id="test-proj",
        candidate_services=["acme-shop-api", "shop-api"],
        client=mock_client,
    )
    # The unrelated error group must be filtered out
    assert res["total_groups"] == 1
    assert res["error_groups"][0]["group_id"] == "grp-app"


def test_query_audit_logs_filters_k8s_coordination_and_enforces_service_matching() -> None:
    mock_client = MagicMock()

    # 1. K8s coordination / lease event
    entry_lease = MagicMock()
    entry_lease.timestamp = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    entry_lease.proto_payload = {
        "methodName": "io.k8s.coordination.v1.leases.renew",
        "resourceName": "coordination.k8s.io/namespaces/kube-node-lease/leases/node-1",
        "status": {"code": 0},
    }

    # 2. Unrelated service event
    entry_unrelated = MagicMock()
    entry_unrelated.timestamp = datetime(2026, 10, 9, 2, 1, 0, tzinfo=timezone.utc)
    entry_unrelated.proto_payload = {
        "methodName": "google.cloud.run.v1.Services.ReplaceService",
        "resourceName": "namespaces/test-proj/services/unrelated-service",
        "status": {"code": 0},
    }

    # 3. Matching candidate service event
    entry_valid = MagicMock()
    entry_valid.timestamp = datetime(2026, 10, 9, 2, 2, 0, tzinfo=timezone.utc)
    entry_valid.proto_payload = {
        "methodName": "google.cloud.run.v1.Services.ReplaceService",
        "resourceName": "namespaces/test-proj/services/acme-shop-api",
        "authenticationInfo": {"principalEmail": "deployer@example.com"},
        "status": {"code": 0},
    }

    mock_client.list_entries.return_value = [entry_lease, entry_unrelated, entry_valid]

    start = datetime(2026, 10, 9, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 9, 3, 0, 0, tzinfo=timezone.utc)

    logs = query_audit_logs(
        project_id="test-proj",
        start_time=start,
        end_time=end,
        service_name="shop-api",
        client=mock_client,
    )

    # Only entry_valid should pass
    assert len(logs) == 1
    assert logs[0]["resource_name"] == "namespaces/test-proj/services/acme-shop-api"
    assert logs[0]["method_name"] == "google.cloud.run.v1.Services.ReplaceService"


def test_query_logs_filters_gke_infra_and_balloon_noise() -> None:
    mock_client = MagicMock()

    # Noise entries that must be suppressed
    e_balloon = MagicMock()
    e_balloon.timestamp = datetime(2026, 10, 10, 2, 0, 0, tzinfo=timezone.utc)
    e_balloon.severity = "ERROR"
    e_balloon.payload = "Failed to sync balloon pod: gke-system-balloon-pod-h4b68"
    e_balloon.log_name = "projects/test-proj/logs/k8s-pod"

    e_containerd = MagicMock()
    e_containerd.timestamp = datetime(2026, 10, 10, 2, 1, 0, tzinfo=timezone.utc)
    e_containerd.severity = "ERROR"
    e_containerd.payload = "containerd runtime error: failed to create podsandbox"
    e_containerd.log_name = "projects/test-proj/logs/containerd"

    e_cni = MagicMock()
    e_cni.timestamp = datetime(2026, 10, 10, 2, 2, 0, tzinfo=timezone.utc)
    e_cni.severity = "WARNING"
    e_cni.payload = "cni_network_config setup timed out"
    e_cni.log_name = "projects/test-proj/logs/cni"

    # Legitimate Cloud Run application error that must be kept
    e_app = MagicMock()
    e_app.timestamp = datetime(2026, 10, 10, 2, 3, 0, tzinfo=timezone.utc)
    e_app.severity = "ERROR"
    e_app.payload = "Scenario 3 (Bad Release): Cannot process checkout; missing PAYMENT_GATEWAY_KEY"
    e_app.log_name = "projects/test-proj/logs/run.googleapis.com%2Fstdout"

    mock_client.list_entries.return_value = [e_balloon, e_containerd, e_cni, e_app]

    res = query_logs(
        filter_str="severity >= WARNING",
        resource_names=["projects/test-proj"],
        client=mock_client,
    )

    assert res["total_returned"] == 1
    assert "missing PAYMENT_GATEWAY_KEY" in res["entries"][0]["payload_excerpt"]


def test_query_audit_logs_filters_gke_autoscaler_and_infra_noise() -> None:
    mock_client = MagicMock()

    # 1. Cluster autoscaler event
    entry_autoscaler = MagicMock()
    entry_autoscaler.timestamp = datetime(2026, 10, 10, 2, 0, 0, tzinfo=timezone.utc)
    entry_autoscaler.proto_payload = {
        "methodName": "compute.instances.insert",
        "resourceName": "projects/test-proj/zones/us-central1-a/instances/gke-system-node",
        "serviceName": "system:cluster-autoscaler",
        "authenticationInfo": {
            "principalEmail": "system:serviceaccount:kube-system:cluster-autoscaler"
        },
        "status": {"code": 0},
    }

    # 2. Network controller event
    entry_anet = MagicMock()
    entry_anet.timestamp = datetime(2026, 10, 10, 2, 1, 0, tzinfo=timezone.utc)
    entry_anet.proto_payload = {
        "methodName": "networking.k8s.io.networkpolicies.create",
        "resourceName": "namespaces/kube-system/networkpolicies/default",
        "serviceName": "anet-controller-manager",
        "status": {"code": 0},
    }

    # 3. Legitimate application change event
    entry_app = MagicMock()
    entry_app.timestamp = datetime(2026, 10, 10, 2, 2, 0, tzinfo=timezone.utc)
    entry_app.proto_payload = {
        "methodName": "google.cloud.run.v1.Services.ReplaceService",
        "resourceName": "namespaces/test-proj/services/shop-api",
        "serviceName": "run.googleapis.com",
        "authenticationInfo": {"principalEmail": "deployer@example.com"},
        "status": {"code": 0},
    }

    mock_client.list_entries.return_value = [entry_autoscaler, entry_anet, entry_app]

    logs = query_audit_logs(
        project_id="test-proj",
        start_time=datetime(2026, 10, 10, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 10, 10, 3, 0, 0, tzinfo=timezone.utc),
        service_name="shop-api",
        client=mock_client,
    )

    assert len(logs) == 1
    assert logs[0]["resource_name"] == "namespaces/test-proj/services/shop-api"
    assert logs[0]["principal_email"] == "deployer@example.com"


# ============================================================================
# Core New Tools Tests (Item 8.2)
# ============================================================================


def test_query_request_logs_aggregation() -> None:
    mock_client = MagicMock()

    def make_entry(status: int, method: str, url: str, latency: str, ts: datetime) -> MagicMock:
        entry = MagicMock()
        entry.timestamp = ts
        entry.http_request = {
            "status": status,
            "requestMethod": method,
            "requestUrl": url,
            "latency": latency,
        }
        return entry

    base_time = datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc)
    mock_entries = [
        make_entry(200, "GET", "https://shop-api.run.app/checkout", "0.100s", base_time),
        make_entry(200, "GET", "https://shop-api.run.app/checkout", "0.200s", base_time),
        make_entry(500, "POST", "https://shop-api.run.app/checkout", "0.500s", base_time),
        make_entry(403, "GET", "https://shop-api.run.app/admin", "0.050s", base_time),
        make_entry(200, "GET", "https://shop-api.run.app/health", "0.010s", base_time),
    ]
    mock_client.list_entries.return_value = mock_entries

    res = query_request_logs(
        service_name="shop-api",
        project_id="test-proj",
        client=mock_client,
    )

    assert res["service_name"] == "shop-api"
    assert res["total_requests"] == 5
    assert res["status_breakdown"] == {"2xx": 3, "3xx": 0, "4xx": 1, "5xx": 1}

    # Route breakdown
    assert "/checkout" in res["route_breakdown"]
    assert res["route_breakdown"]["/checkout"]["count"] == 3
    assert res["route_breakdown"]["/checkout"]["errors"] == 1

    assert "/admin" in res["route_breakdown"]
    assert res["route_breakdown"]["/admin"]["count"] == 1
    assert res["route_breakdown"]["/admin"]["errors"] == 1

    assert "/health" in res["route_breakdown"]
    assert res["route_breakdown"]["/health"]["count"] == 1
    assert res["route_breakdown"]["/health"]["errors"] == 0

    # Latencies (10ms, 50ms, 100ms, 200ms, 500ms)
    assert res["latency_p50_ms"] == 100.0
    assert res["latency_p95_ms"] > 200.0
    assert res["latency_p99_ms"] >= res["latency_p95_ms"]
    assert len(res["sample_entries"]) == 5
    assert "https://console.cloud.google.com/logs/query" in res["console_url"]


def test_query_request_logs_status_class_and_route_filtering() -> None:
    mock_client = MagicMock()

    def make_entry(status: int, url: str) -> MagicMock:
        entry = MagicMock()
        entry.timestamp = datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc)
        entry.http_request = {
            "status": status,
            "requestMethod": "GET",
            "requestUrl": url,
            "latency": "0.050s",
        }
        return entry

    mock_client.list_entries.return_value = [
        make_entry(200, "https://shop-api.run.app/checkout"),
        make_entry(403, "https://shop-api.run.app/checkout"),
        make_entry(500, "https://shop-api.run.app/cart"),
    ]

    # Filter by 403 status class
    res_403 = query_request_logs(
        service_name="shop-api",
        project_id="test-proj",
        status_class="403",
        client=mock_client,
    )
    assert res_403["total_requests"] == 1
    assert res_403["status_breakdown"]["4xx"] == 1

    # Filter by route
    res_cart = query_request_logs(
        service_name="shop-api",
        project_id="test-proj",
        route="/cart",
        client=mock_client,
    )
    assert res_cart["total_requests"] == 1
    assert res_cart["status_breakdown"]["5xx"] == 1


def test_query_request_logs_empty() -> None:
    mock_client = MagicMock()
    mock_client.list_entries.return_value = []

    res = query_request_logs(
        service_name="shop-api",
        project_id="test-proj",
        client=mock_client,
    )
    assert res["total_requests"] == 0
    assert res["latency_p50_ms"] == 0.0
    assert res["latency_p95_ms"] == 0.0
    assert res["route_breakdown"] == {}
    assert res["sample_entries"] == []


def test_aggregate_requests_alias() -> None:
    mock_client = MagicMock()
    mock_client.list_entries.return_value = []

    res = aggregate_requests(
        service_name="shop-api",
        project_id="test-proj",
        client=mock_client,
    )
    assert res["total_requests"] == 0
    assert res["service_name"] == "shop-api"


def test_diff_revisions_full_diff_and_zero_secret_leakage() -> None:
    mock_client = MagicMock()

    # Revision A (Baseline stable revision)
    rev_a = MagicMock()
    container_a = MagicMock()
    container_a.image = "gcr.io/test-proj/shop-api:v1.0"
    container_a.resources.limits = {"cpu": "1", "memory": "256Mi"}
    # Env vars with sensitive secrets
    container_a.env = [
        {"name": "DATABASE_URL", "value": "postgres://user:SUPER_SECRET_PASS@db:5432/shop"},
        {"name": "GATEWAY_API_KEY", "value": "AIzaSySecretKeyValue12345"},
        {"name": "PORT", "value": "8080"},
    ]
    probe_a = MagicMock()
    probe_a.http_get.path = "/health"
    probe_a.http_get.port = 8080
    probe_a.period_seconds = 10
    probe_a.timeout_seconds = 3
    probe_a.failure_threshold = 3
    container_a.readiness_probe = probe_a

    rev_a.template.containers = [container_a]
    rev_a.template.service_account = "sa-old@test-proj.iam.gserviceaccount.com"
    rev_a.template.max_instance_request_concurrency = 80

    # Revision B (New revision with changes and regressions)
    rev_b = MagicMock()
    container_b = MagicMock()
    container_b.image = "gcr.io/test-proj/shop-api:v2.0"
    container_b.resources.limits = {"cpu": "2", "memory": "512Mi"}
    container_b.env = [
        {"name": "DATABASE_URL", "value": "postgres://user:DIFFERENT_SECRET@db:5432/shop"},
        {"name": "PORT", "value": "8080"},
        {"name": "FEATURE_V2_ENABLED", "value": "true"},
    ]
    probe_b = MagicMock()
    probe_b.http_get.path = "/readyz"
    probe_b.http_get.port = 8080
    probe_b.period_seconds = 5
    probe_b.timeout_seconds = 2
    probe_b.failure_threshold = 2
    container_b.readiness_probe = probe_b

    rev_b.template.containers = [container_b]
    rev_b.template.service_account = "sa-new@test-proj.iam.gserviceaccount.com"
    rev_b.template.max_instance_request_concurrency = 100

    def mock_get_revision(name: str) -> MagicMock:
        if "rev-a" in name:
            return rev_a
        return rev_b

    mock_client.get_revision.side_effect = mock_get_revision

    diff = diff_revisions(
        service_name="shop-api",
        rev_a="rev-a",
        rev_b="rev-b",
        project_id="test-proj",
        client=mock_client,
    )

    assert diff["service_name"] == "shop-api"
    assert diff["rev_a"] == "rev-a"
    assert diff["rev_b"] == "rev-b"

    diffs = diff["differences"]
    assert diffs["image_changed"] is True
    assert diffs["image_details"]["rev_a"] == "gcr.io/test-proj/shop-api:v1.0"
    assert diffs["image_details"]["rev_b"] == "gcr.io/test-proj/shop-api:v2.0"

    # Environment variable names ONLY
    assert diffs["env_vars_added"] == ["FEATURE_V2_ENABLED"]
    assert diffs["env_vars_removed"] == ["GATEWAY_API_KEY"]

    # CRITICAL SECURITY RULE: Zero secret leakage assertion!
    raw_output_str = str(diff)
    assert "SUPER_SECRET_PASS" not in raw_output_str
    assert "DIFFERENT_SECRET" not in raw_output_str
    assert "AIzaSySecretKeyValue12345" not in raw_output_str

    # Limits diff
    assert diffs["limits_changed"] is True
    assert diffs["limits_details"]["rev_a"]["cpu"] == "1"
    assert diffs["limits_details"]["rev_b"]["cpu"] == "2"

    # Probes diff
    assert diffs["probes_changed"] is True
    assert diffs["probes_details"]["rev_a"]["readiness_probe"]["path"] == "/health"
    assert diffs["probes_details"]["rev_b"]["readiness_probe"]["path"] == "/readyz"

    # Service Account diff
    assert diffs["service_account_changed"] is True
    assert diffs["service_account_details"]["rev_a"] == "sa-old@test-proj.iam.gserviceaccount.com"
    assert diffs["service_account_details"]["rev_b"] == "sa-new@test-proj.iam.gserviceaccount.com"


def test_diff_revisions_no_changes() -> None:
    mock_client = MagicMock()

    rev = MagicMock()
    container = MagicMock()
    container.image = "gcr.io/test-proj/shop-api:v1.0"
    container.resources.limits = {"cpu": "1", "memory": "256Mi"}
    container.env = [{"name": "PORT", "value": "8080"}]
    container.readiness_probe = None
    container.liveness_probe = None
    container.startup_probe = None
    rev.template.containers = [container]
    rev.template.service_account = "sa@test-proj.iam.gserviceaccount.com"
    rev.template.max_instance_request_concurrency = 80

    mock_client.get_revision.return_value = rev

    diff = diff_revisions(
        service_name="shop-api",
        rev_a="rev-1",
        rev_b="rev-1",
        project_id="test-proj",
        client=mock_client,
    )

    diffs = diff["differences"]
    assert diffs["image_changed"] is False
    assert diffs["env_vars_added"] == []
    assert diffs["env_vars_removed"] == []
    assert diffs["limits_changed"] is False
    assert diffs["probes_changed"] is False
    assert diffs["service_account_changed"] is False
    assert "Image unchanged" in diff["diff_summary"]


def test_get_platform_events_oom_detection() -> None:
    mock_client = MagicMock()

    entry_oom = MagicMock()
    entry_oom.timestamp = datetime(2026, 10, 15, 10, 30, 0, tzinfo=timezone.utc)
    entry_oom.text_payload = (
        "Memory limit of 256Mi exceeded with 257Mi used. "
        "Terminated by kernel OOM-killer (exit code 137)"
    )
    mock_client.list_entries.return_value = [entry_oom]

    res = get_platform_events(
        service_name="shop-api",
        project_id="test-proj",
        client=mock_client,
    )

    assert res["service_name"] == "shop-api"
    assert res["total_events"] == 1
    assert res["has_oom"] is True
    assert res["has_probe_failure"] is False
    assert res["restarts"]["count"] == 1
    assert res["restarts"]["events"][0]["exit_code"] == 137
    assert "OOM termination" in res["restarts"]["events"][0]["reason"]


def test_get_platform_events_probe_failures() -> None:
    mock_client = MagicMock()

    entry_probe = MagicMock()
    entry_probe.timestamp = datetime(2026, 10, 15, 10, 35, 0, tzinfo=timezone.utc)
    entry_probe.text_payload = (
        "Readiness probe failed for container shop-api on path /readyz: "
        "HTTP 503 Service Unavailable"
    )
    mock_client.list_entries.return_value = [entry_probe]

    res = get_platform_events(
        service_name="shop-api",
        project_id="test-proj",
        client=mock_client,
    )

    assert res["total_events"] == 1
    assert res["has_oom"] is False
    assert res["has_probe_failure"] is True
    assert res["probe_failures"]["count"] == 1
    assert res["probe_failures"]["events"][0]["probe_type"] == "readiness"


def test_get_platform_events_clean_zero_events() -> None:
    mock_client = MagicMock()
    mock_client.list_entries.return_value = []

    res = get_platform_events(
        service_name="shop-api",
        project_id="test-proj",
        client=mock_client,
    )

    assert res["total_events"] == 0
    assert res["has_oom"] is False
    assert res["has_probe_failure"] is False
    assert res["restarts"]["count"] == 0
    assert res["probe_failures"]["count"] == 0
    assert res["system_events"]["count"] == 0


def test_query_request_logs_bounded_pagination() -> None:
    mock_client = MagicMock()

    # Mock list_entries returning generator with more than effective_limit entries
    def entry_generator() -> Any:
        for _ in range(150):
            entry = MagicMock()
            entry.httpRequest = {"status": 200, "requestUrl": "/api/items", "latency": "0.010s"}
            yield entry

    mock_client.list_entries.return_value = entry_generator()

    res = query_request_logs(
        service_name="shop-api",
        project_id="test-proj",
        max_entries=25,
        client=mock_client,
    )
    assert res["total_requests"] == 25
    # Verify max_results passed in call
    kwargs = mock_client.list_entries.call_args.kwargs
    assert kwargs.get("max_results") == 25
    assert kwargs.get("page_size") == 25


def test_diff_revisions_multi_container() -> None:
    mock_client = MagicMock()

    # Revision with sidecar container
    rev = MagicMock()
    c1 = MagicMock()
    c1.image = "gcr.io/test-proj/shop-app:v1.0"
    c1.resources.limits = {"cpu": "1", "memory": "256Mi"}
    c1.env = [{"name": "APP_PORT"}, {"name": "APP_ENV"}]
    c1.readiness_probe = None

    c2 = MagicMock()
    c2.image = "gcr.io/test-proj/sidecar-proxy:v1.0"
    c2.resources.limits = {"cpu": "0.5", "memory": "128Mi"}
    c2.env = [{"name": "SIDECAR_CONFIG"}]
    probe = MagicMock()
    probe.http_get.path = "/healthz"
    probe.http_get.port = 9090
    probe.period_seconds = 10
    c2.readiness_probe = probe

    rev.template.containers = [c1, c2]
    rev.template.service_account = "sa@test-proj.iam.gserviceaccount.com"
    rev.template.max_instance_request_concurrency = 80
    mock_client.get_revision.return_value = rev

    diff = diff_revisions(
        service_name="shop-api",
        rev_a="rev-1",
        rev_b="rev-1",
        project_id="test-proj",
        client=mock_client,
    )
    # Check that both container images and all env vars were aggregated
    assert "shop-app:v1.0" in diff["differences"]["image_details"]["rev_a"]
    assert "sidecar-proxy:v1.0" in diff["differences"]["image_details"]["rev_a"]
    assert diff["differences"]["limits_details"]["rev_a"]["cpu"] == "1, 0.5"
    assert diff["differences"]["limits_details"]["rev_a"]["memory"] == "256Mi, 128Mi"
    assert diff["differences"]["probes_details"]["rev_a"]["readiness_probe"]["path"] == "/healthz"


def test_get_platform_events_client_disambiguation() -> None:
    # Caller passes a non-logging client (e.g. ServicesClient without list_entries)
    services_client = MagicMock(spec=["get_service", "list_services"])
    mock_logging_client = MagicMock()
    mock_logging_client.list_entries.return_value = []

    res = get_platform_events(
        service_name="shop-api",
        project_id="test-proj",
        client=services_client,
        logging_client=mock_logging_client,
    )
    assert res["total_events"] == 0
    assert mock_logging_client.list_entries.called
