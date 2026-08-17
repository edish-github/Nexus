"""Diagnostician: explain what is happening, and remember it for next time.

Three jobs, of which the second is the one that compounds.

**Root cause.** Retrieve similar past incidents by hybrid SQL + vector search
over `incidents.symptom_embedding`, then assemble a causal narrative from that
evidence. Bedrock writes the narrative when it is reachable; when it is not, a
template built from the same retrieved evidence does, because a demo must not
hang on an LLM call and a narrative grounded in retrieved rows is still true.

**The precursor snapshot writer.** This is what makes the system smarter about
the future rather than merely well-informed about the past. The trailing window
that preceded this event is promoted out of the 2-hour sensory tier into
`precursor_snapshots`, where it becomes evidence Oracle can match against
forever. The embedding is *reused*, not recomputed — the sensory row already
holds the vector for exactly this window, and re-embedding it would spend a
Titan call to produce the same numbers.

**Birth.** When nothing in memory matches — no similar incident, no candidate
playbook — the system is looking at something it has never seen. Bedrock
proposes a playbook from the raw evidence, the proposal is validated against the
step schema, and a validated one is inserted with a Beta(1,1) prior at
generation 1 with an `evolution_log('birth')` row. A malformed genome is
stillborn: logged, rejected, never inserted, never executed. This is the
cold-start answer.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from nexus_common import bedrock, db, log, metrics
from nexus_common.steps import ACTIONS, PlaybookDraft

logger = log.get_logger("diagnostician")

SIMILAR_LIMIT = 5
NOVEL_THRESHOLD = 0.80   # no incident this close means we have not seen this before
SNAPSHOT_MAX_AGE_MINUTES = 30

# `id <> %s` excludes the incident this diagnosis just created. Without it the
# search finds its own row at similarity 1.0, every incident looks like a perfect
# precedent, and the novel-incident path can never trigger — the cold-start
# answer would be silently unreachable.
SIMILAR_INCIDENTS_SQL = """
    SELECT id::STRING, title, root_cause, severity, mttr_seconds, was_prevented,
           detected_at, 1 - (symptom_embedding <=> %s::VECTOR) AS similarity
    FROM incidents
    WHERE symptom_embedding IS NOT NULL
      AND id <> %s
    ORDER BY symptom_embedding <=> %s::VECTOR
    LIMIT %s
"""

LATEST_TELEMETRY_SQL = f"""
    SELECT id::STRING, region, embedding::STRING, raw_metrics, captured_at
    FROM telemetry_embeddings
    WHERE service_name = %s
      AND captured_at > now() - INTERVAL '{SNAPSHOT_MAX_AGE_MINUTES} minutes'
    ORDER BY captured_at DESC
    LIMIT 1
"""

PROPOSAL_SYSTEM = """You are the diagnostic memory of an autonomous incident-response \
system. You are shown telemetry from a failure pattern with no precedent in memory, and \
you propose a remediation playbook.

