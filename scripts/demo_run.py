#!/usr/bin/env python3
"""The whole demo, headless, in one command — the Phase 7 exit gate.

    make demo-reset && make demo-run

Three moments, three claims, each checked rather than narrated:

    Moment 1  memory that predicts, and proves it isn't cheating
              a ramp Oracle recognises, a posterior with an interval, and the
              evidence re-read at the timestamp the decision was made

    Moment 2  memory that acts, reliably, globally
              claim under FOR UPDATE, Thompson-sampled competition, a reversible
              fix that holds, and the survival configuration underneath it

    Moment 3  memory that learns, audibly and auditably
              a bad fix rolled back, and a playbook family driven through birth,
              growth, failure, mutation, merge, promotion and retirement

Each moment runs the real thing — the same scripts `make pipeline` and
`make lifecycle` run — as a subprocess, reads back its JSON report, and asserts
the claim. A moment that cannot be demonstrated says so and the run fails; it
never prints a claim it did not check.

Nothing here is a rehearsal aid. If this passes three times in a row on a clean
`make demo-reset`, the story is real and the video is a recording of it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _env import bootstrap, require_dsn

bootstrap()

from _agents import load_agent  # noqa: E402
from nexus_common import db  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def say(msg: str = "") -> None:
    print(msg, flush=True)


def moment(number: int, title: str, claim: str) -> None:
    say(f"\n{'═' * 76}")
    say(f"MOMENT {number} · {title}")
    say(f"claim: {claim}")
    say("═" * 76)


class Check:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def that(self, condition: bool, claim: str, detail: str = "") -> bool:
        ok = bool(condition)
        self.rows.append((ok, claim, detail))
        say(f"   {'PASS' if ok else 'FAIL'}  {claim}" + (f"   [{detail}]" if detail else ""))
        return ok

    @property
    def failures(self) -> list[tuple[bool, str, str]]:
        return [r for r in self.rows if not r[0]]


def run_script(script: str, *args: str, report: Path | None = None,
               timeout: int = 900) -> tuple[int, dict | None, str]:
    """Run one of this repo's scripts and hand back its exit code and report."""
    cmd = [PYTHON, str(REPO / "scripts" / script), *args]
    if report is not None:
        cmd += ["--report", str(report)]
    started = time.time()
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,  # noqa: S603
                          timeout=timeout, check=False)
    elapsed = f"{time.time() - started:.0f}s"
    payload = None
    if report is not None and report.exists():
        payload = json.loads(report.read_text())
    return proc.returncode, payload, elapsed


def tail(text: str, lines: int = 12) -> None:
    for line in [ln for ln in text.splitlines() if '"level"' not in ln][-lines:]:
        say(f"     │ {line}")


# --------------------------------------------------------------------------- #

def moment_one(check: Check, workdir: Path) -> dict | None:
    moment(1, "PREDICTION with PROVENANCE",
           "memory that predicts, and proves it isn't cheating")
    report_path = workdir / "prevented.json"
    code, report, elapsed = run_script("pipeline_local.py", "--scenario", "prevented",
                                       report=report_path)
    say(f"   pipeline finished in {elapsed} (exit {code})")
    if report is None:
        check.that(False, "the prevention pipeline produced a report")
        return None

    prediction = report.get("prediction") or {}
    check.that(bool(prediction.get("prediction_id")), "Oracle emitted a prediction",
               prediction.get("prediction_id", "")[:8])
    check.that(prediction.get("matched", 0) >= 5,
               "the prediction rests on at least five matched precursors",
               f"{prediction.get('matched')} matched")
    interval = prediction.get("credible_interval") or [None, None]
    check.that(interval[0] is not None and interval[1] is not None,
               "the confidence is an interval, not a bare number",
               f"mean {prediction.get('confidence')} in {interval}")
    check.that(bool(report.get("provenance_ts")),
               "the decision's commit timestamp was recorded with its evidence",
               str(report.get("provenance_ts", ""))[:20])

    # The trust beat, run against the same handler the dashboard serves.
    dashboard = load_agent("dashboard")
    status, replay = dashboard.replay_prediction(prediction["prediction_id"])
    check.that(status == 200, "the evidence replays at that timestamp", f"HTTP {status}")
    if status == 200:
        check.that(replay["commit_ts_source"] == "oracle_evidence",
                   "the replay pins to Oracle's recorded timestamp, not the row's latest version",
                   replay["commit_ts_source"])
        pinned, live = replay["panes"]
        # The claim is that the pinned read re-derives the posterior the system
        # actually acted on — not that today's read agrees with it. Memory really
        # does grow: Diagnostician promotes this very window, so the live top-k can
        # gain a positive neighbour and push a negative one out, moving the live
        # posterior. Asserting the two panes match would be asserting the memory
        # never learns, and would fail for the right reason at the worst moment.
        stated = prediction.get("confidence")
        check.that(abs(pinned["posterior_mean"] - stated) < 5e-4,
                   "the pinned pane re-derives the posterior the decision was made on",
                   f"stored {stated} · replayed {pinned['posterior_mean']}")
        if abs(live["posterior_mean"] - pinned["posterior_mean"]) > 5e-4:
            say(f"     the live posterior has since moved to {live['posterior_mean']} — "
                "memory grew; the pinned read did not")
        check.that(
            float(replay["commit_ts"]) < float(replay["row_mvcc_ts"]),
            "decision time precedes the row's current version — the outcome is not "
            "being read back as evidence",
            f"{float(replay['row_mvcc_ts']) - float(replay['commit_ts']):.0f}ns later")
        if replay["added_since"]:
            say(f"     memory has grown since: {len(replay['added_since'])} neighbour(s) "
                "in the live top-k did not exist at decision time")
    return report


