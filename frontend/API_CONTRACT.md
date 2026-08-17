# NEXUS dashboard API contract

The dashboard is a static bundle. Every value it renders comes from this API;
nothing is computed from a constant baked into the frontend.

Base URL comes from `VITE_API_BASE_URL` (no trailing slash). In development that
is usually a Lambda Function URL:

```
VITE_API_BASE_URL=https://<id>.lambda-url.us-east-1.on.aws
```

Served by a single Lambda (`agents/dashboard/app.py`, `DashboardFunction` in
`infra/template.yaml`) behind a Function URL with `AuthType: NONE`. It routes on
`rawPath`. All reads use `AS OF SYSTEM TIME follower_read_timestamp()` where the
staleness is acceptable, which is everything except the provenance replay and
the active-prediction lookup.

Seven read endpoints and one control endpoint:

| Method | Path | Polled | Powers |
| --- | --- | --- | --- |
| GET | `/overview` | 5s | Overview (header, fleet, memory, centre panel, backtest, feed) |
| GET | `/predictions` | 5s | Predictions list rail, Approvals queue |
| GET | `/predictions/{id}` | 5s | Prediction detail, Approval card |
| GET | `/predictions/{id}/replay` | on demand | `AS OF SYSTEM TIME` provenance replay |
| GET | `/playbooks` | 30s | Playbooks table |
| GET | `/playbooks/{id}` | 30s | Playbook detail drawer |
| GET | `/evolution` | 5s | Genealogy graph + `evolution_log` feed |
| POST | `/fleet/ramp` | — | Induce a ramp on a service |

Approvals has no endpoint of its own: it is
`GET /predictions?status=awaiting_approval` plus `GET /predictions/{id}`.

---

## Conventions

**Timestamps** are RFC 3339 UTC strings with a `Z` suffix, e.g.
`2026-08-17T06:12:04Z`. The frontend renders relative time itself; the API never
sends `"40m ago"`.

**Nulls are meaningful.** `null` means the database has no value for that field.
The UI renders an em dash, not a zero. It never substitutes a default.

**Cosine distance.** `precursor_snapshots.trajectory_embedding` is indexed with
`vector_cosine_ops`, so `<=>` returns cosine distance in `[0, 2]`. The API sends
`distance` verbatim and also `similarity = 1 - distance`. The UI labels the
column COSINE DISTANCE and shows `distance`.

**Posterior.** `predictions` stores `alpha` and `beta`; playbooks store
`success_count` and `failure_count`. The API derives and sends
`posterior_mean`, `ci_low`, `ci_high` (90% equal-tailed credible interval) so
two clients never disagree about the arithmetic. It never sends a stored fitness
float, because none exists.

**Errors** are `{"error": "<machine_code>", "detail": "<human sentence>"}` with a
non-2xx status. `<machine_code>` is stable and switchable on. Codes in use:
`not_found`, `bad_request`, `db_unreachable`, `gc_threshold_exceeded`,
`generator_unreachable`, `generator_not_configured`.

**Region derivation.** `predictions` has no region column. Where a region is
reported for a prediction it is derived from the most recent
`telemetry_embeddings` row for that `service_name`, falling back to the modal
`precursor_snapshots.region`. The field is `region_derived: true` so the UI can
mark it.

---

## `GET /overview`

Everything the Overview screen needs, in one request. Poll every 5s.

