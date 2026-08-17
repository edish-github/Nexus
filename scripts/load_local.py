#!/usr/bin/env python3
"""Three failures at once — does the pipeline hold? (Phase 7 hardening.)

    make load

Every other harness in this repo drives one incident at a time, which is the
demo's shape and not production's. This one ramps three different services with
three different archetypes simultaneously, lets Oracle look at all of them in one
pass, and then runs the whole pipeline for each concurrently.

What is actually being tested is contention, and there are four places it can go
wrong. Oracle writes three predictions in overlapping transactions against the
same dedup index. Three Sentinels claim three different rows while competing over
overlapping candidate playbooks — the pool family answers more than one service.
Three Chroniclers update playbook counters that may be the *same* counters, which
is the one place a serializable retry is genuinely expected. And all of it runs
against a three-region cluster where every write is a consensus round.

The assertion is not "it was fast". It is that nothing was lost: three
predictions, three claims, three trials recorded, and every serialization failure
retried rather than surfaced.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from _env import bootstrap, require_dsn

bootstrap()

import psycopg  # noqa: E402

from _agents import load_agent  # noqa: E402
from generator.fleet import WINDOW_SAMPLES  # noqa: E402
from nexus_common import db  # noqa: E402
from pipeline_local import LocalFleet, clear_open_predictions  # noqa: E402

# Three services, three archetypes, and deliberately two that share a playbook
# family: `connection_pool_exhaustion` and `thread_pool_starvation` both draw on
# `shed_load` and `set_retry_budget`, so the competitions overlap rather than
# running in separate corners of the memory.
LOAD = (
    ("payments", "connection_pool_exhaustion"),
    ("checkout", "thread_pool_starvation"),
    ("inventory", "memory_leak_oom"),
)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say(f"\n{'─' * 74}\n{title}\n{'─' * 74}")


class Check:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def that(self, condition: bool, claim: str, detail: str = "") -> bool:
        ok = bool(condition)
        self.rows.append((ok, claim, detail))
        say(f"   {'PASS' if ok else 'FAIL'}  {claim}" + (f"   [{detail}]" if detail else ""))
        return ok

    @property
    def failures(self):
        return [r for r in self.rows if not r[0]]


def counters(names: list[str]) -> dict[str, tuple[int, int]]:
    if not names:
        return {}

    def run(conn):
        return conn.execute(
            "SELECT name, success_count, failure_count FROM playbooks "
            "WHERE name = ANY(%s::STRING[])", (names,)
        ).fetchall()

    return {r[0]: (r[1], r[2]) for r in db.tx_retry(run)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Three concurrent incident ramps")
    parser.add_argument("--verification-seconds", type=float, default=6.0)
    args = parser.parse_args()
    require_dsn()

    import os

    os.environ["VERIFICATION_SECONDS"] = str(args.verification_seconds)
    os.environ["VERIFICATION_POLL_SECONDS"] = "1"

    oracle = load_agent("oracle")
    sentinel = load_agent("sentinel")
    diagnostician = load_agent("diagnostician")
    guardian = load_agent("guardian")
    chronicler = load_agent("chronicler")
    check = Check()

    rule("load · three concurrent incident ramps")
    for service, archetype in LOAD:
        say(f"   {service:10s} {archetype}")

    fleet = LocalFleet()
    fleet.start()
    try:
        for service, _ in LOAD:
            clear_open_predictions(service)

        rule("1 · three ramps, advanced together")
        for service, archetype in LOAD:
            fleet.sim.start_ramp(service, archetype, speed=1.0)
        written = 0
        for i in range(1, WINDOW_SAMPLES + 1):
            for service, _ in LOAD:
                fleet.sim.tick(service)
                if i % 6 == 0 or i == WINDOW_SAMPLES:
                    written += 1 if fleet.ingest(fleet.sim, service) else 0
        say(f"   {WINDOW_SAMPLES} ticks each · {written} telemetry windows embedded")
        for service, _ in LOAD:
            state = fleet.sim.services[service]
            say(f"   {service:10s} status={state.status} progress={state.progress:.2f}")

        rule("2 · Oracle over all three in one pass")
        started = time.time()
        results = oracle.predict([s for s, _ in LOAD])
        emitted = [r for r in results if r.get("prediction_id")]
        say(f"   {len(emitted)} of {len(LOAD)} predicted in {time.time() - started:.1f}s")
        for r in results:
            if r.get("prediction_id"):
                say(f"   {r['service']:10s} {r['category']:30s} "
                    f"posterior {r['confidence']} · eta ~{r['eta_minutes']}m")
            else:
                say(f"   {r.get('service', '?'):10s} silent: "
                    f"{r.get('reason') or r.get('skipped')}")
        check.that(len(emitted) == len(LOAD),
                   "every ramp produced a prediction, none lost to the dedup index",
                   f"{len(emitted)}/{len(LOAD)}")
        if not emitted:
            return 1

        rule("3 · three pipelines, concurrently")
        # The fleet has to keep moving under all three verification windows at
        # once, or Guardian reads a frozen world and calls everything flat.
        stop = threading.Event()

        def keep_ticking():
            while not stop.is_set():
                for service, _ in LOAD:
                    fleet.sim.tick(service)
                time.sleep(0.4)

        ticker = threading.Thread(target=keep_ticking, daemon=True)
        ticker.start()

        def one(prediction: dict) -> dict:
            pid = prediction["prediction_id"]
            stage = "sentinel"
            try:
                decision = sentinel.evaluate(pid)
                if not decision.get("claimed"):
                    return {"prediction_id": pid, "error": "not claimed",
                            "stage": stage, "reason": decision.get("reason")}
                stage = "diagnostician"
                diagnosis = diagnostician.diagnose(decision)
                stage = "guardian"
                result = guardian.run(decision)
                stage = "chronicler"
                lifecycle = chronicler.chronicle(pid, result)
                return {
                    "prediction_id": pid, "service": decision.get("service"),
                    "tier": decision.get("tier"),
                    "playbook": (decision.get("playbook") or {}).get("name"),
                    "outcome": result.get("outcome"),
                    "incident": diagnosis.get("incident_id"),
                    "verdict": lifecycle.get("verdict"),
                    "posterior_after": lifecycle.get("posterior_mean"),
                }
            except psycopg.Error as e:
                # The stage matters more than the message: a 40001 that escapes
                # `db.tx_retry` means that stage's transaction is too broad or too
                # long, and knowing which one is the whole diagnosis.
                return {"prediction_id": pid, "stage": stage,
                        "error": f"{e.sqlstate} in {stage}: {str(e).splitlines()[0][:120]}"}

        began = time.time()
        try:
            with ThreadPoolExecutor(max_workers=len(emitted)) as pool:
                outcomes = list(pool.map(one, emitted))
        finally:
            stop.set()
            ticker.join(timeout=2)
        elapsed = time.time() - began

        say(f"   all three finished in {elapsed:.1f}s")
        for o in outcomes:
            if o.get("error"):
                say(f"   {o['prediction_id'][:8]}  ERROR {o['error']}")
            else:
                say(f"   {o['service']:10s} tier {o['tier']:<8s} "
                    f"{(o['playbook'] or '—')[:34]:34s} → {o['outcome']} "
                    f"({o['verdict'] or 'no trial'})")

        rule("4 · nothing lost")
        errors = [o for o in outcomes if o.get("error")]
        check.that(not errors, "no pipeline surfaced a database error",
                   "; ".join(o["error"] for o in errors)[:200] if errors else "")
        check.that(len(outcomes) == len(emitted),
                   "every prediction was carried through", f"{len(outcomes)}/{len(emitted)}")
        acted = [o for o in outcomes if o.get("tier") == "auto"]
        check.that(all(o.get("outcome") in ("prevented", "rolled_back", "inconclusive")
                       for o in acted),
                   "every acting pipeline reached a verdict rather than hanging",
                   ", ".join(sorted({o["outcome"] for o in acted})))

        # The point of the whole exercise: concurrent counter updates on
        # overlapping playbooks must all land.
        trials = [o for o in outcomes if o.get("verdict")]
        names = sorted({o["playbook"] for o in trials if o.get("playbook")})
        after = counters(names)
        for name in names:
            s, f = after.get(name, (0, 0))
            say(f"   {name[:44]:44s} now {s}/{f}")
        # Not "every execution produced a trial": an `inconclusive` verification
        # deliberately produces none, and counting it as a lost update would be
        # asserting the opposite of the design. What must hold is that every
        # execution that reached a verdict was recorded, and that the ones that
        # did not moved nothing.
        decided = [o for o in acted if o.get("outcome") in ("prevented", "rolled_back")]
        undecided = [o for o in acted if o.get("outcome") == "inconclusive"]
        check.that(len(trials) == len(decided),
                   "every execution that reached a verdict was recorded as a trial",
                   f"{len(trials)} trial(s) from {len(decided)} verdict(s)")
        check.that(all(not o.get("verdict") for o in undecided),
                   "an inconclusive verification moved no counter",
                   f"{len(undecided)} inconclusive")
        check.that(len(after) == len(names),
                   "every touched playbook's counters read back",
                   f"{len(after)}/{len(names)}")

        rule("summary")
        say(json.dumps({"ramps": len(LOAD), "predicted": len(emitted),
                        "elapsed_seconds": round(elapsed, 1),
                        "outcomes": [o.get("outcome") or o.get("error") for o in outcomes],
                        "checks_passed": len(check.rows) - len(check.failures),
                        "checks_failed": len(check.failures)}, indent=2))
        return 1 if check.failures else 0
    finally:
        fleet.stop()


if __name__ == "__main__":
    raise SystemExit(main())
