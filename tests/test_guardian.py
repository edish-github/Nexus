"""Guardian's execution, verification and rollback, driven against a fake fleet.

The rollback path is the one that has to be right: it is the demo's third
moment, and more importantly it is the mutation trigger. A rollback that leaves
the fleet holding half a remediation is worse than no rollback at all.
"""
from __future__ import annotations

import pytest

from nexus_common import fleet_client

POOL_STEPS = [
    {"action": "scale_connection_pool", "target": "payments", "params": {"max_size": 200},
     "inverse": {"action": "scale_connection_pool", "params": {"max_size": 50}}},
    {"action": "recycle_connections", "target": "payments", "params": {"idle_timeout_s": 30},
     "inverse": {"action": "recycle_connections", "params": {"idle_timeout_s": 300}}},
]

IRREVERSIBLE_STEPS = [
    {"action": "shed_load", "target": "auth", "params": {"drop_pct": 10},
     "inverse": {"action": "shed_load", "params": {"drop_pct": 0}}},
    {"action": "rotate_certificate", "target": "auth", "params": {"issuer": "internal-ca"}},
]


class FakeFleet:
    """Records calls; can be told to fail, so the unhappy paths are reachable."""

    def __init__(self, *, fail_on: str | None = None):
        self.calls: list[dict] = []
        self.fail_on = fail_on

    def apply_action(self, service, action, params=None, revert_of=None):
        if self.fail_on == action:
            raise fleet_client.FleetUnavailable(f"{action} refused")
        self.calls.append({"service": service, "action": action, "params": params or {},
                           "revert_of": revert_of})
        return {"service": service, "action": action, "effective": True,
                "effect": "reverted" if revert_of else "improving"}


@pytest.fixture
def fleet(guardian, monkeypatch) -> FakeFleet:
    fake = FakeFleet()
    monkeypatch.setattr(guardian.fleet_client, "apply_action", fake.apply_action)
    return fake


# -- the verdict ----------------------------------------------------------- #

def test_a_falling_target_metric_is_an_improvement(guardian):
    verdict, delta = guardian._verdict(0.90, 0.40, "connection_pool_exhaustion")
    assert verdict == "improved" and delta > 0


def test_a_rising_target_metric_is_degradation(guardian):
    verdict, delta = guardian._verdict(0.40, 0.90, "connection_pool_exhaustion")
    assert verdict == "degraded" and delta < 0


def test_a_metric_that_is_supposed_to_rise_is_read_the_other_way(guardian):
    """Cache hit ratio recovering upward is success, not failure."""
    assert guardian._verdict(0.30, 0.95, "cache_stampede")[0] == "improved"
    assert guardian._verdict(0.95, 0.30, "cache_stampede")[0] == "degraded"


def test_noise_sized_movement_is_flat_not_success(guardian):
    assert guardian._verdict(0.500, 0.505, "connection_pool_exhaustion")[0] == "flat"


def test_a_missing_reading_is_unknown_not_success(guardian):
    assert guardian._verdict(None, 0.4, "disk_full")[0] == "unknown"
    assert guardian._verdict(0.9, None, "disk_full")[0] == "unknown"


def test_an_unknown_category_still_has_a_target(guardian):
    from nexus_common.trajectory import outcome_target

    assert outcome_target("something_never_seen") == ("latency_p99_ms", "down")


# -- execution ------------------------------------------------------------- #

def test_steps_execute_in_order_and_are_timed(guardian, fleet):
    records = guardian.execute_steps("payments", POOL_STEPS)
    assert [c["action"] for c in fleet.calls] == [
        "scale_connection_pool", "recycle_connections"]
    assert all(r["ok"] and r["duration_ms"] >= 0 for r in records)


def test_a_step_already_in_desired_state_is_skipped(guardian, fleet):
    """Idempotency: a retry must not compound its own earlier effects."""
    key = fleet_client.step_key("scale_connection_pool", {"max_size": 200})
    records = guardian.execute_steps("payments", POOL_STEPS, already_applied={key})
    assert records[0]["skipped"]
    assert [c["action"] for c in fleet.calls] == ["recycle_connections"]


def test_execution_stops_at_the_first_step_that_cannot_run(guardian, monkeypatch):
    fake = FakeFleet(fail_on="scale_connection_pool")
    monkeypatch.setattr(guardian.fleet_client, "apply_action", fake.apply_action)
    records = guardian.execute_steps("payments", POOL_STEPS)
    assert len(records) == 1 and records[0]["ok"] is False
    assert fake.calls == [], "nothing should have landed"


