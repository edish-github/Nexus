"""The Darwinian engine: what each lifecycle transition does, and when it refuses.

Every test here drives the transition functions against a scripted connection
rather than the cluster. What is being checked is the *decision* — which counter
moves, which threshold holds, which relative is not a sibling — and those are
stated far more clearly as inputs than arranged as database state. The
full-lifecycle run against the live cluster is `scripts/lifecycle_local.py`.
"""
from __future__ import annotations

import json

import pytest

from nexus_common.posterior import SHADOW_WEIGHT


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeConn:
    """Records every statement and replays scripted results in order."""

    def __init__(self, results: list | None = None):
        self.results = list(results or [])
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params or ()))
        rows = self.results.pop(0) if self.results else []
        return FakeCursor(rows)

    def said(self, fragment: str) -> bool:
        return any(fragment.lower() in sql.lower() for sql, _ in self.statements)


def playbook(**overrides) -> dict:
    base = {
        "id": "pb-1", "crdb_region": "aws-us-east-1", "name": "Adaptive pool with breaker",
        "outcome_category": "connection_pool_exhaustion", "success_count": 17,
        "failure_count": 1, "generation": 3, "parent_id": "pb-0", "lineage": ["pb-0"],
        "memory_tier": "operational", "status": "active", "reversible": True,
        "remediation_steps": [{"action": "scale_connection_pool", "target": "{service}",
                               "params": {"max_size": 200},
                               "inverse": {"action": "scale_connection_pool",
                                           "params": {"max_size": 50}}}],
        "inverse_steps": [], "embedding": "[1.0,0.0,0.0]",
    }
    return {**base, **overrides}


DRAFT_JSON = json.dumps({
    "name": "Breaker-first relief",
    "outcome_category": "connection_pool_exhaustion",
    "rationale": "the parent widened the pool into a saturated upstream",
    "remediation_steps": [{
        "action": "set_circuit_breaker", "target": "{service}",
        "params": {"error_threshold": 0.1},
        "inverse": {"action": "set_circuit_breaker", "params": {"error_threshold": 0.5}},
    }],
})


# -- the structural invariant ---------------------------------------------- #

def test_no_transition_writes_its_own_log_row(chronicler):
    """`_record` is the only writer, so a mutation cannot escape without its row."""
    conn = FakeConn([[], [(0,)], [], [], [], [("child-1",)]])
    pb = playbook()
    chronicler.grow(conn, pb, "success", prediction_id="p", incident_id="i", reason="r")
    chronicler.promote(conn, playbook(success_count=20), incident_id="i")
    chronicler.retire(conn, playbook(success_count=0, failure_count=9), incident_id="i")
    chronicler.mutate(conn, pb, chronicler.PlaybookDraft.model_validate_json(DRAFT_JSON),
                      "test", incident_id="i", reason="r")
    assert not conn.said("INSERT INTO evolution_log")


def test_record_writes_one_row_per_event(chronicler):
    conn = FakeConn()
    chronicler._record(conn, [
        chronicler.Lifecycle("growth", "pb-1", {"verdict": "success"}),
        chronicler.Lifecycle("promotion", "pb-1", {}, fitness_after=0.94),
    ])
    assert len(conn.statements) == 2
    assert all("INSERT INTO evolution_log" in sql for sql, _ in conn.statements)


# -- growth ---------------------------------------------------------------- #

def test_a_prevented_trial_is_a_success(chronicler):
    conn, pb = FakeConn(), playbook(success_count=4, failure_count=1)
    event = chronicler.grow(conn, pb, "success", prediction_id="p", incident_id="i",
                            reason="guardian reported 'prevented'")
    assert pb["success_count"] == 5
    assert event.event_type == "growth"
    assert event.fitness_before == pytest.approx(5 / 7, abs=1e-4)
    assert event.fitness_after == pytest.approx(6 / 8, abs=1e-4)
    assert conn.said("success_count = success_count + 1")


def test_a_rolled_back_trial_is_a_failure(chronicler):
    conn, pb = FakeConn(), playbook(success_count=1, failure_count=6)
    event = chronicler.grow(conn, pb, "failure", prediction_id="p", incident_id=None,
                            reason="r")
    assert pb["failure_count"] == 7
    assert event.fitness_after < event.fitness_before


def test_growth_resets_the_disuse_clock(chronicler):
    """The 90-day TTL is a last-use clock, and this is the only thing winding it."""
    conn = FakeConn()
    chronicler.grow(conn, playbook(), "success", prediction_id="p", incident_id=None,
                    reason="r")
    assert conn.said("last_used_at = now()")
    assert conn.said(f"expires_at = now() + INTERVAL '{chronicler.DISUSE_DAYS} days'")


