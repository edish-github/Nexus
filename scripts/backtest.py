#!/usr/bin/env python3
"""Score Oracle against windows it has never seen — the Phase 7 honesty layer.

    make backtest              # score the held-out set, store the run
    make backtest-dry          # score it and print the table, write nothing

The seeder withholds part of the generated world from the database and writes it
to `demo/backtest_set.jsonl` instead. Those windows are the only honest test
material available: every snapshot in `precursor_snapshots` helped build the
memory being tested, so scoring against them measures how well the memory
remembers itself.

Each held-out window is embedded and run through Oracle's own `neighbours_for`
and `assess` — the same k, the same similarity floor, the same minimum match
count, the same emit threshold. A window Oracle declines to predict on counts as
a negative prediction, because silence is what declining means in production.
That makes this a measurement of the whole decision rather than of the posterior
in isolation, and it is why the numbers are lower than a leave-one-out sweep's.

Three things come out:

* **Precision and recall** against the labels the windows carry.
* **Warning and imminence** — how far ahead of the failure the recognized pattern
  begins, and whether Oracle's stated ETA agrees that a complete precursor window
  means "any minute now". See `lead_times` for why those are two questions.
* **Calibration** — predictions bucketed by stated confidence against the rate
  that actually materialized. A model claiming 0.9 should be right about nine
  times in ten; this is where that claim gets checked rather than asserted.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from _env import bootstrap, require_dsn

bootstrap()

from _agents import load_agent  # noqa: E402
from nexus_common import db, embeddings  # noqa: E402

BACKTEST_PATH = Path(__file__).resolve().parents[1] / "demo" / "backtest_set.jsonl"

# Stated-confidence buckets for the calibration check. Oracle never emits below
# 0.60, so the buckets start there; a wider first bucket would be mostly empty.
BUCKETS = ((0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01))


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say(f"\n{'─' * 74}\n{title}\n{'─' * 74}")


def load_holdout() -> list[dict]:
    if not BACKTEST_PATH.exists():
        raise SystemExit(
            f"{BACKTEST_PATH} is missing. Run `make seed` — the held-out set is "
            "written by the seeder, from the same deterministic world."
        )
    rows = [json.loads(line) for line in BACKTEST_PATH.read_text().splitlines() if line.strip()]
    if not rows:
        raise SystemExit(f"{BACKTEST_PATH} is empty")
    return rows


def score(oracle, rows: list[dict]) -> list[dict]:
    """Run Oracle's retrieval and emit gate over every held-out window."""
    results: list[dict] = []

    def run(conn):
        memory = conn.execute("SELECT count(*) FROM precursor_snapshots").fetchone()[0]
        for index, row in enumerate(rows, 1):
            vector = embeddings.embed(row["precursor_text"])
            literal = embeddings.to_vector_literal(vector)
            neighbours = oracle.neighbours_for(conn, literal)
            verdict = oracle.assess(neighbours, row.get("digest") or {})
            actual = bool(row["led_to_incident"])
            results.append({
                "key": row.get("key"),
                "service": row["service"],
                "archetype": row["archetype"],
                "led_to_incident": actual,
                "predicted": verdict is not None,
                "confidence": round(verdict["confidence"], 4) if verdict else None,
                "category": verdict["outcome_category"] if verdict else None,
                "category_correct": (
                    verdict["outcome_category"] == row["archetype"] if verdict else None),
                "matched": verdict["matched"] if verdict else 0,
                "top_similarity": (
                    round(neighbours[0]["similarity"], 4) if neighbours else None),
                "eta_minutes": verdict["eta_minutes"] if verdict else None,
                "actual_lead_minutes": row.get("lead_time_minutes"),
                # Why it stayed silent, when it did. Distinguishing "nothing looked
                # like this" from "it looked like something that recovers" is the
                # difference between a blind spot and a judgement.
                "silent_because": None if verdict else _why_silent(oracle, neighbours),
            })
            if index % 10 == 0:
                say(f"   scored {index}/{len(rows)}")
        return int(memory)

    memory_size = db.tx_retry(run)
    for r in results:
        r["memory_size"] = memory_size
    return results


def _why_silent(oracle, neighbours: list[dict]) -> str:
    close = [n for n in neighbours if n["similarity"] >= oracle.MIN_SIMILARITY]
    if len(close) < oracle.MIN_MATCHES:
        return (f"only {len(close)} neighbour(s) above {oracle.MIN_SIMILARITY} similarity "
                f"(needs {oracle.MIN_MATCHES})")
    positives = [n for n in close if n["led_to_incident"]]
    if not positives:
        return f"all {len(close)} close neighbours recovered on their own"
    return (f"posterior below the {oracle.EMIT_THRESHOLD} emit threshold "
            f"({len(positives)} of {len(close)} neighbours failed)")


def confusion(results: list[dict]) -> dict:
    tp = sum(1 for r in results if r["led_to_incident"] and r["predicted"])
    fn = sum(1 for r in results if r["led_to_incident"] and not r["predicted"])
    fp = sum(1 for r in results if not r["led_to_incident"] and r["predicted"])
    tn = sum(1 for r in results if not r["led_to_incident"] and not r["predicted"])
    return {"true_positive": tp, "false_positive": fp, "false_negative": fn,
            "true_negative": tn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None}


def calibration(results: list[dict]) -> list[dict]:
    """Stated confidence against the rate that actually materialized."""
    out = []
    for low, high in BUCKETS:
        bucket = [r for r in results
                  if r["confidence"] is not None and low <= r["confidence"] < high]
        if not bucket:
            out.append({"bucket": f"{low:.2f}–{min(high, 1.0):.2f}", "n": 0,
                        "stated": None, "realized": None, "gap": None})
            continue
        stated = statistics.fmean(r["confidence"] for r in bucket)
        realized = statistics.fmean(1.0 if r["led_to_incident"] else 0.0 for r in bucket)
        out.append({
            "bucket": f"{low:.2f}–{min(high, 1.0):.2f}", "n": len(bucket),
            "stated": round(stated, 4), "realized": round(realized, 4),
            "gap": round(realized - stated, 4),
        })
    return out


