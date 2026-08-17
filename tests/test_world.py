"""The demo world has to be reproducible and honestly split.

`make demo-reset` is run dozens of times before the video is recorded; if the
world drifts between runs, the rehearsed beats stop landing. And if a held-out
window leaks into the seeded set, the backtest numbers on the dashboard are a
lie — a quiet one, since everything still runs.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from generator import archetypes, world
from generator.trajectory import synthesize

ANCHOR = datetime(2026, 8, 17, tzinfo=UTC)


@pytest.fixture(scope="module")
def w() -> world.World:
    return world.build(anchor=ANCHOR)


def test_build_is_deterministic(w):
    again = world.build(anchor=ANCHOR)
    assert [i.key for i in again.incidents] == [i.key for i in w.incidents]
    assert [i.title for i in again.incidents] == [i.title for i in w.incidents]
    assert (
        again.incidents[0].window.precursor_text == w.incidents[0].window.precursor_text
    )
    assert again.incidents[7].window.trajectory.series == (
        w.incidents[7].window.trajectory.series
    )


def test_a_different_seed_builds_a_different_world(w):
    other = world.build(seed=1234, anchor=ANCHOR)
    assert other.incidents[0].window.precursor_text != w.incidents[0].window.precursor_text


def test_counts_follow_the_configured_world_size(w):
    """Asserted against the constants, not against literals: the world size is an
    operational choice (it is bounded by how fast the cluster will take vector
    writes), but the split arithmetic has to hold whatever it is set to."""
    assert len(w.incidents) == world.N_INCIDENTS
    assert len(w.negatives) == world.N_NEGATIVES
    assert len(w.backtest_incidents) == world.N_HOLDOUT_INCIDENTS
    assert len(w.backtest_negatives) == world.N_HOLDOUT_NEGATIVES
    assert len(w.seeded_incidents) == world.N_INCIDENTS - world.N_HOLDOUT_INCIDENTS
    assert len(w.seeded_negatives) == world.N_NEGATIVES - world.N_HOLDOUT_NEGATIVES


def test_the_holdout_is_a_meaningful_fraction(w):
    """A backtest on a handful of windows measures nothing; one on most of the
    world leaves nothing to seed with."""
    held = world.N_HOLDOUT_INCIDENTS / world.N_INCIDENTS
    assert 0.1 <= held <= 0.4, f"holding out {held:.0%} of incidents"
    assert world.N_NEGATIVES >= 20, "too few negatives to measure false alarms"


def test_holdout_and_seeded_sets_are_disjoint(w):
    seeded = {i.key for i in w.seeded_incidents}
    held = {i.key for i in w.backtest_incidents}
    assert not (seeded & held)
    seeded_neg = {n.key for n in w.seeded_negatives}
    held_neg = {n.key for n in w.backtest_negatives}
    assert not (seeded_neg & held_neg)


def test_every_archetype_appears_in_both_splits(w):
    seeded = {i.archetype for i in w.seeded_incidents}
    held = {i.archetype for i in w.backtest_incidents}
    assert seeded == {a.key for a in archetypes.ARCHETYPES}
    assert held == seeded, "a held-out archetype the seed never saw would not be a backtest"


def test_history_spans_ninety_days(w):
    span = w.incidents[-1].detected_at - w.incidents[0].detected_at
    assert timedelta(days=80) < span <= timedelta(days=world.HISTORY_DAYS)
    assert all(i.detected_at < ANCHOR for i in w.incidents)


def test_incidents_cover_the_whole_fleet(w):
    assert {i.service for i in w.incidents} == set(archetypes.SERVICES)
    assert {i.region for i in w.incidents} == set(archetypes.REGIONS)


def test_negatives_never_led_to_an_incident(w):
    assert all(not n.led_to_incident for n in w.negatives)
    assert all(n.trajectory.failure_range is None for n in w.negatives)


def test_negatives_drift_but_recover():
    """A negative must reach into precursor territory and then come back down."""
    arch = archetypes.get("connection_pool_exhaustion")
    rng = np.random.default_rng(3)
    neg = synthesize(arch, rng=rng, service="payments", region="aws-us-east-1",
                     led_to_incident=False)
    series = neg.series["pool_utilization"]
    baseline = arch.metrics["pool_utilization"].baseline
    assert max(series) > baseline * 1.3, "the drift never became alarming"
    assert series[-1] < max(series) * 0.8, "the window never recovered"


def test_precursor_window_excludes_the_failure(w):
    inc = w.incidents[0]
    pre_end = inc.window.trajectory.precursor_range[1]
    fail_start = inc.window.trajectory.failure_range[0]
    assert pre_end == fail_start, "the precursor must stop exactly where the failure starts"


@pytest.mark.parametrize("fraction", [0.0, 0.25, 0.5, 0.75, 0.99])
def test_the_failure_phase_is_worse_than_the_precursor(w, fraction):
    """Measured as distance from baseline, since some metrics fall towards failure
    (certificate days remaining, cache hit ratio) rather than rising."""
    inc = w.incidents[int(fraction * (len(w.incidents) - 1))]
    arch = archetypes.get(inc.archetype)
    pre = inc.window.trajectory.precursor_metrics()
    fail = inc.window.trajectory.failure_metrics()
    for metric, spec in arch.metrics.items():
        pre_reach = max(abs(v - spec.baseline) for v in pre[metric])
        fail_reach = max(abs(v - spec.baseline) for v in fail[metric])
        assert fail_reach >= pre_reach * 0.95, f"{inc.archetype}/{metric} did not worsen"


def test_precursor_lead_time_is_between_one_and_three_hours(w):
    for inc in w.incidents:
        assert 60 <= inc.window.trajectory.precursor_minutes <= 180


def test_window_bounds_line_up_with_detection(w):
    for inc in w.incidents[:25]:
        assert inc.window.window_end == inc.detected_at
        gap = inc.window.window_end - inc.window.window_start
        assert gap == timedelta(minutes=inc.window.trajectory.precursor_minutes)


def test_digest_carries_the_label_and_the_series(w):
    digest = w.incidents[0].window.digest
    assert digest["archetype"] == w.incidents[0].archetype
    assert digest["led_to_incident"] is True
    assert digest["metrics"] and digest["summary"]


def test_resolved_after_detected(w):
    assert all(i.resolved_at > i.detected_at and i.mttr_seconds > 0 for i in w.incidents)
