"""The live fleet simulator — the thing behind the demo's load-ramp button.

Four services sit at their baselines until someone starts a ramp. A ramp walks a
service through the same archetype curve the historical world was built from,
compressed by a `speed` multiplier so a 90-minute precursor fits inside a
90-second demo beat. Oracle sees the trailing window through
`telemetry_embeddings` and has no idea it is synthetic.

The simulator is pure and side-effect free: `tick()` advances simulated time and
returns the new sample, `window()` hands back the trailing telemetry. Anything
that talks to a database or a network lives in `live.py`.

`apply_action()` is the seam Guardian executes against in Phase 4. A remediation
that matches the running archetype bends the trajectory back towards baseline; a
mismatched one pushes it further along. That is what makes the bad-fix rollback
beat a property of the simulation rather than a scripted animation.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from nexus_common.trajectory import METRIC_SCALES, metric_digest, trajectory_text

from . import archetypes
from .trajectory import SAMPLE_INTERVAL_MINUTES, ease

WINDOW_MINUTES = 180
WINDOW_SAMPLES = WINDOW_MINUTES // SAMPLE_INTERVAL_MINUTES
# Below this many samples a window is too short to say anything about a trend,
# so Oracle should not embed it.
MIN_SAMPLES_TO_MATCH = 6

# What a healthy service reports. Deliberately a small set common to every
# archetype: a service at rest has vital signs, not a diagnosis.
VITALS: dict[str, float] = {
    "latency_p99_ms": 145.0,
    "error_rate": 0.002,
    "cpu_utilization": 0.37,
}
VITALS_JITTER = 0.012

# How much one remediation step moves the trajectory, as a multiple of the
# per-tick drift. Above 1.0 in total, the drift reverses.
EFFECTIVE_RELIEF = 0.6
MISMATCH_PENALTY = -0.25
RELIEF_FLOOR, RELIEF_CEILING = -1.0, 1.8

# Which actions actually address which archetype. Guardian's step actions are
# scored against this: matching actions bend the curve down, mismatched ones
# make things worse, which is how a bad playbook earns its rollback.
EFFECTIVE_ACTIONS: dict[str, frozenset[str]] = {
    "connection_pool_exhaustion": frozenset(
        {"scale_connection_pool", "recycle_connections", "set_circuit_breaker"}
    ),
    "memory_leak_oom": frozenset({"rolling_restart", "restart_worker", "scale_replicas"}),
    "cache_stampede": frozenset({"set_cache_ttl", "warm_cache", "throttle_ingress"}),
    "cert_expiry": frozenset({"rotate_certificate", "shed_load"}),
    "disk_full": frozenset({"prune_disk", "extend_volume"}),
    "bad_deploy_latency_regression": frozenset({"rollback_deploy", "pin_deploy_version"}),
    "thread_pool_starvation": frozenset({"scale_thread_pool", "shed_load", "set_retry_budget"}),
    "dns_timeout_cascade": frozenset({"set_dns_ttl", "failover_resolver", "set_retry_budget"}),
}


def step_key(action: str, params: dict | None) -> str:
    """Identify a step by what state it asks for, not by when it was asked.

    Two calls with the same action and parameters are the same desired state and
    therefore the same step; the same action with different parameters is a
    different one. Guardian and the simulator must agree on this string, so it
    is computed here and echoed back in every action result.
    """
    import json

    return f"{action}:{json.dumps(params or {}, sort_keys=True, default=str)}"


@dataclass
class ServiceState:
    name: str
    region: str
    archetype: str | None = None
    progress: float = 0.0  # 0 → start of precursor drift, 1 → failure onset
    speed: float = 1.0
    relief: float = 0.0  # cumulative remediation effect, negative makes it worse
    recovered: bool = False
    # Applied steps, keyed by action + params, each mapped to the relief it
    # contributed. This is what makes execution idempotent and rollback exact:
    # re-applying a step already in desired state changes nothing, and reverting
    # one removes precisely what it added rather than guessing.
    applied: dict[str, float] = field(default_factory=dict)
    samples: deque[dict[str, float]] = field(default_factory=lambda: deque(maxlen=WINDOW_SAMPLES))
    clock: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def status(self) -> str:
        if self.archetype is None:
            return "healthy"
        if self.recovered:
            return "recovered"
        if self.progress >= 1.0:
            return "failing"
        if self.progress >= 0.6:
            return "degrading"
        return "drifting"


class FleetSimulator:
    """A deterministic-per-seed simulation of the four-service fleet."""

    def __init__(self, *, seed: int = 7, now: datetime | None = None) -> None:
        self._rng = np.random.default_rng(seed)
        start = (now or datetime.now(UTC)).replace(microsecond=0)
        self.services: dict[str, ServiceState] = {}
        for i, name in enumerate(archetypes.SERVICES):
            state = ServiceState(
                name=name, region=archetypes.REGIONS[i % len(archetypes.REGIONS)], clock=start
            )
            self.services[name] = state
            self._prefill(state)

    # -- control API ------------------------------------------------------- #

    def start_ramp(self, service: str, archetype: str, speed: float = 1.0) -> ServiceState:
        """Begin driving `service` along `archetype`'s precursor curve.

        The trailing window is cleared, so what Oracle subsequently embeds is
        drift and nothing but drift — the same construction as the precursor
        snapshots in the seeded world. Mixing a baseline prefix into the live
        window would measure it against a different ruler than the history it is
        matched against.

        `speed` is progress per tick relative to a full window: at `speed=1` the
        drift takes exactly `WINDOW_SAMPLES` ticks to reach failure onset, so a
        full buffer holds one complete precursor sweep — again, the same thing
        the seeded snapshots hold. Higher values compress the beat at the cost
        of resolution. Wall-clock pace is set by the ticker interval, not here.
        """
        state = self._require(service)
        archetypes.get(archetype)  # validate before mutating state
        if speed <= 0:
            raise ValueError("speed must be positive")
        state.archetype = archetype
        state.progress = 0.0
        state.speed = float(speed)
        state.relief = 0.0
        state.recovered = False
        state.applied.clear()
        state.samples.clear()
        return state

    def stop_ramp(self, service: str) -> ServiceState:
        """Return a service to baseline immediately."""
        state = self._require(service)
        state.archetype = None
        state.progress = 0.0
        state.relief = 0.0
        state.recovered = False
        state.applied.clear()
        self._prefill(state)
        return state

    def _prefill(self, state: ServiceState) -> None:
        """Fill a healthy service's window with vitals, so it always has one."""
        state.samples.clear()
        for _ in range(WINDOW_SAMPLES):
            state.samples.append(self._sample(state))
            state.clock += timedelta(minutes=SAMPLE_INTERVAL_MINUTES)

    def apply_action(self, service: str, action: str, params: dict | None = None,
                     revert_of: str | None = None) -> dict:
        """Apply one remediation step, or revert one that was applied earlier.

        Actions are *declarative* — "scale the pool to 200", not "add 150 to the
        pool" — so applying the same action with the same parameters twice is
        the same as applying it once. That is what lets Guardian retry a partly
        finished execution without compounding its own effects.

        `revert_of` is the step key a rollback is undoing. Reverting removes
        exactly the relief that step contributed, which is what "restore the
        prior state" has to mean; replaying the inverse as a fresh forward
        action would instead pile a second remediation on top of the first.
        """
        state = self._require(service)
        if state.archetype is None:
            return {"service": service, "action": action, "effect": "no_active_incident"}

        if revert_of is not None:
            removed = state.applied.pop(revert_of, None)
            if removed is None:
                return {"service": service, "action": action, "params": params or {},
                        "effective": False, "relief": round(state.relief, 3),
                        "effect": "nothing_to_revert", "reverted": revert_of}
            state.relief = max(RELIEF_FLOOR, min(RELIEF_CEILING, state.relief - removed))
            return {"service": service, "action": action, "params": params or {},
                    "effective": True, "relief": round(state.relief, 3),
                    "effect": "reverted", "reverted": revert_of}

        key = step_key(action, params)
        if key in state.applied:
            return {"service": service, "action": action, "params": params or {},
                    "effective": state.applied[key] > 0, "relief": round(state.relief, 3),
                    "effect": "already_applied", "step_key": key}

        helpful = action in EFFECTIVE_ACTIONS.get(state.archetype, frozenset())
        # Calibrated so a correct two-step playbook reverses the drift, one step
        # merely slows it, and a mismatched step accelerates it. Requiring three
        # correct steps to recover would make most playbooks in the catalogue
        # look like failures and turn every run into a rollback.
        delta = EFFECTIVE_RELIEF if helpful else MISMATCH_PENALTY
        state.applied[key] = delta
        state.relief = max(RELIEF_FLOOR, min(RELIEF_CEILING, state.relief + delta))
        return {
            "service": service,
            "action": action,
            "params": params or {},
            "effective": helpful,
            "relief": round(state.relief, 3),
            "effect": "improving" if helpful else "degrading",
            "step_key": key,
        }

    # -- simulation -------------------------------------------------------- #

    def tick(self, service: str | None = None) -> dict[str, ServiceState]:
        """Advance simulated time by one sample interval."""
        targets = [self._require(service)] if service else list(self.services.values())
        for state in targets:
            if state.archetype is not None:
                advance = state.speed / WINDOW_SAMPLES
                # Remediation is modelled as a counter-force on the same axis
                # rather than a jump to healthy: one effective step slows the
                # drift, a whole playbook's worth reverses it, and a mismatched
                # step (negative relief) accelerates it. That is what makes the
                # bad-fix rollback a consequence of the simulation.
                state.progress = max(0.0, state.progress + advance * (1.0 - state.relief))
                # A recovered service keeps reporting its archetype's metrics at
                # baseline, so Guardian's verification window can watch the
                # trajectory come down rather than simply vanish.
                if state.progress <= 0.02 and state.relief > 1.0:
                    state.recovered = True
                    state.progress = 0.0
            state.samples.append(self._sample(state))
            state.clock += timedelta(minutes=SAMPLE_INTERVAL_MINUTES)
        return {s.name: s for s in targets}

    def _sample(self, state: ServiceState) -> dict[str, float]:
        """One multi-metric sample at the service's current position."""
        if state.archetype is None:
            return {
                name: self._noisy(name, value, VITALS_JITTER) for name, value in VITALS.items()
            }
        arch = archetypes.get(state.archetype)
        p = state.progress
        out: dict[str, float] = {}
        for name, spec in arch.metrics.items():
            if p <= 1.0:
                eased = float(ease(spec.shape, np.array([min(1.0, max(0.0, p))]))[0])
                value = spec.baseline + (spec.precursor_end - spec.baseline) * eased
            else:  # past the precursor window: the failure itself
                over = min(1.0, p - 1.0)
                value = spec.precursor_end + (spec.failure_peak - spec.precursor_end) * over
            # A degrading remediation makes the current position visibly worse.
            if state.relief < 0:
                value += (spec.failure_peak - spec.baseline) * 0.12 * abs(state.relief)
            out[name] = self._noisy(name, value, spec.jitter)
        return out

    def _noisy(self, metric: str, value: float, jitter: float) -> float:
        _, lo, hi = METRIC_SCALES[metric]
        if jitter:
            value = value + float(self._rng.normal(0.0, jitter * (hi - lo)))
        return float(np.clip(value, lo, hi))

    # -- readouts ---------------------------------------------------------- #

    def ready(self, service: str) -> bool:
        """Whether the window holds enough samples for a trend to mean anything."""
        return len(self._require(service).samples) >= MIN_SAMPLES_TO_MATCH

    def window(self, service: str) -> dict[str, list[float]]:
        """The trailing telemetry window, as `{metric: [samples]}`.

        Samples within a window are always homogeneous: the buffer is cleared
        whenever a service changes what it is reporting.
        """
        state = self._require(service)
        if not state.samples:
            return {}
        return {name: [s[name] for s in state.samples] for name in sorted(state.samples[-1])}

    def window_minutes(self, service: str) -> float:
        """How much simulated time the trailing window represents.

        A full buffer during a ramp is one complete precursor sweep, so it
        represents the archetype's own lead time — not `samples × 5 minutes`,
        which would be measuring the compressed demo clock instead of the
        phenomenon and would land the window in the wrong duration bucket.
        """
        state = self._require(service)
        if state.archetype is None:
            return float(WINDOW_MINUTES)
        arch = archetypes.get(state.archetype)
        lead = float(np.mean(arch.lead_time_minutes))
        return lead * len(state.samples) / WINDOW_SAMPLES

    def window_text(self, service: str) -> str:
        """The canonical text for the trailing window — what Oracle embeds.

        Serialized as a `precursor` window, because that is what it is being
        compared against; the phase token is part of the canonical text and has
        to agree with the seeded snapshots.
        """
        state = self._require(service)
        metrics = self.window(service)
        if not metrics:
            raise ValueError(f"service {service!r} has no telemetry yet")
        return trajectory_text(
            service=state.name,
            region=state.region,
            window_minutes=self.window_minutes(service),
            metrics=metrics,
            phase="precursor",
        )

    def window_digest(self, service: str) -> dict:
        state = self._require(service)
        return {
            **metric_digest(self.window(service)),
            "status": state.status,
            "archetype": state.archetype,
            "progress": round(state.progress, 3),
        }

    def snapshot(self) -> list[dict]:
        return [
            {
                "service": s.name,
                "region": s.region,
                "status": s.status,
                "archetype": s.archetype,
                "progress": round(s.progress, 3),
                "speed": s.speed,
                "relief": round(s.relief, 3),
                "applied_steps": sorted(s.applied),
                "clock": s.clock.isoformat(),
            }
            for s in self.services.values()
        ]

    def _require(self, service: str) -> ServiceState:
        try:
            return self.services[service]
        except KeyError:
            raise KeyError(
                f"unknown service {service!r}; known: {', '.join(self.services)}"
            ) from None
