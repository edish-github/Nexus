#!/usr/bin/env python3
"""NEXUS migration runner — idempotent, numbered .sql files.

Usage:
    python scripts/migrate.py                 # apply all pending migrations
    python scripts/migrate.py --dry-run       # list what would run, no changes
    python scripts/migrate.py --only 001      # apply just files starting with 001
    python scripts/migrate.py --file sql/changefeed.sql   # run an ad-hoc file

Requires COCKROACH_DB_URL in the environment (or a .env file next to this repo).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "sql"
MIGRATION_RE = re.compile(r"^(\d{3})_.*\.sql$")


def load_dotenv() -> None:
    """Minimal .env loader so the script works without extra deps."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements.

    Strips `--` line comments and splits on top-level `;`, respecting
    single-quoted string literals (including the '' escaped-quote form).
    Sufficient for NEXUS's schema files (no dollar-quoting, no `;` inside
    string literals).
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_string = False
    while i < n:
        ch = sql[i]
        if in_string:
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":  # escaped quote ''
                    buf.append("''")
                    i += 2
                    continue
                in_string = False
            buf.append(ch)
            i += 1
            continue
        # not in a string
        if ch == "'":
            in_string = True
            buf.append(ch)
            i += 1
        elif ch == "-" and i + 1 < n and sql[i + 1] == "-":  # line comment
            j = sql.find("\n", i)
            i = n if j == -1 else j
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def ensure_bookkeeping(conn) -> set[str]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def discover(only: str | None) -> list[Path]:
    files = sorted(p for p in SQL_DIR.iterdir() if MIGRATION_RE.match(p.name))
    if only:
        files = [p for p in files if p.name.startswith(only)]
    return files


def apply_file(conn, path: Path, dry_run: bool) -> None:
    import psycopg

    statements = split_statements(path.read_text())
    print(f"→ {path.name}  ({len(statements)} statements)")
    if dry_run:
        for s in statements:
            print(f"    {s.splitlines()[0][:100]} …")
        return
    for s in statements:
        try:
            conn.execute(s)
        except psycopg.Error as e:
            print(f"  ✗ statement failed:\n    {s[:200]}\n  {e}", file=sys.stderr)
            raise
    conn.execute(
        "INSERT INTO schema_migrations (filename) VALUES (%s) "
        "ON CONFLICT (filename) DO NOTHING",
        (path.name,),
    )
    print(f"  ✓ applied {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="NEXUS migration runner")
    parser.add_argument("--dry-run", action="store_true", help="print, don't apply")
    parser.add_argument("--only", help="only files starting with this prefix (e.g. 001)")
    parser.add_argument("--file", help="run one ad-hoc .sql file (not tracked)")
    args = parser.parse_args()

    try:
        import psycopg
    except ImportError:
        sys.exit("psycopg (v3) is required: `uv pip install 'psycopg[binary]'`")

    load_dotenv()
    dsn = os.environ.get("COCKROACH_DB_URL")
    if not dsn:
        sys.exit("COCKROACH_DB_URL is not set (put it in .env or the environment)")

    # autocommit: CRDB DDL such as ADD REGION / CONFIGURE ZONE cannot run inside
    # an explicit transaction.
    with psycopg.connect(dsn, autocommit=True) as conn:
        if args.file:
            apply_file(conn, (REPO_ROOT / args.file).resolve(), args.dry_run)
            return 0

        applied = ensure_bookkeeping(conn)
        pending = [p for p in discover(args.only) if p.name not in applied]
        if not pending:
            print("Nothing to do — all migrations already applied.")
            return 0
        print(f"{len(pending)} pending migration(s): {', '.join(p.name for p in pending)}")
        for path in pending:
            apply_file(conn, path, args.dry_run)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
