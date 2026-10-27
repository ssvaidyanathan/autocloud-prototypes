"""Cloud Run request_count queries (Cloud Monitoring), shared by the S5 rollback-target
lookup (specialists/change.py) and the recovery note (recovery.py).

Every query is scoped to the configured project, region and service: a same-named service
in another region of the project must not add its requests to the facts the agent states.
"""

from typing import Any, Iterable

REQUEST_COUNT_METRIC = "run.googleapis.com/request_count"


def request_count_filter(region: str, service: str) -> str:
    """Monitoring filter for request_count of one Cloud Run service in one region."""
    return (
        f'metric.type = "{REQUEST_COUNT_METRIC}" AND resource.type = "cloud_run_revision" '
        f'AND resource.labels.location = "{region}" AND resource.labels.service_name = "{service}"'
    )


def request_count_series(
    client: Any, project_id: str, region: str, service: str, start_epoch: float, end_epoch: float
) -> Iterable[Any]:
    """List request_count time series (full view) of `service` in `region` over [start, end]."""
    from google.cloud import monitoring_v3

    req = monitoring_v3.ListTimeSeriesRequest(
        name=f"projects/{project_id}",
        filter=request_count_filter(region, service),
        interval=monitoring_v3.TimeInterval(
            start_time={"seconds": int(start_epoch)}, end_time={"seconds": int(end_epoch)}
        ),
        view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
    )
    return client.list_time_series(request=req)
