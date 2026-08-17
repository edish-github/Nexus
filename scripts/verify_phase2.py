#!/usr/bin/env python3
"""Phase 2 exit gate: prove the seeded world is actually usable.

    make verify                        # everything except the slow checks
    python scripts/verify_phase2.py --load-rows 10000
    python scripts/verify_phase2.py --ttl-check       # waits for the TTL job

Seven checks, in the order the plan lists them:

1. **Index** — the hybrid `WHERE category … ORDER BY embedding <=>` query is
   served by the vector index, not a full scan.
2. **Retrieval** — held-out precursor windows retrieve neighbours of their own
   archetype. These windows were never written to the database, so this measures
   generalization rather than lookup.
3. **Provenance replay** — a prediction's evidence, re-read `AS OF SYSTEM TIME`
   its commit timestamp after the underlying table has been mutated, is
   byte-identical to what it saw. The mutation is verified to have changed the
   present-tense answer, otherwise the test proves nothing.
4. **Follower reads** — the dashboard's aggregate pattern resolves against
   `follower_read_timestamp()`.
5. **Demo-world integrity** — the staged beats are properties of the data: two
   merge-ready pairs, a promotion on the cusp, a zero-trial challenger, a bad
   playbook still above the retirement line, retired ancestors.
6. **Load sanity** — k-NN latency with 10k rows in the sensory tier.
7. **TTL** (opt-in) — a short-TTL row is actually reaped by the row-level TTL job.

Client-observed latency includes the WAN round trip to the cluster, so check 6
reports the server-side execution time from `EXPLAIN ANALYZE` alongside it. Only
the server figure is a statement about the database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time

from _env import REPO_ROOT, bootstrap, require_dsn

bootstrap()

import numpy as np  # noqa: E402
import psycopg  # noqa: E402

from generator import archetypes  # noqa: E402
from nexus_common import config, embeddings  # noqa: E402

K = 14  # Oracle's k, from the plan
GC_SECONDS = 604800  # the 7-day MVCC window sql/002 configures for the replay
LOAD_CHUNK = 500  # rows per committed COPY during the load check
TAG = "verify-phase2"
BACKTEST_PATH = REPO_ROOT / "demo" / "backtest_set.jsonl"
# Posterior mean, as SQL. Fitness is never stored, so every read derives it.
MEAN = "(success_count + 1.0) / (success_count + failure_count + 2.0)"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def vector_literal(text: str) -> str:
    return embeddings.to_vector_literal(embeddings.embed(text))


# --------------------------------------------------------------------------- #
# 1. The vector index is actually used
# --------------------------------------------------------------------------- #

HYBRID_SQL = """
    SELECT id, outcome_category, led_to_incident,
           1 - (trajectory_embedding <=> %s::VECTOR) AS similarity
    FROM precursor_snapshots
    WHERE outcome_category = %s
    ORDER BY trajectory_embedding <=> %s::VECTOR
    LIMIT %s
