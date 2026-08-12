"""CockroachDB access helpers (psycopg3).

A module-level connection pool is created lazily at cold start and reused across
warm Lambda invocations. Retries wrap transient serialization and connection
errors that CockroachDB can raise under contention.
"""
from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None

# CockroachDB retryable states: 40001 (serialization failure), 40003, 08xxx (conn)
_RETRYABLE = ("40001", "40003", "08000", "08003", "08006")


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=config.db_dsn(),
            min_size=1,
            max_size=4,
            kwargs={"autocommit": False, "application_name": "nexus"},
            open=True,
        )
    return _pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


def query(sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
    """Run a read query in autocommit and return all rows."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.commit()
        return rows


def query_at(ts_expr: str, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
    """Run a read query AS OF SYSTEM TIME <ts_expr> (provenance / follower reads).

    `ts_expr` is a SQL time expression, e.g. a decimal HLC string from
    `cluster_logical_timestamp()`, "'-10s'", or "follower_read_timestamp()".
    It is interpolated (not parameterized) because AOST does not accept
    placeholders; pass only trusted, server-derived values.
    """
    if "{AOST}" not in sql:
        raise ValueError("query_at requires a single {AOST} marker in the SQL")
    stitched = sql.replace("{AOST}", f"AS OF SYSTEM TIME {ts_expr}")
    return query(stitched, params)


def tx_retry(fn, *, attempts: int = 5):
    """Run `fn(conn)` inside a serializable transaction, retrying on 40001.

    `fn` receives an open connection; return value is passed back. The
    transaction commits if `fn` returns normally, rolls back on exception.
    """
    delay = 0.05
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with connection() as conn:
                result = fn(conn)
                conn.commit()
                return result
        except psycopg.Error as e:  # noqa: PERF203
            state = getattr(e, "sqlstate", None)
            last = e
            if state in _RETRYABLE and attempt < attempts:
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
                continue
            raise
    raise last  # pragma: no cover


def commit_timestamp(conn: psycopg.Connection) -> str:
    """Return the current transaction's logical commit timestamp as a string.

    Persist this alongside a prediction so its evidence can be replayed later
    with `AS OF SYSTEM TIME <ts>`.
    """
    return conn.execute("SELECT cluster_logical_timestamp()::STRING").fetchone()[0]
