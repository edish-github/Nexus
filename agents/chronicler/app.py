"""Chronicler: the Darwinian engine. Every playbook lifecycle transition passes here.

Guardian changes the world; Chronicler changes the memory. It runs at the end of
every pipeline execution — including the ones that rolled back, because a failed
trial is the most informative thing that can happen to a population.

Six transitions, applied in this order:

    growth      the trial's verdict becomes evidence: one success or one failure
    shadow      a prediction the system watched but did not act on is settled
                against what actually happened, at reduced weight
    mutation    a rollback breeds a variant of the playbook that failed
    merge       two siblings that converged on the same answer become one
    promotion   proven memory is copied into the GLOBAL institutional table
    retirement  memory that has been given enough chances and failed steps aside

It also sweeps. A prediction stuck in `preventing` holds Oracle's dedup guard, so
an execution that died mid-flight would silently make that failure unpredictable
forever; `sweep` is what stops that, and a scheduled `{"sweep": true}` invocation
runs it on days when no prediction fires at all.

**Fitness is never stored.** Every threshold below is compared against
`Beta(success_count + 1, failure_count + 1)`'s mean, derived from the counters at
read time. There is no float to drift.

**One invariant, enforced structurally.** No function in this module writes to
`evolution_log`. Each returns the `Lifecycle` rows describing what it changed,
and `chronicle` inserts them alongside the mutations inside a single serializable
transaction. A lifecycle change therefore cannot reach the database without its
log row travelling with it, and the append-only history is complete by
construction rather than by discipline.

**Model calls happen outside the transaction.** Mutation and merge ask Bedrock
for a new genome, which is network I/O measured in seconds; holding a
serializable transaction open across it would be a contention bug waiting for
load. So the read pass, the proposal, and the write pass are three separate
steps, and the write pass re-checks its preconditions.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from nexus_common import bedrock, db, log, metrics, posterior
from nexus_common.posterior import SHADOW_WEIGHT
from nexus_common.steps import ACTIONS, PlaybookDraft

logger = log.get_logger("chronicler")

# Promotion and retirement thresholds. Both require a trial count as well as a
# posterior mean, because Beta(1,1) has mean 0.5 and a single success takes it to
# 0.67 — a lucky newborn must not be able to reach the institutional tier.
PROMOTION_MEAN = 0.9
PROMOTION_TRIALS = 10
RETIREMENT_MEAN = 0.2
RETIREMENT_TRIALS = 5

# Two playbooks this close in the precursor space are answering the same
# question. Both must also be carrying their weight: merging a good playbook with
# a bad one would launder the bad one's failures into a fresh genome.
MERGE_DISTANCE = 0.15
MERGE_MIN_MEAN = 0.5

# A playbook expires 90 days after its last use; growth is what resets that
# clock, so this is where the disuse TTL is actually driven.
DISUSE_DAYS = 90

# How long after its ETA a shadowed prediction is considered settled, and how
# many are settled per invocation.
SHADOW_GRACE_MINUTES = float(os.environ.get("SHADOW_GRACE_MINUTES", "15"))
SHADOW_SWEEP_LIMIT = 20

# How long a prediction may sit in `preventing` before it is presumed abandoned.
# Generous on purpose: Guardian's verification window plus a Lambda timeout plus
# Step Functions' retries, and then some.
STALE_PREVENTING_MINUTES = float(os.environ.get("STALE_PREVENTING_MINUTES", "30"))

MUTATION_SYSTEM = """You are the evolutionary memory of an autonomous \
incident-response system. A playbook was executed against a live incident, made \
the target metric worse, and was rolled back. You propose a variant.

Rules you must follow:
- Use only these actions: {actions}
- The variant must differ from its parent in mechanism, not only in parameter \
values: the parent's approach is what failed.
- Every step needs an `inverse` unless undoing it is genuinely impossible.
- Keep `target` as the literal string "{service}" — a playbook is procedural \
memory, not a fix for one named service.
- Return a single JSON object and nothing else."""

MERGE_SYSTEM = """You are the evolutionary memory of an autonomous \
incident-response system. Two playbooks evolved independently, converged on \
nearly the same answer, and are both working. You synthesize the canonical \
version that replaces them.

Rules you must follow:
- Use only these actions: {actions}
- Keep every step both parents agree on; where they differ, take the safer \
parameter rather than averaging.
- Every step needs an `inverse` unless undoing it is genuinely impossible.
- Keep `target` as the literal string "{service}".
- Return a single JSON object and nothing else."""

PLAYBOOK_SQL = """
    SELECT id::STRING, crdb_region, name, outcome_category, success_count, failure_count,
           generation, parent_id::STRING, lineage::STRING[], memory_tier, status,
           reversible, remediation_steps, inverse_steps, precursor_embedding::STRING
    FROM playbooks WHERE id = %s
"""

# The two lineage predicates are what keep this a search for siblings rather than
# for relatives, and they have to be in the query rather than in a filter after
# it: a variant sits at its parent's exact position, so `LIMIT 1` on distance
# alone would return the parent every time and the real sibling would never be
# seen. `id <> ALL(lineage)` excludes ancestors; `%s = ANY(lineage)` excludes
# descendants.
MERGE_CANDIDATE_SQL = """
    SELECT id::STRING, crdb_region, name, success_count, failure_count, generation,
           lineage::STRING[], remediation_steps, precursor_embedding::STRING,
           precursor_embedding <=> %s::VECTOR AS distance
    FROM playbooks
    WHERE outcome_category = %s
      AND status = 'active'
      AND id <> %s
      AND id <> ALL(%s::UUID[])
      AND NOT (%s::UUID = ANY(lineage))
      AND precursor_embedding <=> %s::VECTOR < %s
    ORDER BY precursor_embedding <=> %s::VECTOR
    LIMIT 1
