"""The canonical serialization is the ruler every distance is measured against.

If any of these break, every embedding in the database is measuring something
slightly different from what the live path measures, and the k-NN results stop
meaning anything. That failure is silent in production, so it is loud here.
"""
from __future__ import annotations

import pytest

from nexus_common.trajectory import (
    METRIC_SCALES,
    metric_digest,
    quantize,
    summarize_metric,
    trajectory_text,
)

WINDOW = {
    "pool_utilization": [0.34, 0.41, 0.52, 0.68, 0.79, 0.88],
    "latency_p99_ms": [140.0, 180.0, 260.0, 380.0, 500.0, 620.0],
}


def _text(**overrides) -> str:
    kwargs = {
        "service": "payments",
        "region": "aws-us-east-1",
        "window_minutes": 60,
        "metrics": WINDOW,
        "phase": "precursor",
    }
    kwargs.update(overrides)
    return trajectory_text(**kwargs)


def test_serialization_is_deterministic():
    assert _text() == _text()


def test_metric_order_does_not_leak_into_the_text():
    reversed_order = dict(reversed(list(WINDOW.items())))
    assert _text(metrics=reversed_order) == _text()


def test_quantization_is_absolute_not_window_relative():
    """The same value must quantize identically regardless of its neighbours."""
    quiet = {"pool_utilization": [0.30, 0.31, 0.32]}
    loud = {"pool_utilization": [0.30, 0.95, 0.99]}
    assert summarize_metric("pool_utilization", quiet["pool_utilization"])["start"] == (
        summarize_metric("pool_utilization", loud["pool_utilization"])["start"]
    )


def test_jitter_does_not_change_the_text():
    """Two windows differing only by small noise serialize identically."""
    jittered = {
        name: [v * 1.005 for v in series] for name, series in WINDOW.items()
    }
    assert _text(metrics=jittered) == _text()


def test_window_length_is_bucketed_not_exact():
    assert _text(window_minutes=95) == _text(window_minutes=110)
    assert _text(window_minutes=30) != _text(window_minutes=180)


def test_phase_is_part_of_the_text():
    assert _text(phase="precursor") != _text(phase="failure")


def test_rising_and_falling_series_differ():
    rising = {"pool_utilization": [0.2, 0.4, 0.6, 0.9]}
    falling = {"pool_utilization": [0.9, 0.6, 0.4, 0.2]}
    assert _text(metrics=rising) != _text(metrics=falling)


def test_unknown_metric_raises_rather_than_guessing_a_scale():
    with pytest.raises(KeyError, match="METRIC_SCALES"):
        quantize("invented_metric", 1.0)


def test_values_outside_the_nominal_range_saturate():
    assert quantize("pool_utilization", -5.0) == 0
    assert quantize("pool_utilization", 99.0) == 9


def test_empty_metrics_are_rejected():
    with pytest.raises(ValueError):
        _text(metrics={})


def test_digest_keeps_a_downsampled_series_and_the_summary():
    digest = metric_digest({"pool_utilization": [0.1] * 200}, keep_samples=12)
    assert len(digest["metrics"]["pool_utilization"]) <= 12
    assert digest["summary"]["pool_utilization"]["metric"] == "pool_utilization"


def test_every_scale_entry_is_a_usable_range():
    for name, (_unit, lo, hi) in METRIC_SCALES.items():
        assert hi > lo, f"{name} has an empty nominal range"