"""


def check_index(conn, probe: str) -> None:
    """Prove the prefixed vector index serves the hybrid query correctly.

    Deliberately *not* asserted: that the optimizer picks it unprompted. With
    190 snapshots a category filter selects ~19 rows, and scanning those and
    sorting them really is cheaper than descending a vector index — the planner
    is right, and an assertion that it must choose the vector index would be an
    assertion that it must cost the query badly. What has to hold is that the
    index exists, is correctly built, and returns the same answer; the crossover
    is then a matter of how large the memory grows, and check 6 shows the
    optimizer reaching for a vector search at 10k rows.
    """
    print("\n1) hybrid SQL + vector query and the prefixed vector index")
    params = (probe, "connection_pool_exhaustion", probe, K)
    plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + HYBRID_SQL, params).fetchall())
    chosen = "vector search" if "vector search" in plan else "scan + top-k"
    rows_in_table = conn.execute("SELECT count(*) FROM precursor_snapshots").fetchone()[0]
    print(f"     optimizer's unhinted choice at {rows_in_table} rows: {chosen}")
    check("no full table scan in the plan", "FULL SCAN" not in plan.upper(),
          "" if "FULL SCAN" not in plan.upper() else "EXPLAIN:\n" + plan)

    hinted_sql = HYBRID_SQL.replace(
        "FROM precursor_snapshots",
        "FROM precursor_snapshots@precursor_category_trajectory_idx",
    )
    hinted_plan = "\n".join(
        r[0] for r in conn.execute("EXPLAIN " + hinted_sql, params).fetchall()
    )
    prefixed = "vector search" in hinted_plan and "prefix spans" in hinted_plan
    check("the prefixed vector index serves the query as a vector search with "
          "the category as a prefix span", prefixed,
          "" if prefixed else "EXPLAIN:\n" + hinted_plan)

    # Recall, not identity. A CockroachDB vector index is *approximate* — that is
    # the whole reason it is faster than sorting every row — so it may legitimately
    # return a slightly different top-k than an exact scan, especially where
    # distances are near-tied. Asserting byte-identical results asserts that an ANN
    # index is not an ANN index, and it flakes accordingly: this check failed once
    # against a corpus the same query agreed on a minute later. What matters is that
    # the index finds the neighbours that matter, so the assertion is recall against
    # the exact answer, with the top match exact.
    baseline = [r[0] for r in conn.execute(HYBRID_SQL, params).fetchall()]
    through_index = [r[0] for r in conn.execute(hinted_sql, params).fetchall()]
    overlap = len(set(baseline) & set(through_index))
    recall = overlap / len(baseline) if baseline else 0.0
    print(f"     index recall@{len(baseline)} against the exact scan: {recall:.3f} "
          f"({overlap}/{len(baseline)} shared)")
    check("the vector index recovers the exact scan's neighbourhood (recall >= 0.9)",
          recall >= 0.9,
          f"recall {recall:.3f} — the index is missing neighbours the scan finds")
    check("the index agrees with the scan on the nearest neighbour",
          bool(baseline) and baseline[0] == through_index[0],
          "" if baseline and baseline[0] == through_index[0]
          else "the closest match differs, which no amount of approximation excuses")


# --------------------------------------------------------------------------- #
# 2. k-NN retrieval on held-out windows
# --------------------------------------------------------------------------- #

def check_retrieval(conn, spot_checks: int) -> None:
    print(f"\n2) k-NN precursor match on held-out windows (k={K})")
    if not BACKTEST_PATH.exists():
        check("backtest set present", False, f"{BACKTEST_PATH} missing — run `make seed`")
        return
    records = [json.loads(line) for line in BACKTEST_PATH.read_text().splitlines()]
    incidents = [r for r in records if r["kind"] == "incident"]
    negatives = [r for r in records if r["kind"] == "negative"]
    check("held-out set is non-empty", bool(incidents and negatives),
          f"{len(incidents)} incidents, {len(negatives)} negatives")

    # Spot-check one held-out window per archetype, up to `spot_checks`.
    seen: set[str] = set()
    sample = []
    for rec in incidents:
        if rec["archetype"] not in seen:
            seen.add(rec["archetype"])
            sample.append(rec)
        if len(sample) >= spot_checks:
            break

    hits = 0
    for rec in sample:
        probe = vector_literal(rec["precursor_text"])
        rows = conn.execute(
            """
            SELECT outcome_category, led_to_incident,
                   1 - (trajectory_embedding <=> %s::VECTOR) AS similarity
            FROM precursor_snapshots
            ORDER BY trajectory_embedding <=> %s::VECTOR
            LIMIT %s
            """,
            (probe, probe, K),
        ).fetchall()
        same = sum(1 for r in rows if r[0] == rec["archetype"])
        top_sim = rows[0][2]
        ok = rows[0][0] == rec["archetype"] and same >= K * 0.6
        hits += ok
        print(f"     {rec['archetype']:32s} nearest={rows[0][0]:32s} "
              f"{same}/{K} same category, top sim {top_sim:.3f}")
    check(f"held-out windows retrieve their own archetype ({hits}/{len(sample)})",
          hits == len(sample))

    # A negative window must still look like its archetype — that is what makes
    # it a plausible false alarm — but the matched neighbours' outcome labels are
    # what stop Oracle from firing. Confirm both signals exist.
    neg = negatives[0]
    probe = vector_literal(neg["precursor_text"])
    rows = conn.execute(
        """
        SELECT led_to_incident FROM precursor_snapshots
        ORDER BY trajectory_embedding <=> %s::VECTOR LIMIT %s
        """,
        (probe, K),
    ).fetchall()
    positives = sum(1 for r in rows if r[0])
    check("negative windows retrieve a mixed-outcome neighbourhood",
          0 < positives < K,
          f"{positives}/{K} of a held-out negative's neighbours led to an incident")


# --------------------------------------------------------------------------- #
# 3. Provenance replay
# --------------------------------------------------------------------------- #

EVIDENCE_SQL = """
    SELECT id::STRING, outcome_category, led_to_incident,
           round((1 - (trajectory_embedding <=> %s::VECTOR))::NUMERIC, 9)::STRING
    FROM precursor_snapshots {AOST}
    WHERE outcome_category = %s
    ORDER BY trajectory_embedding <=> %s::VECTOR
    LIMIT %s