```jsonc
{
  "generated_at": "2026-08-17T06:12:04Z",
  "read_at": "follower_read_timestamp()",

  "cluster": {
    "database": "nexus",
    "regions": [
      { "region": "aws-us-east-1", "primary": true,  "zones": ["use1-az1","use1-az2","use1-az4"] },
      { "region": "aws-eu-west-1", "primary": false, "zones": ["euw1-az1","euw1-az2","euw1-az3"] }
    ],
    "survival_goal": "region_failure",
    "logical_ts": "1786464724123456789,0"
  },

  // Derived from predictions + incidents. Both sources are named so the UI
  // can label where a number came from.
  "counters": {
    "prevented":  { "value": 63, "source": "incidents.was_prevented" },
    "impacted":   { "value": 87, "source": "incidents" },
    "in_flight":  { "value": 0,  "source": "predictions.prevention_status" },
    "shadowed":   { "value": 0,  "source": "predictions.prevention_status" }
  },

  "fleet": [
    {
      "service_name": "payments",
      "region": "aws-eu-west-1",
      "region_derived": true,
      // "unknown" when no telemetry_embeddings row exists inside the 2h TTL.
      // The UI must render "unknown" as a distinct grey state, never as healthy.
      "status": "unknown",
      "telemetry_samples": 0,
      "last_sample_at": null,
      // Trailing window pulled from telemetry_embeddings.raw_metrics, oldest
      // first. Empty array when the sensory tier is empty. Never synthesised.
      "sparkline": [
        { "captured_at": "2026-08-17T06:09:04Z", "latency_p99_ms": 148.2, "error_rate": 0.002, "cpu_utilization": 0.38 }
      ],
      "latest": { "latency_p99_ms": 148.2, "error_rate": 0.002, "cpu_utilization": 0.38 },
      "delta_pct": null,
      "open_prediction_id": null
    }
  ],

  "memory": {
    "sensory":       { "count": 0,   "ttl": "2h",     "table": "telemetry_embeddings" },
    "episodic":      { "count": 190, "ttl": null,     "table": "precursor_snapshots" },
    "semantic":      { "count": 24,  "ttl": "90d",    "table": "playbooks" },
    "institutional": { "count": 1,   "ttl": "GLOBAL", "table": "institutional_playbooks" }
  },

  // The centre panel. A discriminated union on `kind`, resolved server-side so
  // the UI has one branch per state and no fallback logic of its own.
  //   active_prediction — a row with prevention_status in (pending, preventing)
  //   last_prediction   — the newest resolved predictions row
  //   last_prevention   — newest incidents row with was_prevented = true
  //   empty             — nothing in either table
  "centre": {
    "kind": "last_prevention",
    "heading": "LAST PREVENTION",
    "incident": {
      "id": "fca98d97-f382-4feb-b165-64294b305526",
      "title": "Connection pool exhaustion on payments (aws-us-east-1)",
      "severity": 4,
      "status": "resolved",
      "affected_services": ["payments"],
      "region": "aws-us-east-1",
      "root_cause": "connection_pool_exhaustion confirmed from the failure-window signature",
      "was_predicted": true,
      "was_prevented": true,
      "was_auto_resolved": true,
      "detected_at": "2026-08-17T02:41:12Z",
      "resolved_at": "2026-08-17T02:54:44Z",
      "mttr_seconds": 812,
      "playbook_used": null,
      // The precursor window that preceded it, if one was recorded.
      "precursor": {
        "id": "00fa1ffb-faff-405a-b952-739e8095dc44",
        "outcome_category": "connection_pool_exhaustion",
        "window_start": "2026-08-17T00:36:12Z",
        "window_end": "2026-08-17T02:41:12Z",
        "lead_minutes": 125,
        "metric_names": ["pool_utilization", "queue_wait_ms", "latency_p99_ms", "error_rate"]
      }
    }
  },

  // Leave-one-out k-NN over a deterministic sample of precursor_snapshots.
  // Recomputed at most every 300s; `computed_at` says when. null while the
  // first computation is in flight — the UI shows a skeleton, not a zero.
  "backtest": {
    "computed_at": "2026-08-17T06:07:31Z",
    "method": "leave_one_out",
    "k": 14,
    "threshold": 0.62,
    "sample_size": 30,
    "true_positive": 18, "false_positive": 3,
    "false_negative": 4, "true_negative": 5,
    "precision": 0.857, "recall": 0.818,
    "median_lead_minutes": 97
  },

  "evolution_feed": [ /* EvolutionEvent[], newest first, 14 items */ ]
}
```

### `centre` variants

```jsonc
{ "kind": "active_prediction", "heading": "ACTIVE PREDICTION",
  "prediction": /* Prediction */,
  "pipeline": [ /* PipelineStage[] */ ] }

{ "kind": "last_prediction", "heading": "LAST PREVENTION",
  "prediction": /* Prediction */,
  "pipeline": [ /* PipelineStage[] */ ] }

{ "kind": "last_prevention", "heading": "LAST PREVENTION",
  "incident": /* Incident */ }

{ "kind": "empty", "heading": "NO PREDICTION HISTORY",
  "reason": "predictions and incidents are both empty" }
```

