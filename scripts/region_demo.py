#!/usr/bin/env python3
"""The region-kill beat: a transaction that commits while a region is dead.

    make region-config     read the survival configuration off the Cloud cluster
    make region-up         bring the local three-region cluster up
    make region-demo       open a transaction, kill a region, commit anyway
    make region-down       tear the local cluster down

Two modes, because the claim has two halves and only one of them can be shown on
a managed cluster.

**`--cloud`** reads the real thing. NEXUS runs on CockroachDB Cloud across three
regions with `SURVIVE REGION FAILURE`, and that configuration is a fact about the
cluster, not a slide: this mode prints the regions, the survival goal, the table
localities, and where the replicas of a `REGIONAL BY ROW` range actually live. No
node is harmed.

**`--local`** breaks something. A managed cluster does not offer "stop these
nodes now", and a survival guarantee nobody can watch fail is not a
demonstration — so the local compose cluster runs one node per simulated region
and this mode pulls the plug on one of them *while a serializable transaction is
open*, then commits it. Same engine, same Raft, same guarantee; the plug is
merely reachable.

The transaction is not a toy. It is the shape of Sentinel's competition: read the
candidates under `FOR UPDATE`, decide, write the outcome — the exact operation
that must not lose its mind when infrastructure disappears underneath it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from _env import bootstrap, require_dsn

bootstrap()

import psycopg  # noqa: E402

from nexus_common import db  # noqa: E402

COMPOSE = Path(__file__).resolve().parents[1] / "demo" / "docker-compose.region-kill.yml"
LOCAL_DSN = "postgresql://root@127.0.0.1:26257/defaultdb?sslmode=disable"
# The node that gets killed, and the region it stands for.
VICTIM = "nexus-region-a"
VICTIM_REGION = "aws-us-east-1"
NODES = ("nexus-region-a", "nexus-region-b", "nexus-region-c")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say(f"\n{'─' * 74}\n{title}\n{'─' * 74}")


class Check:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def that(self, condition: bool, claim: str, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            say(f"   PASS  {claim}")
        else:
            self.failures.append(claim)
            say(f"   FAIL  {claim}" + (f"  ({detail})" if detail else ""))
        return condition


def docker(*args: str, check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=check
    )


def compose(*args: str, **kw) -> subprocess.CompletedProcess:
    return docker("compose", "-f", str(COMPOSE), *args, **kw)


def require_docker() -> None:
    try:
        docker("info")
    except FileNotFoundError:
        raise SystemExit("docker is not installed; the region-kill cluster needs it") from None
    except subprocess.CalledProcessError:
        raise SystemExit(
            "the docker daemon is not running. Start Docker Desktop and try again — "
            "this beat needs a cluster whose plug can be pulled."
        ) from None


# --------------------------------------------------------------------------- #
# --cloud: read the configuration off the real cluster
# --------------------------------------------------------------------------- #

def verify_cloud() -> int:
    require_dsn()
    check = Check()
    rule("survival configuration · CockroachDB Cloud")

    def read(conn):
        out: dict = {}
        out["regions"] = conn.execute(
            "SELECT region FROM [SHOW REGIONS FROM DATABASE nexus]").fetchall()
        out["survival"] = conn.execute(
            "SELECT survival_goal FROM [SHOW DATABASES] WHERE database_name = 'nexus'"
        ).fetchone()
        out["localities"] = conn.execute(
            """
            SELECT table_name, locality FROM [SHOW TABLES FROM public]
            WHERE locality IS NOT NULL ORDER BY table_name
            """
        ).fetchall()
        # Where the replicas of one REGIONAL BY ROW range actually live. This is
        # the claim underneath the survival goal: three replicas, three regions.
        out["ranges"] = conn.execute(
            """
            SELECT replicas, replica_localities
            FROM [SHOW RANGES FROM TABLE playbooks WITH DETAILS]
            LIMIT 1
            """
        ).fetchone()
        return out

    facts = db.tx_retry(read)

    regions = [r[0] for r in facts["regions"]]
    say(f"   regions     {', '.join(regions)}")
    check.that(len(regions) >= 3, "the database spans at least three regions",
               f"found {len(regions)}")

    goal = (facts["survival"] or [None])[0]
    say(f"   survival    {goal}")
    check.that(goal == "region", "the survival goal is SURVIVE REGION FAILURE",
               f"got {goal!r}")

    by_row = [t for t, loc in facts["localities"] if "REGIONAL BY ROW" in (loc or "")]
    global_tables = [t for t, loc in facts["localities"] if (loc or "").strip() == "GLOBAL"]
    say(f"   by row      {', '.join(by_row) or '—'}")
    say(f"   global      {', '.join(global_tables) or '—'}")
    check.that(bool(by_row), "at least one table is REGIONAL BY ROW")
    check.that(bool(global_tables), "at least one table is LOCALITY GLOBAL")

    if facts["ranges"]:
        replicas, localities = facts["ranges"]
        say(f"   replicas    {replicas}")
        for loc in (localities or []):
            say(f"               {loc}")
        spread = {str(loc).split(",")[0] for loc in (localities or [])}
        check.that(len(spread) >= 3,
                   "one sampled range has replicas in three distinct regions",
                   f"found {len(spread)}: {spread}")

    rule("summary")
    say(f"   {check.passed} check(s) passed, {len(check.failures)} failed")
    say("   This is configuration, read live. Watching it survive needs a cluster "
        "whose plug is reachable — `make region-up && make region-demo`.")
    return 1 if check.failures else 0


# --------------------------------------------------------------------------- #
# --up / --down: the local cluster
# --------------------------------------------------------------------------- #

def local_sql(sql: str, *, dsn: str = LOCAL_DSN, timeout: int = 10) -> list[tuple]:
    with psycopg.connect(dsn, connect_timeout=timeout, autocommit=True) as conn:
        cur = conn.execute(sql)
        return cur.fetchall() if cur.description else []


def bring_up() -> int:
    require_docker()
    rule("local three-region cluster")
    say("   starting three nodes, one per simulated region…")
    compose("up", "-d")

    # `cockroach init` is only needed once, and is an error afterwards.
    for attempt in range(30):
        try:
            docker("exec", VICTIM, "./cockroach", "init", "--insecure",
                   "--host=nexus-region-a:26357")
            say("   cluster initialized")
            break
        except subprocess.CalledProcessError as e:
            blob = (e.stdout or "") + (e.stderr or "")
            if "already been initialized" in blob:
                say("   cluster already initialized")
                break
            if attempt == 29:
                say(f"   init failed: {blob.strip()[:300]}")
                return 1
            time.sleep(2)

    for attempt in range(30):
        try:
            nodes = local_sql("SELECT count(*) FROM crdb_internal.gossip_nodes "
                              "WHERE is_live")[0][0]
            if int(nodes) >= 3:
                say(f"   {nodes} live nodes")
                break
        except psycopg.Error:
            pass
        if attempt == 29:
            say("   the cluster did not reach three live nodes")
            return 1
        time.sleep(2)

    for locality in local_sql(
        "SELECT node_id, locality FROM crdb_internal.gossip_nodes ORDER BY node_id"
    ):
        say(f"      n{locality[0]}  {locality[1]}")
    say("\n   ready. `make region-demo` opens a transaction and kills "
        f"{VICTIM_REGION}.")
    return 0


def bring_down() -> int:
    require_docker()
    rule("tearing down the local cluster")
    compose("down", "-v")
    say("   gone, including its volumes")
    return 0


# --------------------------------------------------------------------------- #
# --local: the survival proof
# --------------------------------------------------------------------------- #

SETUP = """
CREATE DATABASE IF NOT EXISTS nexus_kill;
CREATE TABLE IF NOT EXISTS nexus_kill.playbooks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL,
    success_count INT NOT NULL DEFAULT 0,
    failure_count INT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nexus_kill.evolution_log (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type TEXT NOT NULL,
    details    JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def seed_local() -> None:
    for statement in filter(None, (s.strip() for s in SETUP.split(";"))):
        local_sql(statement)
    local_sql("DELETE FROM nexus_kill.playbooks")
    local_sql("DELETE FROM nexus_kill.evolution_log")
    local_sql(
        """
        INSERT INTO nexus_kill.playbooks (name, success_count, failure_count) VALUES
            ('Adaptive pool with breaker', 17, 1),
            ('Pool bump with connection drain', 14, 6),
            ('Predictive pool pre-scale', 0, 0)
        """
    )
    # Three replicas of everything, spread one per region. Without this the
    # default zone config could put two replicas in one region and the survival
    # claim would be a coin flip.
    local_sql("ALTER DATABASE nexus_kill CONFIGURE ZONE USING num_replicas = 3, "
              "constraints = '{}', lease_preferences = '[]'")


def survival_proof() -> int:
    require_docker()
    check = Check()
    rule("region-kill · a competition that commits through a region failure")

    seed_local()
    before = local_sql("SELECT count(*) FROM crdb_internal.gossip_nodes WHERE is_live")[0][0]
    say(f"   {before} live nodes, one per region")

    survivor_dsn = LOCAL_DSN.replace(":26257", ":26258")

    # The transaction stays open across the kill. Autocommit off, and the read
    # takes row locks exactly as Sentinel's claim does.
    conn = psycopg.connect(survivor_dsn, connect_timeout=10)
    conn.autocommit = False
    started = time.time()
    candidates = conn.execute(
        "SELECT id::STRING, name, success_count, failure_count FROM nexus_kill.playbooks "
        "ORDER BY name FOR UPDATE"
    ).fetchall()
    say(f"   transaction open · {len(candidates)} candidates read FOR UPDATE")
    check.that(len(candidates) == 3, "the competition read its candidates")

    say(f"\n   killing {VICTIM} ({VICTIM_REGION})…")
    docker("stop", VICTIM, timeout=60)
    time.sleep(2)
    say(f"   {VICTIM} is down")

    # Read from a surviving node: the cluster must still answer.
    live = before
    for _ in range(15):
        try:
            live = local_sql("SELECT count(*) FROM crdb_internal.gossip_nodes WHERE is_live",
                             dsn=survivor_dsn, timeout=20)[0][0]
            if int(live) < int(before):
                break
        except psycopg.Error:
            pass
        time.sleep(1)
    say(f"   surviving nodes report {live} live")
    check.that(int(live) < int(before), "the cluster noticed the region is gone")

    # Now finish the transaction that was already open when the region died.
    winner = max(candidates, key=lambda c: (c[2] + 1) / (c[2] + c[3] + 2))
    try:
        conn.execute(
            "UPDATE nexus_kill.playbooks SET success_count = success_count + 1 WHERE id = %s",
            (winner[0],),
        )
        conn.execute(
            "INSERT INTO nexus_kill.evolution_log (event_type, details) "
            "VALUES ('competition', %s::JSONB)",
            (json.dumps({"winner": winner[1], "killed_region": VICTIM_REGION,
                         "note": "committed while a region was down"}),),
        )
        conn.commit()
        elapsed = time.time() - started
        say(f"\n   COMMITTED after {elapsed:.1f}s, spanning the region failure")
        check.that(True, "the open transaction committed with a region dead")
    except psycopg.Error as e:
        check.that(False, "the open transaction committed with a region dead", str(e)[:200])
    finally:
        conn.close()

    rows = local_sql("SELECT event_type, details->>'winner' FROM nexus_kill.evolution_log",
                     dsn=survivor_dsn, timeout=20)
    say(f"   evolution_log now holds {len(rows)} row(s): "
        + ", ".join(f"{r[0]}({r[1]})" for r in rows))
    check.that(len(rows) == 1, "the write is durable and visible from a surviving region")

    say(f"\n   restoring {VICTIM}…")
    docker("start", VICTIM, timeout=60)
    for _ in range(30):
        time.sleep(2)
        try:
            live = local_sql("SELECT count(*) FROM crdb_internal.gossip_nodes "
                             "WHERE is_live")[0][0]
            if int(live) >= int(before):
                say(f"   {live} live nodes again")
                check.that(True, "the region rejoined without operator intervention")
                break
        except psycopg.Error:
            continue
    else:
        check.that(False, "the region rejoined without operator intervention",
                   "still short of the original node count")

    rule("summary")
    say(f"   {check.passed} check(s) passed, {len(check.failures)} failed")
    if check.failures:
        for f in check.failures:
            say(f"   · {f}")
    return 1 if check.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="The region-kill demo beat")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--cloud", action="store_true",
                      help="read the survival configuration off the Cloud cluster")
    mode.add_argument("--up", action="store_true", help="start the local three-region cluster")
    mode.add_argument("--local", action="store_true",
                      help="kill a region mid-transaction and commit anyway")
    mode.add_argument("--down", action="store_true", help="tear the local cluster down")
    args = parser.parse_args()

    if args.cloud:
        return verify_cloud()
    if args.up:
        return bring_up()
    if args.down:
        return bring_down()
    return survival_proof()


if __name__ == "__main__":
    raise SystemExit(main())
