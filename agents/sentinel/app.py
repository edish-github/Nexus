"""Sentinel: claim a prediction, run the competition, choose a response tier.

Three things happen here, in this order, and the order matters.

**The claim.** Changefeed delivery is at-least-once, so the first thing Sentinel
does is try to take exclusive ownership of the prediction:
`SELECT ... WHERE prevention_status='pending' FOR UPDATE`, then flip it to
`preventing`. A duplicate delivery finds the row already claimed and returns a
clean no-op. Two Sentinels can never both act on one prediction — the same
serializable guarantee that makes the competition safe.

**The competition.** Candidate playbooks are retrieved by vector similarity
within the predicted category, then selected by **Thompson sampling**: each
candidate's Beta posterior is *sampled*, and the sample is weighted by
similarity. Not argmax over a fitness score — argmax is a leaderboard, and a
leaderboard means a newborn playbook never gets selected, never gathers
evidence, and dies by TTL. Sampling is what lets a challenger occasionally beat
an incumbent, earn a trial, and prove itself. Every draw is logged, so the
competition is explainable rather than asserted.

**The tier.** Confidence and reversibility decide what actually happens:

    posterior mean < 0.75              → shadow  (record what would have run)
    >= 0.75 and the playbook reverses  → auto    (execute now)
    >= 0.75 and it does not            → approve (ask a human, with evidence)

That gate is the answer to the failure mode that alerting systems do not have:
a false positive here changes production.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

import numpy as np

from nexus_common import db, log, metrics, posterior
from nexus_common.posterior import SHADOW_WEIGHT, Candidate
from nexus_common.steps import parse_steps

logger = log.get_logger("sentinel")

CANDIDATE_LIMIT = 8       # top-8 by similarity, per the plan
MAX_DISTANCE = 0.35       # beyond this a playbook is not about this situation
AUTO_TIER_THRESHOLD = 0.75

CLAIM_SELECT_SQL = """
    SELECT id::STRING, service_name, predicted_outcome, predicted_severity,
           alpha, beta, current_embedding::STRING, prevention_status, claimed_by
    FROM predictions
    WHERE id = %s
    FOR UPDATE
"""

CANDIDATES_SQL = """
    SELECT id::STRING, name, success_count, failure_count, reversible,
           remediation_steps, inverse_steps, generation, memory_tier,
           1 - (precursor_embedding <=> %s::VECTOR) AS similarity
    FROM playbooks
    WHERE outcome_category = %s
      AND status = 'active'
      AND precursor_embedding <=> %s::VECTOR < %s
    ORDER BY precursor_embedding <=> %s::VECTOR
    LIMIT %s
