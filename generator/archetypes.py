"""The eight incident archetypes the synthetic world is built from.

Each archetype is a parametric model of how a failure announces itself: what the
metrics look like at rest, how they drift through the precursor window, what
they do at the moment of failure, and how far ahead of the failure the drift is
detectable. The shapes are drawn from the patterns that recur across public
postmortems — pool creep, heap creep, hit-ratio collapse, an expiry cliff, a
disk filling, a deploy step change, queue growth against a saturated pool, and a
resolver cascade.

Every metric named here must have an entry in `nexus_common.trajectory.
METRIC_SCALES`; that is what makes two windows of the same archetype quantize —
and therefore embed — alike.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Easing curves for the drift through the precursor window, f: [0,1] → [0,1].
SHAPES = ("ramp", "exp", "step", "sawtooth", "plateau")


@dataclass(frozen=True)
class MetricSpec:
    """How one metric behaves across the four phases of an incident."""

    baseline: float
    precursor_end: float
    failure_peak: float
    shape: str = "ramp"
    jitter: float = 0.015  # noise sd, as a fraction of the metric's nominal span

    def __post_init__(self) -> None:
        if self.shape not in SHAPES:
            raise ValueError(f"unknown shape {self.shape!r}; expected one of {SHAPES}")


@dataclass(frozen=True)
class Archetype:
    key: str
    title: str
    severity: int  # 1–5, matches incidents.severity
    lead_time_minutes: tuple[int, int]  # precursor window length range (1–3h)
    metrics: dict[str, MetricSpec]
    # Services this archetype plausibly afflicts; empty means all of them.
    services: tuple[str, ...] = field(default_factory=tuple)

    @property
    def metric_names(self) -> list[str]:
        return sorted(self.metrics)


ARCHETYPES: tuple[Archetype, ...] = (
    Archetype(
        key="connection_pool_exhaustion",
        title="Connection pool exhaustion",
        severity=4,
        lead_time_minutes=(60, 90),
        metrics={
            # The signature: utilization creeps super-linearly while queue wait
            # and p99 follow it up, and errors only appear at the very end.
            "pool_utilization": MetricSpec(0.34, 0.88, 0.99, "exp", 0.018),
            "active_connections": MetricSpec(120.0, 380.0, 480.0, "exp", 0.015),
            "queue_wait_ms": MetricSpec(8.0, 240.0, 780.0, "exp", 0.02),
            "latency_p99_ms": MetricSpec(140.0, 620.0, 1650.0, "exp", 0.02),
            "error_rate": MetricSpec(0.002, 0.02, 0.28, "plateau", 0.006),
        },
    ),
    Archetype(
        key="memory_leak_oom",
        title="Memory leak to OOM",
        severity=4,
        lead_time_minutes=(120, 180),
        metrics={
            # Slow monotonic heap growth with GC pause following it; the restart
            # counter is what turns the leak into a user-visible incident.
            "heap_used_pct": MetricSpec(0.42, 0.90, 0.99, "ramp", 0.008),
            "rss_mb": MetricSpec(2600.0, 6800.0, 7900.0, "ramp", 0.008),
            "gc_pause_ms": MetricSpec(45.0, 620.0, 1750.0, "exp", 0.02),
            "latency_p99_ms": MetricSpec(160.0, 520.0, 1400.0, "exp", 0.02),
            "restart_count": MetricSpec(0.0, 0.0, 4.0, "step", 0.0),
        },
    ),
    Archetype(
        key="cache_stampede",
        title="Cache stampede",
        severity=3,
        lead_time_minutes=(60, 90),
        metrics={
            # Hit ratio collapses, origin traffic and CPU spike behind it.
            "cache_hit_ratio": MetricSpec(0.94, 0.46, 0.08, "sawtooth", 0.02),
            "origin_qps": MetricSpec(280.0, 2100.0, 4400.0, "exp", 0.03),
            "cpu_utilization": MetricSpec(0.38, 0.79, 0.97, "exp", 0.025),
            "latency_p99_ms": MetricSpec(130.0, 580.0, 1500.0, "exp", 0.025),
            "error_rate": MetricSpec(0.001, 0.015, 0.19, "plateau", 0.005),
        },
    ),
    Archetype(
        key="cert_expiry",
        title="TLS certificate expiry",
        severity=5,
        lead_time_minutes=(60, 120),
        metrics={
            # The one archetype with a countdown rather than a creep: the days
            # remaining fall to zero and handshakes fail as a cliff, not a ramp.
            "cert_days_remaining": MetricSpec(6.0, 0.4, 0.0, "ramp", 0.0),
            "tls_handshake_failures": MetricSpec(0.0, 24.0, 820.0, "step", 0.004),
            "upstream_5xx_rate": MetricSpec(0.002, 0.03, 0.72, "step", 0.006),
            "error_rate": MetricSpec(0.002, 0.02, 0.44, "step", 0.006),
        },
    ),
    Archetype(
        key="disk_full",
        title="Disk exhaustion",
        severity=4,
        lead_time_minutes=(120, 180),
        metrics={
            # A near-linear fill; write latency turns up only in the last decile.
            "disk_used_pct": MetricSpec(0.61, 0.94, 0.999, "ramp", 0.006),
            "write_latency_ms": MetricSpec(18.0, 210.0, 2400.0, "exp", 0.02),
            "iops": MetricSpec(9200.0, 6100.0, 900.0, "exp", 0.03),
            "error_rate": MetricSpec(0.001, 0.012, 0.33, "plateau", 0.005),
        },
    ),
    Archetype(
        key="bad_deploy_latency_regression",
        title="Bad deploy latency regression",
        severity=3,
        lead_time_minutes=(60, 90),
        metrics={
            # Distinctive because the change is a step at deploy time, not drift,
            # and deploy_age_minutes is the marker that dates it.
            "deploy_age_minutes": MetricSpec(0.0, 55.0, 95.0, "ramp", 0.0),
            "latency_p99_ms": MetricSpec(150.0, 700.0, 1250.0, "step", 0.02),
            "cpu_utilization": MetricSpec(0.36, 0.72, 0.91, "step", 0.025),
            "error_rate": MetricSpec(0.002, 0.03, 0.16, "step", 0.006),
        },
    ),
    Archetype(
        key="thread_pool_starvation",
        title="Thread pool starvation",
        severity=4,
        lead_time_minutes=(60, 105),
        metrics={
            # The pool pins at 100% and the backlog, not the pool, is the signal.
            "thread_pool_active_pct": MetricSpec(0.44, 0.97, 1.0, "exp", 0.015),
            "queue_depth": MetricSpec(40.0, 1900.0, 4600.0, "exp", 0.03),
            "rejected_requests": MetricSpec(0.0, 34.0, 720.0, "plateau", 0.004),
            "latency_p99_ms": MetricSpec(145.0, 780.0, 1800.0, "exp", 0.025),
        },
    ),
    Archetype(
        key="dns_timeout_cascade",
        title="DNS timeout cascade",
        severity=3,
        lead_time_minutes=(60, 90),
        metrics={
            # Noisy by construction: resolution time oscillates upward and the
            # retry budget amplifies it into a cascade.
            "dns_resolve_ms": MetricSpec(22.0, 940.0, 2600.0, "sawtooth", 0.05),
            "upstream_timeouts": MetricSpec(1.0, 190.0, 780.0, "sawtooth", 0.04),
            "retry_rate": MetricSpec(0.01, 0.34, 0.86, "exp", 0.03),
            "error_rate": MetricSpec(0.002, 0.04, 0.31, "exp", 0.008),
        },
    ),
)

BY_KEY: dict[str, Archetype] = {a.key: a for a in ARCHETYPES}

# The simulated fleet.
SERVICES: tuple[str, ...] = ("payments", "checkout", "inventory", "auth")
REGIONS: tuple[str, ...] = ("aws-us-east-1", "aws-eu-west-1", "aws-ap-south-1")


def get(key: str) -> Archetype:
    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"unknown archetype {key!r}; known: {', '.join(sorted(BY_KEY))}"
        ) from None