Rules you must follow:
- Use only these actions: {actions}
- Every step needs an `inverse` unless undoing it is genuinely impossible.
- Prefer the smallest reversible intervention that addresses the mechanism the \
telemetry shows, not the symptom.
- Return a single JSON object and nothing else."""


def similar_incidents(conn, embedding: str, exclude_id: str,
                      limit: int = SIMILAR_LIMIT) -> list[dict]:
    rows = conn.execute(
        SIMILAR_INCIDENTS_SQL, (embedding, exclude_id, embedding, limit)
    ).fetchall()
    return [
        {"id": r[0], "title": r[1], "root_cause": r[2], "severity": r[3],
         "mttr_seconds": r[4], "was_prevented": r[5],
         "detected_at": r[6].isoformat() if r[6] else None,
         "similarity": round(float(r[7]), 4)}
        for r in rows
    ]


def _template_narrative(category: str, service: str, matches: list[dict],
                        digest: dict) -> str:
    """A narrative built from retrieved evidence, with no model in the loop."""
    summary = (digest or {}).get("summary", {})
    moving = [
        f"{m} {s['trend']} ({s['start']}→{s['end']})"
        for m, s in sorted(summary.items())
        if s.get("trend") not in ("flat", None)
    ][:4]
    lines = [
        f"{category.replace('_', ' ')} on {service}.",
        f"Trajectory: {'; '.join(moving)}." if moving else "",
    ]
    if matches:
        best = matches[0]
        prevented = sum(1 for m in matches if m["was_prevented"])
        lines.append(
            f"Closest precedent: '{best['title']}' at {best['similarity']:.2f} similarity"
            + (f" — {best['root_cause']}" if best.get("root_cause") else "")
            + f". {prevented} of {len(matches)} similar incidents were prevented."
        )
    else:
        lines.append("No similar incident in memory; this pattern has no precedent.")
    return " ".join(line for line in lines if line)


def narrate(category: str, service: str, matches: list[dict], digest: dict) -> tuple[str, str]:
    """Compose the root-cause narrative. Returns (narrative, source)."""
    template = _template_narrative(category, service, matches, digest)
    evidence = json.dumps({
        "service": service, "predicted_outcome": category,
        "trajectory_summary": (digest or {}).get("summary", {}),
        "similar_incidents": matches,
    }, default=str)[:6000]
    try:
        text = bedrock.claude(
            "Write a two-sentence root-cause narrative for this incident. Ground every "
            "claim in the evidence provided; do not speculate beyond it.\n\n" + evidence,
            system="You explain infrastructure incidents precisely and without filler.",
            max_tokens=300,
        ).strip()
        if text:
            return text, "bedrock"
    except Exception as e:
        # Degrade, never block: the pipeline must not stall on a model call.
        logger.warning("bedrock narrative unavailable, using retrieved-evidence template",
                       error=str(e))
    return template, "template"


def write_precursor_snapshot(conn, service: str, category: str,
                             incident_id: str | None) -> dict | None:
    """Promote the trailing sensory window into durable episodic memory.

    Returns None when there is no fresh telemetry to promote — which is a real
    outcome worth reporting, not something to paper over with a synthetic row.
    """
    row = conn.execute(LATEST_TELEMETRY_SQL, (service,)).fetchone()
    if not row:
        logger.warning("no fresh telemetry to promote into precursor_snapshots",
                       service=service)
        return None
    _, region, embedding, raw_metrics, captured_at = row
    digest = raw_metrics if isinstance(raw_metrics, dict) else {}
    minutes = float(digest.get("precursor_minutes") or digest.get("window_minutes") or 180)
    snapshot_id = conn.execute(
        """
        INSERT INTO precursor_snapshots
            (incident_id, service_name, region, trajectory_embedding, window_start,
             window_end, outcome_category, led_to_incident, metric_digest)
        VALUES (%s, %s, %s, %s::VECTOR, %s, %s, %s, true, %s::JSONB)
        RETURNING id::STRING
        """,
        (incident_id, service, region, embedding,
         captured_at - timedelta(minutes=minutes), captured_at,
         category, json.dumps({**digest, "promoted_from": "telemetry_embeddings",
                               "promoted_at": datetime.now(UTC).isoformat()})),
    ).fetchone()[0]
    logger.info("precursor snapshot written", incident_id=incident_id,
                service=service, snapshot_id=snapshot_id, window_minutes=minutes)
    metrics.put("precursor_snapshots_written", 1, service=service)
    return {"id": snapshot_id, "window_minutes": minutes, "region": region,
            "embedding": embedding, "digest": digest}


def propose_playbook(category: str, service: str, digest: dict,
                     matches: list[dict]) -> PlaybookDraft | None:
    """Ask Bedrock for a playbook, and refuse anything that does not validate."""
    prompt = json.dumps({
        "service": service,
        "observed_pattern": category,
        "trajectory_summary": (digest or {}).get("summary", {}),
        "nearest_incidents": matches,
        "schema": {
            "name": "str", "outcome_category": "str", "rationale": "str",
            "remediation_steps": [{
                "action": "one of the allowed actions", "target": service,
                "params": {}, "inverse": {"action": "...", "params": {}},
            }],
        },
    }, default=str)[:6000]
    try:
        raw = bedrock.claude(
            prompt,
            system=PROPOSAL_SYSTEM.format(actions=", ".join(ACTIONS)),
            max_tokens=1200, temperature=0.3,
        )
    except Exception as e:
        logger.warning("bedrock unavailable; no playbook proposed", error=str(e))
        return None

    text = raw.strip()
    if "```" in text:  # models like to fence their JSON
        text = text.split("```")[1].removeprefix("json").strip()
    try:
        draft = PlaybookDraft.model_validate_json(text)
    except (ValidationError, ValueError) as e:
        # A malformed genome is stillborn. Logged with the offending payload so
        # the prompt can be fixed, but never written and never executed.
        logger.error("proposed playbook rejected by schema validation",
                     error=str(e)[:400], payload=text[:400])
        metrics.put("playbook_proposals_rejected", 1, category=category)
        return None
    return draft


def birth(conn, draft: PlaybookDraft, category: str, region: str,
          embedding: str, incident_id: str | None) -> str:
    """Insert a newborn playbook with a flat prior, and record its birth."""
    steps = [s.model_dump(exclude_none=True) for s in draft.remediation_steps]
    playbook_id = conn.execute(
        """
        INSERT INTO playbooks
            (crdb_region, name, outcome_category, precursor_embedding, remediation_steps,
             inverse_steps, reversible, success_count, failure_count, generation,
             lineage, memory_tier, status)
        VALUES (%s, %s, %s, %s::VECTOR, %s::JSONB, %s::JSONB, %s, 0, 0, 1,
                ARRAY[]::UUID[], 'experimental', 'active')
        RETURNING id::STRING
        """,
        (region, draft.name, category or draft.outcome_category, embedding,
         json.dumps(steps), json.dumps(draft.inverse_steps()), draft.reversible),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO evolution_log
            (event_type, playbook_id, trigger_incident_id, fitness_after, details)
        VALUES ('birth', %s, %s, 0.5, %s::JSONB)
        """,
        (playbook_id, incident_id, json.dumps({
            "reason": "novel incident with no precedent above the similarity threshold",
            "generation": 1, "prior": "Beta(1,1)", "reversible": draft.reversible,
            "rationale": draft.rationale, "proposed_by": "bedrock",
        })),
    )
    logger.info("playbook born", playbook_id=playbook_id, incident_id=incident_id,
                name=draft.name, reversible=draft.reversible)
    metrics.put("playbook_births", 1, category=category)
    return playbook_id


