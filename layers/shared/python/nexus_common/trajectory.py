"""Canonical `trajectory → text` serialization.

Everything that embeds a telemetry window goes through here: the seed generator
when it builds the historical world, and Oracle when it embeds the live window
it is about to match against that world. If the two disagreed by so much as a
rounding rule, every k-NN distance would be measured against a different ruler.

The contract:

* **Stable ordering.** Metrics are emitted in sorted name order, fields in a
  fixed order. No dict iteration order leaks into the text.
* **Absolute quantization.** Each metric is quantized against its own declared
  nominal range in `METRIC_SCALES`, not against the window's own min/max — so
  "q8" means the same level of pool utilization in every window ever embedded.
* **Shape over samples.** The text describes trend, level, slope, peak,
  volatility and shape rather than raw numbers, so two windows that differ only
  by jitter serialize identically and therefore embed identically.

Adding a metric means adding it to `METRIC_SCALES`; a metric with no declared
scale raises rather than silently quantizing against the wrong ruler.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

# metric name → (unit, nominal low, nominal high). The range is the quantization
# ruler, not a hard clamp on the data: values outside it saturate at q0/q9.
METRIC_SCALES: dict[str, tuple[str, float, float]] = {
    "active_connections": ("count", 0.0, 500.0),
    "cache_hit_ratio": ("ratio", 0.0, 1.0),
    "cert_days_remaining": ("days", 0.0, 90.0),
    "cpu_utilization": ("ratio", 0.0, 1.0),
    "deploy_age_minutes": ("minutes", 0.0, 240.0),
    "disk_used_pct": ("ratio", 0.0, 1.0),
    "dns_resolve_ms": ("ms", 0.0, 3000.0),
    "error_rate": ("ratio", 0.0, 0.5),
    "gc_pause_ms": ("ms", 0.0, 2000.0),
    "heap_used_pct": ("ratio", 0.0, 1.0),
    "iops": ("count", 0.0, 20000.0),
    "latency_p99_ms": ("ms", 0.0, 2000.0),
    "origin_qps": ("count", 0.0, 5000.0),
    "pool_utilization": ("ratio", 0.0, 1.0),
    "queue_depth": ("count", 0.0, 5000.0),
    "queue_wait_ms": ("ms", 0.0, 1000.0),
    "rejected_requests": ("count", 0.0, 1000.0),
    "restart_count": ("count", 0.0, 10.0),
    "retry_rate": ("ratio", 0.0, 1.0),
    "rss_mb": ("mb", 0.0, 8192.0),
    "thread_pool_active_pct": ("ratio", 0.0, 1.0),
    "tls_handshake_failures": ("count", 0.0, 1000.0),
    "upstream_5xx_rate": ("ratio", 0.0, 1.0),
    "upstream_timeouts": ("count", 0.0, 1000.0),
    "write_latency_ms": ("ms", 0.0, 3000.0),
}

# The metric Guardian watches during the verification window, per outcome
# category, and the direction that counts as recovery. One metric rather than
# all of them: a fix that pulls the defining signal back is working, and
# demanding every metric improve at once would call a successful remediation a
# failure because a lagging indicator had not caught up yet.
OUTCOME_TARGETS: dict[str, tuple[str, str]] = {
    "connection_pool_exhaustion": ("pool_utilization", "down"),
    "memory_leak_oom": ("heap_used_pct", "down"),
    "cache_stampede": ("cache_hit_ratio", "up"),
    "cert_expiry": ("tls_handshake_failures", "down"),
    "disk_full": ("disk_used_pct", "down"),
    "bad_deploy_latency_regression": ("latency_p99_ms", "down"),
    "thread_pool_starvation": ("queue_depth", "down"),
    "dns_timeout_cascade": ("dns_resolve_ms", "down"),
}


def outcome_target(category: str) -> tuple[str, str]:
    """The (metric, direction) pair that decides whether a remediation worked."""
    try:
        return OUTCOME_TARGETS[category]
    except KeyError:
        # An unknown category is a novel incident; latency is the safest
        # universal proxy for "is this getting better or worse".
        return ("latency_p99_ms", "down")


# Slope is measured as the fraction of a metric's nominal span traversed across
# the whole window, so the buckets are comparable between metrics.
_SLOPE_BUCKETS = (
    (-0.40, "collapsing"),
    (-0.12, "falling"),
    (-0.03, "drifting_down"),
    (0.03, "flat"),
    (0.12, "drifting_up"),
    (0.40, "rising"),
    (math.inf, "surging"),
)

_VOLATILITY_BUCKETS = ((0.01, "steady"), (0.04, "mild"), (0.10, "choppy"), (math.inf, "erratic"))

# Window length is informative — a memory leak announces itself over hours, a
# cache stampede over minutes — but the exact number is not. Bucketing it means
# a 95-minute window and a 110-minute window share a token instead of landing in
# two unrelated hash buckets.
_DURATION_BUCKETS = ((44, "brief"), (89, "short"), (149, "medium"), (math.inf, "long"))


def quantize(metric: str, value: float) -> int:
    """Map a raw metric value onto its 0–9 decile within the metric's nominal range."""
    try:
        _, lo, hi = METRIC_SCALES[metric]
    except KeyError:
        raise KeyError(
            f"metric {metric!r} has no entry in METRIC_SCALES; add one before embedding it"
        ) from None
    span = hi - lo
    if span <= 0:
        return 0
    return max(0, min(9, int((value - lo) / span * 10)))