**PipelineStage** — the Oracle → Sentinel → Guardian → Chronicler stepper. Every
`at` is a real column value, never an estimate. `state` is one of
`done | active | pending | skipped`.

```jsonc
{
  "agent": "Sentinel",
  "state": "pending",
  "at": null,                       // claimed_at / resolved_at / created_at
  "detail": null,                   // e.g. claimed_by
  "source_column": "predictions.claimed_at"
}
```

Stage derivation, so the UI never guesses:

| stage | `done` when | `at` |
| --- | --- | --- |
| Oracle | row exists | `created_at` |
| Sentinel | `claimed_at IS NOT NULL` | `claimed_at` |
| Guardian | `playbook_applied IS NOT NULL` | `claimed_at` |
| Chronicler | `resolved_at IS NOT NULL` | `resolved_at` |

---

## `GET /predictions`

Query params: `status` (repeatable; one of the `prevention_status` enum values
plus the derived pseudo-status `awaiting_approval`), `service`, `limit`
(default 50, max 200).

`awaiting_approval` is derived, not stored: `prevention_status = 'pending'` and
`playbook_applied` points at a playbook with `reversible = false`.

```jsonc
{
  "generated_at": "2026-08-17T06:12:04Z",
  "total": 0,
  "predictions": [ /* Prediction[] */ ]
}
```

### Prediction

```jsonc
{
  "id": "b1e6...",
  "service_name": "payments",
  "region": "aws-us-east-1",
  "region_derived": true,
  "causal_pattern": "connection_pool_exhaustion",
  "predicted_outcome": "connection_pool_exhaustion",
  "predicted_severity": 4,

  "alpha": 11.0,
  "beta": 4.0,
  "posterior_mean": 0.7333,
  "ci_low": 0.5122,
  "ci_high": 0.8971,
  "matching_precursor_count": 14,

  "predicted_eta": "2026-08-17T06:31:00Z",
  "prevention_status": "pending",
  "awaiting_approval": false,
  "claimed_by": null,
  "claimed_at": null,
  "playbook_applied": null,
  "created_at": "2026-08-17T06:11:02Z",
  "resolved_at": null,
  "expires_at": "2026-08-17T12:11:02Z",

  // crdb_internal_mvcc_timestamp of the row. The replay endpoint pins to this.
  "commit_ts": "1786464662000000000,0"
}
```

---

## `GET /predictions/{id}`

The Prediction detail screen and the Approvals card. Everything the mockup shows
as one payload.

```jsonc
{
  "prediction": /* Prediction */,

  // The exact statement the API ran, with the bound literal elided. Rendered
  // verbatim in the SQL panel — the UI must not reconstruct it.
  "retrieval_sql": "SELECT id, service_name, outcome_category, led_to_incident,\n       window_start, window_end,\n       trajectory_embedding <=> $1 AS distance\n  FROM precursor_snapshots\n ORDER BY trajectory_embedding <=> $1\n LIMIT 14",
  "retrieval_k": 14,

  "neighbors": [
    {
      "id": "00fa1ffb-...",
      "service_name": "payments",
      "region": "aws-eu-west-1",
      "outcome_category": "connection_pool_exhaustion",
      "led_to_incident": true,
      "distance": 0.1142,
      "similarity": 0.8858,
      "lead_minutes": 125,
      "window_start": "2026-08-10T00:13:58Z",
      "window_end": "2026-08-10T02:18:58Z"
    }
  ],

  // How alpha and beta were built, so the arithmetic is auditable on screen.
  "posterior_derivation": {
    "incident_matches": 10,
    "benign_matches": 3,
    "alpha_expression": "incident_matches + 1 = 11",
    "beta_expression": "benign_matches + 1 = 4"
  },

  // Thompson sampling. Empty array when Sentinel has not claimed the
  // prediction — the UI shows "SENTINEL HAS NOT CLAIMED THIS PREDICTION YET",
  // it does not invent candidates.
  "competition": [
    {
      "playbook_id": "0c8f...",
      "name": "Widen thread pool",
      "generation": 1,
      "success_count": 7,
      "failure_count": 6,
      "posterior_mean": 0.5333,
      "similarity": 0.912,
      "sampled_theta": 0.6041,
      "score": 0.5509,
      "winner": true
    }
  ],
  "competition_note": null,

  // The winning playbook's program, with per-step execution state.
  // `state` is one of queued | running | applied | rolled_back | unknown.
  "execution": {
    "playbook_id": null,
    "playbook_name": null,
    "reversible": null,
    "steps": [
      {
        "index": 1,
        "action": "scale_connection_pool",
        "target": "payments",
        "params": { "size": 192 },
        "inverse": { "action": "scale_connection_pool", "params": { "size": 64 } },
        "state": "queued"
      }
    ],
    "inverse_steps": []
  }
}
```