def diagnose(decision: dict) -> dict:
    """Diagnostician's whole job, driven by Sentinel's decision."""
    prediction_id = decision["prediction_id"]
    service = decision.get("service")
    category = decision.get("outcome_category")
    had_candidates = bool(decision.get("candidates"))

    def run(conn) -> dict:
        row = conn.execute(
            """
            SELECT current_embedding::STRING, predicted_severity, service_name,
                   predicted_outcome
            FROM predictions WHERE id = %s
            """,
            (prediction_id,),
        ).fetchone()
        if row is None:
            return {"prediction_id": prediction_id, "skipped": "prediction no longer exists"}
        embedding, severity, pred_service, pred_category = row
        svc = service or pred_service
        cat = category or pred_category

        incident_id = conn.execute(
            """
            INSERT INTO incidents
                (title, severity, status, affected_services, was_predicted,
                 symptom_embedding, detected_at)
            VALUES (%s, %s, 'diagnosing', ARRAY[%s], true, %s::VECTOR, now())
            RETURNING id::STRING
            """,
            (f"{cat.replace('_', ' ').title()} on {svc}", severity, svc, embedding),
        ).fetchone()[0]

        matches = similar_incidents(conn, embedding, incident_id)
        snapshot = write_precursor_snapshot(conn, svc, cat, incident_id)
        digest = (snapshot or {}).get("digest", {})

        narrative, source = narrate(cat, svc, matches, digest)
        conn.execute("UPDATE incidents SET root_cause = %s WHERE id = %s",
                     (narrative, incident_id))

        # Novel means twice over unseen: nothing in memory looks like this
        # incident, and nothing in memory claims to fix it.
        closest = matches[0]["similarity"] if matches else 0.0
        novel = closest < NOVEL_THRESHOLD and not had_candidates
        born = None
        if novel:
            draft = propose_playbook(cat, svc, digest, matches)
            if draft is not None:
                born = birth(conn, draft, cat, (snapshot or {}).get("region",
                             "aws-us-east-1"), embedding, incident_id)
            else:
                logger.info("novel incident but no usable proposal; no playbook born",
                            incident_id=incident_id)

        logger.info("diagnosis complete", prediction_id=prediction_id,
                    incident_id=incident_id, similar=len(matches),
                    closest=round(closest, 4), novel=novel,
                    narrative_source=source, playbook_born=born)
        return {
            "prediction_id": prediction_id,
            "incident_id": incident_id,
            "service": svc,
            "outcome_category": cat,
            "root_cause": narrative,
            "narrative_source": source,
            "similar_incidents": matches,
            "closest_similarity": round(closest, 4),
            "precursor_snapshot_id": (snapshot or {}).get("id"),
            "novel": novel,
            "playbook_born": born,
        }

    return db.tx_retry(run)


def handler(event: dict, _context=None) -> dict:
    decision = event.get("decision") or event
    prediction_id = decision.get("prediction_id") or event.get("prediction_id")
    logger.info("diagnostician invoked", prediction_id=prediction_id,
                tier=decision.get("tier"))
    if not prediction_id:
        raise ValueError("no prediction id in the event")
    result = diagnose(decision)
    # Pass Sentinel's decision through untouched so Guardian, the next state,
    # still receives the playbook it is supposed to execute.
    return {"agent": "diagnostician", "decision": decision, "diagnosis": result}


if __name__ == "__main__":  # local: python agents/diagnostician/app.py '<sentinel json>'
    import sys

    os.environ.setdefault("LOG_LEVEL", "INFO")
    print(json.dumps(handler(json.loads(sys.argv[1])), indent=2, default=str))