def test_an_inconclusive_verification_is_not_evidence(chronicler):
    """Flat is not success and it is not failure; it must move no counter."""
    assert "inconclusive" not in chronicler.TRIAL_VERDICTS
    assert "not_executed" not in chronicler.TRIAL_VERDICTS
    assert "no_substrate" not in chronicler.TRIAL_VERDICTS


# -- shadow scoring -------------------------------------------------------- #

def test_shadow_credit_accrues_without_moving_a_counter(chronicler):
    conn, pb = FakeConn([[("success", 0.6, 0)]]), playbook(success_count=4, failure_count=1)
    event = chronicler.grow_shadow(conn, pb, "success", prediction_id="p",
                                   incident_id="i", reason="r")
    assert pb["success_count"] == 4, "0.9 of a trial is not a trial"
    assert event.details["shadow"] == {"outcome": "success", "weight": SHADOW_WEIGHT,
                                       "banked": 0, "accrued": 0.9}
    assert not conn.said("UPDATE playbooks")


def test_shadow_credit_banks_a_whole_trial_when_one_is_due(chronicler):
    """Three and a third shadow observations are worth one execution."""
    conn, pb = FakeConn([[("success", 0.9, 0)]]), playbook(success_count=4, failure_count=1)
    event = chronicler.grow_shadow(conn, pb, "success", prediction_id="p",
                                   incident_id="i", reason="r")
    assert pb["success_count"] == 5
    assert event.details["shadow"]["banked"] == 1
    assert conn.said("success_count = success_count + %s")


def test_shadow_credit_already_banked_is_not_banked_twice(chronicler):
    conn, pb = FakeConn([[("success", 1.2, 1)]]), playbook()
    event = chronicler.grow_shadow(conn, pb, "success", prediction_id="p",
                                   incident_id=None, reason="r")
    assert event.details["shadow"]["banked"] == 0
    assert not conn.said("UPDATE playbooks")


def test_a_shadow_that_never_materialized_is_a_false_alarm(chronicler):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    shadow = [("pred-1", "payments", "connection_pool_exhaustion", "pb-1",
               now - timedelta(hours=2), now - timedelta(hours=1))]
    conn = FakeConn([shadow, [], []])
    events = chronicler.settle_shadows(conn, now)
    assert events == [], "a failure that never arrived says nothing about the playbook"
    assert conn.said("prevention_status = 'false_alarm'")


def test_a_shadow_resolved_by_a_different_playbook_costs_the_choice(chronicler):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    shadow = [("pred-1", "payments", "connection_pool_exhaustion", "pb-1",
               now - timedelta(hours=2), now - timedelta(hours=1))]
    incident = [("inc-1", "pb-9", True, "resolved")]
    pb_row = [tuple(playbook().values())]
    conn = FakeConn([shadow, incident, [], pb_row, [("failure", 0.0, 0)]])
    events = chronicler.settle_shadows(conn, now)
    assert conn.said("prevention_status = 'missed'")
    assert len(events) == 1
    assert events[0].details["shadow"]["outcome"] == "failure"


# -- mutation -------------------------------------------------------------- #

def test_a_rollback_does_not_breed_while_an_untried_variant_exists(chronicler):
    assert chronicler.has_untried_child(FakeConn([[(1,)]]), "pb-1") is True
    assert chronicler.has_untried_child(FakeConn([[(0,)]]), "pb-1") is False


def test_a_variant_starts_at_generation_plus_one_with_a_flat_prior(chronicler):
    conn = FakeConn([[("child-1",)]])
    parent = playbook(generation=2, lineage=["pb-0"], success_count=1, failure_count=6)
    draft = chronicler.PlaybookDraft.model_validate_json(DRAFT_JSON)
    event = chronicler.mutate(conn, parent, draft, "bedrock", incident_id="i",
                              reason="rolled back")
    _, params = conn.statements[0]
    assert params[7] == 3, "the child is one generation on from its parent"
    assert params[8] == "pb-1", "parent_id points at what failed"
    assert params[9] == ["pb-0", "pb-1"], "lineage gains the parent it descends from"
    assert event.event_type == "mutation"
    assert event.fitness_after == 0.5
    assert event.details["parent_status"] == "active", "the parent is not retired by this"


def test_a_malformed_genome_is_stillborn(chronicler):
    assert chronicler._validated("{not json", "bedrock", context="mutation") is None
    assert chronicler._validated(
        json.dumps({"name": "x", "outcome_category": "y", "remediation_steps": []}),
        "bedrock", context="mutation") is None


