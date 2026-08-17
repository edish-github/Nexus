"""Dashboard read API: the only thing the frontend talks to.

Seven read endpoints and one control endpoint, routed in-code behind a single
Lambda Function URL. The contract they implement is `frontend/API_CONTRACT.md`.

Two rules shape everything here:

* Nothing is invented. Every field is a column value, or is derived from column
  values by arithmetic that is named in the response. Where the database has no
  value the response carries `null` and the frontend renders an em dash.
* Posteriors are derived at read time from the trial counters, never stored.
  `alpha`/`beta` on a prediction and `success_count`/`failure_count` on a
  playbook are the only inputs; mean and credible interval are computed here so
  every client agrees on the arithmetic.

Reads that tolerate a few seconds of staleness run
`AS OF SYSTEM TIME follower_read_timestamp()`, which serves them from the
nearest replica. The provenance replay and the live prediction lookups do not,
because those are the two places where staleness would change the answer.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

import numpy as np
import psycopg

from nexus_common import db, log

logger = log.get_logger("dashboard")

# k for every retrieval the dashboard performs, and the posterior mean above
# which the backtest counts a window as a positive prediction. Both are echoed
# in the responses that depend on them so a reader never has to guess.
RETRIEVAL_K = int(os.environ.get("DASHBOARD_RETRIEVAL_K", "14"))
BACKTEST_THRESHOLD = float(os.environ.get("DASHBOARD_BACKTEST_THRESHOLD", "0.62"))
BACKTEST_SAMPLE = int(os.environ.get("DASHBOARD_BACKTEST_SAMPLE", "30"))
BACKTEST_TTL_SECONDS = int(os.environ.get("DASHBOARD_BACKTEST_TTL", "300"))
GENERATOR_URL = os.environ.get("GENERATOR_URL", "").rstrip("/")
GENERATOR_TIMEOUT = float(os.environ.get("GENERATOR_TIMEOUT", "5"))

# Staleness is applied to the whole transaction rather than to a table
# reference. A per-table `AS OF SYSTEM TIME` re-evaluates
# `follower_read_timestamp()` once per mention, which a joined statement rejects
# as an inconsistent timestamp.
FOLLOWER = "follower_read_timestamp()"

CORS = {
    "Access-Control-Allow-Origin": os.environ.get("DASHBOARD_ALLOW_ORIGIN", "*"),
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Max-Age": "300",
}

# CockroachDB raises this when an AS OF SYSTEM TIME read reaches past the range's
# MVCC garbage-collection horizon. It is a normal outcome for an old prediction,
# not a bug, so the replay endpoint reports it as its own status.
_GC_SQLSTATE = "XXUUU"


# --------------------------------------------------------------------------- #
# Response plumbing
# --------------------------------------------------------------------------- #

def _resp(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **CORS},
        "body": json.dumps(body, default=_encode),
    }


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    return str(value)


def _iso(value: datetime | None) -> str | None:
    """RFC 3339 in UTC with a Z suffix. The frontend renders relative time."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _error(status: int, code: str, detail: str) -> dict:
    return _resp(status, {"error": code, "detail": detail})


