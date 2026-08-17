#!/usr/bin/env python3
"""Drive one playbook family through its whole life — the Phase 5 exit gate.

    make lifecycle

Seven transitions in one run, against the live cluster, asserting `evolution_log`
after each one:

    birth ──► growth ──► failure ──► mutation ──► child success
                                                       │
                            promotion ◄── growth ◄── merge with a sibling

Everything runs in a probe category of its own (`lifecycle-probe-<run>`), so the
seeded world is never touched and the run is repeatable. The probe is deleted on
the way out unless `--keep` is passed.

**The proposal seam.** Mutation and merge ask a reasoning model for a new genome,
and this harness has to run without one. It substitutes `chronicler._generate`
with a deterministic function and lets the substitution be recorded: every row
this run writes carries `proposed_by: "lifecycle-harness"`, not `"bedrock"`. The
genome still passes the same `PlaybookDraft` validation Bedrock's output does —
what is stubbed is who wrote it, not whether it was checked.
"""
from __future__ import annotations

import argparse
import json
import uuid

from _env import bootstrap, require_dsn

bootstrap()

import numpy as np  # noqa: E402

from _agents import load_agent  # noqa: E402
from generator.vectors import place  # noqa: E402
from nexus_common import db  # noqa: E402

SERVICE = "payments"
REGION = "aws-us-east-1"

# The genomes this harness proposes in place of a model. Both validate against
# the same schema Bedrock's output has to satisfy.
VARIANT = {
    "name": "Probe variant: breaker before pool",
    "rationale": "the parent widened a pool feeding a saturated upstream",
    "remediation_steps": [{
        "action": "set_circuit_breaker", "target": "{service}",
        "params": {"error_threshold": 0.1, "half_open_after_s": 30},
        "inverse": {"action": "set_circuit_breaker",
                    "params": {"error_threshold": 0.5, "half_open_after_s": 120}},
    }],
}
CANONICAL = {
    "name": "Probe canonical: breaker with drain",
    "rationale": "both parents trip the breaker first and then recycle idle connections",
    "remediation_steps": [
        {"action": "set_circuit_breaker", "target": "{service}",
         "params": {"error_threshold": 0.1, "half_open_after_s": 30},
         "inverse": {"action": "set_circuit_breaker",
                     "params": {"error_threshold": 0.5, "half_open_after_s": 120}}},
        {"action": "recycle_connections", "target": "{service}",
         "params": {"idle_timeout_s": 30},
         "inverse": {"action": "recycle_connections", "params": {"idle_timeout_s": 300}}},
    ],
}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