def test_an_unknown_action_is_rejected_before_it_can_be_executed(chronicler):
    payload = json.dumps({
        "name": "Delete production", "outcome_category": "disk_full",
        "remediation_steps": [{"action": "drop_database", "target": "{service}"}],
    })
    assert chronicler._validated(payload, "bedrock", context="mutation") is None


def test_a_fenced_proposal_is_still_read(chronicler):
    draft = chronicler._validated(f"```json\n{DRAFT_JSON}\n```", "bedrock",
                                  context="mutation")
    assert draft is not None and draft.name == "Breaker-first relief"


# -- merge ----------------------------------------------------------------- #

def sibling_row(*, successes=9, failures=3, distance=0.06, lineage=None, pid="pb-2"):
    return [(pid, "aws-us-east-1", "Graceful recycle", successes, failures, 2,
             lineage or [], [], "[0.99,0.14,0.0]", distance)]


def test_a_close_and_healthy_sibling_is_a_merge_candidate(chronicler):
    got = chronicler.merge_candidate(FakeConn([sibling_row()]), playbook())
    assert got is not None and got["id"] == "pb-2"


def test_a_weak_sibling_is_not_merged(chronicler):
    """Merging in a failing playbook would launder its failures into a new genome."""
    weak = sibling_row(successes=1, failures=8)
    assert chronicler.merge_candidate(FakeConn([weak]), playbook()) is None


def test_a_weak_playbook_does_not_merge_into_a_strong_one(chronicler):
    assert chronicler.merge_candidate(
        FakeConn([sibling_row()]), playbook(success_count=1, failure_count=8)) is None


def test_a_descendant_is_not_a_sibling(chronicler):
    """A variant sits at its parent's position, so without this it merges straight back."""
    child = sibling_row(lineage=["pb-0", "pb-1"], distance=0.0)
    assert chronicler.merge_candidate(FakeConn([child]), playbook()) is None


def test_an_ancestor_is_not_a_sibling(chronicler):
    assert chronicler.merge_candidate(FakeConn([sibling_row(pid="pb-0")]),
                                      playbook()) is None


def test_the_canonical_child_inherits_the_conservative_reading(chronicler):
    conn = FakeConn([[("child-1",)], []])
    left = playbook(success_count=11, failure_count=4, lineage=["pb-0"])
    right = dict(zip(
        ("id", "crdb_region", "name", "success_count", "failure_count", "generation",
         "lineage", "remediation_steps", "embedding", "distance"),
        sibling_row()[0], strict=True))
    draft = chronicler.PlaybookDraft.model_validate_json(DRAFT_JSON)
    events = chronicler.merge(conn, left, right, draft, "bedrock", incident_id="i")
    _, params = conn.statements[0]
    assert params[7] == 9, "successes: the weaker parent's, never the sum"
    assert params[8] == 4, "failures: the worse parent's"
    assert params[11] == ["pb-0", "pb-1", "pb-2"], "lineage is the union, in order"
    assert conn.said("status = 'merged'")
    assert len(events) == 2, "one edge per absorbed parent"
    assert {e.parent_id for e in events} == {"pb-1", "pb-2"}


def test_neither_parent_is_deleted_by_a_merge(chronicler):
    conn = FakeConn([[("child-1",)], []])
    right = dict(zip(
        ("id", "crdb_region", "name", "success_count", "failure_count", "generation",
         "lineage", "remediation_steps", "embedding", "distance"),
        sibling_row()[0], strict=True))
    chronicler.merge(conn, playbook(success_count=11, failure_count=4), right,
                     chronicler.PlaybookDraft.model_validate_json(DRAFT_JSON),
                     "bedrock", incident_id=None)
    assert not conn.said("DELETE FROM playbooks")


def test_the_child_sits_between_its_parents(chronicler):
    import numpy as np

    mid = json.loads(chronicler._midpoint("[1.0,0.0]", "[0.0,1.0]"))
    assert np.linalg.norm(mid) == pytest.approx(1.0, abs=1e-6)
    assert mid[0] == pytest.approx(mid[1], abs=1e-6)


# -- promotion ------------------------------------------------------------- #

def test_a_proven_playbook_is_promoted_to_the_global_table(chronicler):
    conn = FakeConn([[(0,)], [], []])
    event = chronicler.promote(conn, playbook(success_count=18, failure_count=1),
                               incident_id="i")
    assert event is not None and event.event_type == "promotion"
    assert conn.said("INSERT INTO institutional_playbooks")
    assert conn.said("memory_tier = 'institutional'")


