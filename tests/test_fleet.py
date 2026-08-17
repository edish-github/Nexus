"""The live fleet is what the demo drives, so its control API has to behave.

The important property is that a live ramp produces a window that retrieves the
same archetype it is ramping — otherwise Oracle's live predictions and the
seeded history are measuring different things, and the demo silently predicts
the wrong category.
"""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from generator import archetypes, world
from generator.fleet import MIN_SAMPLES_TO_MATCH, WINDOW_SAMPLES, FleetSimulator
from nexus_common import embeddings

NOW = datetime(2026, 8, 17, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _force_local_provider(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    embeddings.provider_name.cache_clear()
    yield
    embeddings.provider_name.cache_clear()


@pytest.fixture
def sim() -> FleetSimulator:
    return FleetSimulator(seed=11, now=NOW)


def test_the_fleet_starts_healthy_with_a_full_window(sim):
    assert set(sim.services) == set(archetypes.SERVICES)
    for service in sim.services:
        assert sim.services[service].status == "healthy"
        assert sim.ready(service)


def test_services_are_spread_across_regions(sim):
    assert len({s.region for s in sim.services.values()}) == len(archetypes.REGIONS)


def test_speed_compresses_the_sweep(sim):
    sim.start_ramp("payments", "disk_full", speed=4)
    for _ in range(WINDOW_SAMPLES // 4):
        sim.tick("payments")
    assert sim.services["payments"].progress == pytest.approx(1.0, abs=0.05)


def test_start_ramp_validates_its_arguments(sim):
    with pytest.raises(KeyError):
        sim.start_ramp("nonexistent", "disk_full")
    with pytest.raises(KeyError):
        sim.start_ramp("payments", "not_an_archetype")
    with pytest.raises(ValueError):
        sim.start_ramp("payments", "disk_full", speed=0)


def test_a_ramp_progresses_through_the_states(sim):
    sim.start_ramp("payments", "connection_pool_exhaustion")
    assert sim.services["payments"].status == "drifting"
    seen = {sim.services["payments"].status}
    for _ in range(WINDOW_SAMPLES + 4):
        sim.tick("payments")
        seen.add(sim.services["payments"].status)
    assert {"drifting", "degrading", "failing"} <= seen


def test_one_sweep_takes_a_full_window_at_unit_speed(sim):
    """The window has to hold exactly one precursor sweep, or it is not
    comparable with the seeded snapshots it gets matched against."""
    sim.start_ramp("payments", "disk_full")
    for _ in range(WINDOW_SAMPLES):
        sim.tick("payments")
    assert sim.services["payments"].progress == pytest.approx(1.0, abs=0.02)
    assert len(sim.services["payments"].samples) == WINDOW_SAMPLES


def test_a_ramp_clears_the_window_so_only_drift_is_embedded(sim):
    sim.start_ramp("checkout", "cache_stampede")
    assert not sim.ready("checkout"), "the window must start empty, not with stale vitals"
    for _ in range(MIN_SAMPLES_TO_MATCH):
        sim.tick("checkout")
    assert sim.ready("checkout")
    window = sim.window("checkout")
    assert set(window) == set(archetypes.get("cache_stampede").metric_names)
    assert len({len(v) for v in window.values()}) == 1, "metric series must be the same length"


def test_only_the_named_service_advances(sim):
    sim.start_ramp("payments", "disk_full")
    sim.start_ramp("auth", "disk_full")
    for _ in range(5):
        sim.tick("payments")
    assert sim.services["payments"].progress > sim.services["auth"].progress


def test_an_effective_action_slows_the_drift(sim):
    """One effective step is a counter-force, not a cure: the drift decelerates."""
    sim.start_ramp("payments", "connection_pool_exhaustion")
    before = sim.services["payments"].progress
    for _ in range(4):
        sim.tick("payments")
    unaided_rate = sim.services["payments"].progress - before

    result = sim.apply_action("payments", "scale_connection_pool", {"max_size": 200})
    assert result["effective"] and result["effect"] == "improving"
    mid = sim.services["payments"].progress
    for _ in range(4):
        sim.tick("payments")
    treated_rate = sim.services["payments"].progress - mid
    assert 0 < treated_rate < unaided_rate


def test_a_full_playbook_reverses_the_drift(sim):
    sim.start_ramp("payments", "connection_pool_exhaustion")
    for _ in range(10):
        sim.tick("payments")
    peak = sim.services["payments"].progress
    for action in ("scale_connection_pool", "recycle_connections", "set_circuit_breaker"):
        sim.apply_action("payments", action, {})
    for _ in range(4):
        sim.tick("payments")
    assert sim.services["payments"].progress < peak


def test_a_mismatched_action_makes_it_worse(sim):
    """This is what turns the bad-fix playbook into a rollback rather than a fluke."""
    sim.start_ramp("payments", "bad_deploy_latency_regression")
    for _ in range(6):
        sim.tick("payments")
    baseline = sim.services["payments"].progress
    for _ in range(4):
        sim.tick("payments")
    unaided_rate = sim.services["payments"].progress - baseline

    result = sim.apply_action("payments", "scale_replicas", {"count": 12})
    assert not result["effective"] and result["effect"] == "degrading"
    assert sim.services["payments"].relief < 0
    mid = sim.services["payments"].progress
    for _ in range(4):
        sim.tick("payments")
    assert sim.services["payments"].progress - mid > unaided_rate


def test_sustained_effective_remediation_reaches_recovery(sim):
    sim.start_ramp("inventory", "thread_pool_starvation")
    for _ in range(6):
        sim.tick("inventory")
    for action in ("scale_thread_pool", "shed_load", "set_retry_budget"):
        sim.apply_action("inventory", action, {})
    for _ in range(WINDOW_SAMPLES):
        sim.tick("inventory")
    assert sim.services["inventory"].status == "recovered"


def test_stop_ramp_returns_the_service_to_vitals(sim):
    sim.start_ramp("auth", "dns_timeout_cascade")
    sim.tick("auth")
    sim.stop_ramp("auth")
    assert sim.services["auth"].status == "healthy"
    assert sim.ready("auth")
    assert set(sim.window("auth")) == {"latency_p99_ms", "error_rate", "cpu_utilization"}


def test_window_text_is_serialized_as_a_precursor(sim):
    sim.start_ramp("payments", "memory_leak_oom")
    for _ in range(MIN_SAMPLES_TO_MATCH):
        sim.tick("payments")
    text = sim.window_text("payments")
    assert text.startswith("telemetry window phase precursor service payments")


def test_window_text_refuses_an_empty_window(sim):
    sim.start_ramp("payments", "disk_full")
    with pytest.raises(ValueError, match="no telemetry"):
        sim.window_text("payments")


@pytest.mark.parametrize(
    "archetype",
    ["connection_pool_exhaustion", "memory_leak_oom", "cache_stampede", "disk_full",
     "thread_pool_starvation", "dns_timeout_cascade", "bad_deploy_latency_regression"],
)
def test_a_live_ramp_retrieves_its_own_archetype_from_the_seeded_world(archetype):
    """The whole point of the canonical serialization, tested end to end.

    A live window, embedded exactly as Oracle would, must find the seeded
    precursor snapshots of the archetype it is actually ramping.
    """
    w = world.build(anchor=NOW)
    seeded = w.seeded_incidents
    library = np.array([embeddings.embed(i.window.precursor_text) for i in seeded])
    labels = [i.archetype for i in seeded]

    sim = FleetSimulator(seed=5, now=NOW)
    sim.start_ramp("payments", archetype)
    for _ in range(WINDOW_SAMPLES):  # one complete precursor sweep
        sim.tick("payments")

    probe = np.asarray(embeddings.embed(sim.window_text("payments")))
    top = np.argsort(-(library @ probe))[:14]
    matched = [labels[i] for i in top]
    assert matched[0] == archetype, f"nearest was {matched[0]}"
    assert matched.count(archetype) >= 10, f"only {matched.count(archetype)}/14 matched"


def test_cert_expiry_is_recognised_from_a_live_ramp():
    """Kept separate: cert expiry is a step function, so it needs a later probe."""
    w = world.build(anchor=NOW)
    seeded = w.seeded_incidents
    library = np.array([embeddings.embed(i.window.precursor_text) for i in seeded])
    labels = [i.archetype for i in seeded]

    sim = FleetSimulator(seed=5, now=NOW)
    sim.start_ramp("auth", "cert_expiry")
    for _ in range(WINDOW_SAMPLES):
        sim.tick("auth")
    probe = np.asarray(embeddings.embed(sim.window_text("auth")))
    top = np.argsort(-(library @ probe))[:14]
    assert labels[top[0]] == "cert_expiry"


def test_snapshot_reports_every_service(sim):
    snap = sim.snapshot()
    assert {s["service"] for s in snap} == set(archetypes.SERVICES)
    assert all("status" in s and "region" in s for s in snap)


def test_two_correct_steps_are_enough_to_reverse_a_drift(sim):
    """Calibration check: most playbooks in the catalogue are two or three steps,
    so requiring more than two to recover would make correct playbooks look like
    failures and turn every verification window into a rollback."""
    sim.start_ramp("payments", "connection_pool_exhaustion")
    for _ in range(10):
        sim.tick("payments")
    peak = sim.services["payments"].progress
    for action in ("scale_connection_pool", "recycle_connections"):
        sim.apply_action("payments", action, {})
    for _ in range(4):
        sim.tick("payments")
    assert sim.services["payments"].progress < peak


def test_reapplying_a_step_in_desired_state_changes_nothing(sim):
    sim.start_ramp("payments", "connection_pool_exhaustion")
    sim.tick("payments")
    first = sim.apply_action("payments", "scale_connection_pool", {"max_size": 200})
    relief = sim.services["payments"].relief
    second = sim.apply_action("payments", "scale_connection_pool", {"max_size": 200})
    assert first["effect"] == "improving"
    assert second["effect"] == "already_applied"
    assert sim.services["payments"].relief == relief


def test_the_same_action_with_different_params_is_a_different_step(sim):
    sim.start_ramp("payments", "connection_pool_exhaustion")
    sim.tick("payments")
    sim.apply_action("payments", "scale_connection_pool", {"max_size": 200})
    relief = sim.services["payments"].relief
    result = sim.apply_action("payments", "scale_connection_pool", {"max_size": 400})
    assert result["effect"] == "improving"
    assert sim.services["payments"].relief > relief


def test_reverting_removes_exactly_what_the_step_contributed(sim):
    """A rollback must restore the prior state, not stack a second remediation."""
    from generator.fleet import step_key

    sim.start_ramp("payments", "connection_pool_exhaustion")
    sim.tick("payments")
    before = sim.services["payments"].relief
    params = {"max_size": 200}
    sim.apply_action("payments", "scale_connection_pool", params)
    key = step_key("scale_connection_pool", params)
    result = sim.apply_action("payments", "scale_connection_pool", {"max_size": 50},
                              revert_of=key)
    assert result["effect"] == "reverted"
    assert sim.services["payments"].relief == pytest.approx(before)


def test_reverting_something_never_applied_is_a_no_op(sim):
    sim.start_ramp("payments", "disk_full")
    sim.tick("payments")
    relief = sim.services["payments"].relief
    result = sim.apply_action("payments", "extend_volume", {}, revert_of="never:applied")
    assert result["effect"] == "nothing_to_revert"
    assert sim.services["payments"].relief == relief


def test_applied_steps_are_visible_so_a_retry_can_see_them(sim):
    sim.start_ramp("payments", "connection_pool_exhaustion")
    sim.tick("payments")
    sim.apply_action("payments", "scale_connection_pool", {"max_size": 200})
    snap = {s["service"]: s for s in sim.snapshot()}
    assert snap["payments"]["applied_steps"] == [
        'scale_connection_pool:{"max_size": 200}']


def test_a_new_ramp_forgets_the_previous_incidents_remediations(sim):
    sim.start_ramp("payments", "connection_pool_exhaustion")
    sim.tick("payments")
    sim.apply_action("payments", "scale_connection_pool", {"max_size": 200})
    sim.start_ramp("payments", "disk_full")
    assert sim.services["payments"].applied == {}
    assert sim.services["payments"].relief == 0.0