`404` with `{"error": "not_found"}` when the id does not exist.

---

## `GET /predictions/{id}/replay`

The provenance replay. Reads the prediction's `crdb_internal_mvcc_timestamp`,
then runs the *same* k-NN statement twice: once pinned with
`AS OF SYSTEM TIME <commit_ts>` and once against current state. No schema change
and no write is involved — the commit timestamp is the row's own MVCC timestamp.

```jsonc
{
  "prediction_id": "b1e6...",
  "commit_ts": "1786464662000000000,0",
  "aost_clause": "AS OF SYSTEM TIME '1786464662000000000,0'",
  "replayed_at": "2026-08-17T06:44:10Z",
  "elapsed_since_commit_seconds": 1988,

  "identical": true,
  "verdict": "BYTE-IDENTICAL",     // or "DIVERGED"

  "panes": [
    {
      "title": "AT DECISION TIME",
      "clause": "AS OF SYSTEM TIME '1786464662000000000,0'",
      "alpha": 11.0, "beta": 4.0, "posterior_mean": 0.7333,
      "rows": [ /* neighbor rows, same shape as /predictions/{id}.neighbors */ ]
    },
    {
      "title": "REPLAYED NOW",
      "clause": "same statement, no AS OF SYSTEM TIME",
      "alpha": 12.0, "beta": 4.0, "posterior_mean": 0.75,
      "rows": [ /* … */ ]
    }
  ],

  // What changed underneath since the decision, so the panel can say why the
  // pinned read is the interesting one.
  "drift": {
    "snapshots_written_since": 3,
    "playbook_counter_changes": 1
  }
}
```

`409` with `{"error": "gc_threshold_exceeded"}` when the commit timestamp is
older than the range GC threshold. The UI renders that as a real explanation of
the MVCC garbage-collection window, not as a generic failure.

---

## `GET /playbooks`

Query params: `tier` (`experimental|operational|institutional|retired`),
`status` (`active|retired|merged`), `limit` (default 100).

```jsonc
{
  "generated_at": "2026-08-17T06:12:04Z",
  "counts": { "all": 30, "active": 25, "institutional": 1, "retired_or_merged": 5 },
  "playbooks": [ /* PlaybookSummary[] */ ]
}
```

### PlaybookSummary

```jsonc
{
  "id": "0c8f...",
  "name": "Widen thread pool",
  "outcome_category": "thread_pool_starvation",
  "generation": 1,
  "memory_tier": "operational",
  "status": "active",
  "reversible": true,
  "success_count": 7,
  "failure_count": 6,
  "posterior_mean": 0.5333,
  "ci_low": 0.3096, "ci_high": 0.7501,
  "trials": 13,
  "parent_id": null,
  "lineage": [],
  "ancestor_count": 0,
  "region": "aws-ap-south-1",
  "locality": "REGIONAL",             // "GLOBAL" for institutional_playbooks
  "created_at": "2026-05-24T00:12:32Z",
  "last_used_at": "2026-08-04T06:12:32Z",
  "promoted_at": null,
  "retired_at": null,
  "expires_at": "2026-11-15T00:12:32Z"
}
```

## `GET /playbooks/{id}`

