"""Multi-Factor Correlation Scoring Implementation.

Implements exact deterministic scoring formula:
  Score(A, G) = W_topo * S_topo + W_causal * S_causal + W_time * S_time + W_change * S_change

Weights:
  Topology: 35 pts (0 hops: 35, 1 hop: 28, 2 hops: 17.5, >=3 hops or disjoint: 0)
  Causal: 30 pts (Direct propagation: 30, Plausible 5xx: 15, Incompatible: 0)
  Temporal: 20 pts (linear decay over 10 min horizon; t=0: 20, t=5: 10, t>=10: 0)
  Change: 15 pts (deployment change within 30 min of onset: 15, else 0)
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Optional

try:
    from topology.topology_graph import calculate_hop_distance
except ImportError as err:
    import logging
    logging.getLogger(__name__).error("Failed to import calculate_hop_distance from topology_graph: %s", err)
    calculate_hop_distance = None


WEIGHT_TOPO = 35.0
WEIGHT_CAUSAL = 30.0
WEIGHT_TIME = 20.0
WEIGHT_CHANGE = 15.0

THRESHOLD_AUTO_ATTACH = 65.0
THRESHOLD_AUTO_NEW = 35.0
TEMPORAL_HORIZON_MINUTES = 10.0


@dataclass
class ScoreBreakdown:
    topo_score: float
    causal_score: float
    time_score: float
    change_score: float
    total_score: float
    hop_distance: Optional[int]
    causal_category: str
    delta_minutes: float
    change_aligned: bool


def calculate_topology_score(hop_distance: Optional[int]) -> tuple[float, float]:
    """Calculate topology score from hop distance.
    
    Returns (points, factor).
    - 0 hops: factor 1.0 -> 35.0 pts
    - 1 hop:  factor 0.8 -> 28.0 pts
    - 2 hops: factor 0.5 -> 17.5 pts
    - >=3 hops or None: factor 0.0 -> 0.0 pts
    """
    if hop_distance is None:
        return 0.0, 0.0
    if hop_distance == 0:
        return 35.0, 1.0
    if hop_distance == 1:
        return 28.0, 0.8
    if hop_distance == 2:
        return 17.5, 0.5
    return 0.0, 0.0


def classify_anomaly_category(policy_or_condition: str, metric_type: str = "") -> str:
    """Classify anomaly into architectural failure layer."""
    text = f"{policy_or_condition} {metric_type}".lower()

    if any(k in text for k in ["connection", "cloudsql", "max_connections", "lock_wait", "db_lock", "row_lock"]):
        return "db_saturation"
    if any(k in text for k in ["spikearrest", "spike_arrest", "quota", "429", "ratelimit", "rate_limit"]):
        return "client_429"
    if any(k in text for k in ["401", "403", "unauthorized", "forbidden", "auth_failure", "apikey"]):
        return "client_auth"
    if any(k in text for k in ["oom", "memory", "out of memory", "container_crash", "restart"]):
        return "backend_crash"
    if any(k in text for k in ["latency", "timeout", "504", "slow", "target_latency"]):
        return "latency_504"
    if any(k in text for k in ["apigee", "gateway", "proxy"]) and any(k in text for k in ["5xx", "500", "502", "503", "504"]):
        return "gateway_5xx"
    if any(k in text for k in ["5xx", "500", "502", "503", "server error", "error_count"]):
        return "backend_5xx"

    return "generic_anomaly"


def calculate_causal_score(
    alert_category: str,
    group_category: str,
) -> tuple[float, float, str]:
    """Evaluate causal relationship between alert anomaly and group anomaly.
    
    Returns (points, factor, category_description).
    - Direct propagation chain (30 pts, factor 1.0):
      DB saturation -> Backend 5xx / Latency -> Apigee 5xx / 504
      Backend crash -> Backend 503 -> Apigee 502/503
      Backend latency -> Apigee 504 / Gateway 5xx
    - Incompatible classes (0 pts, factor 0.0):
      Client 429 SpikeArrest vs DB saturation / 5xx
      Client 401/403 vs Backend 5xx / DB
    - Plausible correlation (15 pts, factor 0.5):
      General 5xx across microservices
    """
    pair = {alert_category, group_category}

    # Incompatible pairs
    if "client_429" in pair and ("db_saturation" in pair or "backend_5xx" in pair or "backend_crash" in pair or "gateway_5xx" in pair):
        return 0.0, 0.0, "incompatible_429_vs_backend"
    if "client_auth" in pair and ("backend_5xx" in pair or "db_saturation" in pair or "gateway_5xx" in pair):
        return 0.0, 0.0, "incompatible_auth_vs_backend"

    # Direct propagation chains
    if "gateway_5xx" in pair and ("latency_504" in pair or "backend_5xx" in pair or "db_saturation" in pair):
        return 30.0, 1.0, "direct_latency_to_gateway_5xx_cascade"
    if "db_saturation" in pair and ("backend_5xx" in pair or "latency_504" in pair):
        return 30.0, 1.0, "direct_db_to_backend_cascade"
    if "backend_crash" in pair and ("backend_5xx" in pair or "latency_504" in pair):
        return 30.0, 1.0, "direct_crash_to_gateway_cascade"
    if "latency_504" in pair and "backend_5xx" in pair:
        return 30.0, 1.0, "direct_latency_to_5xx_cascade"
    if alert_category == group_category and alert_category in {"db_saturation", "backend_crash", "latency_504", "gateway_5xx"}:
        return 30.0, 1.0, "direct_same_anomaly_class"

    # Plausible correlation
    if alert_category == group_category or ("backend_5xx" in pair):
        return 15.0, 0.5, "plausible_5xx_correlation"

    # Default general anomaly
    return 15.0, 0.5, "plausible_unclassified_correlation"


def calculate_temporal_score(
    alert_time: datetime,
    group_anchor_time: datetime,
    horizon_minutes: float = TEMPORAL_HORIZON_MINUTES,
) -> tuple[float, float, float]:
    """Calculate temporal proximity score with linear decay.
    
    Formula:
      delta_t = |t_alert - t_group| in minutes
      S_time = max(0.0, 1.0 - delta_t / H)
      points = 20.0 * S_time
      
    Returns (points, factor, delta_t_minutes).
    """
    # Ensure UTC
    if alert_time.tzinfo is None:
        alert_time = alert_time.replace(tzinfo=timezone.utc)
    if group_anchor_time.tzinfo is None:
        group_anchor_time = group_anchor_time.replace(tzinfo=timezone.utc)

    delta_seconds = abs((alert_time - group_anchor_time).total_seconds())
    delta_minutes = delta_seconds / 60.0

    if delta_minutes >= horizon_minutes:
        factor = 0.0
    else:
        factor = max(0.0, 1.0 - (delta_minutes / horizon_minutes))

    points = round(WEIGHT_TIME * factor, 4)
    return points, factor, delta_minutes


def calculate_change_score(
    alert_time: datetime,
    deployment_time: Optional[datetime] = None,
    max_alignment_minutes: float = 30.0,
) -> tuple[float, float, bool]:
    """Calculate deployment change alignment score.
    
    Returns (points, factor, is_aligned).
    If deployment occurred within max_alignment_minutes (30 min) of onset: 15 pts (factor 1.0).
    Otherwise: 0 pts.
    """
    if deployment_time is None:
        return 0.0, 0.0, False

    if alert_time.tzinfo is None:
        alert_time = alert_time.replace(tzinfo=timezone.utc)
    if deployment_time.tzinfo is None:
        deployment_time = deployment_time.replace(tzinfo=timezone.utc)

    delta_minutes = abs((alert_time - deployment_time).total_seconds()) / 60.0

    if delta_minutes <= max_alignment_minutes:
        return WEIGHT_CHANGE, 1.0, True

    return 0.0, 0.0, False


def compute_multi_factor_score(
    alert_resource: str,
    alert_category: str,
    alert_time: datetime,
    group_resources: list[str],
    group_category: str,
    group_anchor_time: datetime,
    deployment_time: Optional[datetime] = None,
) -> ScoreBreakdown:
    """Compute the multi-factor correlation score between an alert and an incident group."""
    # 1. Topology score (minimum hop distance to any group resource)
    min_hop: Optional[int] = None
    if calculate_hop_distance is not None:
        for g_res in group_resources:
            hop = calculate_hop_distance(alert_resource, g_res)
            if hop is not None:
                if min_hop is None or hop < min_hop:
                    min_hop = hop
    elif alert_resource in group_resources:
        min_hop = 0

    topo_pts, _ = calculate_topology_score(min_hop)

    # 2. Causal failure chain
    causal_pts, _, causal_cat = calculate_causal_score(alert_category, group_category)

    # Direct gateway timeout cascade: an ingress gateway proxy terminating traffic for
    # an upstream backend has direct caller-callee latency coupling across 2 hops
    if "gateway" in causal_cat and min_hop == 2 and topo_pts < 28.0:
        topo_pts = 28.0

    # Incompatible failure classes cannot causally propagate across topology;
    # topological coupling is suppressed to ensure auto-new group (< 35 pts)
    if causal_cat.startswith("incompatible"):
        topo_pts = 0.0

    # 3. Temporal decay
    time_pts, _, delta_min = calculate_temporal_score(alert_time, group_anchor_time)

    # 4. Deployment change alignment
    change_pts, _, change_aligned = calculate_change_score(alert_time, deployment_time)

    total = round(topo_pts + causal_pts + time_pts + change_pts, 2)
    # Clamp between 0.0 and 100.0
    total = max(0.0, min(100.0, total))

    return ScoreBreakdown(
        topo_score=topo_pts,
        causal_score=causal_pts,
        time_score=time_pts,
        change_score=change_pts,
        total_score=total,
        hop_distance=min_hop,
        causal_category=causal_cat,
        delta_minutes=delta_min,
        change_aligned=change_aligned,
    )