def test_steps_are_validated_before_anything_is_executed(guardian, fleet):
    with pytest.raises(ValueError):
        guardian.execute_steps("payments", [{"action": "rm_minus_rf", "target": "x"}])
    assert fleet.calls == []


# -- rollback -------------------------------------------------------------- #

def test_rollback_runs_the_inverses_in_reverse_order(guardian, fleet):
    executed = guardian.execute_steps("payments", POOL_STEPS)
    fleet.calls.clear()
    guardian.rollback_steps("payments", POOL_STEPS, executed)
    assert [c["action"] for c in fleet.calls] == [
        "recycle_connections", "scale_connection_pool"]


def test_rollback_reverts_the_exact_steps_it_undoes(guardian, fleet):
    """Each inverse names the step it cancels, so the fleet ends where it began
    rather than carrying two remediations at once."""
    executed = guardian.execute_steps("payments", POOL_STEPS)
    fleet.calls.clear()
    guardian.rollback_steps("payments", POOL_STEPS, executed)
    reverted = [c["revert_of"] for c in fleet.calls]
    assert all(reverted), "every inverse must name what it reverts"
    assert set(reverted) == {r["key"] for r in executed}


def test_rollback_only_undoes_what_actually_ran(guardian, monkeypatch):
    fake = FakeFleet(fail_on="recycle_connections")
    monkeypatch.setattr(guardian.fleet_client, "apply_action", fake.apply_action)
    executed = guardian.execute_steps("payments", POOL_STEPS)
    fake.fail_on = None
    fake.calls.clear()
    records = guardian.rollback_steps("payments", POOL_STEPS, executed)
    assert len(records) == 1
    assert fake.calls[0]["action"] == "scale_connection_pool"


def test_an_irreversible_step_is_reported_rather_than_silently_skipped(guardian, fleet):
    executed = guardian.execute_steps("auth", IRREVERSIBLE_STEPS)
    fleet.calls.clear()
    records = guardian.rollback_steps("auth", IRREVERSIBLE_STEPS, executed)
    skipped = [r for r in records if r.get("skipped")]
    assert len(skipped) == 1 and "irreversible" in skipped[0]["skipped"]
    assert [c["action"] for c in fleet.calls] == ["shed_load"]


# -- tiers ----------------------------------------------------------------- #

@pytest.mark.parametrize("tier", ["shadow", "approve", "novel"])
def test_guardian_never_executes_off_the_auto_tier(guardian, fleet, tier):
    result = guardian.run({"prediction_id": "p1", "tier": tier, "service": "payments",
                           "outcome_category": "disk_full", "reason": "because"})
    assert result["outcome"] == "not_executed"
    assert fleet.calls == []


def test_without_a_substrate_guardian_reports_it_rather_than_claiming_success(
    guardian, monkeypatch
):
    monkeypatch.setattr(guardian.fleet_client, "configured", lambda: False)
    result = guardian.run({
        "prediction_id": "p1", "tier": "auto", "service": "payments",
        "outcome_category": "connection_pool_exhaustion",
        "playbook": {"id": "pb", "name": "x", "remediation_steps": POOL_STEPS},
    })
    assert result["outcome"] == "no_substrate"


# -- the substrate health check -------------------------------------------- #

def test_a_missing_ccloud_binary_is_reported_not_swallowed(guardian, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(guardian.subprocess, "run", boom)
    health = guardian.substrate_health()
    assert health["available"] is False
    assert "not installed" in health["reason"]


def test_a_failing_ccloud_call_is_reported(guardian, monkeypatch):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "not logged in"

    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **k: Proc())
    health = guardian.substrate_health()
    assert health["available"] is False and "not logged in" in health["reason"]


def test_a_healthy_cluster_is_summarised(guardian, monkeypatch):
    class Proc:
        returncode = 0
        stderr = ""
        stdout = ('{"clusters":[{"name":"nexus","state":"CREATED",'
                  '"regions":[{"name":"us-east-1"},{"name":"eu-west-1"}]}]}')

    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **k: Proc())
    health = guardian.substrate_health()
    assert health["available"] is True
    assert health["clusters"][0]["name"] == "nexus"
    assert health["clusters"][0]["regions"] == ["us-east-1", "eu-west-1"]
