"""Diagnostician's narrative, and the gate that stops a bad genome being born.

The validation test is the important one. Everything Bedrock proposes here ends
up as steps that Guardian will execute against production, so anything that does
not validate must be rejected outright rather than repaired, coerced, or stored
"for review".
"""
from __future__ import annotations

import json

import pytest

DIGEST = {
    "summary": {
        "pool_utilization": {"trend": "rising", "start": "q3", "end": "q8",
                             "shape": "ramp"},
        "error_rate": {"trend": "flat", "start": "q0", "end": "q0", "shape": "flat"},
    }
}

MATCHES = [
    {"id": "inc-1", "title": "Connection pool exhaustion on payments",
     "root_cause": "pool sized for p50, not p99", "severity": 4,
     "mttr_seconds": 900, "was_prevented": True, "detected_at": None,
     "similarity": 0.93},
    {"id": "inc-2", "title": "Connection pool exhaustion on checkout",
     "root_cause": None, "severity": 4, "mttr_seconds": 1400,
     "was_prevented": False, "detected_at": None, "similarity": 0.88},
]

VALID_DRAFT = {
    "name": "Widen pool and trip the breaker early",
    "outcome_category": "connection_pool_exhaustion",
    "rationale": "utilization is climbing super-linearly while errors stay flat",
    "remediation_steps": [
        {"action": "scale_connection_pool", "target": "payments",
         "params": {"max_size": 200},
         "inverse": {"action": "scale_connection_pool", "params": {"max_size": 50}}},
    ],
}


def _no_bedrock(diagnostician, monkeypatch, response=None):
    """Force the model call to fail, or to return a fixed response."""
    if response is None:
        def boom(*a, **k):
            raise RuntimeError("no credentials")
        monkeypatch.setattr(diagnostician.bedrock, "claude", boom)
    else:
        monkeypatch.setattr(diagnostician.bedrock, "claude", lambda *a, **k: response)


# -- the narrative --------------------------------------------------------- #

def test_the_template_narrative_names_what_moved(diagnostician):
    text = diagnostician._template_narrative(
        "connection_pool_exhaustion", "payments", MATCHES, DIGEST)
    assert "connection pool exhaustion" in text
    assert "pool_utilization" in text
    assert "error_rate" not in text, "a flat metric is not part of the story"


def test_the_template_cites_the_closest_precedent_and_its_cause(diagnostician):
    text = diagnostician._template_narrative(
        "connection_pool_exhaustion", "payments", MATCHES, DIGEST)
    assert "0.93" in text
    assert "pool sized for p50" in text
    assert "1 of 2" in text


def test_the_template_says_so_when_there_is_no_precedent(diagnostician):
    text = diagnostician._template_narrative("novel_thing", "payments", [], DIGEST)
    assert "no precedent" in text


def test_the_pipeline_does_not_stall_when_bedrock_is_unavailable(
    diagnostician, monkeypatch
):
    _no_bedrock(diagnostician, monkeypatch)
    narrative, source = diagnostician.narrate(
        "connection_pool_exhaustion", "payments", MATCHES, DIGEST)
    assert source == "template"
    assert "payments" in narrative


def test_bedrock_narrative_is_used_when_it_answers(diagnostician, monkeypatch):
    _no_bedrock(diagnostician, monkeypatch, response="  The pool was undersized.  ")
    narrative, source = diagnostician.narrate(
        "connection_pool_exhaustion", "payments", MATCHES, DIGEST)
    assert source == "bedrock" and narrative == "The pool was undersized."


def test_an_empty_model_response_falls_back(diagnostician, monkeypatch):
    _no_bedrock(diagnostician, monkeypatch, response="   ")
    _, source = diagnostician.narrate(
        "connection_pool_exhaustion", "payments", MATCHES, DIGEST)
    assert source == "template"


# -- the birth gate -------------------------------------------------------- #

def test_a_valid_proposal_is_accepted(diagnostician, monkeypatch):
    _no_bedrock(diagnostician, monkeypatch, response=json.dumps(VALID_DRAFT))
    draft = diagnostician.propose_playbook(
        "connection_pool_exhaustion", "payments", DIGEST, MATCHES)
    assert draft is not None
    assert draft.reversible is True
    assert [s["action"] for s in draft.inverse_steps()] == ["scale_connection_pool"]


def test_a_fenced_json_response_is_still_parsed(diagnostician, monkeypatch):
    """Models like to wrap JSON in code fences; that alone is not malformed."""
    _no_bedrock(diagnostician, monkeypatch,
                response="```json\n" + json.dumps(VALID_DRAFT) + "\n```")
    assert diagnostician.propose_playbook(
        "connection_pool_exhaustion", "payments", DIGEST, MATCHES) is not None


@pytest.mark.parametrize("payload,why", [
    ('{"name": "x"}', "missing required fields"),
    (json.dumps({**VALID_DRAFT, "remediation_steps": [
        {"action": "rm_minus_rf", "target": "payments", "params": {}}]}),
     "an action outside the vocabulary"),
    (json.dumps({**VALID_DRAFT, "remediation_steps": []}), "no steps at all"),
    (json.dumps({**VALID_DRAFT, "urgency": "high"}), "an invented field"),
    ("I think you should scale the pool.", "not JSON at all"),
])
def test_a_malformed_genome_is_stillborn(diagnostician, monkeypatch, payload, why):
    _no_bedrock(diagnostician, monkeypatch, response=payload)
    assert diagnostician.propose_playbook(
        "connection_pool_exhaustion", "payments", DIGEST, MATCHES) is None, why


def test_no_proposal_when_bedrock_is_unreachable(diagnostician, monkeypatch):
    _no_bedrock(diagnostician, monkeypatch)
    assert diagnostician.propose_playbook(
        "connection_pool_exhaustion", "payments", DIGEST, MATCHES) is None


def test_the_proposal_prompt_constrains_the_action_vocabulary(diagnostician):
    from nexus_common.steps import ACTIONS

    prompt = diagnostician.PROPOSAL_SYSTEM.format(actions=", ".join(ACTIONS))
    assert "scale_connection_pool" in prompt
    assert "inverse" in prompt


# -- the precursor snapshot writer ----------------------------------------- #

class FakeConn:
    def __init__(self, rows):
        self.rows = list(rows)
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params or ()))
        rows = self.rows.pop(0) if self.rows else []

        class C:
            def fetchone(self_inner):
                return rows[0] if rows else None

            def fetchall(self_inner):
                return rows

        return C()


def test_the_snapshot_reuses_the_sensory_embedding(diagnostician):
    """Re-embedding a window the sensory tier already vectorised would spend a
    Titan call to produce the same numbers."""
    from datetime import UTC, datetime

    captured = datetime(2026, 8, 17, tzinfo=UTC)
    conn = FakeConn([
        [("tel-1", "aws-eu-west-1", "[0.5,0.5]", {"precursor_minutes": 90}, captured)],
        [("snap-1",)],
    ])
    snapshot = diagnostician.write_precursor_snapshot(
        conn, "payments", "connection_pool_exhaustion", "inc-1")
    assert snapshot["id"] == "snap-1"
    assert snapshot["window_minutes"] == 90
    insert_sql, params = conn.statements[1]
    assert "INSERT INTO precursor_snapshots" in insert_sql
    assert "[0.5,0.5]" in params, "the stored vector must be the one already computed"
    assert "led_to_incident" in insert_sql


def test_no_telemetry_means_no_snapshot_rather_than_a_fabricated_one(diagnostician):
    assert diagnostician.write_precursor_snapshot(
        FakeConn([[]]), "payments", "disk_full", "inc-1") is None
