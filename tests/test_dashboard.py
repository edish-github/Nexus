"""The dashboard's one write: a human's decision on an irreversible remediation.

Everything else in this Lambda reads, and reads are covered by driving them
against the cluster. The decision endpoint is worth stating as tests because
three of its behaviours are safety properties rather than shapes: it refuses an
unknown verdict, it refuses a second answer to the same question, and a rejection
turns the prediction into a shadow record rather than throwing the outcome away.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.test_chronicler import FakeConn


@pytest.fixture(scope="session")
def dashboard():
    from _agents import load_agent

    return load_agent("dashboard")


def pending_row(status: str = "pending"):
    return [(
        status, "pred-1", "pb-1", "inventory", "disk_full",
        "no inverse declared for: prune_disk",
        datetime.now(UTC) + timedelta(minutes=30), 4, "preventing",
    )]


@pytest.fixture(autouse=True)
def _no_bus(monkeypatch, dashboard):
    """No event bus in tests: dispatch is asserted separately, never performed."""
    monkeypatch.setattr(dashboard, "_publish_decision", lambda approval: None)


def run_decision(dashboard, monkeypatch, results, payload):
    """Drive `decide_approval` against a scripted connection."""
    conn = FakeConn(results)
    monkeypatch.setattr(dashboard.db, "tx_retry", lambda fn, **kw: fn(conn))
    status, body = dashboard.decide_approval("appr-1", payload)
    return status, body, conn


# -- what it refuses -------------------------------------------------------- #

def test_an_unknown_verdict_is_refused_before_any_read(dashboard):
    status, body = dashboard.decide_approval("appr-1", {"decision": "maybe"})
    assert status == 400
    assert "approved" in body["detail"]


def test_a_missing_verdict_is_refused(dashboard):
    assert dashboard.decide_approval("appr-1", {})[0] == 400


def test_an_approval_that_no_longer_exists_is_a_404(dashboard, monkeypatch):
    status, body, _ = run_decision(dashboard, monkeypatch, [[]], {"decision": "approved"})
    assert status == 404
    assert "expired" in body["detail"]


def test_a_second_decision_is_refused_rather_than_applied_twice(dashboard, monkeypatch):
    status, body, conn = run_decision(
        dashboard, monkeypatch, [pending_row("approved")], {"decision": "rejected"})
    assert status == 409
    assert body["status"] == "approved"
    assert not conn.said("UPDATE approvals"), "a losing claimant must not write"
    assert not conn.said("INSERT INTO evolution_log")


# -- what it does ----------------------------------------------------------- #

def test_the_decision_is_taken_under_a_row_lock(dashboard, monkeypatch):
    _, _, conn = run_decision(dashboard, monkeypatch, [pending_row()],
                              {"decision": "approved"})
    assert conn.said("FOR UPDATE"), "two people clicking at once must produce one decision"


def test_approving_records_the_decision_and_leaves_the_prediction_open(
        dashboard, monkeypatch):
    status, body, conn = run_decision(dashboard, monkeypatch, [pending_row()],
                                      {"decision": "approved", "decided_by": "maruf"})
    assert status == 200
    assert body["decision"] == "approved"
    assert body["decided_by"] == "maruf"
    assert conn.said("UPDATE approvals")
    assert not conn.said("prevention_status = 'shadowed'"), (
        "an approved prediction stays in `preventing` until Guardian closes it out")


def test_rejecting_turns_the_prediction_into_a_shadow_record(dashboard, monkeypatch):
    """The most informative signal in the loop is a human disagreeing. Keep it."""
    status, body, conn = run_decision(dashboard, monkeypatch, [pending_row()],
                                      {"decision": "rejected"})
    assert status == 200
    assert conn.said("prevention_status = 'shadowed'")
    assert "shadow record" in body["note"]


def test_every_decision_writes_its_own_audit_row(dashboard, monkeypatch):
    for verdict in ("approved", "rejected"):
        _, _, conn = run_decision(dashboard, monkeypatch, [pending_row()],
                                  {"decision": verdict})
        assert conn.said("INSERT INTO evolution_log")
        details = next(params[1] for sql, params in conn.statements
                       if "evolution_log" in sql)
        assert '"kind": "approval_decision"' in details
        assert f'"decision": "{verdict}"' in details


def test_an_anonymous_decision_still_names_someone(dashboard, monkeypatch):
    _, body, _ = run_decision(dashboard, monkeypatch, [pending_row()],
                              {"decision": "approved", "decided_by": "   "})
    assert body["decided_by"] == "dashboard"


def test_a_dispatch_failure_does_not_lose_the_decision(dashboard, monkeypatch):
    """The decision is committed before the bus is touched, and stays committed."""
    monkeypatch.setattr(dashboard, "_publish_decision", lambda approval: None)
    status, body, _ = run_decision(dashboard, monkeypatch, [pending_row()],
                                   {"decision": "approved"})
    assert status == 200
    assert body["dispatched_to"] is None
    assert "nothing was dispatched" in body["note"]


# -- the queue -------------------------------------------------------------- #

def test_the_queue_is_read_at_current_time_not_from_a_follower(dashboard, monkeypatch):
    """A stale queue shows a request someone already answered."""
    seen: dict = {}

    def fake_select(sql, params=(), *, as_of=None):
        seen["as_of"] = as_of
        return []

    monkeypatch.setattr(dashboard, "_select", fake_select)
    dashboard.list_approvals({})
    assert seen["as_of"] is None