def test_the_promotion_cusp_needs_one_more_success(chronicler):
    """The seeded 17/1 playbook is at mean 0.900 exactly — the gate is `> 0.9`."""
    assert chronicler.promote(FakeConn(), playbook(success_count=17, failure_count=1),
                              incident_id=None) is None
    assert chronicler.promote(FakeConn([[(0,)], [], []]),
                              playbook(success_count=18, failure_count=1),
                              incident_id=None) is not None


def test_a_lucky_newborn_cannot_reach_the_institutional_tier(chronicler):
    """Beta(1,1) plus two wins is mean 0.75 on two trials. Trials are the guard."""
    assert chronicler.promote(FakeConn(), playbook(success_count=2, failure_count=0),
                              incident_id=None) is None


def test_promotion_needs_the_mean_as_well_as_the_trials(chronicler):
    assert chronicler.promote(FakeConn(), playbook(success_count=15, failure_count=4),
                              incident_id=None) is None


def test_promotion_is_not_repeated(chronicler):
    assert chronicler.promote(FakeConn([[(1,)]]), playbook(success_count=17,
                              failure_count=1), incident_id=None) is None
    assert chronicler.promote(FakeConn(), playbook(success_count=17, failure_count=1,
                              memory_tier="institutional"), incident_id=None) is None


# -- retirement ------------------------------------------------------------ #

def test_a_playbook_that_keeps_failing_is_retired(chronicler):
    conn = FakeConn()
    event = chronicler.retire(conn, playbook(success_count=1, failure_count=9),
                              incident_id="i")
    assert event is not None and event.event_type == "retirement"
    assert conn.said("status = 'retired'")
    assert conn.said("memory_tier = 'retired'")


def test_retirement_waits_for_enough_evidence(chronicler):
    """One failure out of one is mean 0.33 — unlucky, not proven bad."""
    assert chronicler.retire(FakeConn(), playbook(success_count=0, failure_count=1),
                             incident_id=None) is None


def test_the_bad_fix_survives_until_it_has_really_earned_retirement(chronicler):
    """The seeded bad playbook is 1/6 — mean 0.222, still selectable on purpose."""
    assert chronicler.retire(FakeConn(), playbook(success_count=1, failure_count=6),
                             incident_id=None) is None
    assert chronicler.retire(FakeConn(), playbook(success_count=1, failure_count=9),
                             incident_id=None) is not None


def test_retirement_does_not_shorten_the_disuse_ttl(chronicler):
    """Expiring the ancestry early would delete the genealogy the tree is drawn from."""
    conn = FakeConn()
    chronicler.retire(conn, playbook(success_count=1, failure_count=9), incident_id=None)
    assert not conn.said("expires_at")


def test_an_already_retired_playbook_is_not_retired_again(chronicler):
    assert chronicler.retire(FakeConn(), playbook(success_count=1, failure_count=9,
                             status="retired"), incident_id=None) is None


# -- the stale sweep -------------------------------------------------------- #

def test_an_unanswered_approval_expires_into_a_shadow_record(chronicler):
    """Nobody said no; nobody said anything. That is what shadow means."""
    from datetime import UTC, datetime

    conn = FakeConn([[("appr-1", "pred-1")], [], []])
    swept = chronicler.sweep(conn, datetime.now(UTC))
    assert swept["approvals_expired"] == ["appr-1"]
    assert conn.said("SET status = 'expired'")
    assert conn.said("prevention_status = 'shadowed'")


def test_an_abandoned_execution_is_released_as_a_miss(chronicler):
    """A prediction stuck in `preventing` holds Oracle's dedup guard forever."""
    from datetime import UTC, datetime

    conn = FakeConn([[], [("pred-2", "payments")]])
    swept = chronicler.sweep(conn, datetime.now(UTC))
    assert swept["predictions_abandoned"] == ["pred-2"]
    sql, _ = conn.statements[1]
    assert "prevention_status = 'missed'" in sql


def test_a_prediction_waiting_on_a_human_is_not_abandoned(chronicler):
    """Waiting is not the same as abandoned; the deadline is what bounds waiting."""
    from datetime import UTC, datetime

    conn = FakeConn([[], []])
    chronicler.sweep(conn, datetime.now(UTC))
    sql, _ = conn.statements[1]
    assert "NOT IN (SELECT prediction_id FROM approvals WHERE status = 'pending')" in sql


def test_a_quiet_sweep_writes_nothing(chronicler):
    from datetime import UTC, datetime

    conn = FakeConn([[], []])
    swept = chronicler.sweep(conn, datetime.now(UTC))
    assert swept == {"approvals_expired": [], "predictions_abandoned": []}
    assert len(conn.statements) == 2, "two statements, no follow-up writes"