"""


class AlreadyClaimed(Exception):
    """The prediction was claimed by someone else — a duplicate delivery."""

    def __init__(self, prediction_id: str, holder: str | None, status: str):
        super().__init__(f"prediction {prediction_id} is {status} (held by {holder})")
        self.prediction_id = prediction_id
        self.holder = holder
        self.status = status


def claim(conn, prediction_id: str, claimed_by: str) -> dict:
    """Take exclusive ownership of a prediction, or raise AlreadyClaimed.

    Runs inside the caller's transaction so the row lock is held from the
    `FOR UPDATE` until the status flip commits.
    """
    row = conn.execute(CLAIM_SELECT_SQL, (prediction_id,)).fetchone()
    if row is None:
        raise AlreadyClaimed(prediction_id, None, "missing")
    (pid, service, category, severity, alpha, beta, embedding, status, holder) = row
    if status != "pending":
        raise AlreadyClaimed(pid, holder, status)

    conn.execute(
        """
        UPDATE predictions
        SET prevention_status = 'preventing', claimed_by = %s, claimed_at = now()
        WHERE id = %s
        """,
        (claimed_by, prediction_id),
    )
    return {
        "id": pid, "service_name": service, "outcome_category": category,
        "severity": severity, "alpha": float(alpha), "beta": float(beta),
        "embedding": embedding,
    }


def candidates_for(conn, embedding: str, category: str) -> list[Candidate]:
    rows = conn.execute(
        CANDIDATES_SQL,
        (embedding, category, embedding, MAX_DISTANCE, embedding, CANDIDATE_LIMIT),
    ).fetchall()
    return [
        Candidate(
            playbook_id=r[0], name=r[1], successes=int(r[2]), failures=int(r[3]),
            reversible=bool(r[4]),
            remediation_steps=r[5] if isinstance(r[5], list) else json.loads(r[5] or "[]"),
            inverse_steps=r[6] if isinstance(r[6], list) else json.loads(r[6] or "[]"),
            generation=int(r[7]), memory_tier=r[8], similarity=float(r[9]),
        )
        for r in rows
    ]


def decide_tier(confidence: float, winner: Candidate) -> tuple[str, str]:
    """The tiered response gate. Returns (tier, why)."""
    if confidence < AUTO_TIER_THRESHOLD:
        return "shadow", (
            f"posterior mean {confidence:.3f} is below the {AUTO_TIER_THRESHOLD} "
            "action threshold; recording what would have run"
        )
    if not winner.reversible:
        return "approve", (
            f"confident ({confidence:.3f}) but '{winner.name}' contains a step with no "
            "inverse, so it cannot be rolled back without a human deciding"
        )
    return "auto", (
        f"posterior mean {confidence:.3f} and '{winner.name}' is fully reversible"
    )


def _irreversible_reason(winner: Candidate) -> str:
    try:
        steps = parse_steps(winner.remediation_steps)
    except ValueError:
        return "the playbook's steps did not validate against the step schema"
    offenders = [s.action for s in steps if not s.reversible]
    return (
        f"no inverse declared for: {', '.join(offenders)}" if offenders
        else "the playbook is marked irreversible"
    )


def _record_competition(conn, prediction: dict, winner_draw, draws, tier: str,
                        why: str) -> None:
    """One evolution_log row per competition — the UI's 'why' panel reads this."""
    conn.execute(
        """
        INSERT INTO evolution_log (event_type, playbook_id, fitness_before, details)
        VALUES ('competition', %s, %s, %s::JSONB)
        """,
        (winner_draw.candidate.playbook_id, winner_draw.candidate.posterior_mean,
         json.dumps({
             "kind": "playbook_competition",
             "prediction_id": prediction["id"],
             "service": prediction["service_name"],
             "outcome_category": prediction["outcome_category"],
             "prediction_confidence": round(
                 posterior.mean(prediction["alpha"] - 1, prediction["beta"] - 1), 4),
             "tier": tier,
             "why": why,
             "winner": winner_draw.candidate.playbook_id,
             "shadow_weight": SHADOW_WEIGHT if tier == "shadow" else None,
             "candidates": posterior.explain(draws),
         })),
    )


def _request_approval(conn, prediction: dict, winner: Candidate, draws,
                      confidence: float) -> str | None:
    """Write the approval request with everything a human needs to decide."""
    evidence = {
        "prediction": {
            "id": prediction["id"],
            "service": prediction["service_name"],
            "outcome_category": prediction["outcome_category"],
            "severity": prediction["severity"],
            "posterior": {"alpha": prediction["alpha"], "beta": prediction["beta"],
                          "mean": round(confidence, 4)},
        },
        "playbook": {
            "id": winner.playbook_id, "name": winner.name,
            "generation": winner.generation, "memory_tier": winner.memory_tier,
            "posterior_mean": round(winner.posterior_mean, 4),
            "trials": winner.trials,
            "remediation_steps": winner.remediation_steps,
            "inverse_steps": winner.inverse_steps,
        },
        "competition": posterior.explain(draws),
    }
    row = conn.execute(
        """
        INSERT INTO approvals
            (prediction_id, playbook_id, service_name, outcome_category, reason, evidence)
        VALUES (%s, %s, %s, %s, %s, %s::JSONB)
        ON CONFLICT (prediction_id) WHERE status = 'pending' DO NOTHING
        RETURNING id::STRING
        """,
        (prediction["id"], winner.playbook_id, prediction["service_name"],
         prediction["outcome_category"], _irreversible_reason(winner),
         json.dumps(evidence)),
    ).fetchone()
    return row[0] if row else None