class Check:
    """Assertions that keep running after one fails, so the run reports the whole picture."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def that(self, condition: bool, claim: str, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            say(f"   PASS  {claim}")
        else:
            self.failures.append(claim)
            say(f"   FAIL  {claim}" + (f"  ({detail})" if detail else ""))
        return condition


def vector_literal(v) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def probe_vectors(rng) -> tuple[str, str]:
    """Where the family lives: a random point, and a sibling 0.06 away from it.

    0.06 is inside Chronicler's 0.15 merge radius by a clear margin, so the merge
    predicate is being tested rather than the floating-point boundary.
    """
    anchor = rng.normal(size=1024)
    anchor /= np.linalg.norm(anchor)
    return vector_literal(anchor), vector_literal(place(anchor, 0.06, rng))


# --------------------------------------------------------------------------- #
# Probe world setup and teardown
# --------------------------------------------------------------------------- #

def open_probe(category: str, embedding: str) -> str:
    def run(conn):
        return conn.execute(
            """
            INSERT INTO incidents
                (crdb_region, title, severity, status, affected_services, was_predicted,
                 symptom_embedding, detected_at)
            VALUES (%s, %s, 3, 'diagnosing', ARRAY[%s::STRING], true, %s::VECTOR, now())
            RETURNING id::STRING
            """,
            (REGION, f"Lifecycle probe: {category}", SERVICE, embedding),
        ).fetchone()[0]

    return db.tx_retry(run)


def insert_sibling(category: str, embedding: str, successes: int, failures: int) -> str:
    """An independently evolved sibling, inserted the way the seeder inserts one.

    It has no lineage in common with the family it will merge into, which is what
    makes it a sibling rather than a relative — Chronicler refuses to merge a
    playbook with its own ancestors or descendants.
    """
    steps = [{"action": "set_circuit_breaker", "target": "{service}",
              "params": {"error_threshold": 0.12},
              "inverse": {"action": "set_circuit_breaker",
                          "params": {"error_threshold": 0.5}}}]

    def run(conn):
        return conn.execute(
            """
            INSERT INTO playbooks
                (crdb_region, name, outcome_category, precursor_embedding,
                 remediation_steps, inverse_steps, reversible, success_count,
                 failure_count, generation, lineage, memory_tier, status)
            VALUES (%s, %s, %s, %s::VECTOR, %s::JSONB, %s::JSONB, true, %s, %s, 2,
                    ARRAY[]::UUID[], 'operational', 'active')
            RETURNING id::STRING
            """,
            (REGION, "Probe sibling: convergent breaker", category, embedding,
             json.dumps(steps), json.dumps([]), successes, failures),
        ).fetchone()[0]

    return db.tx_retry(run)


def insert_prediction(category: str, embedding: str, playbook_id: str) -> str:
    """One real prediction row, so the prediction-driven path is exercised too."""
    def run(conn):
        return conn.execute(
            """
            INSERT INTO predictions
                (service_name, causal_pattern, predicted_outcome, predicted_severity,
                 alpha, beta, matching_precursor_count, current_embedding,
                 prevention_status, playbook_applied, resolved_at)
            VALUES (%s, %s, %s, 3, 12.0, 2.0, 13, %s::VECTOR, 'prevented', %s, now())
            RETURNING id::STRING
            """,
            (SERVICE, f"lifecycle probe {category}", category, embedding, playbook_id),
        ).fetchone()[0]

    return db.tx_retry(run)


def close_probe(category: str, keep: bool) -> None:
    if keep:
        say(f"\n   probe kept: outcome_category = '{category}'")
        return

    def run(conn):
        ids = [r[0] for r in conn.execute(
            "SELECT id::STRING FROM playbooks WHERE outcome_category = %s", (category,)
        ).fetchall()]
        if not ids:
            return 0
        conn.execute("DELETE FROM evolution_log WHERE playbook_id = ANY(%s::UUID[]) "
                     "OR parent_id = ANY(%s::UUID[])", (ids, ids))
        conn.execute("DELETE FROM institutional_playbooks WHERE source_playbook_id = "
                     "ANY(%s::UUID[])", (ids,))
        conn.execute("DELETE FROM predictions WHERE predicted_outcome = %s", (category,))
        conn.execute("UPDATE incidents SET playbook_used = NULL WHERE playbook_used = "
                     "ANY(%s::UUID[])", (ids,))
        # parent_id is a self-reference, so break the chain before deleting.
        conn.execute("UPDATE playbooks SET parent_id = NULL WHERE id = ANY(%s::UUID[])",
                     (ids,))
        conn.execute("DELETE FROM playbooks WHERE id = ANY(%s::UUID[])", (ids,))
        conn.execute("DELETE FROM incidents WHERE title = %s",
                     (f"Lifecycle probe: {category}",))
        return len(ids)

    removed = db.tx_retry(run)
    say(f"\n   probe removed: {removed} playbook(s) and their history")


# --------------------------------------------------------------------------- #
# Reads used by the assertions
# --------------------------------------------------------------------------- #

def read_playbook(playbook_id: str) -> dict | None:
    def run(conn):
        row = conn.execute(
            "SELECT name, success_count, failure_count, generation, parent_id::STRING, "
            "lineage::STRING[], memory_tier, status FROM playbooks WHERE id = %s",
            (playbook_id,),
        ).fetchone()
        if not row:
            return None
        return dict(zip(("name", "successes", "failures", "generation", "parent_id",
                         "lineage", "memory_tier", "status"), row, strict=True))

    return db.tx_retry(run)


def history(category: str) -> list[dict]:
    """Every evolution_log row belonging to the probe family, oldest first."""
    def run(conn):
        rows = conn.execute(
            """
            SELECT e.event_type, e.playbook_id::STRING, e.parent_id::STRING,
                   e.fitness_before, e.fitness_after, e.details, e.created_at
            FROM evolution_log e
            JOIN playbooks p ON p.id = e.playbook_id
            WHERE p.outcome_category = %s
            ORDER BY e.created_at, e.event_type
            """,
            (category,),
        ).fetchall()
        return [dict(zip(("event_type", "playbook_id", "parent_id", "fitness_before",
                          "fitness_after", "details", "created_at"), r, strict=True))
                for r in rows]

    return db.tx_retry(run)


def institutional(playbook_id: str) -> dict | None:
    def run(conn):
        row = conn.execute(
            "SELECT name, success_count, failure_count, generation FROM "
            "institutional_playbooks WHERE source_playbook_id = %s", (playbook_id,)
        ).fetchone()
        return dict(zip(("name", "successes", "failures", "generation"), row,
                        strict=True)) if row else None

    return db.tx_retry(run)


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Drive one playbook through its whole life")
    parser.add_argument("--keep", action="store_true",
                        help="leave the probe family in the database for inspection")
    args = parser.parse_args()
    require_dsn()

    chronicler = load_agent("chronicler")
    diagnostician = load_agent("diagnostician")

    # The proposal seam, substituted and labelled. See the module docstring.
    def harness_proposal(prompt, *, system, max_tokens=1200, temperature=0.4):
        genome = CANONICAL if "parents" in prompt else VARIANT
        return json.dumps({**genome, "outcome_category": category}), "lifecycle-harness"

    chronicler._generate = harness_proposal

    run_id = uuid.uuid4().hex[:8]
    category = f"lifecycle-probe-{run_id}"
    rng = np.random.default_rng()
    anchor, near = probe_vectors(rng)
    check = Check()

    rule(f"lifecycle probe · {category}")
    incident_id = open_probe(category, anchor)
    say(f"   incident {incident_id}")

    def trial(playbook_id: str, outcome: str, *, prediction_id: str | None = None) -> dict:
        return chronicler.chronicle(prediction_id, {
            "playbook_id": playbook_id, "outcome": outcome, "service": SERVICE,
            "incident_id": incident_id,
        })

    try:
        # -- 1 · birth ------------------------------------------------------ #
        rule("1 · birth")
        draft = diagnostician.PlaybookDraft.model_validate({
            **VARIANT, "name": "Probe founder: pool widen",
            "outcome_category": category,
            "remediation_steps": [{
                "action": "scale_connection_pool", "target": "{service}",
                "params": {"max_size": 200},
                "inverse": {"action": "scale_connection_pool", "params": {"max_size": 50}},
            }],
        })
        founder = db.tx_retry(lambda conn: diagnostician.birth(
            conn, draft, category, REGION, anchor, incident_id))
        say(f"   founder  {founder}")
        born = read_playbook(founder)
        check.that(born["generation"] == 1, "the founder is generation 1")
        check.that(born["successes"] == 0 and born["failures"] == 0,
                   "it starts on a flat Beta(1,1) prior with nothing proven")
        check.that(any(e["event_type"] == "birth" for e in history(category)),
                   "evolution_log records the birth")

        # -- 2 · growth ----------------------------------------------------- #
        rule("2 · growth")
        prediction_id = insert_prediction(category, anchor, founder)
        first = trial(founder, "prevented", prediction_id=prediction_id)
        say(f"   trial 1 via prediction {prediction_id}: {first['applied']['growth']}")
        trial(founder, "prevented")
        state = read_playbook(founder)
        say(f"   founder now {state['successes']}/{state['failures']}")
        check.that(state["successes"] == 2, "two prevented trials are two successes")
        growth = [e for e in history(category) if e["event_type"] == "growth"]
        check.that(len(growth) == 2, "one growth row per trial", f"{len(growth)} rows")
        check.that(all(e["fitness_after"] > e["fitness_before"] for e in growth),
                   "each success raises the posterior mean")

        # -- 3 · failure → mutation ----------------------------------------- #
        rule("3 · failure → mutation")
        result = trial(founder, "rolled_back")
        child = result["applied"]["mutation"]
        say(f"   rollback bred {child}")
        check.that(child is not None, "a rolled-back trial breeds a variant")
        if child is None:
            raise SystemExit("no variant was bred; the rest of the arc cannot run")
        variant = read_playbook(child)
        check.that(variant["generation"] == 2, "the variant is one generation on")
        check.that(variant["parent_id"] == founder, "its parent is the playbook that failed")
        check.that(variant["lineage"] == [founder], "lineage carries the ancestor")
        check.that(variant["successes"] == 0 and variant["failures"] == 0,
                   "the variant inherits no evidence, only a genome")
        check.that(read_playbook(founder)["status"] == "active",
                   "the parent stays active — one bad trial is one bad trial")
        mutation = next(e for e in history(category) if e["event_type"] == "mutation")
        check.that(mutation["details"]["proposed_by"] == "lifecycle-harness",
                   "the log names who proposed the genome, not who it wishes had")

        # A second rollback must not breed again while the first variant is untried.
        again = trial(founder, "rolled_back")
        check.that(again["applied"]["mutation"] is None,
                   "a second rollback does not breed while an untried variant exists")

        # -- 4 · the child proves itself ------------------------------------ #
        rule("4 · child success")
        for _ in range(3):
            trial(child, "prevented")
        variant = read_playbook(child)
        say(f"   variant now {variant['successes']}/{variant['failures']}")
        check.that(variant["successes"] == 3, "the variant earned its own evidence")

        # -- 5 · merge with a sibling --------------------------------------- #
        rule("5 · merge with a sibling")
        sibling = insert_sibling(category, near, successes=6, failures=0)
        say(f"   sibling  {sibling} at 0.06 cosine, 6/0")
        merged_run = trial(child, "prevented")
        canonical = merged_run["applied"]["merge"]
        say(f"   merged into {canonical}")
        check.that(canonical is not None, "two converged siblings merge into one")
        if canonical is None:
            raise SystemExit("no merge happened; the rest of the arc cannot run")
        child_state = read_playbook(canonical)
        check.that(child_state["successes"] == 4 and child_state["failures"] == 0,
                   "the canonical child inherits min(successes), max(failures)",
                   f"got {child_state['successes']}/{child_state['failures']}")
        check.that(read_playbook(child)["status"] == "merged"
                   and read_playbook(sibling)["status"] == "merged",
                   "both parents are marked merged, not deleted")
        check.that(set(child_state["lineage"]) == {founder, child, sibling},
                   "the child's lineage is the union of both parents'",
                   str(child_state["lineage"]))
        merges = [e for e in history(category) if e["event_type"] == "merge"]
        check.that(len(merges) == 2, "one merge row per absorbed parent")
        check.that({e["parent_id"] for e in merges} == {child, sibling},
                   "each merge row names the parent it absorbed")

        # -- 6 · promotion --------------------------------------------------- #
        rule("6 · promotion")
        promoted = None
        for _ in range(8):
            outcome = trial(canonical, "prevented")
            if outcome["applied"]["promotion"]:
                promoted = outcome
                break
        final = read_playbook(canonical)
        say(f"   canonical now {final['successes']}/{final['failures']} · "
            f"tier {final['memory_tier']}")
        check.that(promoted is not None, "sustained success promotes to the global tier")
        check.that(final["memory_tier"] == "institutional", "the source row is re-tiered")
        copy = institutional(canonical)
        check.that(copy is not None,
                   "the playbook is copied into institutional_playbooks (LOCALITY GLOBAL)")
        promotions = [e for e in history(category) if e["event_type"] == "promotion"]
        check.that(len(promotions) == 1, "promotion happens once, not once per trial")

        # -- 7 · retirement -------------------------------------------------- #
        # The founder would need a dozen more rollbacks to fall below the line, so
        # retirement is shown on a playbook that arrives already close to it. Both
        # halves of the predicate get exercised: at 0/4 the mean is 0.167 and only
        # the five-trial minimum is holding it up.
        rule("7 · retirement")
        doomed = insert_sibling(category, vector_literal(place(
            np.array(json.loads(anchor)), 0.4, rng)), successes=0, failures=3)
        say(f"   doomed   {doomed} at 0/3 — two trials short of the minimum")
        fourth = trial(doomed, "rolled_back")
        check.that(fourth["applied"]["retirement"] is None,
                   "a low mean on four trials is not yet enough evidence to retire on")
        check.that(fourth["applied"]["mutation"] is not None,
                   "it breeds on the way down — dying is what makes room for the variant")
        fifth = trial(doomed, "rolled_back")
        state = read_playbook(doomed)
        say(f"   doomed now {state['successes']}/{state['failures']} · "
            f"status {state['status']}")
        check.that(fifth["applied"]["retirement"] == doomed,
                   "below the line with enough trials, it is retired")
        check.that(state["status"] == "retired" and state["memory_tier"] == "retired",
                   "it leaves selection in both status and tier")

        # -- 8 · the log is the story --------------------------------------- #
        rule("8 · evolution_log")
        log = history(category)
        for e in log:
            before = "—" if e["fitness_before"] is None else f"{e['fitness_before']:.3f}"
            after = "—" if e["fitness_after"] is None else f"{e['fitness_after']:.3f}"
            note = e["details"].get("reason") or e["details"].get("verdict") or ""
            say(f"   {e['created_at']:%H:%M:%S}  {e['event_type']:<10} "
                f"{(e['playbook_id'] or '')[:8]}  {before} → {after}  {str(note)[:44]}")
        kinds = [e["event_type"] for e in log]
        check.that(kinds.count("birth") == 1, "exactly one birth")
        check.that(kinds.count("mutation") == 2, "two mutations — one per rolled-back line")
        check.that(kinds.count("merge") == 2, "two merge edges")
        check.that(kinds.count("promotion") == 1, "one promotion")
        check.that(kinds.count("retirement") == 1, "one retirement")
        check.that(
            all(e["playbook_id"] for e in log),
            "every row names the playbook it is about")
        check.that(
            kinds.index("birth") < kinds.index("mutation") < kinds.index("merge")
            < kinds.index("promotion"),
            "the log reads in the order the life was lived")

        rule("summary")
        say(json.dumps({
            "category": category,
            "founder": founder, "variant": child, "sibling": sibling,
            "canonical": canonical,
            "events": len(log), "event_types": sorted(set(kinds)),
            "checks_passed": check.passed, "checks_failed": len(check.failures),
        }, indent=2))
        if check.failures:
            say("\nFAILED:")
            for f in check.failures:
                say(f"  · {f}")
        return 1 if check.failures else 0
    finally:
        close_probe(category, args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
