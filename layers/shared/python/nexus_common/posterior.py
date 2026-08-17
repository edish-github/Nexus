"""Beta posteriors: the one place fitness is computed.

Fitness is never stored. A playbook's quality is `Beta(success_count + 1,
failure_count + 1)` derived from its counters at read time, which is what keeps
one source of truth and stops a cached float drifting away from the evidence.

Two things are computed from that posterior:

* **The mean and a credible interval** — what the dashboard shows, and what the
  tiered gate compares against its thresholds. A mean alone hides the difference
  between 1 success out of 1 and 90 out of 100; both have mean 0.67 and 0.90 but
  wildly different intervals.
* **A Thompson sample** — what selection uses. Sampling rather than taking the
  argmax is the whole reason a newborn playbook ever gets a turn: with Beta(1,1)
  its posterior is uniform, so roughly one draw in ten beats a 0.9 incumbent's
  sample and the challenger gets to prove itself.
"""
from __future__ import annotations

from dataclasses import dataclass

# Shadow-tier trials are scored against the eventual outcome but count for less
# than a real execution: the playbook was never actually run, so the evidence is
# weaker. Written down here rather than inline so Sentinel and Chronicler cannot
# disagree about it.
SHADOW_WEIGHT = 0.3


def mean(successes: float, failures: float) -> float:
    """Posterior mean of Beta(s+1, f+1)."""
    return (successes + 1.0) / (successes + failures + 2.0)


def variance(successes: float, failures: float) -> float:
    a, b = successes + 1.0, failures + 1.0
    n = a + b
    return (a * b) / (n * n * (n + 1.0))


def credible_interval(successes: float, failures: float, level: float = 0.9
                      ) -> tuple[float, float]:
    """Equal-tailed credible interval for Beta(s+1, f+1).

    Uses the exact Beta quantile when SciPy is available and a normal
    approximation otherwise — the Lambda layer ships numpy but not SciPy, and a
    displayed interval is not worth another 30 MB of dependency.
    """
    a, b = successes + 1.0, failures + 1.0
    tail = (1.0 - level) / 2.0
    try:
        from scipy.stats import beta as _beta  # type: ignore[import-not-found]

        return float(_beta.ppf(tail, a, b)), float(_beta.ppf(1.0 - tail, a, b))
    except ImportError:
        import numpy as np

        m = a / (a + b)
        sd = float(np.sqrt(variance(successes, failures)))
        # 90% → 1.6449, 95% → 1.9600; good enough once there are a few trials,
        # and clamped so it never reports an impossible probability.
        z = float(np.sqrt(2.0) * _erfinv(level))
        return max(0.0, m - z * sd), min(1.0, m + z * sd)


def _erfinv(x: float) -> float:
    """Inverse error function (Giles' rational approximation), for the z-score."""
    import numpy as np

    w = -np.log((1.0 - x) * (1.0 + x))
    if w < 5.0:
        w -= 2.5
        p = 2.81022636e-08
        for c in (3.43273939e-07, -3.5233877e-06, -4.39150654e-06,
                  0.00021858087, -0.00125372503, -0.00417768164,
                  0.246640727, 1.50140941):
            p = p * w + c
    else:
        w = np.sqrt(w) - 3.0
        p = -0.000200214257
        for c in (0.000100950558, 0.00134934322, -0.00367342844,
                  0.00573950773, -0.0076224613, 0.00943887047,
                  1.00167406, 2.83297682):
            p = p * w + c
    return float(p * x)


def sample(successes: float, failures: float, rng) -> float:
    """One draw from Beta(s+1, f+1). `rng` is a numpy Generator."""
    return float(rng.beta(successes + 1.0, failures + 1.0))


@dataclass(frozen=True)
class Candidate:
    """A playbook competing for one prediction."""

    playbook_id: str
    name: str
    similarity: float
    successes: int
    failures: int
    reversible: bool
    remediation_steps: list
    inverse_steps: list
    generation: int
    memory_tier: str

    @property
    def posterior_mean(self) -> float:
        return mean(self.successes, self.failures)

    @property
    def trials(self) -> int:
        return self.successes + self.failures


@dataclass(frozen=True)
class Draw:
    """One candidate's showing in a competition — the row the UI explains."""

    candidate: Candidate
    beta_sample: float
    score: float


def compete(candidates: list[Candidate], rng) -> tuple[Draw, list[Draw]]:
    """Run one Thompson-sampled competition.

    Each candidate's score is its Beta draw weighted by how well its precursor
    pattern matches the situation at hand — a contextual bandit, where the
    context is vector similarity. Returns the winner and every draw, because a
    competition nobody can inspect is just a black box with extra steps.
    """
    if not candidates:
        raise ValueError("a competition needs at least one candidate")
    draws = []
    for candidate in candidates:
        drawn = sample(candidate.successes, candidate.failures, rng)
        draws.append(Draw(candidate=candidate, beta_sample=drawn,
                          score=drawn * candidate.similarity))
    draws.sort(key=lambda d: -d.score)
    return draws[0], draws


def explain(draws: list[Draw]) -> list[dict]:
    """Render a competition as the JSON stored in `evolution_log.details`."""
    return [
        {
            "playbook_id": d.candidate.playbook_id,
            "name": d.candidate.name,
            "similarity": round(d.candidate.similarity, 4),
            "successes": d.candidate.successes,
            "failures": d.candidate.failures,
            "posterior_mean": round(d.candidate.posterior_mean, 4),
            "beta_sample": round(d.beta_sample, 4),
            "score": round(d.score, 4),
            "generation": d.candidate.generation,
            "memory_tier": d.candidate.memory_tier,
        }
        for d in draws
    ]
