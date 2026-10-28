"""Cloud Run request_count queries (Cloud Monitoring), shared by the S5 rollback-target
lookup (specialists/change.py) and the recovery note (recovery.py).

Every query is scoped to the configured project, region and service: a same-named service
in another region of the project must not add its requests to the facts the agent states.
"""

from typing import Any, Iterable

REQUEST_COUNT_METRIC = "run.googleapis.com/request_count"


def request_count_filter(region: str, service: str, revision: str | None = None) -> str:
    """Monitoring filter for request_count of one Cloud Run service (or one revision of it) in one region."""
    flt = (
        f'metric.type = "{REQUEST_COUNT_METRIC}" AND resource.type = "cloud_run_revision" '
        f'AND resource.labels.location = "{region}" AND resource.labels.service_name = "{service}"'
    )
    return f'{flt} AND resource.labels.revision_name = "{revision}"' if revision else flt


def request_count_series(
    client: Any, project_id: str, region: str, service: str, start_epoch: float, end_epoch: float,
    revision: str | None = None, timeout: float | None = None,
) -> Iterable[Any]:
    """List request_count time series (full view) of `service` in `region` over [start, end].

    With `revision`, only that revision's series; with `timeout`, the call's deadline in seconds.
    """
    from google.cloud import monitoring_v3

    req = monitoring_v3.ListTimeSeriesRequest(
        name=f"projects/{project_id}",
        filter=request_count_filter(region, service, revision),
        interval=monitoring_v3.TimeInterval(
            start_time={"seconds": int(start_epoch)}, end_time={"seconds": int(end_epoch)}
        ),
        view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
    )
    if timeout is None:
        return client.list_time_series(request=req)
    return client.list_time_series(request=req, timeout=timeout)