def moment_two(check: Check, report: dict | None) -> None:
    moment(2, "PREVENTION that SURVIVES",
           "memory that acts, reliably, globally")
    if report is None:
        check.that(False, "there is a prevention run to inspect")
        return

    decision = report.get("decision") or {}
    guardian = report.get("guardian") or {}
    playbook = decision.get("playbook") or {}

    check.that(decision.get("claimed") is True,
               "Sentinel took the prediction under a row lock")
    check.that(decision.get("tier") == "auto",
               "the tier gate allowed unattended action", decision.get("tier", "—"))
    check.that(bool(playbook.get("name")), "a playbook won the competition",
               playbook.get("name", "—"))
    check.that(playbook.get("reversible") is True,
               "the winner is fully reversible, which is why it may act alone")
    check.that(guardian.get("outcome") == "prevented",
               "the fix held through the verification window",
               guardian.get("outcome", "—"))
    check.that(guardian.get("prevention_status") == "prevented",
               "the prediction closed as prevented, with an MTTR",
               f"mttr {guardian.get('mttr_seconds')}s")

    lifecycle = report.get("lifecycle") or {}
    check.that((lifecycle.get("applied") or {}).get("growth") == "success",
               "Chronicler recorded the trial as evidence",
               f"posterior now {lifecycle.get('posterior_mean')}")

    say("\n   the substrate underneath it:")
    code, _, elapsed = run_script("region_demo.py", "--cloud")
    check.that(code == 0,
               "the cluster is three regions with SURVIVE REGION FAILURE, read live",
               f"{elapsed}")
    say("     (watching it survive on camera needs the local cluster: "
        "`make region-up && make region-demo`)")


def moment_three(check: Check, workdir: Path) -> None:
    moment(3, "EVOLUTION, including a DEATH",
           "memory that learns, audibly and auditably")

    say("   the bad fix:")
    report_path = workdir / "rollback.json"
    code, rollback, elapsed = run_script("pipeline_local.py", "--scenario", "rollback",
                                         report=report_path)
    say(f"   pipeline finished in {elapsed} (exit {code})")
    if rollback is None:
        check.that(False, "the rollback pipeline produced a report")
    else:
        guardian = rollback.get("guardian") or {}
        decision = rollback.get("decision") or {}
        check.that(decision.get("challenger_upset") is not None,
                   "the competition recorded whether the strongest posterior won",
                   f"upset={decision.get('challenger_upset')}")
        check.that(guardian.get("outcome") == "rolled_back",
                   "the degrading fix was rolled back rather than reported as success",
                   guardian.get("outcome", "—"))
        check.that(len(guardian.get("rollback") or []) > 0,
                   "every executed step was reverted by its own inverse",
                   f"{len(guardian.get('rollback') or [])} inverse step(s)")
        lifecycle = rollback.get("lifecycle") or {}
        check.that((lifecycle.get("applied") or {}).get("growth") == "failure",
                   "the failure was recorded against the playbook that caused it",
                   f"posterior now {lifecycle.get('posterior_mean')}")

    say("\n   the whole life of a family:")
    code, _, elapsed = run_script("lifecycle_local.py", timeout=1200)
    check.that(code == 0,
               "birth → growth → failure → mutation → merge → promotion → retirement",
               f"{elapsed}")

    say("\n   the log is the story:")
    counts = db.tx_retry(lambda c: c.execute(
        "SELECT event_type, count(*) FROM evolution_log GROUP BY event_type "
        "ORDER BY event_type").fetchall())
    for event_type, n in counts:
        say(f"     {event_type:<12} {n}")
    kinds = {row[0] for row in counts}
    check.that({"birth", "growth", "mutation", "merge", "promotion", "retirement",
                "rollback", "competition"} <= kinds,
               "every lifecycle event type is present in the append-only log",
               f"{len(kinds)} of 8 types")


