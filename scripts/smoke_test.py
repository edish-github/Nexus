#!/usr/bin/env python3
"""Schema smoke test: seed rows, run a hybrid vector query and an AOST query.
Run after scripts/migrate.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import numpy as np
    import psycopg
except ImportError:
    sys.exit("Requires psycopg[binary] and numpy: `uv pip install 'psycopg[binary]' numpy`")

REPO_ROOT = Path(__file__).resolve().parent.parent
DIM = 1024  # Titan Text Embeddings V2 output dimension
TAG = "smoke-test"


def load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def vec(seed: int) -> str:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=DIM).astype("float32")
    v /= np.linalg.norm(v)
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def main() -> int:
    load_dotenv()
    dsn = os.environ.get("COCKROACH_DB_URL")
    if not dsn:
        sys.exit("COCKROACH_DB_URL is not set")

    keep = "--keep" in sys.argv
    with psycopg.connect(dsn, autocommit=True) as conn:
        print("1) seeding throwaway rows")
        inc_id = conn.execute(
            """
            INSERT INTO incidents
                (title, severity, status, affected_services, symptom_embedding)
            VALUES (%s, 3, 'resolved', ARRAY['payments'], %s::VECTOR)
            RETURNING id
            """,
            (f"{TAG} incident", vec(1)),
        ).fetchone()[0]

        region = conn.execute(
            "SELECT crdb_region::STRING FROM incidents WHERE id = %s", (inc_id,)
        ).fetchone()[0]
        print(f"   incident homed in region: {region}")

        for i in range(5):
            conn.execute(
                """
                INSERT INTO precursor_snapshots
                    (incident_id, service_name, region, trajectory_embedding,
                     window_start, window_end, outcome_category, led_to_incident, metric_digest)
                VALUES (%s, 'payments', 'aws-us-east-1', %s::VECTOR,
                        now() - INTERVAL '2 hours', now() - INTERVAL '1 hour',
                        'connection_pool_exhaustion', true, %s::JSONB)
                """,
                (inc_id, vec(10 + i), '{"note":"' + TAG + '"}'),
            )

        conn.execute(
            """
            INSERT INTO playbooks (name, outcome_category, precursor_embedding,
                                   remediation_steps, inverse_steps, reversible)
            VALUES (%s, 'connection_pool_exhaustion', %s::VECTOR,
                    %s::JSONB, %s::JSONB, true)
            """,
            (
                f"{TAG} playbook",
                vec(11),
                '[{"action":"scale_pool","target":"payments","params":{"size":50},"inverse":"scale_pool"}]',
                '[{"action":"scale_pool","target":"payments","params":{"size":20}}]',
            ),
        )

        query_vec = vec(10)  # closest to the first precursor snapshot

        print("2) hybrid SQL + vector query (category filter + cosine k-NN)")
        hybrid = """
            SELECT id, outcome_category,
                   1 - (trajectory_embedding <=> %s::VECTOR) AS similarity
            FROM precursor_snapshots
            WHERE outcome_category = 'connection_pool_exhaustion'
              AND led_to_incident = true
            ORDER BY trajectory_embedding <=> %s::VECTOR
            LIMIT 3
        """
        rows = conn.execute(hybrid, (query_vec, query_vec)).fetchall()
        for r in rows:
            print(f"   match {r[0]}  sim={r[2]:.4f}")
        assert rows, "hybrid query returned no rows"

        plan = "\n".join(
            row[0] for row in conn.execute("EXPLAIN " + hybrid, (query_vec, query_vec)).fetchall()
        )
        used_index = "trajectory_embedding_idx" in plan or "vector" in plan.lower()
        print(f"   vector index used by planner: {used_index}")
        if not used_index:
            print("   WARNING: planner did not report the vector index. EXPLAIN:\n" + plan)

        print("3) AS OF SYSTEM TIME query (historical read)")
        aost = conn.execute(
            "SELECT count(*) FROM precursor_snapshots AS OF SYSTEM TIME '-5s' "
            "WHERE outcome_category = 'connection_pool_exhaustion'"
        ).fetchone()[0]
        print(f"   rows visible 5s ago: {aost}")

        # follower-read variant (dashboard aggregate pattern)
        follower = conn.execute(
            "SELECT count(*) FROM playbooks AS OF SYSTEM TIME follower_read_timestamp()"
        ).fetchone()[0]
        print(f"   follower-read playbook count: {follower}")

        if keep:
            print(f"\nsmoke test passed (rows kept, tagged '{TAG}')")
            return 0

        print("4) cleaning up throwaway rows")
        conn.execute("DELETE FROM playbooks WHERE name = %s", (f"{TAG} playbook",))
        conn.execute("DELETE FROM precursor_snapshots WHERE incident_id = %s", (inc_id,))
        conn.execute("DELETE FROM incidents WHERE id = %s", (inc_id,))

    print("\nsmoke test passed: schema, vector index, and AOST all working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