"""

# Shadow credit is banked from this ledger rather than from a column. See
# `_shadow_ledger` for why the arithmetic lives in the append-only log.
SHADOW_LEDGER_SQL = """
    SELECT details->'shadow'->>'outcome',
           coalesce(sum((details->'shadow'->>'weight')::FLOAT), 0),
           coalesce(sum((details->'shadow'->>'banked')::INT), 0)
    FROM evolution_log
    WHERE playbook_id = %s
      AND event_type = 'growth'
      AND details->'shadow' IS NOT NULL
    GROUP BY 1
"""

UNSETTLED_SHADOWS_SQL = """
    SELECT id::STRING, service_name, predicted_outcome, playbook_applied::STRING,
           created_at, predicted_eta
    FROM predictions
    WHERE prevention_status = 'shadowed'
      AND playbook_applied IS NOT NULL
      AND coalesce(predicted_eta, created_at) < %s
    ORDER BY created_at
    LIMIT %s
"""


# --------------------------------------------------------------------------- #
# The one shape every transition speaks
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Lifecycle:
    """One `evolution_log` row, paired with the mutation that earned it."""

    event_type: str
    playbook_id: str
    details: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    incident_id: str | None = None
    fitness_before: float | None = None
    fitness_after: float | None = None


def _record(conn, events: list[Lifecycle]) -> None:
    """The only writer of `evolution_log` in this module."""
    for e in events:
        conn.execute(
            """
            INSERT INTO evolution_log
                (event_type, playbook_id, parent_id, trigger_incident_id,
                 fitness_before, fitness_after, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s::JSONB)
            """,
            (e.event_type, e.playbook_id, e.parent_id, e.incident_id,
             e.fitness_before, e.fitness_after, json.dumps(e.details, default=str)),
        )


def _mean(playbook: dict) -> float:
    return posterior.mean(playbook["success_count"], playbook["failure_count"])


def _trials(playbook: dict) -> int:
    return playbook["success_count"] + playbook["failure_count"]


def load_playbook(conn, playbook_id: str) -> dict | None:
    row = conn.execute(PLAYBOOK_SQL, (playbook_id,)).fetchone()
    if row is None:
        return None
    keys = ("id", "crdb_region", "name", "outcome_category", "success_count",
            "failure_count", "generation", "parent_id", "lineage", "memory_tier",
            "status", "reversible", "remediation_steps", "inverse_steps", "embedding")
    return dict(zip(keys, row, strict=True))


# --------------------------------------------------------------------------- #
# Growth
# --------------------------------------------------------------------------- #

# Guardian's outcomes, mapped to what they are evidence of. "inconclusive" is
# deliberately absent: a verification window that showed no movement taught us
# nothing, and recording it as either a success or a failure would be inventing
# evidence to keep a counter moving.
TRIAL_VERDICTS = {
    "prevented": "success",
    "rolled_back": "failure",
}
# The same mapping read off the prediction row, for an invocation that arrives
# without Guardian's result attached.
STATUS_VERDICTS = {
    "prevented": "success",
    "missed": "failure",
}


def grow(conn, playbook: dict, verdict: str, *, prediction_id: str | None,
         incident_id: str | None, reason: str) -> Lifecycle:
    """Apply a full-weight trial result and reset the disuse clock."""
    before = _mean(playbook)
    column = "success_count" if verdict == "success" else "failure_count"
    conn.execute(
        f"""
        UPDATE playbooks
        SET {column} = {column} + 1,
            last_used_at = now(),
            expires_at = now() + INTERVAL '{DISUSE_DAYS} days'
        WHERE id = %s
        """,
        (playbook["id"],),
    )
    playbook[column] += 1
    after = _mean(playbook)
    return Lifecycle(
        event_type="growth", playbook_id=playbook["id"], incident_id=incident_id,
        fitness_before=round(before, 4), fitness_after=round(after, 4),
        details={
            "kind": "trial", "verdict": verdict, "weight": 1.0, "reason": reason,
            "prediction_id": prediction_id,
            "successes": playbook["success_count"], "failures": playbook["failure_count"],
            "trials": _trials(playbook),
            "expires_at_reset_to_days": DISUSE_DAYS,
        },
    )


def _shadow_ledger(conn, playbook_id: str) -> dict[str, tuple[float, int]]:
    """Shadow credit accrued and already banked, per verdict.

    `success_count` and `failure_count` are integers, and a shadow observation is
    worth 0.3 of a trial — so the fraction has to live somewhere. It lives here,
    summed out of the append-only log rather than parked in a column: every
    shadow growth row carries the weight it contributed and the whole trials it
    caused to be banked, so the outstanding credit is
    `sum(weight) - sum(banked)` and needs no mutable state to stay correct. Three
    and a third shadow observations move a counter by one.
    """
    rows = conn.execute(SHADOW_LEDGER_SQL, (playbook_id,)).fetchall()
    return {r[0]: (float(r[1]), int(r[2])) for r in rows if r[0]}


def grow_shadow(conn, playbook: dict, verdict: str, *, prediction_id: str,
                incident_id: str | None, reason: str) -> Lifecycle:
    """Apply a reduced-weight shadow result, banking a whole trial when one is due."""
    before = _mean(playbook)
    accrued, banked = _shadow_ledger(conn, playbook["id"]).get(verdict, (0.0, 0))
    accrued += SHADOW_WEIGHT
    to_bank = int(math.floor(accrued)) - banked
    column = "success_count" if verdict == "success" else "failure_count"
    if to_bank > 0:
        conn.execute(
            f"UPDATE playbooks SET {column} = {column} + %s WHERE id = %s",
            (to_bank, playbook["id"]),
        )
        playbook[column] += to_bank
    after = _mean(playbook)
    return Lifecycle(
        event_type="growth", playbook_id=playbook["id"], incident_id=incident_id,
        fitness_before=round(before, 4), fitness_after=round(after, 4),
        details={
            "kind": "shadow_trial", "reason": reason, "prediction_id": prediction_id,
            "shadow": {"outcome": verdict, "weight": SHADOW_WEIGHT,
                       "banked": to_bank, "accrued": round(accrued, 4)},
            "successes": playbook["success_count"], "failures": playbook["failure_count"],
        },
    )


# --------------------------------------------------------------------------- #
# Shadow settlement
# --------------------------------------------------------------------------- #

def unsettled_shadows(conn, now: datetime) -> list[dict]:
    cutoff = now - timedelta(minutes=SHADOW_GRACE_MINUTES)
    rows = conn.execute(UNSETTLED_SHADOWS_SQL, (cutoff, SHADOW_SWEEP_LIMIT)).fetchall()
    return [
        {"id": r[0], "service": r[1], "category": r[2], "playbook_id": r[3],
         "created_at": r[4], "eta": r[5]}
        for r in rows
    ]


def _materialized(conn, shadow: dict, now: datetime) -> dict | None:
    """The incident the shadowed prediction was about, if one actually happened."""
    deadline = (shadow["eta"] or shadow["created_at"]) + timedelta(
        minutes=SHADOW_GRACE_MINUTES)
    row = conn.execute(
        """
        SELECT id::STRING, playbook_used::STRING, was_prevented, status
        FROM incidents
        WHERE %s::STRING = ANY(affected_services)
          AND detected_at >= %s
          AND detected_at <= %s
        ORDER BY detected_at
        LIMIT 1
        """,
        (shadow["service"], shadow["created_at"], min(deadline, now)),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "playbook_used": row[1], "was_prevented": row[2],
            "status": row[3]}


def settle_shadows(conn, now: datetime) -> list[Lifecycle]:
    """Score what the system watched but chose not to act on.

    A shadow record is a claim with two halves — "this will fail" and "this is
    what I would have run" — and each half is settled against a different fact.

    The prediction is settled against whether the failure arrived: it becomes
    `missed` if it did and `false_alarm` if it did not. That is what feeds the
    precision panel, and it is the only place a false alarm is ever written down.

    The playbook is settled only when there is something to compare it against.
    If the incident arrived and was eventually resolved by the very playbook the
    shadow had chosen, the choice was right and earns reduced-weight credit; if a
    different playbook is what actually resolved it, the choice was wrong and
    costs the same. If the failure never arrived, or arrived and nobody resolved
    it, the playbook gets nothing — it was never run, so there is no evidence
    about it either way, and manufacturing some would be the exact self-deception
    the shadow tier exists to avoid.
    """
    events: list[Lifecycle] = []
    for shadow in unsettled_shadows(conn, now):
        incident = _materialized(conn, shadow, now)
        if incident is None:
            conn.execute(
                "UPDATE predictions SET prevention_status = 'false_alarm', "
                "resolved_at = now() WHERE id = %s",
                (shadow["id"],),
            )
            logger.info("shadow settled as a false alarm", prediction_id=shadow["id"],
                        service=shadow["service"], category=shadow["category"])
            metrics.put("shadow_false_alarms", 1, service=shadow["service"])
            continue

        conn.execute(
            "UPDATE predictions SET prevention_status = 'missed', resolved_at = now() "
            "WHERE id = %s",
            (shadow["id"],),
        )
        metrics.put("shadow_missed_preventions", 1, service=shadow["service"])

        used = incident["playbook_used"]
        if used is None:
            logger.info("shadow materialized but nothing resolved it; no playbook evidence",
                        prediction_id=shadow["id"], incident_id=incident["id"])
            continue
        playbook = load_playbook(conn, shadow["playbook_id"])
        if playbook is None:
            continue
        if used == shadow["playbook_id"]:
            verdict = "success" if incident["was_prevented"] else "failure"
            reason = (f"shadow choice was executed for real on incident {incident['id']} "
                      f"and {'held' if verdict == 'success' else 'did not hold'}")
        else:
            verdict = "failure"
            reason = (f"a different playbook ({used}) is what resolved incident "
                      f"{incident['id']}")
        events.append(grow_shadow(conn, playbook, verdict, prediction_id=shadow["id"],
                                  incident_id=incident["id"], reason=reason))
        logger.info("shadow settled against the outcome", prediction_id=shadow["id"],
                    playbook_id=shadow["playbook_id"], verdict=verdict,
                    incident_id=incident["id"])
    return events


# --------------------------------------------------------------------------- #
# The stale sweep
# --------------------------------------------------------------------------- #

def sweep(conn, now: datetime) -> dict:
    """Un-wedge anything the pipeline left holding a lock it no longer deserves.

    Two things can leave a prediction sitting in `preventing` forever: an
    approval request nobody answered, and a pipeline execution that died between
    Sentinel's claim and Guardian's close-out. Neither is hypothetical — a Lambda
    timeout produces the second one — and a prediction stuck in `preventing`
    holds Oracle's dedup guard, so the same failure can never be predicted again.
    That is a silent, permanent blind spot, which is worse than the crash.

    An unanswered approval expires into a **shadow** record rather than a miss.
    Nobody said no; nobody said anything. The playbook stays attached and
    unexecuted, which is exactly what shadow means, and the eventual outcome
    scores the choice.

    An abandoned execution becomes a **miss**, because something was supposed to
    act and nothing did.
    """
    expired = conn.execute(
        """
        UPDATE approvals SET status = 'expired'
        WHERE status = 'pending' AND deadline < %s
        RETURNING id::STRING, prediction_id::STRING
        """,
        (now,),
    ).fetchall()
    for _, prediction_id in expired:
        conn.execute(
            "UPDATE predictions SET prevention_status = 'shadowed' "
            "WHERE id = %s AND prevention_status = 'preventing'",
            (prediction_id,),
        )

    # Only predictions with no approval outstanding: one waiting on a human is
    # not abandoned, it is waiting, and the deadline above is what bounds that.
    abandoned = conn.execute(
        """
        UPDATE predictions SET prevention_status = 'missed', resolved_at = now()
        WHERE prevention_status = 'preventing'
          AND coalesce(claimed_at, created_at) < %s
          AND id NOT IN (SELECT prediction_id FROM approvals WHERE status = 'pending')
        RETURNING id::STRING, service_name
        """,
        (now - timedelta(minutes=STALE_PREVENTING_MINUTES),),
    ).fetchall()

    if expired or abandoned:
        logger.info("stale sweep", approvals_expired=len(expired),
                    predictions_abandoned=len(abandoned),
                    stale_after_minutes=STALE_PREVENTING_MINUTES)
    for name, count in (("approvals_expired", len(expired)),
                        ("predictions_abandoned", len(abandoned))):
        metrics.put(name, count)
    return {
        "approvals_expired": [a[0] for a in expired],
        "predictions_abandoned": [p[0] for p in abandoned],
    }


# --------------------------------------------------------------------------- #
# Proposals — the two transitions that need a new genome written
# --------------------------------------------------------------------------- #

def _generate(prompt: str, *, system: str, max_tokens: int = 1200,
              temperature: float = 0.4) -> tuple[str, str]:
    """Ask the reasoning model for a genome. Returns (raw text, source label).

    The single seam through which every proposal passes, so a harness that has to
    drive the lifecycle without Bedrock can substitute one function and have the
    substitution recorded honestly: the source label it returns is what lands in
    `evolution_log.details.proposed_by`.
    """
    return bedrock.claude(prompt, system=system, max_tokens=max_tokens,
                          temperature=temperature), "bedrock"


def _validated(raw: str, source: str, *, context: str) -> PlaybookDraft | None:
    """Parse a proposal, and refuse anything that does not validate.

    Same rule as birth: a malformed genome is stillborn. It is logged with the
    offending payload so the prompt can be fixed, and it is never written.
    """
    text = raw.strip()
    if "```" in text:
        text = text.split("```")[1].removeprefix("json").strip()
    try:
        return PlaybookDraft.model_validate_json(text)
    except (ValidationError, ValueError) as e:
        logger.error("proposed genome rejected by schema validation", context=context,
                     source=source, error=str(e)[:400], payload=text[:400])
        metrics.put("playbook_proposals_rejected", 1, context=context)
        return None


def propose_variant(playbook: dict, failure: dict) -> tuple[PlaybookDraft | None, str]:
    """Ask for a variant of a playbook that just failed and was rolled back."""
    prompt = json.dumps({
        "failed_playbook": {
            "name": playbook["name"], "generation": playbook["generation"],
            "outcome_category": playbook["outcome_category"],
            "remediation_steps": playbook["remediation_steps"],
            "trials": _trials(playbook), "posterior_mean": round(_mean(playbook), 4),
        },
        "incident": {"service": failure.get("service"),
                     "outcome_category": playbook["outcome_category"],
                     "root_cause": failure.get("root_cause")},
        "rollback_telemetry": failure.get("verification"),
        "schema": {
            "name": "str", "outcome_category": playbook["outcome_category"],
            "rationale": "why this variant addresses what the parent got wrong",
            "remediation_steps": [{
                "action": "one of the allowed actions", "target": "{service}",
                "params": {}, "inverse": {"action": "...", "params": {}},
            }],
        },
    }, default=str)[:6000]
    try:
        raw, source = _generate(prompt, system=MUTATION_SYSTEM.format(
            actions=", ".join(ACTIONS), service="{service}"))
    except Exception as e:
        logger.warning("no variant proposed; the reasoning model is unreachable",
                       playbook_id=playbook["id"], error=str(e))
        return None, "unavailable"
    return _validated(raw, source, context="mutation"), source


def propose_canonical(left: dict, right: dict, category: str
                      ) -> tuple[PlaybookDraft | None, str]:
    """Ask for the canonical playbook that replaces two converged siblings."""
    prompt = json.dumps({
        "outcome_category": category,
        "parents": [
            {"name": p["name"], "remediation_steps": p["remediation_steps"],
             "successes": p["success_count"], "failures": p["failure_count"],
             "posterior_mean": round(_mean(p), 4)}
            for p in (left, right)
        ],
        "cosine_distance": round(right.get("distance", 0.0), 4),
        "schema": {
            "name": "str", "outcome_category": category,
            "rationale": "what the two parents agreed on",
            "remediation_steps": [{
                "action": "one of the allowed actions", "target": "{service}",
                "params": {}, "inverse": {"action": "...", "params": {}},
            }],
        },
    }, default=str)[:6000]
    try:
        raw, source = _generate(prompt, system=MERGE_SYSTEM.format(
            actions=", ".join(ACTIONS), service="{service}"), temperature=0.2)
    except Exception as e:
        logger.warning("no canonical playbook proposed; the reasoning model is unreachable",
                       playbook_id=left["id"], error=str(e))
        return None, "unavailable"
    return _validated(raw, source, context="merge"), source


# --------------------------------------------------------------------------- #
# Mutation
# --------------------------------------------------------------------------- #

def has_untried_child(conn, playbook_id: str) -> bool:
    """Whether an earlier failure already bred a variant nobody has tried yet.

    A variant that has not been given a turn is not evidence that another variant
    is needed, and without this check a playbook that keeps losing breeds one
    child per rollback until the candidate list is nothing but untested siblings.
    """
    row = conn.execute(
        """
        SELECT count(*) FROM playbooks
        WHERE parent_id = %s AND status = 'active'
          AND success_count = 0 AND failure_count = 0
        """,
        (playbook_id,),
    ).fetchone()
    return int(row[0]) > 0


def mutate(conn, parent: dict, draft: PlaybookDraft, source: str, *,
           incident_id: str | None, reason: str) -> Lifecycle:
    """Insert a variant at generation+1 with a flat prior. The parent stays active.

    The parent is not retired here: one bad trial is one bad trial, and the whole
    point of keeping both alive is that the competition decides between them on
    evidence rather than on the assumption that newer is better.
    """
    steps = [s.model_dump(exclude_none=True) for s in draft.remediation_steps]
    lineage = list(parent["lineage"] or []) + [parent["id"]]
    child_id = conn.execute(
        """
        INSERT INTO playbooks
            (crdb_region, name, outcome_category, precursor_embedding, remediation_steps,
             inverse_steps, reversible, success_count, failure_count, generation,
             parent_id, lineage, memory_tier, status)
        VALUES (%s, %s, %s, %s::VECTOR, %s::JSONB, %s::JSONB, %s, 0, 0, %s,
                %s, %s::UUID[], 'experimental', 'active')
        RETURNING id::STRING
        """,
        (parent["crdb_region"], draft.name, parent["outcome_category"],
         parent["embedding"], json.dumps(steps), json.dumps(draft.inverse_steps()),
         draft.reversible, parent["generation"] + 1, parent["id"], lineage),
    ).fetchone()[0]
    logger.info("variant bred from a failed trial", parent_id=parent["id"],
                playbook_id=child_id, generation=parent["generation"] + 1,
                name=draft.name, proposed_by=source)
    metrics.put("playbook_mutations", 1, category=parent["outcome_category"])
    return Lifecycle(
        event_type="mutation", playbook_id=child_id, parent_id=parent["id"],
        incident_id=incident_id, fitness_before=round(_mean(parent), 4),
        fitness_after=0.5,
        details={
            "reason": reason, "generation": parent["generation"] + 1,
            "prior": "Beta(1,1)", "parent_name": parent["name"],
            "parent_posterior_mean": round(_mean(parent), 4),
            "reversible": draft.reversible, "rationale": draft.rationale,
            "proposed_by": source, "parent_status": "active",
        },
    )


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #

def merge_candidate(conn, playbook: dict) -> dict | None:
    """The nearest active sibling close enough — and good enough — to merge with.

    A relative is not a sibling. A mutation is placed at its parent's position in
    the precursor space, because a variant claims to answer the same pattern, so
    without the lineage check every child would sit at distance 0 from its parent
    and merge back into it the moment it won a trial. Merge is for playbooks that
    arrived at the same answer *independently*; collapsing a family into itself is
    not convergence, it is forgetting how it got there.
    """
    row = conn.execute(
        MERGE_CANDIDATE_SQL,
        (playbook["embedding"], playbook["outcome_category"], playbook["id"],
         list(playbook["lineage"] or []), playbook["id"],
         playbook["embedding"], MERGE_DISTANCE, playbook["embedding"]),
    ).fetchone()
    if row is None:
        return None
    keys = ("id", "crdb_region", "name", "success_count", "failure_count", "generation",
            "lineage", "remediation_steps", "embedding", "distance")
    sibling = dict(zip(keys, row, strict=True))
    sibling["distance"] = float(sibling["distance"])
    if _mean(playbook) <= MERGE_MIN_MEAN or _mean(sibling) <= MERGE_MIN_MEAN:
        return None
    # Restated in Python as well as in SQL: this is the predicate that decides
    # whether a family collapses into itself, and it is worth being unmissable.
    if sibling["id"] in (playbook["lineage"] or []) or \
            playbook["id"] in (sibling["lineage"] or []):
        return None
    return sibling


def _midpoint(left: str, right: str) -> str:
    """The normalized midpoint of two vector literals — the child's position.

    Deterministic on purpose. The child's *steps* are synthesized, but where it
    sits in the precursor space is a fact about its parents, not a judgement, so
    no model is asked for it.
    """
    import numpy as np

    a = np.array(json.loads(left), dtype="float64")
    b = np.array(json.loads(right), dtype="float64")
    mid = a + b
    norm = np.linalg.norm(mid)
    if norm == 0:  # pragma: no cover — two exactly opposed vectors cannot be 0.15 apart
        return left
    mid = mid / norm
    return "[" + ",".join(f"{x:.6f}" for x in mid) + "]"


def merge(conn, left: dict, right: dict, draft: PlaybookDraft, source: str,
          *, incident_id: str | None) -> list[Lifecycle]:
    """Replace two converged siblings with one canonical child.

    The child inherits `min(successes)` and `max(failures)` from its parents —
    the most conservative reading of the evidence that still transfers it. A
    flat prior would be cleaner in theory and wrong in practice: it would retire
    two proven playbooks in favour of an untested one and leave a hole in the
    category exactly where the memory used to be strongest. Taking the weaker
    parent's successes and the worse parent's failures means the merge can never
    claim more competence than was actually demonstrated.

    Neither parent is deleted. `status = 'merged'` removes them from selection
    and keeps them in the genealogy, which is the answer to "what did this come
    from?" — and to "undo the merge".
    """
    successes = min(left["success_count"], right["success_count"])
    failures = max(left["failure_count"], right["failure_count"])
    lineage: list[str] = []
    for parent in (left, right):
        for ancestor in list(parent["lineage"] or []) + [parent["id"]]:
            if ancestor not in lineage:
                lineage.append(ancestor)
    steps = [s.model_dump(exclude_none=True) for s in draft.remediation_steps]
    stronger = left if _mean(left) >= _mean(right) else right
    child_id = conn.execute(
        """
        INSERT INTO playbooks
            (crdb_region, name, outcome_category, precursor_embedding, remediation_steps,
             inverse_steps, reversible, success_count, failure_count, generation,
             parent_id, lineage, memory_tier, status)
        VALUES (%s, %s, %s, %s::VECTOR, %s::JSONB, %s::JSONB, %s, %s, %s, %s,
                %s, %s::UUID[], 'operational', 'active')
        RETURNING id::STRING
        """,
        (left["crdb_region"], draft.name, left["outcome_category"],
         _midpoint(left["embedding"], right["embedding"]), json.dumps(steps),
         json.dumps(draft.inverse_steps()), draft.reversible, successes, failures,
         max(left["generation"], right["generation"]) + 1, stronger["id"], lineage),
    ).fetchone()[0]
    conn.execute(
        "UPDATE playbooks SET status = 'merged' WHERE id = ANY(%s::UUID[])",
        ([left["id"], right["id"]],),
    )
    logger.info("siblings merged", playbook_id=child_id, parents=[left["id"], right["id"]],
                distance=round(right["distance"], 4), name=draft.name, proposed_by=source)
    metrics.put("playbook_merges", 1, category=left["outcome_category"])

    child_mean = posterior.mean(successes, failures)
    # One row per absorbed parent, so the genealogy tree draws an edge from each.
    return [
        Lifecycle(
            event_type="merge", playbook_id=child_id, parent_id=parent["id"],
            incident_id=incident_id, fitness_before=round(_mean(parent), 4),
            fitness_after=round(child_mean, 4),
            details={
                "reason": (f"cosine distance {right['distance']:.4f} < {MERGE_DISTANCE} "
                           f"with both posterior means above {MERGE_MIN_MEAN}"),
                "absorbed_parent": parent["id"], "absorbed_parent_name": parent["name"],
                "parent_status": "merged", "distance": round(right["distance"], 4),
                "inherited": {"successes": successes, "failures": failures,
                              "rule": "min(successes), max(failures) across parents"},
                "proposed_by": source, "rationale": draft.rationale,
            },
        )
        for parent in (left, right)
    ]


# --------------------------------------------------------------------------- #
# Promotion and retirement
# --------------------------------------------------------------------------- #

def promote(conn, playbook: dict, *, incident_id: str | None) -> Lifecycle | None:
    """Copy proven memory into the GLOBAL institutional table.

    `institutional_playbooks` is `LOCALITY GLOBAL`, so once a playbook lands
    there every region reads it locally instead of paying a cross-region hop to
    the row's home. Promotion is what a system knowing something *everywhere*
    actually looks like in a schema.
    """
    mean = _mean(playbook)
    if mean <= PROMOTION_MEAN or _trials(playbook) < PROMOTION_TRIALS:
        return None
    if playbook["memory_tier"] == "institutional":
        return None
    existing = conn.execute(
        "SELECT count(*) FROM institutional_playbooks WHERE source_playbook_id = %s",
        (playbook["id"],),
    ).fetchone()[0]
    if int(existing) > 0:
        return None

    lineage = list(playbook["lineage"] or [])
    conn.execute(
        """
        INSERT INTO institutional_playbooks
            (source_playbook_id, name, outcome_category, precursor_embedding,
             remediation_steps, inverse_steps, reversible, success_count,
             failure_count, generation, lineage)
        VALUES (%s, %s, %s, %s::VECTOR, %s::JSONB, %s::JSONB, %s, %s, %s, %s, %s::UUID[])
        """,
        (playbook["id"], playbook["name"], playbook["outcome_category"],
         playbook["embedding"], json.dumps(playbook["remediation_steps"]),
         json.dumps(playbook["inverse_steps"]), playbook["reversible"],
         playbook["success_count"], playbook["failure_count"],
         playbook["generation"], lineage),
    )
    conn.execute(
        "UPDATE playbooks SET memory_tier = 'institutional', promoted_at = now() "
        "WHERE id = %s",
        (playbook["id"],),
    )
    playbook["memory_tier"] = "institutional"
    logger.info("playbook promoted to the institutional tier", playbook_id=playbook["id"],
                name=playbook["name"], posterior_mean=round(mean, 4),
                trials=_trials(playbook))
    metrics.put("playbook_promotions", 1, category=playbook["outcome_category"])
    return Lifecycle(
        event_type="promotion", playbook_id=playbook["id"], incident_id=incident_id,
        fitness_before=round(mean, 4), fitness_after=round(mean, 4),
        details={
            "reason": (f"posterior mean {mean:.4f} > {PROMOTION_MEAN} with "
                       f"{_trials(playbook)} trials (>= {PROMOTION_TRIALS})"),
            "trials": _trials(playbook), "target": "institutional_playbooks",
            "locality": "GLOBAL", "memory_tier": "institutional",
        },
    )


def retire(conn, playbook: dict, *, incident_id: str | None) -> Lifecycle | None:
    """Take a playbook out of selection once it has had enough chances and failed.

    `expires_at` is left where growth last put it. The 90-day clock is a *disuse*
    clock, not a punishment: a retired playbook stops being used, so it stops
    having its expiry pushed forward, and Row-Level TTL reaps it 90 days after
    the last time it was actually tried. Setting it to now here would delete the
    ancestry the genealogy tree is drawn from.
    """
    mean = _mean(playbook)
    if mean >= RETIREMENT_MEAN or _trials(playbook) < RETIREMENT_TRIALS:
        return None
    if playbook["status"] != "active":
        return None
    conn.execute(
        "UPDATE playbooks SET status = 'retired', memory_tier = 'retired', "
        "retired_at = now() WHERE id = %s",
        (playbook["id"],),
    )
    playbook["status"] = "retired"
    logger.info("playbook retired", playbook_id=playbook["id"], name=playbook["name"],
                posterior_mean=round(mean, 4), trials=_trials(playbook))
    metrics.put("playbook_retirements", 1, category=playbook["outcome_category"])
    return Lifecycle(
        event_type="retirement", playbook_id=playbook["id"], incident_id=incident_id,
        fitness_before=round(mean, 4), fitness_after=round(mean, 4),
        details={
            "reason": (f"posterior mean {mean:.4f} < {RETIREMENT_MEAN} after "
                       f"{_trials(playbook)} trials (>= {RETIREMENT_TRIALS})"),
            "trials": _trials(playbook),
            "expires_at": f"unchanged — {DISUSE_DAYS}-day disuse TTL from its last trial",
        },
    )


# --------------------------------------------------------------------------- #
# The invocation
# --------------------------------------------------------------------------- #

def _read_context(conn, prediction_id: str | None, outcome: dict) -> dict:
    """Everything the write pass needs, gathered before any model is called."""
    context: dict[str, Any] = {
        "prediction_id": prediction_id,
        "incident_id": outcome.get("incident_id"),
        "service": outcome.get("service"),
        "verification": outcome.get("verification"),
        "root_cause": outcome.get("root_cause"),
        "verdict": None,
        "playbook": None,
    }
    playbook_id = outcome.get("playbook_id")
    status = None
    if prediction_id:
        row = conn.execute(
            "SELECT playbook_applied::STRING, prevention_status, service_name "
            "FROM predictions WHERE id = %s",
            (prediction_id,),
        ).fetchone()
        if row:
            playbook_id = playbook_id or row[0]
            status = row[1]
            context["service"] = context["service"] or row[2]
    if not playbook_id:
        context["reason"] = "no playbook was selected, so there is nothing to score"
        return context

    context["playbook"] = load_playbook(conn, playbook_id)
    guardian_outcome = outcome.get("outcome")
    if guardian_outcome in TRIAL_VERDICTS:
        context["verdict"] = TRIAL_VERDICTS[guardian_outcome]
        context["reason"] = f"guardian reported '{guardian_outcome}'"
    elif guardian_outcome is None and status in STATUS_VERDICTS:
        context["verdict"] = STATUS_VERDICTS[status]
        context["reason"] = f"prediction resolved as '{status}'"
    else:
        context["reason"] = (
            f"guardian reported '{guardian_outcome}', which is not evidence either way"
            if guardian_outcome else "no trial result to score"
        )
    return context


def chronicle(prediction_id: str | None, outcome: dict | None = None) -> dict:
    """Read, propose, then apply. The whole of Chronicler's job."""
    outcome = outcome or {}
    now = datetime.now(UTC)

    context = db.tx_retry(lambda conn: _read_context(conn, prediction_id, outcome))
    playbook = context.get("playbook")

    # --- the proposals, outside any transaction ---------------------------- #
    variant: tuple[PlaybookDraft | None, str] = (None, "not attempted")
    if playbook and context["verdict"] == "failure":
        needed = db.tx_retry(lambda conn: not has_untried_child(conn, playbook["id"]))
        if needed:
            variant = propose_variant(playbook, context)
        else:
            logger.info("rollback did not breed: an untried variant already exists",
                        playbook_id=playbook["id"])

    sibling = None
    if playbook and playbook["status"] == "active":
        sibling = db.tx_retry(lambda conn: merge_candidate(conn, playbook))
    canonical: tuple[PlaybookDraft | None, str] = (None, "not attempted")
    if sibling:
        canonical = propose_canonical(playbook, sibling, playbook["outcome_category"])

    # --- the write pass ---------------------------------------------------- #
    def apply(conn) -> dict:
        events: list[Lifecycle] = []
        applied: dict[str, Any] = {
            "growth": None, "mutation": None, "merge": None,
            "promotion": None, "retirement": None,
        }
        swept = sweep(conn, now)

        current = load_playbook(conn, playbook["id"]) if playbook else None
        if current and context["verdict"]:
            events.append(grow(conn, current, context["verdict"],
                               prediction_id=prediction_id,
                               incident_id=context["incident_id"],
                               reason=context["reason"]))
            applied["growth"] = context["verdict"]

        events.extend(settle_shadows(conn, now))

        # Mutation before retirement, deliberately. A playbook whose last trial
        # takes it below the retirement line breeds and then steps aside in the
        # same pass — which is the point of the whole mechanism, not an ordering
        # accident.
        if current and variant[0] is not None and not has_untried_child(conn, current["id"]):
            event = mutate(conn, current, variant[0], variant[1],
                           incident_id=context["incident_id"],
                           reason="the trial degraded the target metric and was rolled back")
            events.append(event)
            applied["mutation"] = event.playbook_id

        if current and current["status"] == "active" and canonical[0] is not None:
            # Re-check under the lock: the sibling may have been merged or
            # retired between the read pass and now.
            fresh = merge_candidate(conn, current)
            if fresh and fresh["id"] == sibling["id"]:
                merged = merge(conn, current, fresh, canonical[0], canonical[1],
                               incident_id=context["incident_id"])
                events.extend(merged)
                applied["merge"] = merged[0].playbook_id
                current = load_playbook(conn, current["id"])

        if current and current["status"] == "active":
            promotion = promote(conn, current, incident_id=context["incident_id"])
            if promotion:
                events.append(promotion)
                applied["promotion"] = current["id"]
            retirement = retire(conn, current, incident_id=context["incident_id"])
            if retirement:
                events.append(retirement)
                applied["retirement"] = current["id"]

        _record(conn, events)
        return {"events": events, "applied": applied, "swept": swept,
                "playbook": current or playbook}

    result = db.tx_retry(apply)
    events = result["events"]
    final = result["playbook"]

    logger.info("lifecycle applied", prediction_id=prediction_id,
                playbook_id=(final or {}).get("id"), events=len(events),
                types=[e.event_type for e in events], **{
                    k: v for k, v in result["applied"].items() if v})
    metrics.put("lifecycle_events", len(events))

    return {
        "prediction_id": prediction_id,
        "playbook_id": (final or {}).get("id"),
        "playbook_name": (final or {}).get("name"),
        "verdict": context["verdict"],
        "reason": context.get("reason"),
        "posterior_mean": round(_mean(final), 4) if final else None,
        "trials": _trials(final) if final else None,
        "memory_tier": (final or {}).get("memory_tier"),
        "status": (final or {}).get("status"),
        "applied": result["applied"],
        "swept": result["swept"],
        "lifecycle_events": len(events),
        "events": [
            {"event_type": e.event_type, "playbook_id": e.playbook_id,
             "parent_id": e.parent_id, "fitness_before": e.fitness_before,
             "fitness_after": e.fitness_after,
             "reason": e.details.get("reason") or e.details.get("verdict")}
            for e in events
        ],
    }


