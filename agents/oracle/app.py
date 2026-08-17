"""Oracle: predict failures from precursor-snapshot matches.

Runs on a schedule (60s in demo mode — chosen over telemetry-driven triggering
because a fixed cadence makes the demo deterministic). For each service it takes
the most recent embedded telemetry window from the sensory tier, finds its
nearest historical precursors, and emits a prediction when those neighbours
agree that this is how a failure starts.

The confidence is a **Beta posterior over the matched neighbours' outcomes**,
not a decorated cosine similarity:

    alpha = (matched precursors that led to an incident) + 1
    beta  = (matched precursors that recovered on their own) + 1

Both parameters are stored, so the credible interval can be recomputed anywhere
downstream and the difference between "3 of 3 neighbours" and "30 of 30" stays
visible instead of collapsing into the same 0.9.

The INSERT at the end is what fires the changefeed, so this function is the
head of the whole pipeline.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import psycopg

from nexus_common import db, log, metrics, posterior

logger = log.get_logger("oracle")

K = 14                      # neighbours consulted per prediction
MIN_SIMILARITY = 0.72       # below this a neighbour is not evidence about us
MIN_MATCHES = 5             # too few neighbours is a coincidence, not a pattern
EMIT_THRESHOLD = 0.60       # posterior mean below this is not worth a prediction
TELEMETRY_MAX_AGE_MINUTES = 15

# The neighbourhood query. No category filter: the category is the *answer*, so
# filtering by it here would be assuming the conclusion.
NEIGHBOURS_SQL = """
    SELECT id::STRING, outcome_category, led_to_incident, metric_digest,
           1 - (trajectory_embedding <=> %s::VECTOR) AS similarity
    FROM precursor_snapshots
    ORDER BY trajectory_embedding <=> %s::VECTOR
    LIMIT %s
"""

# The freshness bound is interpolated because it is a module constant, not user
# input; the service name stays a bound parameter.
LATEST_TELEMETRY_SQL = f"""
    SELECT id::STRING, service_name, region, embedding::STRING, raw_metrics, captured_at
    FROM telemetry_embeddings
    WHERE service_name = %s
      AND captured_at > now() - INTERVAL '{TELEMETRY_MAX_AGE_MINUTES} minutes'
    ORDER BY captured_at DESC
    LIMIT 1
"""

SERVICES_SQL = f"""
    SELECT DISTINCT service_name
    FROM telemetry_embeddings
    WHERE captured_at > now() - INTERVAL '{TELEMETRY_MAX_AGE_MINUTES} minutes'
"""


def _progress_estimate(live_digest: dict, neighbours: list[dict]) -> float:
    """How far through its drift the live window appears to be, in [0, 1].

    Both the live window and every matched snapshot carry the same quantized
    end-level summary, so the ratio of the live end level to the neighbours' end
    levels says roughly how much of the journey has already happened. It is a
    coarse estimator — deciles, averaged over the metrics the two windows share —
    but it is computed from stored evidence rather than guessed, and it is what
    turns "these look like precursors" into "and you have about 40 minutes".
    """
    live = (live_digest or {}).get("summary", {})
    if not live:
        return 0.5  # no digest to reason from; assume mid-drift
    ratios: list[float] = []
    for n in neighbours:
        their = (n.get("digest") or {}).get("summary", {})
        for metric, live_summary in live.items():
            theirs = their.get(metric)
            if not theirs:
                continue
            live_end = _decile(live_summary.get("end"))
            their_end = _decile(theirs.get("end"))
            live_start = _decile(live_summary.get("start"))
            if live_end is None or their_end is None or live_start is None:
                continue
            travelled = live_end - live_start
            total = their_end - live_start
            if abs(total) < 1:  # the neighbour ends where we started: no journey
                continue
            ratios.append(max(0.0, min(1.0, travelled / total)))
    if not ratios:
        return 0.5
    return sum(ratios) / len(ratios)


def _decile(token: str | None) -> int | None:
    if isinstance(token, str) and token.startswith("q") and token[1:].isdigit():
        return int(token[1:])
    return None


def _eta(neighbours: list[dict], progress: float) -> tuple[datetime, int]:
    """Median remaining time to failure, from the neighbours' own lead times."""
    leads = sorted(
        float((n.get("digest") or {}).get("precursor_minutes", 90))
        for n in neighbours
        if n["led_to_incident"]
    )
    if not leads:
        median = 60.0
    else:
        mid = len(leads) // 2
        median = leads[mid] if len(leads) % 2 else (leads[mid - 1] + leads[mid]) / 2.0
    remaining = max(5.0, median * (1.0 - progress))
    return datetime.now(UTC) + timedelta(minutes=remaining), int(round(remaining))