def lead_times(results: list[dict]) -> tuple[float | None, float | None]:
    """How much warning the recognized patterns afford, and whether Oracle knows
    where in one a window sits.

    These are two different questions and it is easy to conflate them. A held-out
    window covers its incident's *entire* precursor period, so the time remaining
    at its end is zero by construction — the failure is imminent. So:

    * `median_lead_minutes` is a property of the patterns Oracle recognized: how
      far ahead of the failure the precursor pattern begins, and therefore how
      much warning the system can give when it catches one early.
    * the error term is Oracle's stated ETA measured against that zero. On a
      complete window it should say "any minute now", and this is where that gets
      checked. Comparing the ETA to the window's *length* instead would score a
      correct "imminent" as an hour of error, which is a bug in the ruler.
    """
    hits = [r for r in results if r["led_to_incident"] and r["predicted"]]
    leads = [float(r["actual_lead_minutes"]) for r in hits if r["actual_lead_minutes"]]
    etas = [float(r["eta_minutes"]) for r in hits if r["eta_minutes"] is not None]
    return (round(statistics.median(leads), 1) if leads else None,
            round(statistics.median(etas), 1) if etas else None)


def store(oracle, results: list[dict], matrix: dict, buckets: list[dict],
          lead: float | None, lead_error: float | None) -> str:
    def run(conn):
        return conn.execute(
            """
            INSERT INTO backtest_runs
                (method, k, min_similarity, min_matches, emit_threshold,
                 embedding_provider, sample_size, memory_size, true_positive,
                 false_positive, false_negative, true_negative, precision, recall,
                 median_lead_minutes, median_eta_minutes, calibration, detail)
            VALUES ('holdout_oracle_replay', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s::JSONB, %s::JSONB)
            RETURNING id::STRING
            """,
            (oracle.K, oracle.MIN_SIMILARITY, oracle.MIN_MATCHES, oracle.EMIT_THRESHOLD,
             embeddings.provider_name(), len(results),
             results[0]["memory_size"] if results else 0,
             matrix["true_positive"], matrix["false_positive"], matrix["false_negative"],
             matrix["true_negative"], matrix["precision"], matrix["recall"],
             lead, lead_error, json.dumps(buckets), json.dumps(results)),
        ).fetchone()[0]

    return db.tx_retry(run)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest Oracle on held-out windows")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the report without storing the run")
    args = parser.parse_args()
    require_dsn()

    oracle = load_agent("oracle")
    rows = load_holdout()

    rule("backtest · Oracle replayed over held-out windows")
    say(f"   held out    {len(rows)} windows "
        f"({sum(1 for r in rows if r['led_to_incident'])} incidents, "
        f"{sum(1 for r in rows if not r['led_to_incident'])} negatives)")
    say(f"   embedder    {embeddings.provider_name()}")
    say(f"   gate        k={oracle.K} · similarity ≥ {oracle.MIN_SIMILARITY} · "
        f"≥ {oracle.MIN_MATCHES} matches · posterior ≥ {oracle.EMIT_THRESHOLD}")
    say("")

    results = score(oracle, rows)
    matrix = confusion(results)
    buckets = calibration(results)
    lead, lead_error = lead_times(results)

    rule("confusion matrix")
    say(f"   memory      {results[0]['memory_size']} snapshots")
    say(f"   TP {matrix['true_positive']:3d}   FP {matrix['false_positive']:3d}")
    say(f"   FN {matrix['false_negative']:3d}   TN {matrix['true_negative']:3d}")
    say(f"   precision   {matrix['precision']}")
    say(f"   recall      {matrix['recall']}")
    correct = [r for r in results if r["category_correct"]]
    predicted = [r for r in results if r["predicted"]]
    say(f"   category    {len(correct)}/{len(predicted)} predictions named the right archetype")
    say(f"   warning     median {lead} min of precursor pattern ahead of the failure")
    say(f"   imminence   median stated ETA {lead_error} min on a window with 0 min left")

    rule("calibration · stated confidence vs realized rate")
    for b in buckets:
        if not b["n"]:
            say(f"   {b['bucket']}   —  no predictions in this bucket")
            continue
        say(f"   {b['bucket']}   n={b['n']:3d}  stated {b['stated']:.3f}  "
            f"realized {b['realized']:.3f}  gap {b['gap']:+.3f}")

    misses = [r for r in results if r["led_to_incident"] and not r["predicted"]]
    alarms = [r for r in results if not r["led_to_incident"] and r["predicted"]]
    if misses:
        rule(f"missed ({len(misses)}) · failures Oracle stayed silent on")
        for r in misses[:10]:
            say(f"   {r['archetype']:32s} {r['service']:10s} {r['silent_because']}")
    if alarms:
        rule(f"false alarms ({len(alarms)}) · windows that recovered anyway")
        for r in alarms[:10]:
            say(f"   {r['archetype']:32s} {r['service']:10s} "
                f"confidence {r['confidence']} over {r['matched']} matches")
    else:
        say("\n   no false alarms in this run — with this few negatives that is a "
            "small-sample result, not a claim of perfection")

    if args.dry_run:
        say("\n   dry run: nothing stored")
        return 0
    run_id = store(oracle, results, matrix, buckets, lead, lead_error)
    say(f"\n   stored as backtest_runs {run_id} — the dashboard reads the newest row")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
