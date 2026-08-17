"""Parametric telemetry synthesis: turn an archetype into a metric timeline.

Every incident in the world is four phases sampled at a fixed interval:

    baseline ──► precursor drift ──► failure ──► recovery
    (45m flat)   (45–180m, the      (20m, the   (40m back
                  window Oracle      spike)      to rest)
                  actually matches)

A **negative window** is the same shape with the failure phase removed and the
drift stopped part-way: it wanders into precursor territory and then recovers on
its own. Those are what make the precision panel honest — a system that has
never seen drift-that-resolved will call every wobble an incident.

Noise is applied twice: per-sample jitter on every metric, and occasional benign
transient spikes anywhere in the timeline, so no two windows of the same
archetype are byte-identical while still quantizing to the same canonical text.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from nexus_common.trajectory import METRIC_SCALES

from .archetypes import Archetype

SAMPLE_INTERVAL_MINUTES = 5
BASELINE_MINUTES = 45
FAILURE_MINUTES = 20
RECOVERY_MINUTES = 40


def ease(shape: str, t: np.ndarray) -> np.ndarray:
    """Map normalized phase progress onto the drift curve for a given shape."""
    if shape == "ramp":
        return t
    if shape == "exp":
        return t**2.4  # quiet for most of the window, then it runs away
    if shape == "step":
        return np.clip((t - 0.55) / 0.15, 0.0, 1.0)
    if shape == "sawtooth":
        return np.clip(t + 0.12 * np.sin(t * 6.0 * np.pi) * (1.0 - t), 0.0, 1.0)
    if shape == "plateau":
        return np.clip((t - 0.62) / 0.38, 0.0, 1.0) ** 1.6  # nothing until late
    raise ValueError(f"unknown shape {shape!r}")


@dataclass(frozen=True)
class Trajectory:
    """One simulated timeline, plus the slices that get embedded."""

    service: str
    region: str
    archetype: str
    led_to_incident: bool
    precursor_minutes: int
    sample_interval_minutes: int
    series: dict[str, list[float]]
    precursor_range: tuple[int, int]
    failure_range: tuple[int, int] | None

    def _slice(self, span: tuple[int, int]) -> dict[str, list[float]]:
        lo, hi = span
        return {name: values[lo:hi] for name, values in self.series.items()}

    def precursor_metrics(self) -> dict[str, list[float]]:
        """The window Oracle matches against: drift only, no failure."""
        return self._slice(self.precursor_range)

    def failure_metrics(self) -> dict[str, list[float]]:
        """The window the incident's symptom embedding is built from."""
        if self.failure_range is None:
            raise ValueError("a negative window has no failure phase")
        return self._slice(self.failure_range)


def _phase_values(
    spec_from: float, spec_to: float, samples: int, shape: str
) -> np.ndarray:
    if samples <= 0:
        return np.empty(0)
    t = np.linspace(0.0, 1.0, samples, endpoint=False) + (1.0 / samples)
    return spec_from + (spec_to - spec_from) * ease(shape, t)


def synthesize(
    archetype: Archetype,
    *,
    rng: np.random.Generator,
    service: str,
    region: str,
    led_to_incident: bool = True,
    precursor_minutes: int | None = None,
) -> Trajectory:
    """Build one trajectory. Deterministic for a given `rng` state."""
    lo, hi = archetype.lead_time_minutes
    if precursor_minutes is None:
        precursor_minutes = int(rng.integers(lo, hi + 1))
    precursor_minutes = int(round(precursor_minutes / SAMPLE_INTERVAL_MINUTES)) * (
        SAMPLE_INTERVAL_MINUTES
    )

    n_base = BASELINE_MINUTES // SAMPLE_INTERVAL_MINUTES
    n_pre = max(1, precursor_minutes // SAMPLE_INTERVAL_MINUTES)
    n_fail = FAILURE_MINUTES // SAMPLE_INTERVAL_MINUTES if led_to_incident else 0
    n_rec = RECOVERY_MINUTES // SAMPLE_INTERVAL_MINUTES

    # A negative only travels part of the way into the precursor's drift before
    # turning around — close enough to be a plausible alarm, short of the cliff.
    reach = 1.0 if led_to_incident else float(rng.uniform(0.52, 0.78))
    # Per-incident severity multiplier, so two runs of the same archetype are
    # not carbon copies of one another.
    intensity = float(rng.uniform(0.92, 1.08))

    series: dict[str, list[float]] = {}
    for name, spec in archetype.metrics.items():
        _, scale_lo, scale_hi = METRIC_SCALES[name]
        span = scale_hi - scale_lo

        drift_end = spec.baseline + (spec.precursor_end - spec.baseline) * reach * intensity
        baseline = np.full(n_base, spec.baseline)
        precursor = _phase_values(spec.baseline, drift_end, n_pre, spec.shape)
        if led_to_incident:
            peak = spec.baseline + (spec.failure_peak - spec.baseline) * intensity
            failure = _phase_values(drift_end, peak, n_fail, "ramp")
            recovery = _phase_values(peak, spec.baseline, n_rec, "ramp")
        else:
            failure = np.empty(0)
            recovery = _phase_values(drift_end, spec.baseline, n_rec, "ramp")

        values = np.concatenate([baseline, precursor, failure, recovery])
        if spec.jitter:
            values = values + rng.normal(0.0, spec.jitter * span, size=values.shape)
        # Benign transients: a short bump that goes nowhere, ~40% of windows.
        if spec.jitter and rng.random() < 0.4:
            at = int(rng.integers(0, max(1, len(values) - 2)))
            values[at : at + 2] += rng.normal(0.0, 1.0) * 0.06 * span
        values = np.clip(values, scale_lo, scale_hi)
        series[name] = [float(v) for v in values]

    pre_start = n_base
    pre_end = n_base + n_pre
    fail_range = (pre_end, pre_end + n_fail) if led_to_incident else None
    return Trajectory(
        service=service,
        region=region,
        archetype=archetype.key,
        led_to_incident=led_to_incident,
        precursor_minutes=n_pre * SAMPLE_INTERVAL_MINUTES,
        sample_interval_minutes=SAMPLE_INTERVAL_MINUTES,
        series=series,
        precursor_range=(pre_start, pre_end),
        failure_range=fail_range,
    )
