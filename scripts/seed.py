#!/usr/bin/env python3
"""Build the entire demo world into CockroachDB, from cold, in one command.

    make seed                # migrate, then seed
    python scripts/seed.py --dry-run     # build the world, write nothing
    python scripts/seed.py --seed 1234   # a different world

What lands in the database:

    incidents               150   (200 generated, 50 withheld for the backtest)
    precursor_snapshots     190   (150 incident precursors + 40 negatives)
    playbooks                30   (four generations, with their family tree)
    institutional_playbooks   1   (the one already-promoted playbook)
    evolution_log           ~190  (birth/mutation/growth/rollback/merge/…)

What deliberately does not: the 50 held-out incidents and 20 held-out negatives.
They are written to `demo/backtest_set.jsonl` instead, so the Phase 7 backtest
measures prediction on windows the memory has genuinely never seen.

`telemetry_embeddings` is also left empty: it has a 2-hour TTL, so seeding it
would be seeding something that evaporates. The live generator fills it.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

from _env import REPO_ROOT, bootstrap, require_dsn

bootstrap()

import numpy as np  # noqa: E402
import psycopg  # noqa: E402

from generator import playbooks as pb  # noqa: E402
from generator import vectors, world  # noqa: E402
from nexus_common import config, embeddings  # noqa: E402

STATEMENT_BATCH = 25  # rows per multi-VALUES statement; 1024-dim literals are ~9 KB each
COMMIT_BATCH = 500  # rows per committed transaction batch
BACKTEST_PATH = REPO_ROOT / "demo" / "backtest_set.jsonl"
MANIFEST_PATH = REPO_ROOT / "demo" / "seed_manifest.json"

# Playbooks are procedural memory: each family is homed in the region where it
# was learned, so the REGIONAL BY ROW locality is doing real work rather than
# putting everything in the primary region.
ARCHETYPE_HOME = {
    "connection_pool_exhaustion": "aws-us-east-1",
    "memory_leak_oom": "aws-eu-west-1",
    "cache_stampede": "aws-ap-south-1",
    "cert_expiry": "aws-us-east-1",
    "disk_full": "aws-eu-west-1",
    "bad_deploy_latency_regression": "aws-us-east-1",
    "thread_pool_starvation": "aws-ap-south-1",
    "dns_timeout_cascade": "aws-eu-west-1",
}

# Delete order respects every foreign key: evolution_log and
# institutional_playbooks point at playbooks, precursor_snapshots at incidents.
WIPE_ORDER = (
    "evolution_log",
    "institutional_playbooks",
    "predictions",
    "precursor_snapshots",
    "telemetry_embeddings",
    "playbooks",
    "incidents",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def run_in_tx(conn: psycopg.Connection, fn, attempts: int = 5):
    """Run fn(conn) inside an explicit transaction with retry on 40001/serialization failures."""
    delay = 0.05
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            with conn.transaction():
                return fn(conn)
        except psycopg.Error as e:
            state = getattr(e, "sqlstate", None)
            last_err = e
            if state in ("40001", "40003", "08000", "08003", "08006") and attempt < attempts:
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
                continue
            raise
    if last_err:
        raise last_err


def wipe(conn: psycopg.Connection) -> None:
    log("resetting the demo world")
    run_in_tx(conn, lambda c: c.execute(
        "UPDATE playbooks SET parent_id = NULL WHERE parent_id IS NOT NULL"))
    for table in WIPE_ORDER:
        def _wipe_table(c: psycopg.Connection, tbl=table) -> None:
            deleted = c.execute(f"DELETE FROM {tbl}").rowcount
            if deleted:
                log(f"   {tbl}: {deleted} rows removed")
        run_in_tx(conn, _wipe_table)


def reassert_zone_configs(conn: psycopg.Connection) -> None:
    """Re-apply `sql/002`'s GC windows, because they are silently destructible.

    The 7-day `gc.ttlseconds` on `precursor_snapshots` and `predictions` is what
    makes the provenance replay resolve at a decision's commit timestamp rather
    than failing on the GC threshold. Nothing in this repo truncates — `wipe()`
    uses `DELETE`, which preserves zone configs — but `TRUNCATE` recreates a table
    under a new ID and discards its zone config, and one typed at a prompt during
    load-test debugging is exactly how `precursor_snapshots` came to be inheriting
    75 minutes while everything appeared to work. The replay only ever runs seconds
    after the decision, so a 75-minute window and a 7-day one look identical right
    up until a judge asks about yesterday.

    So the reset re-asserts them rather than assuming they survived. The statements
    are read from the migration instead of being restated here, so there is one
    source of truth for the number.
    """
    path = REPO_ROOT / "sql" / "002_zone_configs.sql"
    statements = [s.strip() for s in path.read_text().split(";") if s.strip()
                  and not s.strip().startswith("--")]
    for statement in statements:
        run_in_tx(conn, lambda c, s=statement: c.execute(s))
    log(f"   zone configs re-asserted from {path.name} ({len(statements)} statement(s))")


def embed_all(texts: list[str], label: str) -> list[list[float]]:
    started = time.perf_counter()
    out = []
    for i, text in enumerate(texts, start=1):
        out.append(embeddings.embed(text))
        if i % 50 == 0 or i == len(texts):
            log(f"   {label}: {i}/{len(texts)}")
    log(f"   {label}: done in {time.perf_counter() - started:.1f}s")
    return out


NAMESPACE_NEXUS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def derive_uuid(key: str) -> str:
    return str(uuid.uuid5(NAMESPACE_NEXUS, key))


def insert_batched(
    conn: psycopg.Connection, sql: str, rows: list[tuple], label: str = "rows"
) -> None:
    """Insert `rows` in commit batches of COMMIT_BATCH (500) rows, with a commit per batch

    wrapped in run_in_tx() transaction retry. Each transaction batch issues multi-VALUES
    statements of STATEMENT_BATCH (25) rows.
    """
    if not rows:
        return
    head, _, tail = sql.partition("VALUES")
    head = head + "VALUES "

    on_conflict = ""
    if "ON CONFLICT" in tail:
        values_part, _, conflict_part = tail.partition("ON CONFLICT")
        placeholder = values_part.strip()
        on_conflict = " ON CONFLICT " + conflict_part.strip()
    else:
        placeholder = tail.strip()

    for tx_start in range(0, len(rows), COMMIT_BATCH):
        tx_chunk = rows[tx_start : tx_start + COMMIT_BATCH]

        def _do_insert(c: psycopg.Connection, chunk=tx_chunk) -> None:
            for start in range(0, len(chunk), STATEMENT_BATCH):
                stmt_rows = chunk[start : start + STATEMENT_BATCH]
                stmt = head + ", ".join([placeholder] * len(stmt_rows)) + on_conflict
                params = [value for row in stmt_rows for value in row]
                c.execute(stmt, params)

        run_in_tx(conn, _do_insert)
        log(f"   {label}: {tx_start + len(tx_chunk)}/{len(rows)}")


# --------------------------------------------------------------------------- #
# Seeding steps
# --------------------------------------------------------------------------- #

def seed_incidents(conn, w: world.World, vecs: dict[str, list[float]]) -> dict[str, str]:
    """Insert the seeded incidents; return {incident key: uuid}."""
    seeded = w.seeded_incidents
    ids = {inc.key: derive_uuid(f"incident::{inc.key}") for inc in seeded}
    rows = [
        (
            ids[i.key],
            i.region,  # crdb_region: the incident is homed where it was observed
            i.title,
            i.severity,
            "resolved",
            [i.service],
            i.was_predicted,
            i.was_prevented,
            i.was_auto_resolved,
            f"{i.archetype} confirmed from the failure-window signature",
            embeddings.to_vector_literal(vecs[f"symptom::{i.key}"]),
            i.detected_at,
            i.resolved_at,
            i.mttr_seconds,
        )
        for i in seeded
    ]
    sql = """
        INSERT INTO incidents
            (id, crdb_region, title, severity, status, affected_services, was_predicted,
             was_prevented, was_auto_resolved, root_cause, symptom_embedding,
             detected_at, resolved_at, mttr_seconds)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::VECTOR, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
    """
    insert_batched(conn, sql, rows, label="incidents")
    return ids


def seed_precursors(conn, w: world.World, vecs, incident_ids: dict[str, str]) -> int:
    rows = []
    for inc in w.seeded_incidents:
        win = inc.window
        rows.append(
            (
                derive_uuid(f"precursor::{win.key}"),
                incident_ids[inc.key], win.service, win.region,
                embeddings.to_vector_literal(vecs[f"window::{win.key}"]),
                win.window_start, win.window_end, win.archetype, True,
                json.dumps(win.digest), win.window_end,
            )
        )
    for win in w.seeded_negatives:
        rows.append(
            (
                derive_uuid(f"precursor::{win.key}"),
                None, win.service, win.region,
                embeddings.to_vector_literal(vecs[f"window::{win.key}"]),
                win.window_start, win.window_end, win.archetype, False,
                json.dumps(win.digest), win.window_end,
            )
        )
    sql = """
        INSERT INTO precursor_snapshots
            (id, incident_id, service_name, region, trajectory_embedding, window_start,
             window_end, outcome_category, led_to_incident, metric_digest, created_at)
        VALUES (%s, %s, %s, %s, %s::VECTOR, %s, %s, %s, %s, %s::JSONB, %s)
        ON CONFLICT (id) DO NOTHING
    """
    insert_batched(conn, sql, rows, label="precursor_snapshots")
    return len(rows)


def playbook_vectors(w: world.World, vecs, rng) -> dict[str, list[float]]:
    """Place each playbook near its archetype's precursor centroid.

    Distances are exact, which is what makes the two staged merge pairs a
    property of the seed rather than a hope: both land inside Chronicler's
    `distance < 0.15` predicate, and no other sibling pair does.
    """
    by_archetype: dict[str, list[list[float]]] = {}
    for inc in w.seeded_incidents:
        by_archetype.setdefault(inc.archetype, []).append(vecs[f"window::{inc.window.key}"])
    centroids = {k: vectors.centroid(v) for k, v in by_archetype.items()}

    placed: dict[str, list[float]] = {}
    for spec in pb.SPECS:  # non-duplicates first: a twin is placed relative to its pair
        if spec.near_duplicate_of is None:
            placed[spec.slug] = vectors.place(centroids[spec.archetype], spec.spread, rng)
    for spec in pb.SPECS:
        if spec.near_duplicate_of is not None:
            twin = placed[spec.near_duplicate_of]
            placed[spec.slug] = vectors.place(twin, 0.065, rng)
    return placed


def seed_playbooks(conn, vecs: dict[str, list[float]], now: datetime) -> dict[str, str]:
    """Insert playbooks parents-first so parent_id and lineage resolve."""
    ids: dict[str, str] = {}
    lineage: dict[str, list[str]] = {}
    absorbed: dict[str, list[str]] = {}
    for spec in pb.SPECS:
        if spec.merged_into:
            absorbed.setdefault(spec.merged_into, []).append(spec.slug)

    rows = []
    for spec in sorted(pb.SPECS, key=lambda s: (s.generation, s.slug)):
        pb_id = derive_uuid(f"playbook::{spec.slug}")
        ids[spec.slug] = pb_id
        parent_id = ids.get(spec.parent) if spec.parent else None
        chain = list(lineage.get(spec.parent, [])) + ([parent_id] if parent_id else [])
        for other in absorbed.get(spec.slug, []):
            if other in ids and ids[other] not in chain:
                chain.append(ids[other])
        lineage[spec.slug] = chain

        # Targets stay as the `{service}` placeholder. A playbook is procedural
        # memory — how to relieve pool exhaustion, not how to fix payments — so
        # Guardian binds the target to whatever service the prediction is about.
        steps = spec.steps_template()
        born = now - timedelta(days=spec.born_days_ago)
        last_used = now - timedelta(days=max(0.5, spec.born_days_ago * 0.15))
        rows.append(
            (
                pb_id,
                ARCHETYPE_HOME[spec.archetype],
                spec.name,
                spec.archetype,
                embeddings.to_vector_literal(vecs[spec.slug]),
                json.dumps(steps),
                json.dumps(_inverse_program(steps)),
                _reversible(spec.steps),
                spec.successes,
                spec.failures,
                spec.generation,
                parent_id,
                chain,
                spec.memory_tier,
                spec.status,
                last_used if spec.memory_tier == "institutional" else None,
                last_used if spec.status == "retired" else None,
                last_used,
                born,
            )
        )

    sql = """
        INSERT INTO playbooks
            (id, crdb_region, name, outcome_category, precursor_embedding,
             remediation_steps, inverse_steps, reversible, success_count,
             failure_count, generation, parent_id, lineage, memory_tier, status,
             promoted_at, retired_at, last_used_at, created_at, expires_at)
        VALUES (%s, %s, %s, %s, %s::VECTOR, %s::JSONB, %s::JSONB, %s, %s, %s, %s, %s,
                %s::UUID[], %s, %s, %s, %s, %s, %s, now() + INTERVAL '90 days')
        ON CONFLICT (id) DO NOTHING
    """
    insert_batched(conn, sql, rows, label="playbooks")
    return ids


def _reversible(steps: list[dict]) -> bool:
    return all(step.get("inverse") for step in steps)


def _inverse_program(steps: list[dict]) -> list[dict]:
    """Every declared inverse, in reverse execution order — the rollback program."""
    out = []
    for step in reversed(steps):
        inv = step.get("inverse")
        if inv:
            out.append({"action": inv["action"], "target": step["target"],
                        "params": inv.get("params", {})})
    return out


def seed_institutional(conn, ids: dict[str, str], now: datetime) -> int:
    """Copy the already-promoted playbooks into the GLOBAL table."""
    promoted = [s for s in pb.SPECS if s.memory_tier == "institutional"]
    for spec in promoted:
        inst_id = derive_uuid(f"institutional::{spec.slug}")
        run_in_tx(
            conn,
            lambda c, iid=inst_id, sid=ids[spec.slug]: c.execute(
                """
                INSERT INTO institutional_playbooks
                    (id, source_playbook_id, name, outcome_category, precursor_embedding,
                     remediation_steps, inverse_steps, reversible, success_count,
                     failure_count, generation, lineage, promoted_at)
                SELECT %s, id, name, outcome_category, precursor_embedding, remediation_steps,
                       inverse_steps, reversible, success_count, failure_count, generation,
                       lineage, %s
                FROM playbooks WHERE id = %s
                ON CONFLICT (id) DO NOTHING
                """,
                (iid, now, sid),
            ),
        )
    log(f"   institutional_playbooks: {len(promoted)}/{len(promoted)} (GLOBAL locality)")
    return len(promoted)


def seed_evolution_log(conn, ids: dict[str, str], incident_ids: dict[str, str],
                       now: datetime) -> int:
    events = pb.lifecycle_events()
    incident_pool = list(incident_ids.values())
    rows = []
    for n, ev in enumerate(events):
        trigger = (
            incident_pool[n % len(incident_pool)]
            if ev.event_type in ("growth", "rollback") and incident_pool
            else None
        )
        ev_id = derive_uuid(f"evolution::{n}:{ev.event_type}:{ev.playbook_slug}")
        rows.append(
            (
                ev_id,
                ev.event_type,
                ids[ev.playbook_slug],
                ids.get(ev.parent_slug) if ev.parent_slug else None,
                trigger,
                ev.fitness_before,
                ev.fitness_after,
                json.dumps(ev.details),
                now - timedelta(days=ev.days_ago),
            )
        )
    sql = """
        INSERT INTO evolution_log
            (id, event_type, playbook_id, parent_id, trigger_incident_id,
             fitness_before, fitness_after, details, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::JSONB, %s)
        ON CONFLICT (id) DO NOTHING
    """
    insert_batched(conn, sql, rows, label="evolution_log")
    return len(rows)


def refresh_statistics(conn: psycopg.Connection) -> None:
    """Recollect table statistics so the planner costs the seeded world correctly.

    Automatic stats collection is triggered by row-count change and can lag a
    bulk seed by long enough to matter: with stale statistics the optimizer
    estimates one row per table and picks a scan over the vector index.
    """
    log("refreshing statistics")
    for tbl in ("incidents", "precursor_snapshots", "playbooks",
                "institutional_playbooks", "evolution_log"):
        try:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '3s'")
                cur.execute(f"ANALYZE {tbl}")
        except Exception:
            log(f"   skipped ANALYZE {tbl}")
    log("   table statistics refreshed")


def write_backtest(w: world.World) -> int:
    """Persist the held-out set. Nothing here is ever written to the database."""
    BACKTEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BACKTEST_PATH.open("w") as fh:
        for inc in w.backtest_incidents:
            fh.write(json.dumps({
                "kind": "incident", "key": inc.key, "service": inc.service,
                "region": inc.region, "archetype": inc.archetype,
                "severity": inc.severity, "led_to_incident": True,
                "detected_at": inc.detected_at.isoformat(),
                "window_start": inc.window.window_start.isoformat(),
                "window_end": inc.window.window_end.isoformat(),
                "lead_time_minutes": inc.window.trajectory.precursor_minutes,
                "precursor_text": inc.window.precursor_text,
                "digest": inc.window.digest,
            }) + "\n")
        for win in w.backtest_negatives:
            fh.write(json.dumps({
                "kind": "negative", "key": win.key, "service": win.service,
                "region": win.region, "archetype": win.archetype,
                "led_to_incident": False,
                "window_start": win.window_start.isoformat(),
                "window_end": win.window_end.isoformat(),
                "precursor_text": win.precursor_text,
                "digest": win.digest,
            }) + "\n")
    total = len(w.backtest_incidents) + len(w.backtest_negatives)
    log(f"   demo/backtest_set.jsonl: {total} held-out windows "
        f"({len(w.backtest_incidents)} incidents, {len(w.backtest_negatives)} negatives)")
    return total


def write_manifest(w: world.World, counts: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps({
        "seed": w.seed,
        "anchor": w.anchor.isoformat(),
        "seeded_at": datetime.now(UTC).isoformat(),
        # Bedrock and the local embedder are different vector spaces. Recording
        # which one built this world is what makes a mismatch detectable instead
        # of quietly producing meaningless distances.
        "embedding_provider": embeddings.provider_name(),
        "embedding_dim": config.EMBEDDING_DIM,
        "counts": counts,
    }, indent=2, default=str) + "\n")
    log(f"   demo/seed_manifest.json written (provider: {embeddings.provider_name()})")


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the NEXUS demo world")
    parser.add_argument("--seed", type=int, default=world.DEFAULT_SEED)
    parser.add_argument("--dry-run", action="store_true",
                        help="build and report the world without touching the database")
    args = parser.parse_args()

    log(f"building world (seed={args.seed}, provider={embeddings.provider_name()})")
    w = world.build(seed=args.seed)
    log(f"   {len(w.incidents)} incidents, {len(w.negatives)} negatives generated")
    log(f"   holdout: {len(w.backtest_incidents)} incidents + "
        f"{len(w.backtest_negatives)} negatives withheld from the database")

    if args.dry_run:
        by_arch: dict[str, int] = {}
        for inc in w.incidents:
            by_arch[inc.archetype] = by_arch.get(inc.archetype, 0) + 1
        for key in sorted(by_arch):
            log(f"   {key:38s} {by_arch[key]:3d}")
        log("dry run: nothing written")
        return 0

    dsn = require_dsn()

    log("embedding the world")
    texts: dict[str, str] = {}
    for inc in w.seeded_incidents:
        texts[f"symptom::{inc.key}"] = inc.symptom_text
        texts[f"window::{inc.window.key}"] = inc.window.precursor_text
    for win in w.seeded_negatives:
        texts[f"window::{win.key}"] = win.precursor_text
    keys = list(texts)
    vectors_by_key = dict(zip(keys, embed_all([texts[k] for k in keys], "embeddings"),
                              strict=True))

    rng = np.random.default_rng(args.seed + 1)
    pb_vectors = playbook_vectors(w, vectors_by_key, rng)

    now = datetime.now(UTC).replace(microsecond=0)
    started = time.perf_counter()
    with psycopg.connect(dsn, autocommit=True) as conn:
        wipe(conn)
        reassert_zone_configs(conn)
        log("writing the world")
        incident_ids = seed_incidents(conn, w, vectors_by_key)
        n_precursors = seed_precursors(conn, w, vectors_by_key, incident_ids)
        playbook_ids = seed_playbooks(conn, pb_vectors, now)
        n_institutional = seed_institutional(conn, playbook_ids, now)
        n_events = seed_evolution_log(conn, playbook_ids, incident_ids, now)
        refresh_statistics(conn)

    n_backtest = write_backtest(w)
    write_manifest(w, {
        "incidents": len(incident_ids),
        "precursor_snapshots": n_precursors,
        "playbooks": len(playbook_ids),
        "institutional_playbooks": n_institutional,
        "evolution_log": n_events,
        "backtest_windows": n_backtest,
    })
    log(f"\nseeded in {time.perf_counter() - started:.1f}s — run `make verify` to check it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
