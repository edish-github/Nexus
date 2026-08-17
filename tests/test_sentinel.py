"""Sentinel's claim and its tiered gate.

The claim is tested against a fake connection rather than a live database
because what matters is the decision it makes on each row state — pending,
already claimed, gone — and those are awkward to arrange for real and trivial to
state directly. The genuine concurrency test (five deliveries, one execution)
runs against the cluster in `scripts/pipeline_local.py`.
"""
from __future__ import annotations

import pytest

from nexus_common.posterior import Candidate


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeConn:
    """Records every statement and replays scripted results in order."""

    def __init__(self, results: list):
        self.results = list(results)
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params or ()))
        rows = self.results.pop(0) if self.results else []
        return FakeCursor(rows)

    def said(self, fragment: str) -> bool:
        return any(fragment.lower() in sql.lower() for sql, _ in self.statements)


PENDING_ROW = [(
    "pred-1", "payments", "connection_pool_exhaustion", 4, 13.0, 2.0,
    "[0.1,0.2]", "pending", None,
)]


def candidate(pid: str, s: int, f: int, sim: float = 0.9, reversible: bool = True,
              name: str | None = None) -> Candidate:
    return Candidate(
        playbook_id=pid, name=name or pid, similarity=sim, successes=s, failures=f,
        reversible=reversible, remediation_steps=[], inverse_steps=[],
        generation=2, memory_tier="operational",
    )


# -- the claim ------------------------------------------------------------- #

def test_a_pending_prediction_is_claimed(sentinel):
    conn = FakeConn([PENDING_ROW, []])
    claimed = sentinel.claim(conn, "pred-1", "sentinel-a")
    assert claimed["id"] == "pred-1"
    assert claimed["outcome_category"] == "connection_pool_exhaustion"
    assert conn.said("FOR UPDATE"), "the claim must lock the row it is taking"
    assert conn.said("SET prevention_status = 'preventing'")


def test_a_duplicate_delivery_finds_the_row_already_claimed(sentinel):
    taken = [("pred-1", "payments", "connection_pool_exhaustion", 4, 13.0, 2.0,
              "[0.1]", "preventing", "sentinel-a")]
    conn = FakeConn([taken])
    with pytest.raises(sentinel.AlreadyClaimed) as excinfo:
        sentinel.claim(conn, "pred-1", "sentinel-b")
    assert excinfo.value.holder == "sentinel-a"
    assert excinfo.value.status == "preventing"
    assert not conn.said("UPDATE predictions"), "a losing claimant must not write"


def test_a_missing_prediction_is_not_a_crash(sentinel):
    conn = FakeConn([[]])
    with pytest.raises(sentinel.AlreadyClaimed) as excinfo:
        sentinel.claim(conn, "gone", "sentinel-a")
    assert excinfo.value.status == "missing"


def test_an_already_resolved_prediction_is_not_reclaimed(sentinel):
    done = [("pred-1", "payments", "disk_full", 4, 13.0, 2.0, "[0.1]",
             "prevented", "sentinel-a")]
    with pytest.raises(sentinel.AlreadyClaimed):
        sentinel.claim(FakeConn([done]), "pred-1", "sentinel-b")


# -- the tiered gate ------------------------------------------------------- #

def test_low_confidence_shadows_even_a_reversible_playbook(sentinel):
    tier, why = sentinel.decide_tier(0.62, candidate("p", 20, 1))
    assert tier == "shadow"
    assert "0.75" in why


def test_high_confidence_and_reversible_acts(sentinel):
    tier, _ = sentinel.decide_tier(0.91, candidate("p", 20, 1, reversible=True))
    assert tier == "auto"


def test_high_confidence_and_irreversible_asks_a_human(sentinel):
    tier, why = sentinel.decide_tier(0.91, candidate("p", 20, 1, reversible=False))
    assert tier == "approve"
    assert "rolled back" in why or "inverse" in why


def test_the_threshold_boundary_acts_rather_than_shadows(sentinel):
    assert sentinel.decide_tier(0.75, candidate("p", 9, 2))[0] == "auto"
    assert sentinel.decide_tier(0.7499, candidate("p", 9, 2))[0] == "shadow"


def test_confidence_is_the_predictions_not_the_playbooks(sentinel):
    """A brilliant playbook does not make a weak prediction actionable."""
    tier, _ = sentinel.decide_tier(0.5, candidate("great", 99, 0))
    assert tier == "shadow"


def test_irreversible_reason_names_the_offending_action(sentinel):
    c = Candidate(
        playbook_id="p", name="rotate", similarity=0.9, successes=8, failures=1,
        reversible=False, generation=1, memory_tier="operational", inverse_steps=[],
        remediation_steps=[
            {"action": "shed_load", "target": "auth", "params": {},
             "inverse": {"action": "shed_load", "params": {}}},
            {"action": "rotate_certificate", "target": "auth", "params": {}},
        ],
    )
    assert "rotate_certificate" in sentinel._irreversible_reason(c)


def test_unparseable_steps_do_not_crash_the_explanation(sentinel):
    c = candidate("p", 1, 1, reversible=False)
    object.__setattr__(c, "remediation_steps", {"not": "a list"})
    assert "step schema" in sentinel._irreversible_reason(c)


# -- candidate retrieval --------------------------------------------------- #

def test_candidate_query_filters_by_category_status_and_distance(sentinel):
    conn = FakeConn([[]])
    sentinel.candidates_for(conn, "[0.1]", "disk_full")
    sql, params = conn.statements[0]
    assert "outcome_category = %s" in sql
    assert "status = 'active'" in sql
    assert "<=> %s::VECTOR <" in sql
    assert sentinel.MAX_DISTANCE in params
    assert sentinel.CANDIDATE_LIMIT in params


def test_candidates_are_parsed_into_the_shared_candidate_type(sentinel):
    row = [("pb-1", "Adaptive pool", 17, 1, True,
            [{"action": "scale_connection_pool", "target": "payments", "params": {}}],
            [], 3, "operational", 0.94)]
    got = sentinel.candidates_for(FakeConn([row]), "[0.1]", "connection_pool_exhaustion")
    assert len(got) == 1
    assert got[0].posterior_mean == pytest.approx(0.9)
    assert got[0].similarity == pytest.approx(0.94)
    assert got[0].reversible is True