def handler(event: dict, _context=None) -> dict:
    # Two ways in. A scheduled `{"sweep": true}` runs the stale sweep alone, so
    # nothing stays wedged on a day when no prediction fires. Otherwise Step
    # Functions hands Chronicler whatever Guardian returned, including on the
    # rollback path; the local runner and the tests pass the same shape.
    if event.get("sweep"):
        swept = db.tx_retry(lambda conn: sweep(conn, datetime.now(UTC)))
        logger.info("scheduled sweep complete", **{k: len(v) for k, v in swept.items()})
        return {"agent": "chronicler", "mode": "sweep", "swept": swept}

    outcome = event.get("guardian") or event
    prediction_id = outcome.get("prediction_id") or event.get("prediction_id") or (
        event.get("detail", {}).get("prediction", {}).get("id"))
    logger.info("chronicler invoked", prediction_id=prediction_id,
                guardian_outcome=outcome.get("outcome"))
    result = chronicle(str(prediction_id) if prediction_id else None, outcome)
    return {"agent": "chronicler", **result}


if __name__ == "__main__":  # local: python agents/chronicler/app.py '<guardian json>'
    import sys

    os.environ.setdefault("LOG_LEVEL", "INFO")
    print(json.dumps(handler(json.loads(sys.argv[1])), indent=2, default=str))