def _severity(category: str, neighbours: list[dict]) -> int:
    """Severity of the majority category, taken from the archetype's own history."""
    severities = [
        int((n.get("digest") or {}).get("severity", 0))
        for n in neighbours
        if n["outcome_category"] == category and (n.get("digest") or {}).get("severity")
    ]
    if severities:
        return max(1, min(5, round(sum(severities) / len(severities))))
    # The seed's digests predate the severity field; fall back to the median
    # severity of the catalogue rather than inventing a number per category.
    return 4


def neighbours_for(conn, embedding_literal: str, k: int = K) -> list[dict]:
    rows = conn.execute(NEIGHBOURS_SQL, (embedding_literal, embedding_literal, k)).fetchall()
    return [
        {
            "id": r[0],
            "outcome_category": r[1],
            "led_to_incident": r[2],
            "digest": r[3] if isinstance(r[3], dict) else json.loads(r[3] or "{}"),
            "similarity": float(r[4]),
        }
        for r in rows
    ]


def assess(neighbours: list[dict], live_digest: dict) -> dict | None:
    """Turn a neighbourhood into a prediction, or decide there isn't one.

    Returns None when the evidence does not support emitting: too few close
    neighbours, or a posterior that a coin would beat.
    """
    close = [n for n in neighbours if n["similarity"] >= MIN_SIMILARITY]
    if len(close) < MIN_MATCHES:
        return None

    positives = [n for n in close if n["led_to_incident"]]
    negatives = [n for n in close if not n["led_to_incident"]]
    if not positives:
        return None

    # Majority category among the neighbours that actually failed. Negatives
    # inform the posterior but do not get a vote on what the failure would be.
    tally: dict[str, float] = {}
    for n in positives:
        tally[n["outcome_category"]] = tally.get(n["outcome_category"], 0.0) + n["similarity"]
    category = max(tally, key=tally.get)

    alpha = float(len(positives) + 1)
    beta = float(len(negatives) + 1)
    confidence = posterior.mean(len(positives), len(negatives))
    if confidence < EMIT_THRESHOLD:
        return None

    progress = _progress_estimate(live_digest, positives)
    eta, minutes = _eta(positives, progress)
    low, high = posterior.credible_interval(len(positives), len(negatives))
    return {
        "outcome_category": category,
        "alpha": alpha,
        "beta": beta,
        "confidence": confidence,
        "credible_interval": [round(low, 4), round(high, 4)],
        "matched": len(close),
        "positives": len(positives),
        "negatives": len(negatives),
        "severity": _severity(category, positives),
        "eta": eta,
        "eta_minutes": minutes,
        "progress": round(progress, 3),
        "top_similarity": round(close[0]["similarity"], 4),
        "evidence": [
            {"snapshot_id": n["id"], "category": n["outcome_category"],
             "led_to_incident": n["led_to_incident"],
             "similarity": round(n["similarity"], 4)}
            for n in close
        ],
    }


def _causal_pattern(assessment: dict, live_digest: dict) -> str:
    """A one-line human summary of what the trajectory is doing."""
    summary = (live_digest or {}).get("summary", {})
    rising = [
        f"{m} {s['trend']} to {s['end']}"
        for m, s in sorted(summary.items())
        if s.get("trend") in ("rising", "surging", "drifting_up")
    ][:3]
    if rising:
        return "; ".join(rising)
    return f"{assessment['matched']} precursors matched at {assessment['top_similarity']:.2f}"