```jsonc
{
  "playbook": /* PlaybookSummary */,
  "steps": [
    { "index": 1, "action": "scale_thread_pool", "target": "payments",
      "params": { "size": 96 },
      "inverse": { "action": "scale_thread_pool", "params": { "size": 48 } } }
  ],
  "inverse_steps": [ /* the rollback program, in reverse execution order */ ],
  // Root → self, resolved from playbooks.lineage.
  "lineage": [ { "id": "…", "name": "…", "generation": 1, "posterior_mean": 0.53 } ],
  "children": [ { "id": "…", "name": "…", "generation": 2, "posterior_mean": 0.61 } ],
  "timeline": [ /* EvolutionEvent[] for this playbook, newest first */ ],
  "institutional": null    // the institutional_playbooks row, when promoted
}
```

---

## `GET /evolution`

Query params: `category` (an `outcome_category`), `limit` (feed length, default
60, max 300).

Nodes and edges are pre-laid-out as a graph description; the client positions
them with react-flow's layout, but parentage and typing come from here.

```jsonc
{
  "generated_at": "2026-08-17T06:12:04Z",
  "categories": ["cache_stampede", "cert_expiry", "connection_pool_exhaustion"],
  "nodes": [
    {
      "id": "0c8f...",
      "name": "Widen thread pool",
      "outcome_category": "thread_pool_starvation",
      "generation": 1,
      "memory_tier": "operational",
      "status": "active",
      "posterior_mean": 0.5333,
      "trials": 13,
      // proven | experimental | failing | institutional | retired
      // Derived server-side from tier + status + posterior so the legend and
      // the node colour can never disagree.
      "class": "experimental"
    }
  ],
  "edges": [
    { "id": "e:<child>", "source": "<parent id>", "target": "<child id>",
      "kind": "parent" }        // parent | mutation | merge
  ],
  "events": [ /* EvolutionEvent[] */ ],
  "event_counts": { "growth": 145, "mutation": 22, "birth": 8, "rollback": 4,
                    "retirement": 3, "merge": 2, "promotion": 1 }
}
```

### EvolutionEvent

```jsonc
{
  "id": "…",
  "event_type": "growth",          // birth growth mutation competition merge promotion retirement rollback
  "playbook_id": "7fff...",
  "playbook_name": "Recycle saturated connections",
  "parent_id": null,
  "parent_name": null,
  "trigger_incident_id": "fca9...",
  "fitness_before": 0.7142857142857143,
  "fitness_after": 0.75,
  "details": { "successes": 1, "failures": 0, "cumulative_trials": 6 },
  "created_at": "2026-08-15T00:12:32Z"
}
```

`details` is free-form JSONB written by the agents. The UI renders it as
key/value pairs without assuming any particular key exists.

---

## `POST /fleet/ramp`

The judge-facing control. Forwards to the synthetic fleet's control API
(`generator/live.py`, started with `make live`) at the Lambda's `GENERATOR_URL`
environment variable.

```jsonc
// request
{ "service": "payments", "archetype": "connection_pool_exhaustion", "speed": 4 }

// 202
{ "accepted": true, "service": "payments",
  "archetype": "connection_pool_exhaustion", "speed": 4, "status": "drifting",
  "note": "Oracle samples the sensory tier on a 60s cadence." }
```

`archetype` may be omitted, in which case the generator's default selection
applies. Valid archetypes are the eight keys in `generator/archetypes.py`.

Failure modes, both rendered by the UI as a visible banner with the returned
`detail` string — never as a silent no-op and never as a fake success:

```jsonc
// 503
{ "error": "generator_not_configured",
  "detail": "GENERATOR_URL is not set on the dashboard Lambda." }

// 503
{ "error": "generator_unreachable",
  "detail": "No response from the fleet generator at <url>. Start it with `make live`." }
```

### What the UI must not do after a ramp

A ramp writes into `telemetry_embeddings`. Turning that into a prediction is
Oracle's job. Until Oracle is implemented, `predictions` stays empty and the
stepper must say so: after the ramp is accepted the UI shows a live elapsed
counter and the Oracle stage as `active`, and if no prediction row appears it
states that plainly rather than advancing the stepper on a timer.

---

## Failure handling

Any endpoint may return `503 {"error": "db_unreachable"}` when the connection
pool cannot reach the cluster. The frontend shows a persistent
"memory layer unreachable" banner, keeps the last successfully rendered data on
screen, marks it stale with the time of the last good response, and keeps
polling. It never clears the view to zeros and never silently swallows the
error.
