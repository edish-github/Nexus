"""Oracle's judgement, tested without a database.

`assess()` is deliberately a pure function of a neighbourhood, so the decisions
that matter — is this evidence, what failure is it, how confident, how long have
we got — can be tested directly rather than inferred from what landed in a table.
"""
from __future__ import annotations

import pytest


def neighbour(category: str, led: bool, similarity: float, *, lead: int = 90,
              end: str = "q8", start: str = "q3") -> dict:
    return {
        "id": f"snap-{category}-{similarity}",
        "outcome_category": category,
        "led_to_incident": led,
        "similarity": similarity,
        "digest": {
            "precursor_minutes": lead,
            "summary": {"pool_utilization": {"start": start, "end": end,
                                             "trend": "rising"}},
        },
    }


def live_digest(end: str = "q6", start: str = "q3") -> dict:
    return {"summary": {"pool_utilization": {"start": start, "end": end,
                                             "trend": "rising"}}}


def test_a_clear_precursor_cluster_produces_a_prediction(oracle):
    hood = [neighbour("connection_pool_exhaustion", True, 0.95 - i * 0.01) for i in range(12)]
    result = oracle.assess(hood, live_digest())
    assert result is not None
    assert result["outcome_category"] == "connection_pool_exhaustion"
    assert result["positives"] == 12 and result["negatives"] == 0
    assert result["confidence"] > 0.9


def test_too_few_close_neighbours_is_a_coincidence_not_a_pattern(oracle):
    hood = [neighbour("disk_full", True, 0.93), neighbour("disk_full", True, 0.91)]
    assert oracle.assess(hood, live_digest()) is None


def test_distant_neighbours_are_not_evidence(oracle):
    """Fourteen neighbours, none of them close, is still nothing to go on."""
    hood = [neighbour("cache_stampede", True, 0.40) for _ in range(14)]
    assert oracle.assess(hood, live_digest()) is None


def test_a_neighbourhood_that_all_recovered_emits_nothing(oracle):
    hood = [neighbour("cache_stampede", False, 0.9) for _ in range(10)]
    assert oracle.assess(hood, live_digest()) is None


def test_negatives_pull_the_posterior_down(oracle):
    """The benign-drift examples are what stop every wobble becoming an alarm."""
    confident = [neighbour("disk_full", True, 0.9) for _ in range(12)]
    contested = confident[:9] + [neighbour("disk_full", False, 0.9) for _ in range(3)]
    high = oracle.assess(confident, live_digest())
    low = oracle.assess(contested, live_digest())
    assert high["confidence"] > 0.9
    assert low is not None
    assert low["confidence"] < high["confidence"]
    assert low["beta"] > high["beta"]


def test_an_even_split_falls_below_the_emit_threshold(oracle):
    """Half the neighbours recovered: a coin flip is not a prediction."""
    hood = ([neighbour("disk_full", True, 0.9) for _ in range(6)]
            + [neighbour("disk_full", False, 0.9) for _ in range(6)])
    assert oracle.assess(hood, live_digest()) is None


def test_a_mostly_benign_neighbourhood_stays_silent(oracle):
    hood = ([neighbour("disk_full", True, 0.9) for _ in range(3)]
            + [neighbour("disk_full", False, 0.9) for _ in range(11)])
    assert oracle.assess(hood, live_digest()) is None


def test_alpha_and_beta_are_stored_not_a_bare_confidence(oracle):
    hood = ([neighbour("dns_timeout_cascade", True, 0.9) for _ in range(9)]
            + [neighbour("dns_timeout_cascade", False, 0.9) for _ in range(2)])
    result = oracle.assess(hood, live_digest())
    assert result["alpha"] == 10.0 and result["beta"] == 3.0
    low, high = result["credible_interval"]
    assert low < result["confidence"] < high


def test_the_majority_category_wins_the_vote(oracle):
    hood = ([neighbour("memory_leak_oom", True, 0.94) for _ in range(8)]
            + [neighbour("disk_full", True, 0.90) for _ in range(3)])
    assert oracle.assess(hood, live_digest())["outcome_category"] == "memory_leak_oom"


def test_negatives_do_not_get_a_vote_on_which_failure_it_is(oracle):
    """A recovered window says 'maybe nothing', not 'and it would have been X'."""
    hood = ([neighbour("memory_leak_oom", True, 0.93) for _ in range(7)]
            + [neighbour("cert_expiry", False, 0.99) for _ in range(3)])
    assert oracle.assess(hood, live_digest())["outcome_category"] == "memory_leak_oom"


def test_eta_shrinks_as_the_drift_progresses(oracle):
    hood = [neighbour("disk_full", True, 0.92, lead=120, end="q9", start="q2")
            for _ in range(10)]
    early = oracle.assess(hood, live_digest(end="q3", start="q2"))
    late = oracle.assess(hood, live_digest(end="q8", start="q2"))
    assert early["eta_minutes"] > late["eta_minutes"]
    assert late["progress"] > early["progress"]


def test_eta_is_always_in_the_future(oracle):
    hood = [neighbour("disk_full", True, 0.95, lead=60, end="q9", start="q1")
            for _ in range(10)]
    result = oracle.assess(hood, live_digest(end="q9", start="q1"))
    assert result["eta_minutes"] >= 5


def test_progress_falls_back_to_mid_drift_without_a_digest(oracle):
    hood = [neighbour("disk_full", True, 0.9) for _ in range(10)]
    assert oracle.assess(hood, {})["progress"] == 0.5


def test_evidence_lists_every_neighbour_consulted(oracle):
    hood = [neighbour("disk_full", True, 0.95 - i * 0.005) for i in range(14)]
    result = oracle.assess(hood, live_digest())
    assert len(result["evidence"]) == result["matched"] == 14
    assert all({"snapshot_id", "similarity", "led_to_incident"} <= set(e)
               for e in result["evidence"])


def test_severity_is_in_range(oracle):
    hood = [neighbour("cert_expiry", True, 0.95) for _ in range(10)]
    assert 1 <= oracle.assess(hood, live_digest())["severity"] <= 5


def test_causal_pattern_describes_what_is_moving(oracle):
    hood = [neighbour("connection_pool_exhaustion", True, 0.95) for _ in range(10)]
    assessment = oracle.assess(hood, live_digest())
    pattern = oracle._causal_pattern(assessment, live_digest())
    assert "pool_utilization" in pattern


@pytest.mark.parametrize("similarity", [0.71, 0.5, 0.0])
def test_the_similarity_floor_is_enforced(oracle, similarity):
    hood = [neighbour("disk_full", True, similarity) for _ in range(14)]
    assert oracle.assess(hood, live_digest()) is None