def emit(conn, service: str, region: str, embedding_literal: str,
         assessment: dict, live_digest: dict) -> str | None:
    """Insert the prediction. Returns its id, or None if one is already open.

    Deduplication is enforced by the partial unique index on
    `(service_name, predicted_outcome) WHERE prevention_status IN
    ('pending','preventing')` rather than by a check-then-insert, which would
    race two concurrent Oracle invocations against each other.
    """
    try:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO predictions
                    (service_name, causal_pattern, predicted_outcome, predicted_severity,
                     alpha, beta, matching_precursor_count, current_embedding, predicted_eta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::VECTOR, %s)
                RETURNING id::STRING
                """,
                (service, _causal_pattern(assessment, live_digest),
                 assessment["outcome_category"], assessment["severity"],
                 assessment["alpha"], assessment["beta"], assessment["matched"],
                 embedding_literal, assessment["eta"]),
            ).fetchone()
            prediction_id = row[0]
            commit_ts = db.commit_timestamp(conn)
    except psycopg.errors.UniqueViolation:
        logger.info("prediction suppressed: one is already open",
                    service=service, category=assessment["outcome_category"])
        metrics.put("predictions_deduplicated", 1, service=service)
        return None

    # The provenance record. Storing the commit timestamp with the evidence is
    # what makes the replay button honest: the same query, at the same instant,
    # cannot be re-answered with anything the system learned afterwards.
    _record_evidence(conn, prediction_id, commit_ts, assessment, region)
    logger.info(
        "prediction emitted", prediction_id=prediction_id, service=service,
        category=assessment["outcome_category"],
        confidence=round(assessment["confidence"], 4),
        interval=assessment["credible_interval"],
        matched=assessment["matched"], eta_minutes=assessment["eta_minutes"],
        commit_ts=commit_ts,
    )
    return prediction_id


def _record_evidence(conn, prediction_id: str, commit_ts: str,
                     assessment: dict, region: str) -> None:
    """Attach the evidence bundle to the prediction via evolution_log.

    `predictions` has no free-form column, and inventing one would mean a
    migration for something that is really an audit record — which is what
    evolution_log is for. `event_type='competition'` is the closest existing
    kind; the details make clear this is the prediction's evidence, not a
    playbook competition.
    """
    conn.execute(
        """
        INSERT INTO evolution_log (event_type, details)
        VALUES ('competition', %s::JSONB)
        """,
        (json.dumps({
            "kind": "prediction_evidence",
            "prediction_id": prediction_id,
            "commit_timestamp": commit_ts,
            "region": region,
            "posterior": {"alpha": assessment["alpha"], "beta": assessment["beta"],
                          "mean": round(assessment["confidence"], 4),
                          "credible_interval": assessment["credible_interval"]},
            "matched": assessment["matched"],
            "positives": assessment["positives"],
            "negatives": assessment["negatives"],
            "progress_estimate": assessment["progress"],
            "eta_minutes": assessment["eta_minutes"],
            "evidence": assessment["evidence"],
        }),),
    )


def _predict_one(conn, service: str) -> dict:
    """Assess one service and emit its prediction. One transaction's worth of work."""
    row = conn.execute(LATEST_TELEMETRY_SQL, (service,)).fetchone()
    if not row:
        return {"service": service, "prediction_id": None,
                "skipped": "no fresh telemetry"}
    _, _, region, embedding, raw_metrics, captured_at = row
    live_digest = raw_metrics if isinstance(raw_metrics, dict) else {}

    neighbours = neighbours_for(conn, embedding)
    assessment = assess(neighbours, live_digest)
    if assessment is None:
        logger.info("no prediction", service=service,
                    top_similarity=round(neighbours[0]["similarity"], 4)
                    if neighbours else None)
        return {"service": service, "prediction_id": None,
                "reason": "evidence below threshold"}

    prediction_id = emit(conn, service, region, embedding, assessment, live_digest)
    return {
        "service": service,
        "prediction_id": prediction_id,
        "category": assessment["outcome_category"],
        "confidence": round(assessment["confidence"], 4),
        "credible_interval": assessment["credible_interval"],
        "matched": assessment["matched"],
        "eta_minutes": assessment["eta_minutes"],
        "captured_at": captured_at.isoformat(),
        "_assessment": assessment,
    }


def predict(services: list[str] | None = None) -> list[dict]:
    """One Oracle cycle. Returns a summary per service considered.

    Each service is its own serializable transaction rather than one transaction
    for the whole cycle. Two reasons: a serialization failure on a busy
    multi-region cluster then costs one service's retry instead of the entire
    cycle's, and a prediction that has been written is not rolled back because a
    later service contended with something else.
    """
    if services is None:
        services = db.tx_retry(
            lambda conn: [r[0] for r in conn.execute(SERVICES_SQL).fetchall()]
        )
    if not services:
        logger.info("no fresh telemetry in the sensory tier — nothing to predict from")
        return []

    results: list[dict] = []
    for service in services:
        result = db.tx_retry(lambda conn, svc=service: _predict_one(conn, svc))
        assessment = result.pop("_assessment", None)
        if assessment is not None:
            metrics.put("predictions_emitted", 1 if result["prediction_id"] else 0,
                        service=service)
            metrics.put("mean_confidence", assessment["confidence"], unit="None",
                        service=service)
        results.append(result)
    return results


def handler(event: dict | None = None, _context=None) -> dict:
    event = event or {}
    services = event.get("services")
    if isinstance(services, str):
        services = [services]
    logger.info("oracle invoked", source=event.get("source"), services=services)
    results = predict(services)
    emitted = sum(1 for r in results if r.get("prediction_id"))
    return {"agent": "oracle", "predictions_emitted": emitted, "results": results}


if __name__ == "__main__":  # local: python agents/oracle/app.py
    os.environ.setdefault("LOG_LEVEL", "INFO")
    print(json.dumps(handler({}), indent=2, default=str))
