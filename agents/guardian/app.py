"""Guardian: execute the fix, watch what happens, undo it if it made things worse.

Guardian is the only agent that changes the world, so it is the one built around
the assumption that it will sometimes be wrong.

    execute steps ──► verification window ──► better?  ──► success
                                              │
                                              worse ──► run the inverses in
                                                        reverse ──► trial failure
                                                                ──► evolution_log('rollback')

**Rollback is not an apology.** It is the mutation trigger: a failed trial is
what hands Chronicler the material to breed a better variant. The bad-fix lane
in the seeded world exists so this path runs on camera.

**Steps are idempotent by construction.** The action vocabulary is declarative —
`scale_connection_pool` to a size, not by an amount — so a retry that re-applies
a step already in its desired state changes nothing. The fleet enforces this
too, keying applied steps by action and parameters, which is also how a rollback
names precisely the step it is undoing.

Guardian never executes on the shadow or approval tiers. On shadow it records
what would have run; on approval it waits for a human. Only `auto` acts.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime

from nexus_common import config, db, fleet_client, log, metrics
from nexus_common.steps import parse_steps
from nexus_common.trajectory import METRIC_SCALES, outcome_target

logger = log.get_logger("guardian")

# Demo mode: real verification windows are minutes, but the whole beat has to
# fit in a demo. The fleet ticks every few seconds, so 45s is a dozen-odd
# samples — enough to see a trend, short enough to watch.
VERIFICATION_SECONDS = float(os.environ.get("VERIFICATION_SECONDS", "45"))
VERIFICATION_POLL_SECONDS = float(os.environ.get("VERIFICATION_POLL_SECONDS", "5"))
# How much the target metric must move, as a fraction of its nominal span,
# before the change counts as real rather than as noise.
IMPROVEMENT_EPSILON = 0.02


def _target_reading(window: dict[str, list[float]], category: str) -> float | None:
    """The current value of the metric that decides whether this worked."""
    metric, _ = outcome_target(category)
    series = window.get(metric)
    if not series:
        return None
    tail = series[-3:]  # smooth over jitter without lagging the trend
    return sum(tail) / len(tail)


def _verdict(before: float | None, after: float | None, category: str) -> tuple[str, float]:
    """Compare two readings of the target metric. Returns (verdict, delta)."""
    if before is None or after is None:
        return "unknown", 0.0
    metric, direction = outcome_target(category)
    _, lo, hi = METRIC_SCALES.get(metric, ("", 0.0, 1.0))
    span = (hi - lo) or 1.0
    # Normalize so "improvement" is positive regardless of which way the metric
    # is supposed to move: a rising cache hit ratio and a falling p99 are both
    # recovery.
    delta = (before - after) / span if direction == "down" else (after - before) / span
    if delta > IMPROVEMENT_EPSILON:
        return "improved", delta
    if delta < -IMPROVEMENT_EPSILON:
        return "degraded", delta
    return "flat", delta


def _artifact(key: str, body: dict) -> str | None:
    """Snapshot an execution record to S3. Never fails the execution."""
    bucket = config.ARTIFACTS_BUCKET
    if not bucket:
        logger.debug("no artifacts bucket configured; execution record not persisted")
        return None
    try:
        import boto3

        boto3.client("s3", region_name=config.AWS_REGION).put_object(
            Bucket=bucket, Key=key,
            Body=json.dumps(body, indent=2, default=str).encode(),
            ContentType="application/json",
        )
        return f"s3://{bucket}/{key}"
    except Exception as e:
        logger.warning("artifact not written", key=key, error=str(e))
        return None


def substrate_health() -> dict:
    """Read-only health check of the CockroachDB cluster via the ccloud CLI.

    Scoped to a read-only service account by RBAC, and read-only again here: the
    only subcommand invoked lists clusters. If ccloud is not installed or not
    authenticated the check reports that plainly rather than silently passing —
    a health check that cannot fail is not a health check.
    """
    started = time.perf_counter()
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
            ["ccloud", "cluster", "list", "--output", "json"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except FileNotFoundError:
        return {"available": False, "reason": "ccloud CLI is not installed on this host"}
    except subprocess.TimeoutExpired:
        return {"available": False, "reason": "ccloud timed out after 15s"}

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if proc.returncode != 0:
        return {"available": False, "reason": (proc.stderr or "").strip()[:200],
                "elapsed_ms": elapsed_ms}
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"available": False, "reason": "ccloud returned non-JSON output",
                "elapsed_ms": elapsed_ms}
    clusters = payload.get("clusters", payload if isinstance(payload, list) else [])
    return {
        "available": True,
        "elapsed_ms": elapsed_ms,
        "clusters": [
            {"name": c.get("name"), "state": c.get("state"), "regions":
             [r.get("name") for r in (c.get("regions") or [])]}
            for c in clusters
        ],
    }


def execute_steps(service: str, steps: list, *, already_applied: set[str] | None = None
                  ) -> list[dict]:
    """Run remediation steps in order, timing each one."""
    already_applied = already_applied or set()
    records = []
    for index, step in enumerate(parse_steps(steps, service=service)):
        key = fleet_client.step_key(step.action, step.params)
        if key in already_applied:
            records.append({"index": index, "action": step.action, "key": key,
                            "skipped": "already in desired state", "duration_ms": 0})
            continue
        started = time.perf_counter()
        try:
            result = fleet_client.apply_action(step.target or service, step.action,
                                               step.params)
            ok, detail = True, result
        except fleet_client.FleetUnavailable as e:
            ok, detail = False, {"error": str(e)}
        records.append({
            "index": index, "action": step.action, "target": step.target,
            "params": step.params, "key": key, "ok": ok, "result": detail,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        if not ok:
            break  # a step that could not run makes the rest meaningless
    return records


def rollback_steps(service: str, steps: list, executed: list[dict]) -> list[dict]:
    """Undo the executed steps, in reverse order, using their declared inverses.

    Each inverse reverts the exact step it corresponds to rather than being
    replayed as a fresh action, so the fleet ends where it started instead of
    carrying two remediations at once.
    """
    parsed = parse_steps(steps, service=service)
    applied = {r["key"]: r for r in executed if r.get("ok")}
    records = []
    for index, step in reversed(list(enumerate(parsed))):
        key = fleet_client.step_key(step.action, step.params)
        if key not in applied:
            continue
        if step.inverse is None:
            records.append({"index": index, "action": step.action, "key": key,
                            "skipped": "no inverse declared — this step is irreversible"})
            continue
        started = time.perf_counter()
        try:
            result = fleet_client.apply_action(
                step.target or service, step.inverse.action, step.inverse.params,
                revert_of=key,
            )
            ok, detail = True, result
        except fleet_client.FleetUnavailable as e:
            ok, detail = False, {"error": str(e)}
        records.append({
            "index": index, "inverse_of": step.action, "action": step.inverse.action,
            "params": step.inverse.params, "reverted_key": key, "ok": ok,
            "result": detail, "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        })
    return records


def verify(service: str, category: str, baseline: float | None,
           seconds: float = VERIFICATION_SECONDS) -> dict:
    """Watch the target metric for `seconds` and decide whether the fix worked."""
    metric, direction = outcome_target(category)
    readings: list[dict] = []
    deadline = time.time() + seconds
    latest = baseline
    while time.time() < deadline:
        time.sleep(min(VERIFICATION_POLL_SECONDS, max(0.0, deadline - time.time())))
        try:
            window = fleet_client.window(service)
        except fleet_client.FleetUnavailable as e:
            return {"verdict": "unknown", "reason": str(e), "metric": metric,
                    "direction": direction, "baseline": baseline, "readings": readings}
        latest = _target_reading(window, category)
        readings.append({"at": datetime.now(UTC).isoformat(), "value": latest})
    verdict, delta = _verdict(baseline, latest, category)
    return {
        "verdict": verdict, "metric": metric, "direction": direction,
        "baseline": baseline, "final": latest, "normalized_delta": round(delta, 4),
        "window_seconds": seconds, "readings": readings,
    }


def _close_out(conn, prediction_id: str, service: str, category: str, severity: int,
               playbook_id: str | None, outcome: str, health: dict,
               narrative: str) -> dict:
    """Record the result against the prediction and its incident."""
    prevented = outcome == "prevented"
    status = "prevented" if prevented else "missed"
    conn.execute(
        "UPDATE predictions SET prevention_status = %s, resolved_at = now() WHERE id = %s",
        (status, prediction_id),
    )
    row = conn.execute(
        """
        SELECT id::STRING, detected_at FROM incidents
        WHERE %s::STRING = ANY(affected_services)
          AND status NOT IN ('resolved','postmortem')
        ORDER BY detected_at DESC LIMIT 1
        """,
        (service,),
    ).fetchone()
    if row:
        incident_id, detected_at = row[0], row[1]
        mttr = max(1, int((datetime.now(UTC) - detected_at).total_seconds()))
        conn.execute(
            """
            UPDATE incidents
            SET status = 'resolved', was_prevented = %s, was_auto_resolved = %s,
                playbook_used = %s, resolved_at = now(), mttr_seconds = %s,
                root_cause = COALESCE(root_cause, %s)
            WHERE id = %s
            """,
            (prevented, prevented, playbook_id, mttr, narrative, incident_id),
        )
    else:
        incident_id, mttr = None, None
        conn.execute(
            """
            INSERT INTO incidents
                (title, severity, status, affected_services, was_predicted,
                 was_prevented, was_auto_resolved, playbook_used, root_cause,
                 resolved_at, mttr_seconds)
            VALUES (%s, %s, 'resolved', ARRAY[%s::STRING], true, %s, %s, %s, %s, now(), 0)
            RETURNING id::STRING
            """,
            (f"{category} on {service}", severity, service, prevented, prevented,
             playbook_id, narrative),
        )
    return {"incident_id": incident_id, "mttr_seconds": mttr,
            "prevention_status": status, "health": health}


def run(decision: dict) -> dict:
    """Guardian's whole job, driven by Sentinel's decision."""
    prediction_id = decision["prediction_id"]
    tier = decision.get("tier")
    service = decision.get("service")
    category = decision.get("outcome_category")
    playbook = decision.get("playbook") or {}
    playbook_id = playbook.get("id")

    if tier != "auto":
        logger.info("no execution on this tier", prediction_id=prediction_id, tier=tier,
                    reason=decision.get("reason"))
        return {"prediction_id": prediction_id, "tier": tier, "outcome": "not_executed",
                "reason": decision.get("reason", f"tier is {tier}")}

    if not fleet_client.configured():
        logger.error("GENERATOR_URL is not set — refusing to report a fix that never ran",
                     prediction_id=prediction_id)
        return {"prediction_id": prediction_id, "tier": tier, "outcome": "no_substrate",
                "reason": "GENERATOR_URL is not configured"}

    started_at = datetime.now(UTC)
    health = substrate_health()

    try:
        baseline = _target_reading(fleet_client.window(service), category)
    except fleet_client.FleetUnavailable as e:
        return {"prediction_id": prediction_id, "tier": tier, "outcome": "no_substrate",
                "reason": str(e)}

    # A retry must not re-apply what already landed; ask the fleet what it holds.
    try:
        snap = {s["service"]: s for s in fleet_client.snapshot()}
        already = set(snap.get(service, {}).get("applied_steps", []))
    except fleet_client.FleetUnavailable:
        already = set()

    executed = execute_steps(service, playbook["remediation_steps"], already_applied=already)
    failed_to_run = [r for r in executed if r.get("ok") is False]
    logger.info("remediation executed", prediction_id=prediction_id,
                playbook_id=playbook_id, steps=len(executed),
                failed=len(failed_to_run), baseline=baseline)

    verification = verify(service, category, baseline)
    rolled_back: list[dict] = []
    if verification["verdict"] == "degraded" or failed_to_run:
        rolled_back = rollback_steps(service, playbook["remediation_steps"], executed)
        outcome = "rolled_back"
    elif verification["verdict"] == "improved":
        outcome = "prevented"
    else:
        # Flat or unknown is not success. Claiming it as one is how a system
        # convinces itself a playbook works when it does nothing at all.
        outcome = "inconclusive"

    record = {
        "prediction_id": prediction_id, "playbook_id": playbook_id,
        "playbook_name": playbook.get("name"), "service": service,
        "outcome_category": category, "tier": tier, "outcome": outcome,
        "started_at": started_at.isoformat(), "finished_at": datetime.now(UTC).isoformat(),
        "steps": executed, "verification": verification, "rollback": rolled_back,
        "substrate_health": health,
    }
    artifact = _artifact(
        f"incidents/{prediction_id}/execution-{int(started_at.timestamp())}.json", record
    )
    record["artifact"] = artifact

    narrative = (
        f"{category} on {service}: '{playbook.get('name')}' executed "
        f"{len(executed)} step(s); verification {verification['verdict']} "
        f"({verification.get('metric')} {verification.get('direction')})."
    )

    def close(conn):
        return _close_out(conn, prediction_id, service, category,
                          decision.get("severity", 3), playbook_id,
                          outcome, health, narrative)

    closing = db.tx_retry(close)

    if outcome == "rolled_back":
        _log_rollback(prediction_id, playbook_id, verification, rolled_back,
                      closing.get("incident_id"))

    metrics.put("remediations", 1, outcome=outcome, service=service)
    metrics.put("rollbacks", 1 if outcome == "rolled_back" else 0, service=service)
    logger.info("guardian finished", prediction_id=prediction_id, outcome=outcome,
                playbook_id=playbook_id, verdict=verification["verdict"],
                rolled_back=len(rolled_back), artifact=artifact,
                ccloud_available=health.get("available"))
    return {**record, **closing, "trial_success": outcome == "prevented"}


def _log_rollback(prediction_id: str, playbook_id: str | None, verification: dict,
                  rolled_back: list[dict], incident_id: str | None) -> None:
    """The rollback row Chronicler reads to decide what to mutate."""
    if not playbook_id:
        return

    def write(conn):
        conn.execute(
            """
            INSERT INTO evolution_log
                (event_type, playbook_id, trigger_incident_id, details)
            VALUES ('rollback', %s, %s, %s::JSONB)
            """,
            (playbook_id, incident_id, json.dumps({
                "prediction_id": prediction_id,
                "reason": "verification window showed degradation",
                "verification": verification,
                "inverse_steps_executed": len(rolled_back),
                "rollback": rolled_back,
            })),
        )

    db.tx_retry(write)


def handler(event: dict, _context=None) -> dict:
    # Step Functions passes Sentinel's output straight through; the local runner
    # and the tests pass the same shape.
    decision = event.get("decision") or event
    prediction_id = decision.get("prediction_id") or event.get("prediction_id")
    logger.info("guardian invoked", prediction_id=prediction_id,
                tier=decision.get("tier"))
    if not prediction_id:
        raise ValueError("no prediction id in the event")
    return {"agent": "guardian", **run(decision)}


if __name__ == "__main__":  # local smoke: python agents/guardian/app.py '<sentinel json>'
    import sys

    os.environ.setdefault("LOG_LEVEL", "INFO")
    print(json.dumps(handler(json.loads(sys.argv[1])), indent=2, default=str))