"""


def _digest(rows) -> str:
    return hashlib.sha256(
        "\n".join("|".join(str(c) for c in row) for row in rows).encode()
    ).hexdigest()


def check_provenance(conn, probe: str) -> None:
    print("\n3) provenance replay (AS OF SYSTEM TIME at the prediction's commit)")
    category = "connection_pool_exhaustion"
    params = (probe, category, probe, K)

    # The evidence, and the exact instant it was read, captured atomically with
    # the prediction that cites it.
    with conn.transaction():
        cur = conn.execute(EVIDENCE_SQL.replace("{AOST}", ""), params)
        evidence_then = cur.fetchall()
        commit_ts = conn.execute("SELECT cluster_logical_timestamp()::STRING").fetchone()[0]
        pred_id = conn.execute(
            """
            INSERT INTO predictions
                (service_name, causal_pattern, predicted_outcome, predicted_severity,
                 alpha, beta, matching_precursor_count, current_embedding, predicted_eta)
            VALUES (%s, %s, %s, 4, %s, %s, %s, %s::VECTOR, now() + INTERVAL '50 minutes')
            RETURNING id
            """,
            (f"{TAG}-svc", "pool utilization creep with p99 rise", category,
             float(sum(1 for r in evidence_then if r[2]) + 1),
             float(sum(1 for r in evidence_then if not r[2]) + 1),
             len(evidence_then), probe),
        ).fetchone()[0]
    digest_then = _digest(evidence_then)
    print(f"     prediction {pred_id} committed at {commit_ts}")
    print(f"     evidence digest {digest_then[:16]}… over {len(evidence_then)} rows")

    # Now change history: insert snapshots that sit closer to the probe than
    # anything the prediction saw, so the present-tense answer must move.
    inserted = []
    for i in range(3):
        row = conn.execute(
            """
            INSERT INTO precursor_snapshots
                (service_name, region, trajectory_embedding, window_start, window_end,
                 outcome_category, led_to_incident, metric_digest)
            VALUES (%s, 'aws-us-east-1', %s::VECTOR, now() - INTERVAL '2 hours', now(),
                    %s, true, %s::JSONB)
            RETURNING id
            """,
            (f"{TAG}-svc", probe, category, json.dumps({"note": TAG, "n": i})),
        ).fetchone()
        inserted.append(row[0])

    evidence_now = conn.execute(EVIDENCE_SQL.replace("{AOST}", ""), params).fetchall()
    changed = _digest(evidence_now) != digest_then
    check("mutating the evidence table changes the present-tense answer", changed,
          "3 nearer snapshots inserted")

    # The GC window the replay depends on, asserted *after* the transaction above.
    # A replay run seconds after its decision succeeds at any GC threshold, so the
    # check passing above is no evidence the window is what the schema says:
    # `precursor_snapshots` was found inheriting 4500s (75 minutes) instead of the
    # configured 604800 after a manual TRUNCATE recreated the table and discarded
    # its zone config, and nothing here caught it.
    #
    # It reads zone configuration, which touches system tables and refreshes
    # descriptor leases. Placed *before* the transaction above, that reliably
    # pushed its read timestamp and made it fail with RETRY_SERIALIZABLE 3 runs out
    # of 3. Order matters here; leave it after the commit.
    for table in ("precursor_snapshots", "predictions"):
        raw = conn.execute(
            f"SELECT raw_config_sql FROM [SHOW ZONE CONFIGURATION FOR TABLE {table}]"
        ).fetchone()
        setting = next((line.strip().rstrip(",") for line in (raw[0] or "").splitlines()
                        if "gc.ttlseconds" in line), None)
        seconds = int(setting.split("=")[1]) if setting else None
        ok = seconds == GC_SECONDS
        check(f"{table} retains a 7-day MVCC window for replay", ok,
              f"gc.ttlseconds = {seconds}" if ok else
              f"gc.ttlseconds = {seconds if seconds is not None else 'inherited (unset)'}, "
              f"expected {GC_SECONDS} — re-apply sql/002_zone_configs.sql")

    replayed = conn.execute(
        EVIDENCE_SQL.replace("{AOST}", f"AS OF SYSTEM TIME {commit_ts}"), params
    ).fetchall()
    identical = _digest(replayed) == digest_then
    check("AS OF SYSTEM TIME replay reproduces the evidence byte-for-byte", identical,
          f"{digest_then[:16]}… == {_digest(replayed)[:16]}…" if identical
          else f"{digest_then[:16]}… != {_digest(replayed)[:16]}…")

    conn.execute("DELETE FROM precursor_snapshots WHERE id = ANY(%s)", (inserted,))
    conn.execute("DELETE FROM predictions WHERE id = %s", (pred_id,))


# --------------------------------------------------------------------------- #
# 4. Follower reads
# --------------------------------------------------------------------------- #

def check_follower_reads(conn) -> None:
    print("\n4) follower-read aggregates (the dashboard's read path)")
    rows = conn.execute(
        """
        SELECT outcome_category,
               count(*) AS playbooks,
               round(avg((success_count + 1.0) / (success_count + failure_count + 2.0)), 3)
        FROM playbooks AS OF SYSTEM TIME follower_read_timestamp()
        WHERE status = 'active'
        GROUP BY outcome_category
        ORDER BY outcome_category
        """
    ).fetchall()
    for cat, n, mean in rows:
        print(f"     {cat:32s} {n} active, mean posterior {mean}")
    expected = len(archetypes.ARCHETYPES)
    check("follower-read aggregate returns every archetype", len(rows) == expected,
          f"{len(rows)}/{expected} categories")


# --------------------------------------------------------------------------- #
# 5. The staged demo beats are properties of the seed
# --------------------------------------------------------------------------- #

def check_world_integrity(conn) -> None:
    print("\n5) demo-world integrity (the staged beats)")
    # Chronicler's merge predicate, run against the seed: near-duplicate siblings
    # in the same category with both posterior means above 0.5.
    pairs = conn.execute(
        """
        SELECT a.name, b.name,
               round((a.precursor_embedding <=> b.precursor_embedding)::NUMERIC, 4)
        FROM playbooks a JOIN playbooks b
          ON a.outcome_category = b.outcome_category AND a.id < b.id
        WHERE a.status = 'active' AND b.status = 'active'
          AND a.precursor_embedding <=> b.precursor_embedding < 0.15
          AND (a.success_count + 1.0) / (a.success_count + a.failure_count + 2.0) > 0.5
          AND (b.success_count + 1.0) / (b.success_count + b.failure_count + 2.0) > 0.5
        ORDER BY 3
        """
    ).fetchall()
    for a, b, d in pairs:
        print(f"     merge-ready: {a} ~ {b} (distance {d})")
    check("exactly two merge-ready pairs exist", len(pairs) == 2, f"found {len(pairs)}")

    promo = conn.execute(
        f"SELECT name, {MEAN}, success_count + failure_count FROM playbooks "
        f"WHERE status='active' AND memory_tier <> 'institutional' "
        f"AND success_count + failure_count >= 10 ORDER BY 2 DESC LIMIT 1"
    ).fetchone()
    check("a promotion candidate sits just under the 0.9 threshold",
          promo is not None and 0.88 <= float(promo[1]) <= 0.901,
          f"{promo[0]}: mean {float(promo[1]):.3f} over {promo[2]} trials" if promo else "none")

    challenger = conn.execute(
        "SELECT name, generation FROM playbooks "
        "WHERE success_count = 0 AND failure_count = 0 AND status = 'active'"
    ).fetchall()
    check("a zero-trial challenger can be sampled", len(challenger) >= 1,
          ", ".join(f"{n} (gen {g})" for n, g in challenger))

    bad = conn.execute(
        f"SELECT name, {MEAN} FROM playbooks WHERE status='active' ORDER BY 2 LIMIT 1"
    ).fetchone()
    check("the bad-fix playbook is still selectable (above the 0.2 retirement line)",
          bad is not None and 0.2 < float(bad[1]) < 0.35,
          f"{bad[0]}: mean {float(bad[1]):.3f}" if bad else "none")

    counts = dict(conn.execute(
        "SELECT status, count(*) FROM playbooks GROUP BY status"
    ).fetchall())
    check("retired ancestors and merged parents are present, not deleted",
          counts.get("retired", 0) >= 3 and counts.get("merged", 0) >= 2, str(counts))

    gens = conn.execute("SELECT max(generation) FROM playbooks").fetchone()[0]
    check("the family tree is four generations deep", gens == 4, f"max generation {gens}")

    kinds = dict(conn.execute(
        "SELECT event_type, count(*) FROM evolution_log GROUP BY event_type"
    ).fetchall())
    expected = {"birth", "mutation", "growth", "rollback", "merge", "promotion", "retirement"}
    check("evolution_log carries every lifecycle event type",
          expected <= set(kinds), str(dict(sorted(kinds.items()))))

    regions = dict(conn.execute(
        "SELECT crdb_region::STRING, count(*) FROM playbooks GROUP BY 1"
    ).fetchall())
    check("playbooks are homed across all three regions", len(regions) == 3, str(regions))

    globals_ = conn.execute("SELECT count(*) FROM institutional_playbooks").fetchone()[0]
    check("the GLOBAL institutional table is populated", globals_ >= 1, f"{globals_} rows")


# --------------------------------------------------------------------------- #
# 6. Load sanity
# --------------------------------------------------------------------------- #

def check_load(conn, rows: int, probe: str) -> None:
    print(f"\n6) load sanity ({rows} telemetry rows)")
    if rows <= 0:
        print("     skipped (--load-rows 0)")
        return
    rng = np.random.default_rng(99)
    existing = conn.execute(
        "SELECT count(*) FROM telemetry_embeddings WHERE service_name LIKE %s", (f"{TAG}%",)
    ).fetchone()[0]

    if existing < rows:
        started = time.perf_counter()
        base = np.asarray(json.loads(probe))
        # COPY rather than INSERT: 10k 1024-dimension literals is ~70 MB and a
        # round trip per row would dominate the measurement. Chunked and
        # committed as it goes, because one 10k-row transaction against a
        # vector-indexed table on a remote cluster is a very different animal
        # from twenty 500-row ones — and because a re-run then resumes from
        # wherever the last one stopped rather than starting over.
        for start in range(existing, rows, LOAD_CHUNK):
            stop = min(start + LOAD_CHUNK, rows)
            with conn.cursor().copy(
                "COPY telemetry_embeddings "
                "(service_name, region, metric_type, embedding, expires_at) FROM STDIN"
            ) as copy:
                for i in range(start, stop):
                    v = base + rng.normal(0, 0.35, size=base.shape)
                    v /= np.linalg.norm(v)
                    copy.write_row((
                        f"{TAG}-{i % 4}", archetypes.REGIONS[i % 3], "trajectory_window",
                        "[" + ",".join(f"{x:.5f}" for x in v) + "]",
                        "2099-01-01 00:00:00+00",
                    ))
            print(f"     loaded {stop}/{rows} ({time.perf_counter() - started:.0f}s)",
                  flush=True)
        conn.execute("ANALYZE telemetry_embeddings")

    total = conn.execute("SELECT count(*) FROM telemetry_embeddings").fetchone()[0]
    sql = ("SELECT id FROM telemetry_embeddings "
           "ORDER BY embedding <=> %s::VECTOR LIMIT 14")
    timings = []
    for _ in range(25):
        t0 = time.perf_counter()
        conn.execute(sql, (probe,)).fetchall()
        timings.append((time.perf_counter() - t0) * 1000)
    p50, p95 = statistics.median(timings), sorted(timings)[int(len(timings) * 0.95) - 1]

    plan = "\n".join(r[0] for r in conn.execute("EXPLAIN ANALYZE " + sql, (probe,)).fetchall())
    server_ms = None
    for line in plan.splitlines():
        if "execution time:" in line:
            server_ms = line.split("execution time:")[1].strip()
    print(f"     rows in sensory tier: {total}")
    print(f"     client-observed  p50 {p50:.1f}ms  p95 {p95:.1f}ms  (includes the WAN round trip)")
    print(f"     server execution time: {server_ms}")
    ok = server_ms is not None and _ms(server_ms) < 100
    check("server-side k-NN execution under 100ms at 10k rows", ok, f"{server_ms}")
    # At this volume the optimizer should reach for the vector index unprompted,
    # which is the other half of what check 1 could not assert at 190 rows.
    check("the optimizer chooses a vector search at 10k rows",
          "vector search" in plan, "" if "vector search" in plan else "EXPLAIN ANALYZE:\n" + plan)


def _ms(text: str) -> float:
    text = text.strip()
    if text.endswith("ms"):
        return float(text[:-2])
    if text.endswith("s"):
        return float(text[:-1]) * 1000
    return float(text)


# --------------------------------------------------------------------------- #
# 7. Row-level TTL actually reaps
# --------------------------------------------------------------------------- #

def check_ttl(conn, probe: str, timeout_s: int = 480) -> None:
    print(f"\n7) row-level TTL reap (waiting up to {timeout_s}s for the TTL job)")
    row_id = conn.execute(
        """
        INSERT INTO telemetry_embeddings
            (service_name, region, metric_type, embedding, expires_at)
        VALUES (%s, 'aws-us-east-1', 'ttl-probe', %s::VECTOR, now() - INTERVAL '1 hour')
        RETURNING id
        """,
        (f"{TAG}-ttl", probe),
    ).fetchone()[0]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        alive = conn.execute(
            "SELECT count(*) FROM telemetry_embeddings WHERE id = %s", (row_id,)
        ).fetchone()[0]
        if not alive:
            check("an expired row is reaped by the TTL job", True,
                  f"gone after {int(timeout_s - (deadline - time.time()))}s")
            return
        time.sleep(20)
    conn.execute("DELETE FROM telemetry_embeddings WHERE id = %s", (row_id,))
    check("an expired row is reaped by the TTL job", False,
          f"still present after {timeout_s}s (ttl_job_cron on this table is '*/5 * * * *')")


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 verification")
    parser.add_argument("--load-rows", type=int, default=10000,
                        help="rows to put in telemetry_embeddings for the latency check")
    parser.add_argument("--spot-checks", type=int, default=5)
    parser.add_argument("--ttl-check", action="store_true",
                        help="also wait for the row-level TTL job to reap an expired row")
    parser.add_argument("--keep", action="store_true", help="leave the load-test rows behind")
    args = parser.parse_args()

    dsn = require_dsn()
    print(f"embedding provider: {embeddings.provider_name()} (dim {config.EMBEDDING_DIM})")
    probe_text = _probe_text()
    probe = vector_literal(probe_text)

    with psycopg.connect(dsn, autocommit=True) as conn:
        check_index(conn, probe)
        check_retrieval(conn, args.spot_checks)
        check_provenance(conn, probe)
        check_follower_reads(conn)
        check_world_integrity(conn)
        check_load(conn, args.load_rows, probe)
        if args.ttl_check:
            check_ttl(conn, probe)
        if not args.keep:
            conn.execute(
                "DELETE FROM telemetry_embeddings WHERE service_name LIKE %s", (f"{TAG}%",)
            )

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


def _probe_text() -> str:
    """A synthetic connection-pool precursor window, built the same way the world was."""
    from generator import archetypes as a
    from generator.trajectory import synthesize
    from nexus_common.trajectory import trajectory_text

    traj = synthesize(
        a.get("connection_pool_exhaustion"),
        rng=np.random.default_rng(4242),
        service="payments",
        region="aws-us-east-1",
    )
    return trajectory_text(
        service="payments", region="aws-us-east-1",
        window_minutes=traj.precursor_minutes,
        metrics=traj.precursor_metrics(), phase="precursor",
    )


if __name__ == "__main__":
    raise SystemExit(main())
