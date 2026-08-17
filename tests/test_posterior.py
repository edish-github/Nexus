"""The Beta posterior and Thompson sampling — the selection maths.

The property that has to hold is not "the best playbook usually wins" but
"a newborn playbook sometimes wins". If exploration ever stops, evolution stops
with it: the challenger never gathers evidence, never improves its posterior,
and dies by TTL having never been tried.
"""
from __future__ import annotations

import numpy as np
import pytest

from nexus_common import posterior
from nexus_common.posterior import Candidate


def candidate(pid: str, s: int, f: int, sim: float = 1.0, reversible: bool = True
              ) -> Candidate:
    return Candidate(
        playbook_id=pid, name=pid, similarity=sim, successes=s, failures=f,
        reversible=reversible, remediation_steps=[], inverse_steps=[],
        generation=1, memory_tier="operational",
    )


def test_mean_is_the_laplace_smoothed_rate():
    assert posterior.mean(0, 0) == 0.5
    assert posterior.mean(1, 0) == pytest.approx(2 / 3)
    assert posterior.mean(17, 1) == pytest.approx(0.9)
    assert posterior.mean(0, 8) == pytest.approx(0.1)


def test_evidence_narrows_the_interval():
    """Same mean, more trials, tighter interval — the thing a bare float hides."""
    few = posterior.credible_interval(2, 0)
    many = posterior.credible_interval(80, 20)
    assert (few[1] - few[0]) > (many[1] - many[0])


def test_interval_brackets_the_mean_and_stays_a_probability():
    for s, f in [(0, 0), (1, 0), (17, 1), (3, 9), (100, 3)]:
        low, high = posterior.credible_interval(s, f)
        assert 0.0 <= low <= posterior.mean(s, f) <= high <= 1.0


def test_a_wider_level_gives_a_wider_interval():
    narrow = posterior.credible_interval(10, 5, level=0.5)
    wide = posterior.credible_interval(10, 5, level=0.99)
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])


def test_samples_stay_in_the_unit_interval():
    rng = np.random.default_rng(0)
    draws = [posterior.sample(3, 2, rng) for _ in range(500)]
    assert all(0.0 <= d <= 1.0 for d in draws)


def test_a_zero_trial_challenger_beats_a_strong_incumbent_sometimes():
    """The whole reason selection samples instead of taking the argmax."""
    rng = np.random.default_rng(7)
    incumbent = candidate("incumbent", 17, 1)
    challenger = candidate("challenger", 0, 0)
    wins = sum(
        posterior.compete([incumbent, challenger], rng)[0].candidate.playbook_id
        == "challenger"
        for _ in range(2000)
    )
    # Frequent enough that exploration is real, rare enough that it is not random.
    assert 100 < wins < 900, f"challenger won {wins}/2000"


def test_the_stronger_playbook_still_wins_most_of_the_time():
    rng = np.random.default_rng(11)
    incumbent = candidate("incumbent", 17, 1)
    challenger = candidate("challenger", 0, 0)
    wins = sum(
        posterior.compete([incumbent, challenger], rng)[0].candidate.playbook_id
        == "incumbent"
        for _ in range(2000)
    )
    assert wins > 1100, "selection is not exploiting what it knows"


def test_similarity_weights_the_draw():
    """A perfect playbook for the wrong pattern should lose to a decent one for
    the right pattern."""
    rng = np.random.default_rng(3)
    off_target = candidate("off", 40, 0, sim=0.30)
    on_target = candidate("on", 12, 3, sim=0.98)
    wins = sum(
        posterior.compete([off_target, on_target], rng)[0].candidate.playbook_id == "on"
        for _ in range(500)
    )
    assert wins > 450


def test_a_consistently_failing_playbook_almost_never_wins():
    rng = np.random.default_rng(5)
    good = candidate("good", 20, 1)
    bad = candidate("bad", 1, 6)
    wins = sum(
        posterior.compete([good, bad], rng)[0].candidate.playbook_id == "bad"
        for _ in range(2000)
    )
    assert wins < 200, f"the bad playbook won {wins}/2000"


def test_every_candidate_gets_a_draw_and_they_are_ranked():
    rng = np.random.default_rng(1)
    pool = [candidate(f"p{i}", i, 8 - i) for i in range(8)]
    winner, draws = posterior.compete(pool, rng)
    assert len(draws) == len(pool)
    assert draws[0] is winner
    assert [d.score for d in draws] == sorted((d.score for d in draws), reverse=True)


def test_an_empty_competition_is_an_error_not_a_silent_none():
    with pytest.raises(ValueError, match="at least one candidate"):
        posterior.compete([], np.random.default_rng(0))


def test_explain_exposes_every_number_the_ui_needs():
    rng = np.random.default_rng(2)
    _, draws = posterior.compete([candidate("a", 3, 1), candidate("b", 0, 0)], rng)
    rows = posterior.explain(draws)
    assert len(rows) == 2
    for row in rows:
        assert {"playbook_id", "similarity", "successes", "failures",
                "posterior_mean", "beta_sample", "score"} <= set(row)


def test_shadow_weight_is_defined_once_and_is_a_discount():
    assert 0.0 < posterior.SHADOW_WEIGHT < 1.0


def test_candidate_reports_its_own_posterior():
    c = candidate("x", 17, 1)
    assert c.trials == 18
    assert c.posterior_mean == pytest.approx(0.9)