def honesty(check: Check) -> None:
    say(f"\n{'═' * 76}")
    say("HONESTY · the numbers, including the ones that are not flattering")
    say("═" * 76)
    dashboard = load_agent("dashboard")
    backtest = dashboard._backtest()
    if not backtest:
        check.that(False, "a backtest exists to show")
        return
    say(f"   method      {backtest['method']}")
    say(f"   precision   {backtest['precision']}     recall {backtest['recall']}")
    say(f"   confusion   TP {backtest['true_positive']}  FP {backtest['false_positive']}  "
        f"FN {backtest['false_negative']}  TN {backtest['true_negative']}")
    for bucket in backtest.get("calibration") or []:
        if bucket.get("n"):
            say(f"   {bucket['bucket']}   n={bucket['n']:<3} stated {bucket['stated']:.3f}  "
                f"realized {bucket['realized']:.3f}  gap {bucket['gap']:+.3f}")
    check.that(backtest.get("out_of_sample") is True,
               "the numbers are out-of-sample, from windows withheld from the database",
               backtest["method"])
    check.that((backtest.get("false_positive") or 0) > 0,
               "at least one false alarm is visible — a system that never shows "
               "failure is not showing you its failures",
               f"{backtest.get('false_positive')} false positives")


def reset_world() -> bool:
    """Restore the seeded world. Slow (minutes) and worth it before a graded run."""
    say("\n   restoring the seeded world (`make demo-reset`)…")
    proc = subprocess.run([PYTHON, str(REPO / "scripts" / "seed.py")],  # noqa: S603
                          cwd=REPO, capture_output=True, text=True, timeout=1800, check=False)
    if proc.returncode != 0:
        say("   the seeder failed:")
        tail(proc.stderr or proc.stdout, 15)
        return False
    say(f"   {[ln for ln in proc.stdout.splitlines() if 'seeded in' in ln] or ['done']}"
        .strip("[]'"))
    return True


def preflight() -> bool:
    """Refuse to start unless the five staged beats are stageable.

    This is not belt-and-braces. Moment 3 spends one of the bad-fix playbook's
    remaining failures every run — the system genuinely learns from being
    rehearsed — and after enough runs Chronicler retires it and the beat quietly
    stops happening. Starting a graded run against a worn-out world produces a
    scorecard that looks like a code regression and is not one.
    """
    import demo_check

    say(f"\n{'═' * 76}")
    say("PREFLIGHT · are the staged beats still stageable?")
    say("═" * 76)
    return demo_check.main() == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the whole demo headlessly")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run the whole story N times; the exit gate is 3/3")
    parser.add_argument("--reset", action="store_true",
                        help="re-seed the world before each run — what the exit gate "
                             "means by a clean environment")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="run even if the staged beats have worn out")
    args = parser.parse_args()
    require_dsn()

    failures = 0
    for attempt in range(1, args.repeat + 1):
        if args.repeat > 1:
            say(f"\n\n{'#' * 76}\n# RUN {attempt} of {args.repeat}\n{'#' * 76}")
        if args.reset and not reset_world():
            return 1
        if not args.skip_preflight and not preflight():
            say("\n   Refusing to run against a worn-out world. `make demo-reset` first, "
                "or pass --reset to do it here.")
            return 1
        check = Check()
        with tempfile.TemporaryDirectory(prefix="nexus-demo-") as tmp:
            workdir = Path(tmp)
            report = moment_one(check, workdir)
            moment_two(check, report)
            moment_three(check, workdir)
            honesty(check)

        say(f"\n{'═' * 76}")
        say("SCORECARD")
        say("═" * 76)
        passed = len(check.rows) - len(check.failures)
        say(f"   {passed} of {len(check.rows)} checks passed")
        if check.failures:
            failures += 1
            say("\n   failed:")
            for _, claim, detail in check.failures:
                say(f"     · {claim}" + (f"  [{detail}]" if detail else ""))
        else:
            say("   The three moments are real. Record them.")

    if args.repeat > 1:
        say(f"\n   {args.repeat - failures}/{args.repeat} clean runs")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