def _rows_as_dicts(cur) -> list[dict]:
    names = [d.name for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


# Connection-level failures a pooled reader sees when the cluster is busy or a
# node drops a session. They are worth one retry with a fresh connection; a
# statement timeout is not, because retrying it just burns the request budget.
_RETRYABLE = ("40001", "40003", "08000", "08003", "08006", "57P01")


def _select(sql: str, params: tuple = (), *, as_of: str | None = None) -> list[dict]:
    """Run one read. `as_of` is a SQL time expression pinning the transaction.

    `SET TRANSACTION AS OF SYSTEM TIME` has to be the first statement in the
    transaction, which it is: psycopg opens the transaction lazily, so this is
    what starts it.
    """
    for attempt in (1, 2):
        try:
            with db.connection() as conn:
                with conn.cursor() as cur:
                    if as_of:
                        cur.execute(f"SET TRANSACTION AS OF SYSTEM TIME {as_of}")
                    cur.execute(sql, params)
                    out = _rows_as_dicts(cur)
                conn.commit()
                return out
        except psycopg.Error as e:
            state = getattr(e, "sqlstate", None)
            dropped = state is None and "connection" in str(e).lower()
            if attempt == 1 and (state in _RETRYABLE or dropped):
                logger.warning("retrying read", sqlstate=state, error=str(e)[:120])
                continue
            raise
    raise RuntimeError("unreachable")


def _try_select(sql: str, params: tuple = (), *, as_of: str | None = None,
                what: str) -> tuple[list[dict], str | None]:
    """A read that is allowed to fail without failing the request.

    Used for the panels that read `telemetry_embeddings`. That table is written
    by a live process and read at current time, so a stuck writer's open
    transaction can block it while every other table answers normally. When that
    happens the panel reports itself degraded and names the reason, which is
    what the UI puts on screen — it does not fall back to a plausible number.
    """
    try:
        return _select(sql, params, as_of=as_of), None
    except Exception as e:
        logger.warning("degraded read", what=what, error=str(e))
        return [], f"{what}: {str(e).splitlines()[0][:200]}"


# --------------------------------------------------------------------------- #
# Beta arithmetic — the one thing every screen shares
# --------------------------------------------------------------------------- #

_GRID = (np.arange(2000) + 0.5) / 2000


def _beta_stats(alpha: float, beta: float) -> dict:
    """Posterior mean and a 90% equal-tailed credible interval.

    Computed on a fixed grid rather than with an incomplete-beta inverse so the
    layer needs nothing beyond numpy. 2000 points puts the interval endpoints
    within 0.0005, which is finer than anything the UI displays.
    """
    a, b = float(alpha), float(beta)
    if a <= 0 or b <= 0:
        return {"posterior_mean": None, "ci_low": None, "ci_high": None}
    logpdf = (a - 1) * np.log(_GRID) + (b - 1) * np.log1p(-_GRID)
    weights = np.exp(logpdf - logpdf.max())
    cdf = np.cumsum(weights) / weights.sum()
    return {
        "posterior_mean": round(a / (a + b), 6),
        "ci_low": round(float(_GRID[int(np.searchsorted(cdf, 0.05))]), 6),
        "ci_high": round(float(_GRID[min(int(np.searchsorted(cdf, 0.95)), len(_GRID) - 1)]), 6),
    }


def _playbook_posterior(success: int, failure: int) -> dict:
    """Beta(s + 1, f + 1) — the flat prior a playbook is born with."""
    return _beta_stats(float(success) + 1.0, float(failure) + 1.0)


# --------------------------------------------------------------------------- #
# Shared row shaping
# --------------------------------------------------------------------------- #

def _shape_prediction(row: dict, regions: dict[str, str]) -> dict:
    out = {
        "id": str(row["id"]),
        "service_name": row["service_name"],
        "region": regions.get(row["service_name"]),
        "region_derived": True,
        "causal_pattern": row["causal_pattern"],
        "predicted_outcome": row["predicted_outcome"],
        "predicted_severity": row["predicted_severity"],
        "alpha": float(row["alpha"]),
        "beta": float(row["beta"]),
        "matching_precursor_count": row["matching_precursor_count"],
        "predicted_eta": _iso(row["predicted_eta"]),
        "prevention_status": row["prevention_status"],
        "awaiting_approval": bool(row.get("awaiting_approval")),
        "claimed_by": row["claimed_by"],
        "claimed_at": _iso(row["claimed_at"]),
        "playbook_applied": str(row["playbook_applied"]) if row["playbook_applied"] else None,
        "created_at": _iso(row["created_at"]),
        "resolved_at": _iso(row["resolved_at"]),
        "expires_at": _iso(row["expires_at"]),
        "commit_ts": str(row["commit_ts"]) if row.get("commit_ts") is not None else None,
    }
    out.update(_beta_stats(out["alpha"], out["beta"]))
    return out


def _shape_playbook(row: dict) -> dict:
    success, failure = int(row["success_count"]), int(row["failure_count"])
    lineage = [str(x) for x in (row.get("lineage") or [])]
    out = {
        "id": str(row["id"]),
        "name": row["name"],
        "outcome_category": row["outcome_category"],
        "generation": row["generation"],
        "memory_tier": row["memory_tier"],
        "status": row["status"],
        "reversible": row["reversible"],
        "success_count": success,
        "failure_count": failure,
        "trials": success + failure,
        "parent_id": str(row["parent_id"]) if row.get("parent_id") else None,
        "lineage": lineage,
        "ancestor_count": len(lineage),
        "region": row.get("region"),
        "locality": "GLOBAL" if row["memory_tier"] == "institutional" else "REGIONAL",
        "created_at": _iso(row.get("created_at")),
        "last_used_at": _iso(row.get("last_used_at")),
        "promoted_at": _iso(row.get("promoted_at")),
        "retired_at": _iso(row.get("retired_at")),
        "expires_at": _iso(row.get("expires_at")),
    }
    out.update(_playbook_posterior(success, failure))
    return out


def _node_class(row: dict) -> str:
    """The colour semantics, resolved once so the legend cannot drift from the node.

    Retirement and merge win over everything: a playbook that is out of service
    reads as out of service regardless of the posterior it died with.
    """
    if row["status"] != "active" or row["memory_tier"] == "retired":
        return "retired"
    if row["memory_tier"] == "institutional":
        return "institutional"
    mean = _playbook_posterior(row["success_count"], row["failure_count"])["posterior_mean"]
    if mean is None:
        return "experimental"
    if mean >= 0.75:
        return "proven"
    if mean >= 0.45:
        return "experimental"
    return "failing"


def _shape_step(index: int, step: dict) -> dict:
    return {
        "index": index,
        "action": step.get("action"),
        "target": step.get("target"),
        "params": step.get("params") or {},
        "inverse": step.get("inverse"),
    }


def _shape_event(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "event_type": row["event_type"],
        "playbook_id": str(row["playbook_id"]) if row.get("playbook_id") else None,
        "playbook_name": row.get("playbook_name"),
        "parent_id": str(row["parent_id"]) if row.get("parent_id") else None,
        "parent_name": row.get("parent_name"),
        "trigger_incident_id": (
            str(row["trigger_incident_id"]) if row.get("trigger_incident_id") else None
        ),
        "fitness_before": row.get("fitness_before"),
        "fitness_after": row.get("fitness_after"),
        "details": row.get("details") or {},
        "created_at": _iso(row["created_at"]),
    }


def _service_regions() -> dict[str, str]:
    """service_name -> region, from live telemetry first, seeded history second.

    `predictions` has no region column, so this is how a prediction acquires
    one. Responses that use it carry `region_derived: true`.
    """
    rows, _ = _try_select(
        """
        SELECT service_name, region, count(*) AS n, max(captured_at) AS latest
          FROM telemetry_embeddings
         GROUP BY service_name, region
        """,
        as_of=FOLLOWER,
        what="telemetry_embeddings region lookup",
    )
    out: dict[str, tuple[Any, str]] = {}
    for row in rows:
        key = (row["latest"], row["n"])
        if row["service_name"] not in out or key > out[row["service_name"]][0]:
            out[row["service_name"]] = (key, row["region"])
    historical = _select(
        """
        SELECT service_name, region, count(*) AS n
          FROM precursor_snapshots
         GROUP BY service_name, region
         ORDER BY service_name, n DESC
        """,
        as_of=FOLLOWER,
    )
    for row in historical:
        out.setdefault(row["service_name"], ((None, row["n"]), row["region"]))
    return {service: region for service, (_, region) in out.items()}


# --------------------------------------------------------------------------- #
# GET /overview
# --------------------------------------------------------------------------- #

# The topology does not change while the process lives, so it is resolved once
# and kept — including a failed resolution, which must not be retried on every
# poll.
_topology: dict | None = None


def _cluster() -> dict:
    """Topology straight out of the cluster.

    `SHOW DATABASES` rather than `SHOW REGIONS FROM DATABASE`: the two carry the
    same region list, but the latter reads per-region descriptors that contend
    with the TTL and stats jobs and can take longer than the whole request
    budget. Best effort either way — a locked-down role may not be allowed to
    run SHOW at all, and the header degrades rather than failing the request.
    """
    global _topology
    if _topology is None:
        resolved: dict[str, Any] = {"database": None, "regions": [], "survival_goal": None}
        try:
            row = _select(
                "SELECT * FROM [SHOW DATABASES] WHERE database_name = current_database()"
            )[0]
            primary = row.get("primary_region")
            resolved = {
                "database": row.get("database_name"),
                "regions": [
                    {"region": r, "primary": r == primary}
                    for r in (row.get("regions") or [])
                ],
                "survival_goal": row.get("survival_goal"),
            }
        except Exception as e:
            logger.warning("cluster topology unavailable", error=str(e))
        _topology = resolved

    out: dict[str, Any] = {**_topology, "logical_ts": None}
    try:
        out["logical_ts"] = _select("SELECT cluster_logical_timestamp()::STRING AS ts")[0]["ts"]
    except Exception as e:
        logger.warning("cluster_logical_timestamp unavailable", error=str(e))
    return out


def _fleet(
    regions: dict[str, str], open_predictions: dict[str, str]
) -> tuple[list[dict], list[str]]:
    """One tile per service the database knows about.

    The service list is discovered, not configured: whatever has telemetry or
    history is what the fleet is. A service with no telemetry inside the 2-hour
    sensory TTL reports `unknown`, which the UI renders as its own grey state —
    an unobserved service is not a healthy one.

    Returns the tiles and any degradation notices, which travel to the UI rather
    than being swallowed.
    """
    services = sorted(regions)
    rows, latest_err = _try_select(
        """
        SELECT DISTINCT ON (service_name)
               service_name, region, raw_metrics, captured_at
          FROM telemetry_embeddings
         ORDER BY service_name, captured_at DESC
        """,
        what="sensory tier (latest window per service)",
    )
    latest = {r["service_name"]: r for r in rows}

    count_rows, count_err = _try_select(
        "SELECT service_name, count(*) AS n FROM telemetry_embeddings GROUP BY service_name",
        what="sensory tier (sample counts)",
    )
    counts = {r["service_name"]: r["n"] for r in count_rows}
    degraded = [e for e in (latest_err, count_err) if e]

    tiles = []
    for service in services:
        row = latest.get(service)
        digest = (row or {}).get("raw_metrics") or {}
        series = digest.get("metrics") or {}
        summary = digest.get("summary") or {}
        # `metrics` in the digest is already downsampled to at most 24 points
        # per metric, oldest first — that is the sparkline.
        length = max((len(v) for v in series.values()), default=0)
        sparkline = [
            {name: values[i] for name, values in series.items() if i < len(values)}
            for i in range(length)
        ]
        latest_point = sparkline[-1] if sparkline else {}
        delta = None
        if length >= 2:
            key = "latency_p99_ms" if "latency_p99_ms" in series else next(iter(series), None)
            if key and series[key][0]:
                delta = round((series[key][-1] - series[key][0]) / series[key][0] * 100, 2)
        tiles.append(
            {
                "service_name": service,
                "region": (row or {}).get("region") or regions.get(service),
                "region_derived": row is None,
                # A blocked sensory read leaves this "unknown", never "healthy".
                "status": digest.get("status") or "unknown",
                "archetype": digest.get("archetype"),
                "progress": digest.get("progress"),
                "telemetry_samples": None if count_err else counts.get(service, 0),
                "last_sample_at": _iso((row or {}).get("captured_at")),
                "sparkline": sparkline,
                "latest": latest_point,
                "summary": summary,
                "delta_pct": delta,
                "open_prediction_id": open_predictions.get(service),
            }
        )
    return tiles, degraded


def _memory_and_counters() -> tuple[dict, dict, list[str]]:
    """The memory tiers and the headline counters, in as few round trips as possible.

    Every count below is independent, so they go out as one statement of
    scalar subqueries rather than one statement each. The dashboard polls every
    five seconds against a cluster that may be a continent away; the round trips
    cost more than the counts do.
    """
    tiers = _select(
        """
        SELECT (SELECT count(*) FROM precursor_snapshots) AS episodic,
               (SELECT count(*) FROM playbooks
                 WHERE status = 'active' AND memory_tier <> 'institutional') AS semantic,
               (SELECT count(*) FROM institutional_playbooks) AS institutional,
               (SELECT count(*) FILTER (WHERE was_prevented) FROM incidents) AS prevented,
               (SELECT count(*) FILTER (WHERE NOT was_prevented) FROM incidents) AS impacted
        """,
        as_of=FOLLOWER,
    )[0]
    # The sensory tier is not read at a follower timestamp: a 4.8s-stale count
    # of a 2-hour-TTL table is the one place staleness would show. That also
    # makes it the one count that a blocked writer can stall, so it is the one
    # count allowed to come back null.
    sensory, sensory_err = _try_select(
        "SELECT count(*) AS n FROM telemetry_embeddings",
        what="sensory tier (row count)",
    )
    memory = {
        "sensory": {"count": sensory[0]["n"] if sensory else None, "ttl": "2h",
                    "table": "telemetry_embeddings"},
        "episodic": {"count": tiers["episodic"], "ttl": None,
                     "table": "precursor_snapshots"},
        "semantic": {"count": tiers["semantic"], "ttl": "90d", "table": "playbooks"},
        "institutional": {"count": tiers["institutional"], "ttl": "GLOBAL",
                          "table": "institutional_playbooks"},
    }
    return memory, tiers, ([sensory_err] if sensory_err else [])


def _counters(incidents: dict) -> dict:
    pred = _select(
        """
        SELECT count(*) FILTER (WHERE prevention_status IN ('pending','preventing')) AS in_flight,
               count(*) FILTER (WHERE prevention_status = 'shadowed') AS shadowed
          FROM predictions
        """
    )[0]
    inc = incidents
    return {
        "prevented": {"value": inc["prevented"], "source": "incidents.was_prevented"},
        "impacted": {"value": inc["impacted"], "source": "incidents"},
        "in_flight": {"value": pred["in_flight"], "source": "predictions.prevention_status"},
        "shadowed": {"value": pred["shadowed"], "source": "predictions.prevention_status"},
    }


def _pipeline(prediction: dict) -> list[dict]:
    """The Oracle -> Sentinel -> Guardian -> Chronicler stepper.

    Every timestamp is a column value. A stage with no column to stand on is
    `pending` with `at: null`; nothing here advances on a timer.
    """
    claimed, applied = prediction["claimed_at"], prediction["playbook_applied"]
    resolved = prediction["resolved_at"]
    stages = [
        ("Oracle", True, prediction["created_at"], "prediction emitted",
         "predictions.created_at"),
        ("Sentinel", claimed is not None, claimed, prediction["claimed_by"],
         "predictions.claimed_at"),
        ("Guardian", applied is not None, claimed if applied else None,
         f"playbook {applied[:8]}" if applied else None, "predictions.playbook_applied"),
        ("Chronicler", resolved is not None, resolved, prediction["prevention_status"],
         "predictions.resolved_at"),
    ]
    out, seen_pending = [], False
    for agent, done, at, detail, column in stages:
        if done and not seen_pending:
            state = "done"
        elif not seen_pending:
            state, seen_pending = "active", True
        else:
            state = "pending"
        out.append({"agent": agent, "state": state, "at": at, "detail": detail,
                    "source_column": column})
    return out


def _centre(regions: dict[str, str]) -> dict:
    """The centre panel, resolved server-side into one of four states.

    The frontend has one branch per `kind` and no fallback logic of its own,
    which is what keeps it from ever rendering a plausible-looking placeholder.
    """
    active = _select(
        """
        SELECT *, crdb_internal_mvcc_timestamp::STRING AS commit_ts
          FROM predictions
         WHERE prevention_status IN ('pending','preventing')
         ORDER BY created_at DESC LIMIT 1
        """
    )
    if active:
        prediction = _shape_prediction(active[0], regions)
        return {"kind": "active_prediction", "heading": "ACTIVE PREDICTION",
                "prediction": prediction, "pipeline": _pipeline(prediction)}

    resolved = _select(
        """
        SELECT *, crdb_internal_mvcc_timestamp::STRING AS commit_ts
          FROM predictions
         WHERE resolved_at IS NOT NULL
         ORDER BY resolved_at DESC LIMIT 1
        """
    )
    if resolved:
        prediction = _shape_prediction(resolved[0], regions)
        return {"kind": "last_prediction", "heading": "LAST PREVENTION",
                "prediction": prediction, "pipeline": _pipeline(prediction)}

    # No prediction has ever been written. The prevention history that does
    # exist lives in `incidents`, so the panel shows the most recent one with
    # its precursor window rather than going blank.
    incident = _select(
        """
        SELECT id, title, severity, status, affected_services, crdb_region AS region,
               root_cause, was_predicted, was_prevented, was_auto_resolved,
               playbook_used, detected_at, resolved_at, mttr_seconds
          FROM incidents
         WHERE was_prevented
         ORDER BY detected_at DESC LIMIT 1
        """,
        as_of=FOLLOWER,
    )
    if not incident:
        return {"kind": "empty", "heading": "NO PREDICTION HISTORY",
                "reason": "predictions and incidents are both empty"}

    row = incident[0]
    precursor = _select(
        """
        SELECT id, outcome_category, window_start, window_end, metric_digest
          FROM precursor_snapshots
         WHERE incident_id = %s LIMIT 1
        """,
        (row["id"],),
        as_of=FOLLOWER,
    )
    window = None
    if precursor:
        p = precursor[0]
        digest = p["metric_digest"] or {}
        window = {
            "id": str(p["id"]),
            "outcome_category": p["outcome_category"],
            "window_start": _iso(p["window_start"]),
            "window_end": _iso(p["window_end"]),
            "lead_minutes": digest.get("precursor_minutes"),
            "metric_names": sorted((digest.get("metrics") or {}).keys()),
            "metrics": digest.get("metrics") or {},
        }
    return {
        "kind": "last_prevention",
        "heading": "LAST PREVENTION",
        "incident": {
            "id": str(row["id"]),
            "title": row["title"],
            "severity": row["severity"],
            "status": row["status"],
            "affected_services": list(row["affected_services"] or []),
            "region": row["region"],
            "root_cause": row["root_cause"],
            "was_predicted": row["was_predicted"],
            "was_prevented": row["was_prevented"],
            "was_auto_resolved": row["was_auto_resolved"],
            "playbook_used": str(row["playbook_used"]) if row["playbook_used"] else None,
            "detected_at": _iso(row["detected_at"]),
            "resolved_at": _iso(row["resolved_at"]),
            "mttr_seconds": row["mttr_seconds"],
            "precursor": window,
        },
    }


# Module-scope cache: the backtest is 30 vector searches, which is too much to
# repeat on a 5-second poll and too cheap to justify its own endpoint.
_backtest_cache: dict | None = None
_backtest_at: float = 0.0


def _backtest() -> dict | None:
    """Leave-one-out k-NN over a deterministic sample of the episodic tier.

    The held-out set in `demo/backtest_set.jsonl` is deliberately absent from
    the database, so it cannot be scored here. What this measures instead is
    honest and reproducible: each sampled window is matched against every other
    window, and the resulting posterior is compared to the label the window
    actually carries. `method` in the response names it.
    """
    global _backtest_cache, _backtest_at
    import time

    now = time.time()
    if _backtest_cache is not None and now - _backtest_at < BACKTEST_TTL_SECONDS:
        return _backtest_cache

    tp = fp = fn = tn = 0
    leads: list[float] = []
    # One connection and one pinned timestamp for the whole sweep. Opening a
    # transaction per neighbour search costs more round trips than the searches
    # themselves, and a moving timestamp would score each window against a
    # slightly different memory.
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET TRANSACTION AS OF SYSTEM TIME {FOLLOWER}")
            cur.execute(
                """
                SELECT id, led_to_incident, metric_digest->>'precursor_minutes' AS lead
                  FROM precursor_snapshots
                 ORDER BY id LIMIT %s
                """,
                (BACKTEST_SAMPLE,),
            )
            sample = _rows_as_dicts(cur)
            if not sample:
                conn.commit()
                return None
            for row in sample:
                cur.execute(
                    """
                    SELECT led_to_incident FROM precursor_snapshots
                     WHERE id <> %s
                     ORDER BY trajectory_embedding <=> (
                         SELECT trajectory_embedding FROM precursor_snapshots WHERE id = %s
                     )
                     LIMIT %s
                    """,
                    (row["id"], row["id"], RETRIEVAL_K),
                )
                neighbors = cur.fetchall()
                incidents = sum(1 for n in neighbors if n[0])
                mean = (incidents + 1) / (len(neighbors) + 2)
                positive = mean >= BACKTEST_THRESHOLD
                if row["led_to_incident"]:
                    if positive:
                        tp += 1
                        if row["lead"]:
                            leads.append(float(row["lead"]))
                    else:
                        fn += 1
                else:
                    fp += 1 if positive else 0
                    tn += 0 if positive else 1
        conn.commit()

    _backtest_cache = {
        "computed_at": _iso(datetime.now(UTC)),
        "method": "leave_one_out",
        "k": RETRIEVAL_K,
        "threshold": BACKTEST_THRESHOLD,
        "sample_size": len(sample),
        "true_positive": tp, "false_positive": fp,
        "false_negative": fn, "true_negative": tn,
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        "median_lead_minutes": round(float(np.median(leads)), 1) if leads else None,
    }
    _backtest_at = now
    return _backtest_cache


def get_overview() -> dict:
    regions = _service_regions()
    open_predictions = {
        r["service_name"]: str(r["id"])
        for r in _select(
            """
            SELECT DISTINCT ON (service_name) service_name, id
              FROM predictions
             WHERE prevention_status IN ('pending','preventing')
             ORDER BY service_name, created_at DESC
            """
        )
    }
    fleet, fleet_degraded = _fleet(regions, open_predictions)
    memory, incidents, memory_degraded = _memory_and_counters()
    return {
        "generated_at": _iso(datetime.now(UTC)),
        "read_at": "follower_read_timestamp()",
        "cluster": _cluster(),
        "counters": _counters(incidents),
        "fleet": fleet,
        "memory": memory,
        "centre": _centre(regions),
        "backtest": _backtest(),
        "evolution_feed": _evolution_events(limit=14),
        # Non-empty when a panel could not be read. The UI shows these verbatim
        # rather than rendering the affected panel as if it were healthy.
        "degraded": fleet_degraded + memory_degraded,
    }


# --------------------------------------------------------------------------- #
# GET /predictions and /predictions/{id}
# --------------------------------------------------------------------------- #

# `awaiting_approval` is not a stored status. It is a pending prediction whose
# selected playbook declares itself irreversible, which is exactly the condition
# the approval gate exists for.
_AWAITING = """
    p.prevention_status = 'pending'
    AND pb.id IS NOT NULL AND pb.reversible = false
"""

_PREDICTION_SELECT = f"""
    SELECT p.*, p.crdb_internal_mvcc_timestamp::STRING AS commit_ts,
           ({_AWAITING}) AS awaiting_approval
      FROM predictions p
      LEFT JOIN playbooks pb ON pb.id = p.playbook_applied
"""


def list_predictions(params: dict) -> dict:
    statuses = [s for s in (params.get("status") or "").split(",") if s]
    service = params.get("service")
    limit = min(int(params.get("limit") or 50), 200)

    where, args = [], []
    if "awaiting_approval" in statuses:
        where.append(f"({_AWAITING})")
        statuses = [s for s in statuses if s != "awaiting_approval"]
    if statuses:
        where.append("p.prevention_status = ANY(%s)")
        args.append(statuses)
    if service:
        where.append("p.service_name = %s")
        args.append(service)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    rows = _select(
        f"{_PREDICTION_SELECT} {clause} ORDER BY p.created_at DESC LIMIT %s",
        (*args, limit),
    )
    regions = _service_regions()
    return {
        "generated_at": _iso(datetime.now(UTC)),
        "total": len(rows),
        "predictions": [_shape_prediction(r, regions) for r in rows],
    }


# The retrieval statement, shown verbatim in the SQL panel. `$1` is the
# prediction's stored embedding; it is a subquery here so a 1024-dimension
# literal never crosses the wire.
_RETRIEVAL_SQL = """SELECT id, service_name, region, outcome_category, led_to_incident,
       window_start, window_end,
       trajectory_embedding <=> $1 AS distance
  FROM precursor_snapshots
 ORDER BY trajectory_embedding <=> $1
 LIMIT {k}"""

_RETRIEVAL_EXEC = """
    SELECT ps.id, ps.service_name, ps.region, ps.outcome_category, ps.led_to_incident,
           ps.window_start, ps.window_end,
           ps.metric_digest->>'precursor_minutes' AS lead_minutes,
           ps.trajectory_embedding <=> (
               SELECT current_embedding FROM predictions WHERE id = %s
           ) AS distance
      FROM precursor_snapshots ps
     ORDER BY ps.trajectory_embedding <=> (
               SELECT current_embedding FROM predictions WHERE id = %s
           )
     LIMIT %s
"""


def _neighbors(prediction_id: str, as_of: str | None = None) -> list[dict]:
    """The k nearest precursor windows to this prediction's stored embedding.

    `as_of` pins the whole transaction, so the prediction's embedding and the
    snapshots it is compared against are read at one timestamp. Pinning only
    the snapshot table would compare a current embedding against historical
    neighbours — a different question than the one the replay is asking.
    """
    rows = _select(
        _RETRIEVAL_EXEC, (prediction_id, prediction_id, RETRIEVAL_K), as_of=as_of
    )
    return [
        {
            "id": str(r["id"]),
            "service_name": r["service_name"],
            "region": r["region"],
            "outcome_category": r["outcome_category"],
            "led_to_incident": r["led_to_incident"],
            "distance": round(float(r["distance"]), 6),
            "similarity": round(1.0 - float(r["distance"]), 6),
            "lead_minutes": float(r["lead_minutes"]) if r["lead_minutes"] else None,
            "window_start": _iso(r["window_start"]),
            "window_end": _iso(r["window_end"]),
        }
        for r in rows
    ]


def _competition(prediction: dict) -> tuple[list[dict], str | None]:
    """The Thompson-sampling table, reconstructed from the candidates' counters.

    Sentinel's sampled theta is not persisted, so it cannot be shown after the
    fact. Rather than invent a draw, the score column reports the deterministic
    part — posterior mean times cosine similarity — and `competition_note`
    states which column is missing and why.
    """
    if not prediction["playbook_applied"]:
        return [], None
    rows = _select(
        """
        SELECT pb.id, pb.name, pb.generation, pb.success_count, pb.failure_count,
               pb.memory_tier, pb.status,
               pb.precursor_embedding <=> (
                   SELECT current_embedding FROM predictions WHERE id = %s
               ) AS distance
          FROM playbooks pb
         WHERE pb.outcome_category = %s AND pb.status = 'active'
         ORDER BY distance LIMIT 8
        """,
        (prediction["id"], prediction["causal_pattern"]),
        as_of=FOLLOWER,
    )
    out = []
    for r in rows:
        stats = _playbook_posterior(r["success_count"], r["failure_count"])
        similarity = round(1.0 - float(r["distance"]), 6)
        out.append(
            {
                "playbook_id": str(r["id"]),
                "name": r["name"],
                "generation": r["generation"],
                "memory_tier": r["memory_tier"],
                "success_count": r["success_count"],
                "failure_count": r["failure_count"],
                "posterior_mean": stats["posterior_mean"],
                "similarity": similarity,
                "sampled_theta": None,
                "score": round((stats["posterior_mean"] or 0) * similarity, 6),
                "winner": str(r["id"]) == prediction["playbook_applied"],
            }
        )
    note = (
        "Sentinel's sampled theta is drawn at selection time and is not persisted, "
        "so it cannot be replayed. SCORE here is the deterministic part of the "
        "selection: posterior mean x cosine similarity."
    )
    return out, note


def _execution(prediction: dict) -> dict:
    """The winning playbook's program with per-step state.

    Guardian does not persist a step cursor, so a step's state is only known at
    the two ends: everything is `applied` once the prediction resolved as
    prevented, `rolled_back` if it did not, and `unknown` while it is in flight.
    The UI shows `unknown` as an explicit state rather than as progress.
    """
    empty = {"playbook_id": None, "playbook_name": None, "reversible": None,
             "steps": [], "inverse_steps": []}
    if not prediction["playbook_applied"]:
        return empty
    rows = _select(
        """
        SELECT id, name, reversible, remediation_steps, inverse_steps
          FROM playbooks WHERE id = %s
        """,
        (prediction["playbook_applied"],),
        as_of=FOLLOWER,
    )
    if not rows:
        return empty
    row = rows[0]
    status = prediction["prevention_status"]
    state = ("applied" if status == "prevented"
             else "rolled_back" if status in ("missed", "false_alarm")
             else "unknown")
    return {
        "playbook_id": str(row["id"]),
        "playbook_name": row["name"],
        "reversible": row["reversible"],
        "steps": [
            {**_shape_step(i, s), "state": state}
            for i, s in enumerate(row["remediation_steps"] or [], start=1)
        ],
        "inverse_steps": [
            _shape_step(i, s) for i, s in enumerate(row["inverse_steps"] or [], start=1)
        ],
    }


def get_prediction(prediction_id: str) -> dict | None:
    rows = _select(f"{_PREDICTION_SELECT} WHERE p.id = %s", (prediction_id,))
    if not rows:
        return None
    prediction = _shape_prediction(rows[0], _service_regions())
    neighbors = _neighbors(prediction_id)
    incidents = sum(1 for n in neighbors if n["led_to_incident"])
    benign = len(neighbors) - incidents
    competition, note = _competition(prediction)
    return {
        "prediction": prediction,
        "retrieval_sql": _RETRIEVAL_SQL.format(k=RETRIEVAL_K),
        "retrieval_k": RETRIEVAL_K,
        "neighbors": neighbors,
        "posterior_derivation": {
            "incident_matches": incidents,
            "benign_matches": benign,
            "alpha_expression": f"incident_matches + 1 = {incidents + 1}",
            "beta_expression": f"benign_matches + 1 = {benign + 1}",
        },
        "competition": competition,
        "competition_note": note,
        "execution": _execution(prediction),
    }


def replay_prediction(prediction_id: str) -> tuple[int, dict]:
    """Re-read the decision's evidence at the prediction row's own MVCC timestamp.

    No column stores a commit timestamp, and none needs to:
    `crdb_internal_mvcc_timestamp` is the timestamp the row was written at, and
    it is exactly what `AS OF SYSTEM TIME` wants. The same statement then runs
    against current state, and the two are compared row for row.
    """
    rows = _select(
        """
        SELECT id, alpha, beta, created_at,
               crdb_internal_mvcc_timestamp::STRING AS commit_ts
          FROM predictions WHERE id = %s
        """,
        (prediction_id,),
    )
    if not rows:
        return 404, {"error": "not_found", "detail": f"no prediction {prediction_id}"}

    row = rows[0]
    commit_ts = str(row["commit_ts"])
    clause = f"AS OF SYSTEM TIME '{commit_ts}'"
    try:
        pinned = _neighbors(prediction_id, as_of=f"'{commit_ts}'")
    except Exception as e:
        state = getattr(e, "sqlstate", None)
        message = str(e)
        if state == _GC_SQLSTATE or "garbage collection" in message or "GC threshold" in message:
            return 409, {
                "error": "gc_threshold_exceeded",
                "detail": (
                    "This decision's commit timestamp is older than the range's MVCC "
                    "garbage-collection threshold, so the evidence can no longer be "
                    "read back at that timestamp."
                ),
                "commit_ts": commit_ts,
            }
        raise

    live = _neighbors(prediction_id)
    identical = [n["id"] for n in pinned] == [n["id"] for n in live] and all(
        abs(a["distance"] - b["distance"]) < 1e-9 for a, b in zip(pinned, live, strict=False)
    )

    def stats(neighbors: list[dict]) -> dict:
        incidents = sum(1 for n in neighbors if n["led_to_incident"])
        alpha, beta = incidents + 1.0, len(neighbors) - incidents + 1.0
        return {"alpha": alpha, "beta": beta, **_beta_stats(alpha, beta)}

    written_since = _select(
        "SELECT count(*) AS n FROM precursor_snapshots WHERE created_at > %s",
        (row["created_at"],),
    )[0]["n"]
    created = row["created_at"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)

    return 200, {
        "prediction_id": prediction_id,
        "commit_ts": commit_ts,
        "aost_clause": clause,
        "replayed_at": _iso(datetime.now(UTC)),
        "elapsed_since_commit_seconds": int((datetime.now(UTC) - created).total_seconds()),
        "identical": identical,
        "verdict": "BYTE-IDENTICAL" if identical else "DIVERGED",
        "panes": [
            {"title": "AT DECISION TIME", "clause": clause, **stats(pinned), "rows": pinned},
            {"title": "REPLAYED NOW", "clause": "same statement, no AS OF SYSTEM TIME",
             **stats(live), "rows": live},
        ],
        "drift": {"snapshots_written_since": written_since},
    }


# --------------------------------------------------------------------------- #
# GET /playbooks and /playbooks/{id}
# --------------------------------------------------------------------------- #

_PLAYBOOK_COLUMNS = """
    id, name, outcome_category, generation, memory_tier, status, reversible,
    success_count, failure_count, parent_id, lineage, crdb_region AS region,
    created_at, last_used_at, promoted_at, retired_at, expires_at
"""


def list_playbooks(params: dict) -> dict:
    where, args = [], []
    if params.get("tier"):
        where.append("memory_tier = %s")
        args.append(params["tier"])
    if params.get("status"):
        where.append("status = %s")
        args.append(params["status"])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    limit = min(int(params.get("limit") or 100), 300)

    rows = _select(
        f"SELECT {_PLAYBOOK_COLUMNS} FROM playbooks {clause} "
        f"ORDER BY generation, name LIMIT %s",
        (*args, limit),
        as_of=FOLLOWER,
    )
    counts = _select(
        """
        SELECT count(*) AS all_count,
               count(*) FILTER (WHERE status = 'active') AS active,
               count(*) FILTER (WHERE memory_tier = 'institutional') AS institutional,
               count(*) FILTER (WHERE status <> 'active') AS retired_or_merged
          FROM playbooks
        """,
        as_of=FOLLOWER,
    )[0]
    shaped = [_shape_playbook(r) for r in rows]
    shaped.sort(key=lambda p: (p["posterior_mean"] or 0), reverse=True)
    return {
        "generated_at": _iso(datetime.now(UTC)),
        "counts": {
            "all": counts["all_count"],
            "active": counts["active"],
            "institutional": counts["institutional"],
            "retired_or_merged": counts["retired_or_merged"],
        },
        "playbooks": shaped,
    }


def get_playbook(playbook_id: str) -> dict | None:
    rows = _select(
        f"""
        SELECT {_PLAYBOOK_COLUMNS}, remediation_steps, inverse_steps
          FROM playbooks WHERE id = %s
        """,
        (playbook_id,),
        as_of=FOLLOWER,
    )
    if not rows:
        return None
    row = rows[0]
    playbook = _shape_playbook(row)

    chain_ids = playbook["lineage"] + [playbook["id"]]
    chain = _select(
        f"SELECT {_PLAYBOOK_COLUMNS} FROM playbooks WHERE id = ANY(%s)",
        (chain_ids,),
        as_of=FOLLOWER,
    )
    by_id = {str(r["id"]): _shape_playbook(r) for r in chain}
    lineage = [by_id[i] for i in chain_ids if i in by_id]

    children = [
        _shape_playbook(r)
        for r in _select(
            f"SELECT {_PLAYBOOK_COLUMNS} FROM playbooks WHERE parent_id = %s",
            (playbook_id,),
        as_of=FOLLOWER,
    )
    ]
    institutional = _select(
        """
        SELECT id, name, generation, promoted_at, success_count, failure_count
          FROM institutional_playbooks WHERE source_playbook_id = %s
        """,
        (playbook_id,),
        as_of=FOLLOWER,
    )
    return {
        "playbook": playbook,
        "steps": [
            _shape_step(i, s) for i, s in enumerate(row["remediation_steps"] or [], start=1)
        ],
        "inverse_steps": [
            _shape_step(i, s) for i, s in enumerate(row["inverse_steps"] or [], start=1)
        ],
        "lineage": lineage,
        "children": children,
        "timeline": _evolution_events(limit=20, playbook_id=playbook_id),
        "institutional": (
            {
                "id": str(institutional[0]["id"]),
                "name": institutional[0]["name"],
                "generation": institutional[0]["generation"],
                "promoted_at": _iso(institutional[0]["promoted_at"]),
                "success_count": institutional[0]["success_count"],
                "failure_count": institutional[0]["failure_count"],
            }
            if institutional
            else None
        ),
    }


# --------------------------------------------------------------------------- #
# GET /evolution
# --------------------------------------------------------------------------- #

def _evolution_events(
    limit: int = 60, category: str | None = None, playbook_id: str | None = None
) -> list[dict]:
    where, args = [], []
    if category:
        where.append("pb.outcome_category = %s")
        args.append(category)
    if playbook_id:
        where.append("e.playbook_id = %s")
        args.append(playbook_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = _select(
        f"""
        SELECT e.id, e.event_type, e.playbook_id, e.parent_id, e.trigger_incident_id,
               e.fitness_before, e.fitness_after, e.details, e.created_at,
               pb.name AS playbook_name, parent.name AS parent_name
          FROM evolution_log e
          LEFT JOIN playbooks pb ON pb.id = e.playbook_id
          LEFT JOIN playbooks parent ON parent.id = e.parent_id
          {clause}
         ORDER BY e.created_at DESC LIMIT %s
        """,
        (*args, limit),
        as_of=FOLLOWER,
    )
    return [_shape_event(r) for r in rows]


def get_evolution(params: dict) -> dict:
    category = params.get("category") or None
    limit = min(int(params.get("limit") or 60), 300)

    where, args = [], []
    if category:
        where.append("outcome_category = %s")
        args.append(category)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    rows = _select(
        f"""
        SELECT id, name, outcome_category, generation, memory_tier, status,
               success_count, failure_count, parent_id, lineage
          FROM playbooks {clause}
        """,
        tuple(args),
        as_of=FOLLOWER,
    )
    present = {str(r["id"]) for r in rows}
    nodes, edges = [], []
    for r in rows:
        stats = _playbook_posterior(r["success_count"], r["failure_count"])
        node_id = str(r["id"])
        nodes.append(
            {
                "id": node_id,
                "name": r["name"],
                "outcome_category": r["outcome_category"],
                "generation": r["generation"],
                "memory_tier": r["memory_tier"],
                "status": r["status"],
                "posterior_mean": stats["posterior_mean"],
                "trials": r["success_count"] + r["failure_count"],
                "class": _node_class(r),
            }
        )
        parent = str(r["parent_id"]) if r["parent_id"] else None
        if parent and parent in present:
            edges.append({"id": f"e:{node_id}", "source": parent, "target": node_id,
                          "kind": "mutation" if r["generation"] > 1 else "parent"})
        # A merge shows up as a lineage entry that is not the declared parent:
        # the canonical child carries both absorbed branches in `lineage`.
        for ancestor in (str(x) for x in (r["lineage"] or [])):
            if ancestor in present and ancestor != parent:
                edges.append({"id": f"m:{ancestor}:{node_id}", "source": ancestor,
                              "target": node_id, "kind": "merge"})

    categories = [
        r["outcome_category"]
        for r in _select(
            "SELECT DISTINCT outcome_category FROM playbooks ORDER BY outcome_category",
            as_of=FOLLOWER,
        )
    ]
    counts = _select(
        "SELECT event_type, count(*) AS n FROM evolution_log GROUP BY event_type",
        as_of=FOLLOWER,
    )
    return {
        "generated_at": _iso(datetime.now(UTC)),
        "categories": categories,
        "nodes": nodes,
        "edges": edges,
        "events": _evolution_events(limit=limit, category=category),
        "event_counts": {r["event_type"]: r["n"] for r in counts},
    }


# --------------------------------------------------------------------------- #
# POST /fleet/ramp
# --------------------------------------------------------------------------- #

def start_ramp(payload: dict) -> tuple[int, dict]:
    """Forward a ramp request to the synthetic fleet's control API.

    The fleet simulator holds its state in memory in `generator/live.py`, so
    this is a proxy rather than a reimplementation. When the generator is not
    running the caller is told exactly that; nothing here pretends a ramp
    started.
    """
    service = payload.get("service")
    if not service:
        return 400, {"error": "bad_request", "detail": "`service` is required"}
    if not GENERATOR_URL:
        return 503, {
            "error": "generator_not_configured",
            "detail": "GENERATOR_URL is not set on the dashboard Lambda.",
        }

    body = {"service": service, "speed": payload.get("speed", 4)}
    if payload.get("archetype"):
        body["archetype"] = payload["archetype"]
    request = urllib.request.Request(  # noqa: S310 — URL comes from our own config
        f"{GENERATOR_URL}/ramp",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=GENERATOR_TIMEOUT) as response:  # noqa: S310
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"error": "generator_rejected", "detail": e.read().decode()[:400]}
    except Exception as e:
        return 503, {
            "error": "generator_unreachable",
            "detail": (
                f"No response from the fleet generator at {GENERATOR_URL}. "
                f"Start it with `make live`. ({e})"
            ),
        }
    return 202, {
        "accepted": True,
        "service": result.get("service", service),
        "archetype": result.get("archetype"),
        "speed": result.get("speed"),
        "status": result.get("status"),
        "note": "Oracle samples the sensory tier on a 60s cadence.",
    }


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

def _params(event: dict) -> dict:
    return {k: v for k, v in (event.get("queryStringParameters") or {}).items() if v is not None}


def _route(method: str, path: str, event: dict) -> dict:
    segments = [s for s in path.strip("/").split("/") if s]

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": ""}

    if method == "POST":
        if segments == ["fleet", "ramp"]:
            raw = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                import base64

                raw = base64.b64decode(raw).decode()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return _error(400, "bad_request", "body is not valid JSON")
            status, body = start_ramp(payload)
            return _resp(status, body)
        return _error(404, "not_found", f"no route for POST /{'/'.join(segments)}")

    if method != "GET":
        return _error(405, "bad_request", f"{method} is not supported")

    if segments in ([], ["health"]):
        return _resp(200, {"ok": True, "service": "nexus-dashboard",
                           "generator_configured": bool(GENERATOR_URL)})
    if segments == ["overview"]:
        return _resp(200, get_overview())
    if segments == ["predictions"]:
        return _resp(200, list_predictions(_params(event)))
    if len(segments) == 2 and segments[0] == "predictions":
        found = get_prediction(segments[1])
        return _resp(200, found) if found else _error(404, "not_found", "no such prediction")
    if len(segments) == 3 and segments[0] == "predictions" and segments[2] == "replay":
        status, body = replay_prediction(segments[1])
        return _resp(status, body)
    if segments == ["playbooks"]:
        return _resp(200, list_playbooks(_params(event)))
    if len(segments) == 2 and segments[0] == "playbooks":
        found = get_playbook(segments[1])
        return _resp(200, found) if found else _error(404, "not_found", "no such playbook")
    if segments == ["evolution"]:
        return _resp(200, get_evolution(_params(event)))

    return _error(404, "not_found", f"no route for GET /{'/'.join(segments)}")


def handler(event: dict, _context=None) -> dict:
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method") or event.get("httpMethod") or "GET"
    path = http.get("path") or event.get("rawPath") or "/"
    try:
        return _route(method, path, event)
    except Exception as e:
        # A dead connection pool is the expected failure here. It is reported
        # with its own code so the dashboard can show "memory layer unreachable"
        # instead of a generic error, and keep the last good data on screen.
        logger.error("request failed", method=method, path=path, error=str(e))
        return _error(503, "db_unreachable", str(e)[:400])
