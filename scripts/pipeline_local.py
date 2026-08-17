#!/usr/bin/env python3
"""Drive the whole prevention pipeline locally — the Phase 3 and 4 exit gates.

    make pipeline                       # the prevented-incident run
    make pipeline-rollback              # the bad-fix → rollback run
    make pipeline-approval              # the irreversible fix a human authorizes
    python scripts/pipeline_local.py --scenario novel
    python scripts/pipeline_local.py --scenario concurrency

Step Functions is not deployed on a laptop, so this script plays its part: it
starts the synthetic fleet in-process, ramps a service, feeds the sensory tier,
and then calls Sentinel → Diagnostician → Guardian → Chronicler in the same order
the state machine does, passing each stage's output to the next. Everything else
is real — the same agent code, the same queries, against the same cluster.

Scenarios
---------
`prevented`    a ramp Oracle recognises, a reversible playbook, a fix that holds
`rollback`     the deliberately-bad playbook wins, degrades the fleet, is undone
`novel`        a pattern no playbook claims — the cold-start path
`concurrency`  the same prediction delivered five times, one execution
`approval`     an irreversible fix waits at the gate until a human answers
"""
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from _env import bootstrap, require_dsn

bootstrap()

import numpy as np  # noqa: E402
import psycopg  # noqa: E402

from _agents import load_agent  # noqa: E402
from generator.fleet import WINDOW_SAMPLES  # noqa: E402
from nexus_common import db, posterior  # noqa: E402

# A weak playbook winning is rare by design — that is selection working. The
# driver searches for the seed on which it happens rather than re-running the
# whole pipeline until it does; the draw itself is untouched.
SEED_SEARCH = 40000

SCENARIOS = {
    "prevented": {
        "service": "payments",
        "archetype": "connection_pool_exhaustion",
        "force_playbook": None,
    },
    "rollback": {
        "service": "inventory",
        "archetype": "bad_deploy_latency_regression",
        # The bad fix: scales out under a code regression. Thompson sampling
        # gives it a turn roughly one time in five; the driver finds a seed that
        # makes that turn happen now rather than re-running until it does. The
        # draw is still a real draw — only the moment is chosen.
        "force_playbook": "Scale out under regression",
    },
    "novel": {
        "service": "checkout",
        "archetype": "cache_stampede",
        "force_playbook": None,
        "override_category": "quota_exhaustion_cascade",
    },
    "concurrency": {
        "service": "auth",
        "archetype": "dns_timeout_cascade",
        "force_playbook": None,
        "deliveries": 5,
    },
    "approval": {
        # Deleting data has no inverse, so this playbook can never run unattended
        # however confident Oracle is. The gate holds, a human answers through the
        # dashboard's write endpoint, and Guardian runs on that authority rather
        # than on the tier.
        #
        # `disk_full` rather than `cert_expiry`, which is the other irreversible
        # family: a certificate failure is a cliff, so at prediction time its
        # metric has barely moved and Guardian's verdict can only be
        # `inconclusive` (see OUTCOME_TARGETS). Disk pressure is a creep, so the
        # beat ends in a verdict instead of a shrug.
        "service": "inventory",
        "archetype": "disk_full",
        "force_playbook": "Extend then prune",
        "decide": "approved",
    },
}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