def evaluate(prediction_id: str, *, claimed_by: str | None = None,
             seed: int | None = None) -> dict:
    """Claim, compete, and decide. The whole of Sentinel's job."""
    claimed_by = claimed_by or f"sentinel-{uuid.uuid4().hex[:8]}"
    rng = np.random.default_rng(seed)

    def run(conn) -> dict:
        try:
            prediction = claim(conn, prediction_id, claimed_by)
        except AlreadyClaimed as e:
            logger.info("duplicate delivery ignored", prediction_id=e.prediction_id,
                        status=e.status, holder=e.holder)
            metrics.put("duplicate_deliveries", 1)
            return {"prediction_id": prediction_id, "claimed": False,
                    "status": e.status, "tier": None, "reason": "already claimed"}

        confidence = posterior.mean(prediction["alpha"] - 1, prediction["beta"] - 1)
        candidates = candidates_for(conn, prediction["embedding"],
                                    prediction["outcome_category"])
        if not candidates:
            # Nothing in memory answers this pattern. That is not a failure —
            # it is the cold-start path, and Diagnostician turns it into a birth.
            logger.info("no candidate playbooks", prediction_id=prediction_id,
                        category=prediction["outcome_category"])
            metrics.put("predictions_without_candidates", 1)
            return {"prediction_id": prediction_id, "claimed": True,
                    "tier": "novel", "reason": "no active playbook matches this pattern",
                    "service": prediction["service_name"],
                    "outcome_category": prediction["outcome_category"],
                    "confidence": round(confidence, 4), "candidates": []}

        winner_draw, draws = posterior.compete(candidates, rng)
        winner = winner_draw.candidate
        tier, why = decide_tier(confidence, winner)
        if tier == "approve":
            why = f"{why} ({_irreversible_reason(winner)})"

        _record_competition(conn, prediction, winner_draw, draws, tier, why)

        approval_id = None
        if tier == "approve":
            approval_id = _request_approval(conn, prediction, winner, draws, confidence)
        elif tier == "shadow":
            # Shadow: nothing is executed, but the choice is recorded so
            # Chronicler can score it against the eventual outcome at reduced
            # weight. The system learns even when it does not act.
            conn.execute(
                "UPDATE predictions SET prevention_status = 'shadowed', playbook_applied = %s "
                "WHERE id = %s",
                (winner.playbook_id, prediction_id),
            )
        if tier in ("auto", "approve"):
            conn.execute("UPDATE predictions SET playbook_applied = %s WHERE id = %s",
                         (winner.playbook_id, prediction_id))

        # A challenger winning is the visible face of exploration; worth its own
        # log line, because it is the moment the demo is built around.
        incumbent = max(draws, key=lambda d: d.candidate.posterior_mean).candidate
        upset = incumbent.playbook_id != winner.playbook_id
        logger.info(
            "competition decided", prediction_id=prediction_id,
            playbook_id=winner.playbook_id, winner=winner.name, tier=tier,
            confidence=round(confidence, 4),
            winner_posterior=round(winner.posterior_mean, 4),
            beta_sample=round(winner_draw.beta_sample, 4),
            similarity=round(winner.similarity, 4), candidates=len(draws),
            challenger_upset=upset,
        )
        metrics.put("competitions", 1, tier=tier)
        metrics.put("challenger_upsets", 1 if upset else 0)

        return {
            "prediction_id": prediction_id,
            "claimed": True,
            "claimed_by": claimed_by,
            "service": prediction["service_name"],
            "outcome_category": prediction["outcome_category"],
            "severity": prediction["severity"],
            "confidence": round(confidence, 4),
            "tier": tier,
            "reason": why,
            "approval_id": approval_id,
            "challenger_upset": upset,
            "playbook": {
                "id": winner.playbook_id,
                "name": winner.name,
                "reversible": winner.reversible,
                "generation": winner.generation,
                "posterior_mean": round(winner.posterior_mean, 4),
                "trials": winner.trials,
                "similarity": round(winner.similarity, 4),
                "remediation_steps": winner.remediation_steps,
                "inverse_steps": winner.inverse_steps,
            },
            "candidates": posterior.explain(draws),
        }

    return db.tx_retry(run)


def handler(event: dict, _context=None) -> dict:
    detail: dict[str, Any] = event.get("detail", event)
    prediction = detail.get("prediction", {})
    prediction_id = prediction.get("id") or detail.get("prediction_id")
    logger.info("sentinel invoked", prediction_id=prediction_id,
                idempotency_key=detail.get("idempotency_key"))
    if not prediction_id:
        raise ValueError("no prediction id in the event")
    result = evaluate(str(prediction_id))
    return {"agent": "sentinel", **result}


if __name__ == "__main__":  # local: python agents/sentinel/app.py <prediction-id>
    import sys

    os.environ.setdefault("LOG_LEVEL", "INFO")
    print(json.dumps(handler({"prediction_id": sys.argv[1]}), indent=2, default=str))
