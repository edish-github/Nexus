"""CloudWatch custom metrics, in the `NEXUS` namespace.

Every agent emits its domain counters through here. Emission is best-effort by
design: a metrics call that raises would fail a pipeline stage over telemetry
about the pipeline, which is exactly the wrong trade. Failures are logged and
swallowed, and on a machine with no AWS credentials the whole thing degrades to
a log line so the agents stay runnable locally.
"""
from __future__ import annotations

import functools
import os

from . import config, log

logger = log.get_logger("metrics")

ENABLED = os.environ.get("NEXUS_METRICS", "1") not in ("0", "false", "False")


@functools.cache
def _client():
    import boto3

    return boto3.client("cloudwatch", region_name=config.AWS_REGION)


def put(name: str, value: float, *, unit: str = "Count", **dimensions: str) -> None:
    """Emit one datapoint. Never raises."""
    if not ENABLED:
        return
    datum = {"MetricName": name, "Value": float(value), "Unit": unit}
    if dimensions:
        datum["Dimensions"] = [
            {"Name": k, "Value": str(v)} for k, v in sorted(dimensions.items()) if v
        ]
    try:
        _client().put_metric_data(Namespace=config.METRIC_NAMESPACE, MetricData=[datum])
    except Exception as e:
        logger.debug("metric not emitted", metric=name, value=value, error=str(e))


def put_many(data: dict[str, float], *, unit: str = "Count", **dimensions: str) -> None:
    for name, value in data.items():
        put(name, value, unit=unit, **dimensions)