def _bucket(value: float, buckets: Sequence[tuple[float, str]]) -> str:
    for edge, label in buckets:
        if value <= edge:
            return label
    return buckets[-1][1]


def _shape(series: Sequence[float]) -> str:
    """Classify the series' gross form from its own first differences."""
    if len(series) < 3:
        return "flat"
    deltas = [b - a for a, b in zip(series, series[1:], strict=False)]
    span = max(series) - min(series)
    if span == 0:
        return "flat"
    rising = sum(1 for d in deltas if d > 0)
    falling = sum(1 for d in deltas if d < 0)
    monotone = max(rising, falling) / len(deltas)
    peak_at = series.index(max(series)) / (len(series) - 1)
    if monotone >= 0.85:
        return "ramp"
    if monotone >= 0.6:
        return "step" if abs(peak_at - 0.5) > 0.3 else "ramp"
    if 0.25 < peak_at < 0.85 and (max(series) - series[-1]) > 0.5 * span:
        return "spike"
    return "oscillating"


def summarize_metric(metric: str, series: Sequence[float]) -> dict[str, object]:
    """Reduce one metric's samples to the quantized descriptors used in the text."""
    if not series:
        raise ValueError(f"metric {metric!r} has no samples")
    _, lo, hi = METRIC_SCALES[metric]
    span = (hi - lo) or 1.0
    start, end = float(series[0]), float(series[-1])
    slope = (end - start) / span
    if len(series) > 1:
        diffs = [abs(b - a) / span for a, b in zip(series, series[1:], strict=False)]
        volatility = sum(diffs) / len(diffs)
    else:
        volatility = 0.0
    return {
        "metric": metric,
        "trend": _bucket(slope, _SLOPE_BUCKETS),
        "start": f"q{quantize(metric, start)}",
        "end": f"q{quantize(metric, end)}",
        "peak": f"q{quantize(metric, max(series))}",
        "trough": f"q{quantize(metric, min(series))}",
        "volatility": _bucket(volatility, _VOLATILITY_BUCKETS),
        "shape": _shape([float(x) for x in series]),
    }


def trajectory_text(
    *,
    service: str,
    region: str,
    window_minutes: float,
    metrics: Mapping[str, Sequence[float]],
    phase: str = "precursor",
) -> str:
    """Serialize a telemetry window into the canonical text that gets embedded.

    Deterministic for a given input: identical windows produce identical text,
    and therefore identical embeddings.
    """
    if not metrics:
        raise ValueError("a trajectory needs at least one metric series")
    header = (
        f"telemetry window phase {phase} service {service} region {region} "
        f"duration {_bucket(float(window_minutes), _DURATION_BUCKETS)}"
    )
    lines = [header]
    for name in sorted(metrics):
        s = summarize_metric(name, metrics[name])
        lines.append(
            f"metric {s['metric']} trend {s['trend']} shape {s['shape']} "
            f"start {s['start']} end {s['end']} peak {s['peak']} trough {s['trough']} "
            f"volatility {s['volatility']}"
        )
    return "\n".join(lines)


def metric_digest(
    metrics: Mapping[str, Sequence[float]], *, keep_samples: int = 24
) -> dict[str, object]:
    """Build the JSONB digest stored alongside a snapshot.

    Keeps a downsampled series per metric (for the dashboard sparklines) plus the
    same quantized summary the text was built from (for explaining a match).
    """
    digest: dict[str, object] = {"metrics": {}, "summary": {}}
    for name in sorted(metrics):
        series = [float(x) for x in metrics[name]]
        step = max(1, math.ceil(len(series) / keep_samples))
        digest["metrics"][name] = [round(v, 4) for v in series[::step]][:keep_samples]
        digest["summary"][name] = summarize_metric(name, series)
    return digest