class Stages:
    """Numbered section headings that stay correct when a scenario adds a stage."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self, title: str) -> None:
        self.n += 1
        rule(f"{self.n} · {title}")


# --------------------------------------------------------------------------- #
# The fleet, served in-process so fleet_client talks to it exactly as it would
# talk to a deployed generator.
# --------------------------------------------------------------------------- #

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalFleet:
    """Runs `generator.live`'s app on a background thread, ticker disabled.

    The ticker is off because the driver advances the simulation itself: a demo
    that has to wait out a wall clock is a demo nobody can debug.
    """

    def __init__(self) -> None:
        import os

        os.environ["GENERATOR_INGEST"] = "0"
        os.environ["GENERATOR_TICK_SECONDS"] = "3600"
        from generator import live

        self.port = free_port()
        self.app = live.build_app()
        self.sim = self._simulator()
        self.ingest = live.ingest_window
        self._server = None
        os.environ["GENERATOR_URL"] = f"http://127.0.0.1:{self.port}"

    def _simulator(self):
        # build_app closes over its simulator; reach it through the route that
        # already exposes it rather than duplicating construction.
        for route in self.app.routes:
            closure = getattr(route.endpoint, "__closure__", None) or ()
            for cell in closure:
                if type(cell.cell_contents).__name__ == "FleetSimulator":
                    return cell.cell_contents
        raise RuntimeError("could not reach the simulator behind the app")

    def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                                log_level="error")
        self._server = uvicorn.Server(config)
        threading.Thread(target=self._server.run, daemon=True).start()
        for _ in range(100):
            time.sleep(0.05)
            if getattr(self._server, "started", False):
                return
        raise RuntimeError("the local fleet did not start")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True

    def advance(self, ticks: int, *, service: str, ingest_every: int = 6) -> int:
        """Tick the simulation and write telemetry into the sensory tier."""
        written = 0
        for i in range(1, ticks + 1):
            self.sim.tick(service)
            if i % ingest_every == 0 or i == ticks:
                if self.ingest(self.sim, service):
                    written += 1
        return written


# --------------------------------------------------------------------------- #

def clear_open_predictions(service: str) -> None:
    """A previous run's open prediction would trip Oracle's dedup guard."""
    def run(conn):
        return conn.execute(
            "DELETE FROM predictions WHERE service_name = %s "
            "AND prevention_status IN ('pending','preventing')",
            (service,),
        ).rowcount

    removed = db.tx_retry(run)
    if removed:
        say(f"   cleared {removed} open prediction(s) left over from an earlier run")


def pick_seed_for(prediction_id: str, playbook_name: str, sentinel) -> int | None:
    """Find a Thompson seed under which `playbook_name` wins this competition.

    The candidates and their posteriors are read exactly as Sentinel would read
    them, and `compete` is the same function Sentinel calls. Only the seed is
    chosen — the draw itself is untouched.
    """
    def read(conn):
        row = conn.execute(
            "SELECT current_embedding::STRING, predicted_outcome FROM predictions "
            "WHERE id = %s", (prediction_id,),
        ).fetchone()
        if not row:
            return None
        return sentinel.candidates_for(conn, row[0], row[1])

    candidates = db.tx_retry(read)
    if not candidates:
        return None
    for seed in range(SEED_SEARCH):
        winner, _ = posterior.compete(candidates, np.random.default_rng(seed))
        if winner.candidate.name == playbook_name:
            return seed
    return None


def retarget_as_novel(prediction_id: str, category: str) -> None:
    """Turn a prediction into one about a pattern memory has genuinely never seen.

    Relabelling alone is not enough, and finding that out is the point:
    Diagnostician's novel gate requires *both* that no playbook claims the
    category and that no past incident resembles the symptom. A cache-stampede
    window wearing a new category name still matches every seeded cache stampede
    at ~1.0, and the gate correctly refuses to call it novel. So the embedding is
    replaced too, with a random unit vector — the vector-space equivalent of a
    failure mode nobody has ever logged.
    """
    # Entropy-seeded on purpose. A fixed seed would produce the same "unseen"
    # pattern every run, and the second run would find the first run's incident
    # sitting at similarity 1.0 — correctly concluding it is not novel at all.
    # Something genuinely unprecedented has to be new each time.
    rng = np.random.default_rng()
    vector = rng.normal(size=1024)
    vector /= np.linalg.norm(vector)
    literal = "[" + ",".join(f"{x:.6f}" for x in vector) + "]"

    def run(conn):
        conn.execute(
            "UPDATE predictions SET predicted_outcome = %s, current_embedding = %s::VECTOR "
            "WHERE id = %s",
            (category, literal, prediction_id),
        )

    db.tx_retry(run)


def evidence_for(prediction_id: str) -> dict | None:
    def run(conn):
        row = conn.execute(
            "SELECT details FROM evolution_log "
            "WHERE details->>'prediction_id' = %s AND details->>'kind' = "
            "'prediction_evidence' ORDER BY created_at DESC LIMIT 1",
            (prediction_id,),
        ).fetchone()
        return row[0] if row else None

    return db.tx_retry(run)


def competition_for(prediction_id: str) -> dict | None:
    def run(conn):
        row = conn.execute(
            "SELECT details FROM evolution_log "
            "WHERE details->>'prediction_id' = %s AND details->>'kind' = "
            "'playbook_competition' ORDER BY created_at DESC LIMIT 1",
            (prediction_id,),
        ).fetchone()
        return row[0] if row else None

    return db.tx_retry(run)


def rollback_rows(prediction_id: str) -> int:
    def run(conn):
        return conn.execute(
            "SELECT count(*) FROM evolution_log WHERE event_type = 'rollback' "
            "AND details->>'prediction_id' = %s", (prediction_id,),
        ).fetchone()[0]

    return db.tx_retry(run)


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Drive the NEXUS pipeline locally")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="prevented")
    parser.add_argument("--ticks", type=int, default=WINDOW_SAMPLES,
                        help="simulation ticks before Oracle looks (one full sweep)")
    parser.add_argument("--verification-seconds", type=float, default=6.0,
                        help="Guardian's verification window; short, because the "
                             "driver advances the fleet itself")
    args = parser.parse_args()
    scenario = SCENARIOS[args.scenario]
    service, archetype = scenario["service"], scenario["archetype"]

    require_dsn()
    import os

    os.environ["VERIFICATION_SECONDS"] = str(args.verification_seconds)
    os.environ["VERIFICATION_POLL_SECONDS"] = "1"

    stage = Stages()
    oracle = load_agent("oracle")
    sentinel = load_agent("sentinel")
    diagnostician = load_agent("diagnostician")
    guardian = load_agent("guardian")
    chronicler = load_agent("chronicler")
    dashboard = load_agent("dashboard")

    rule(f"scenario: {args.scenario}  ·  {archetype} on {service}")
    fleet = LocalFleet()
    fleet.start()
    say(f"   fleet serving at {os.environ['GENERATOR_URL']}")

    report: dict = {"scenario": args.scenario, "service": service, "archetype": archetype}
    try:
        clear_open_predictions(service)

        stage("load ramp")
        fleet.sim.start_ramp(service, archetype, speed=1.0)
        written = fleet.advance(args.ticks, service=service)
        state = fleet.sim.services[service]
        say(f"   {args.ticks} ticks · status={state.status} "
            f"progress={state.progress:.2f} · {written} telemetry windows embedded")

        stage("Oracle")
        results = oracle.predict([service])
        outcome = results[0] if results else {}
        prediction_id = outcome.get("prediction_id")
        if not prediction_id:
            say(f"   no prediction: {outcome.get('reason') or outcome.get('skipped')}")
            return 1
        say(f"   prediction {prediction_id}")
        say(f"   category   {outcome['category']}")
        say(f"   posterior  mean {outcome['confidence']} "
            f"90% CI {outcome['credible_interval']} over {outcome['matched']} precursors")
        say(f"   ETA        ~{outcome['eta_minutes']} minutes")
        report["prediction"] = outcome

        evidence = evidence_for(prediction_id)
        if evidence:
            say(f"   provenance commit timestamp {evidence['commit_timestamp']}")
            say(f"              {evidence['positives']} of {evidence['matched']} "
                f"matched precursors led to an incident")
            report["provenance_ts"] = evidence["commit_timestamp"]

        if scenario.get("override_category"):
            retarget_as_novel(prediction_id, scenario["override_category"])
            say(f"   retargeted at '{scenario['override_category']}' with an unseen "
                "embedding — a pattern memory has no precedent for")

        stage("Sentinel")
        seed = None
        if scenario.get("force_playbook"):
            seed = pick_seed_for(prediction_id, scenario["force_playbook"], sentinel)
            say(f"   seeking a draw won by '{scenario['force_playbook']}': "
                + (f"seed {seed}" if seed is not None
                   else f"not found in {SEED_SEARCH} draws — it is a weak playbook, "
                        "which is the point"))

        if args.scenario == "concurrency":
            deliveries = scenario["deliveries"]
            say(f"   delivering the same prediction {deliveries}× in parallel")
            with ThreadPoolExecutor(max_workers=deliveries) as pool:
                outcomes = list(pool.map(
                    lambda _: _safe_evaluate(sentinel, prediction_id), range(deliveries)))
            claimed = [o for o in outcomes if o.get("claimed")]
            say(f"   claimed by {len(claimed)} of {deliveries}; "
                f"{deliveries - len(claimed)} clean no-ops")
            report["concurrency"] = {"deliveries": deliveries, "claims": len(claimed)}
            ok = len(claimed) == 1
            say(f"   {'PASS' if ok else 'FAIL'}: exactly one execution proceeds")
            decision = claimed[0] if claimed else outcomes[0]
        else:
            decision = sentinel.evaluate(prediction_id, seed=seed)

        say(f"   tier       {decision['tier']}")
        say(f"   reason     {decision['reason']}")
        if decision.get("playbook"):
            pb = decision["playbook"]
            say(f"   winner     {pb['name']}  (gen {pb['generation']}, "
                f"posterior {pb['posterior_mean']} over {pb['trials']} trials, "
                f"similarity {pb['similarity']})")
            if decision.get("challenger_upset"):
                say("   the strongest posterior did NOT win — exploration in action")
        report["decision"] = {k: v for k, v in decision.items() if k != "candidates"}

        competition = competition_for(prediction_id)
        if competition:
            say(f"   competition logged with {len(competition['candidates'])} candidates:")
            for c in competition["candidates"]:
                mark = "→" if c["playbook_id"] == decision.get("playbook", {}).get("id") else " "
                say(f"     {mark} {c['name'][:38]:38s} "
                    f"posterior {c['posterior_mean']:.3f} "
                    f"sample {c['beta_sample']:.3f} × sim {c['similarity']:.3f} "
                    f"= {c['score']:.3f}")
        if decision.get("approval_id"):
            say(f"   approval request {decision['approval_id']} awaiting a human")

        if scenario.get("decide"):
            stage("the human answers")
            decision = human_decision(dashboard, decision, scenario["decide"], say)

        stage("Diagnostician")
        diagnosis = diagnostician.diagnose(decision)
        say(f"   incident   {diagnosis.get('incident_id')}")
        say(f"   similar    {len(diagnosis.get('similar_incidents', []))} "
            f"(closest {diagnosis.get('closest_similarity')})")
        say(f"   snapshot   {diagnosis.get('precursor_snapshot_id')} "
            "written to precursor_snapshots")
        say(f"   narrative  [{diagnosis.get('narrative_source')}] "
            f"{(diagnosis.get('root_cause') or '')[:150]}")
        if diagnosis.get("novel"):
            say("   NOVEL: nothing in memory matches this pattern")
            say(f"   birth      {diagnosis.get('playbook_born') or 'no proposal produced'}")
        report["diagnosis"] = {k: v for k, v in diagnosis.items()
                               if k != "similar_incidents"}

        stage("Guardian")
        # Guardian's verification window reads the live fleet, so keep the
        # simulation moving underneath it while it watches.
        stop = threading.Event()

        def keep_ticking():
            while not stop.is_set():
                fleet.sim.tick(service)
                time.sleep(0.4)

        ticker = threading.Thread(target=keep_ticking, daemon=True)
        ticker.start()
        try:
            result = guardian.run(decision)
        finally:
            stop.set()
            ticker.join(timeout=2)

        say(f"   outcome    {result['outcome']}")
        if result.get("verification"):
            v = result["verification"]
            say(f"   verified   {v['verdict']} on {v.get('metric')} "
                f"({v.get('direction')}): {v.get('baseline')} → {v.get('final')}")
        if result.get("steps"):
            for s in result["steps"]:
                label = s.get("skipped") or ("ok" if s.get("ok") else "FAILED")
                say(f"     step {s['index']} {s['action']:26s} {label} "
                    f"({s.get('duration_ms', 0)}ms)")
        if result.get("rollback"):
            say(f"   ROLLED BACK: {len(result['rollback'])} inverse step(s)")
            for r in result["rollback"]:
                say(f"     undo {r.get('action', '?'):26s} "
                    f"reverting {r.get('inverse_of', '?')}")
            say(f"   evolution_log rollback rows: {rollback_rows(prediction_id)}")
        health = result.get("substrate_health") or {}
        say(f"   ccloud     {'ok' if health.get('available') else health.get('reason')}")
        if result.get("prevention_status"):
            say(f"   prediction {result['prevention_status']} · "
                f"incident {result.get('incident_id')} · "
                f"mttr {result.get('mttr_seconds')}s")
        report["guardian"] = {k: v for k, v in result.items()
                              if k not in ("verification", "substrate_health")}

        stage("Chronicler")
        lifecycle = chronicler.chronicle(prediction_id, result)
        say(f"   verdict    {lifecycle['verdict'] or '—'}  ({lifecycle['reason']})")
        if lifecycle["playbook_id"]:
            say(f"   posterior  {lifecycle['posterior_mean']} over "
                f"{lifecycle['trials']} trials · tier {lifecycle['memory_tier']} · "
                f"{lifecycle['status']}")
        for kind, value in lifecycle["applied"].items():
            if value:
                say(f"   {kind:<10} {value}")
        if lifecycle["verdict"] == "failure" and not lifecycle["applied"]["mutation"]:
            say("   mutation   none — breeding a variant needs the reasoning model, "
                "and no proposal was produced")
        say(f"   evolution_log rows written: {lifecycle['lifecycle_events']}")
        report["lifecycle"] = lifecycle

        rule("summary")
        say(json.dumps({
            "scenario": args.scenario,
            "prediction": prediction_id,
            "confidence": outcome["confidence"],
            "tier": decision["tier"],
            "playbook": (decision.get("playbook") or {}).get("name"),
            "guardian_outcome": result["outcome"],
            "rolled_back": bool(result.get("rollback")),
            "incident": result.get("incident_id"),
            "lifecycle": {k: v for k, v in lifecycle["applied"].items() if v},
            "posterior_after": lifecycle["posterior_mean"],
        }, indent=2))
        return 0
    finally:
        fleet.stop()


def human_decision(dashboard, decision: dict, verdict: str, out) -> dict:
    """Answer the approval request through the dashboard's own write endpoint.

    Not a shortcut around the API — it is the API, called as a function instead of
    over HTTP, so the decision goes through the same `FOR UPDATE` claim, writes
    the same `evolution_log` row, and reports the same refusal on a second
    attempt. Approving normally puts an event on the bus for the post-approval
    state machine; with no bus configured locally the endpoint says so, and this
    driver plays that machine's part by handing Guardian the decision itself.
    """
    approvals = dashboard.list_approvals({"status": "pending", "limit": 5})["approvals"]
    mine = [a for a in approvals if a["prediction_id"] == decision["prediction_id"]]
    if not mine:
        out("   no approval request is open for this prediction")
        return decision
    approval = mine[0]
    out(f"   request    {approval['id']}")
    out(f"   reason     {approval['reason']}")
    out(f"   deadline   {approval['deadline']}")

    status, body = dashboard.decide_approval(approval["id"], {"decision": verdict,
                                                             "decided_by": "pipeline-driver"})
    out(f"   decision   {verdict} → HTTP {status}")
    out(f"   {body.get('note', body)}")

    # A second click must be refused, not applied twice.
    again, _ = dashboard.decide_approval(approval["id"], {"decision": verdict})
    out(f"   {'PASS' if again == 409 else 'FAIL'}: a second decision is refused "
        f"(HTTP {again})")

    if verdict != "approved":
        return {**decision, "tier": "rejected",
                "reason": "a human declined the remediation; it is now a shadow record"}
    # The authority the tier gate was withholding has been supplied.
    return {**decision, "tier": "auto",
            "reason": f"approved by {body.get('decided_by')} — {decision['reason']}"}


def _safe_evaluate(sentinel, prediction_id: str) -> dict:
    try:
        return sentinel.evaluate(prediction_id)
    except psycopg.Error as e:
        return {"claimed": False, "error": str(e)[:120]}


if __name__ == "__main__":
    raise SystemExit(main())
