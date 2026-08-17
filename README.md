# NEXUS — the memory operating system for AI agents

> Infrastructure that remembers, predicts, heals — and evolves.
> Operational knowledge with a family tree: playbooks that are born, compete, mutate, merge, and die.

**To run it: [`demo/README.md`](demo/README.md).** Fill in `DB_DSN`, then
`make seed && make demo-run` prints a 24-check scorecard of the whole story.


---

## Repo layout

```
sql/            numbered, idempotent migrations (000_regions → 006_backtest_runs)
                + changefeed.sql
scripts/        migrate.py, seed.py (builds the demo world), verify_phase2.py (memory-core
                gate), pipeline_local.py (the five pipeline scenarios), lifecycle_local.py
                (the whole playbook lifecycle), backtest.py (the honesty layer),
                load_local.py (three concurrent incidents), region_demo.py (survival),
                demo_check.py / demo_run.py (stage the demo, then grade it)
layers/shared/  Lambda layer: nexus_common {db, bedrock, embeddings, trajectory, steps,
                log, config} + requirements
agents/         thin Lambda bodies: oracle, sentinel, diagnostician, guardian, chronicler,
                receiver (changefeed webhook), poller (fallback path, disabled),
                dashboard (read-only HTTP API behind a Function URL)
infra/          SAM template, samconfig, two Step Functions definitions:
                nexus.asl.json (Sentinel→Diagnostician→Guardian→Chronicler) and
                approved.asl.json (Guardian→Chronicler, entered after a human approves)
generator/      synthetic world: archetypes, trajectory synthesis, seeded population and
                genealogy, live fleet simulator + ramp control API
tests/          unit tests for the memory core and every agent (no database required)
demo/           README.md (how to run everything), DEMO_SCRIPT.md, JUDGE_QA.md,
                architecture.svg, the region-kill compose file
frontend/       dashboard (React + Vite + Tailwind + Recharts + react-flow);
                API_CONTRACT.md is the contract with agents/dashboard
Makefile        one-command ops (seed/verify/test/deploy/migrate/live/…)
```

## Status

Everything below is either verified against the live three-region cluster or
labelled with what is missing. Nothing is marked complete on the strength of the
code alone.

| Area | State |
|---|---|
| Multi-region schema, vector indexes, TTLs, zone configs | Verified live — `make verify`, 19/19 on a freshly seeded world |
| Migration runner + schema smoke test | Verified live |
| Synthetic world generator, embedding pipeline, seeded memory | Verified live — 155 snapshots, 30 playbooks, deterministic |
| Live fleet simulator + ramp control API | Verified live |
| Oracle · Sentinel · Diagnostician · Guardian · Chronicler | Verified live — `make demo-run`, 24/24 |
| Unit suite | 242 tests, no database required · ruff clean · frontend builds and lints clean |
| Playbook lifecycle: birth, growth, shadow, mutation, merge, promotion, retirement | Verified live — `make lifecycle`, 36/36, all 8 event types |
| Human-in-the-loop approval gate (read, decide, dispatch) | Verified live — `make pipeline-approval` |
| Concurrency: duplicate delivery, and three simultaneous incidents | Verified live — `make pipeline-concurrency`, `make load` 7/7 |
| Out-of-sample backtest + calibration | Verified live — `make backtest` |
| Dashboard API (8 read routes, 2 writes) · UI (5 views) | Verified live; UI builds and lints clean |
| Region survival **configuration** | Verified live — `make region-config`, 5/5 |
| Region survival **demonstrated by killing a node** | Written, **not exercised** — needs Docker running |
| Bedrock-authored genomes (birth, mutation, merge) | Written and unit-tested; **degrade rather than invent** without credentials |
| AWS stack: layer, 8 Lambdas, EventBridge, 2 state machines, S3, secrets, CloudWatch | Complete in the repo, **never deployed** |
| Changefeed → receiver → EventBridge → Step Functions | Complete in the repo, **never created on the cluster** |

### Backtest — Oracle on windows withheld from the database

`make backtest` embeds the held-out set in `demo/backtest_set.jsonl` and runs
Oracle's own retrieval and emit gate against it. Out-of-sample: those windows were
never written to `precursor_snapshots`. A window Oracle declines to predict on
counts as a negative, because that is what silence means in production.

| | |
|---|---|
| Held out | 42 windows — 30 incidents, 12 negatives |
| Memory scored against | 155 precursor snapshots |
| Precision · recall | **0.882** · **1.000** |
| Confusion | TP 30 · FP 4 · FN 0 · TN 8 |
| Category named correctly | 32 of 34 predictions |
| Warning available | median 80 min of precursor pattern before the failure |
| Imminence | median stated ETA 5 min on a window with 0 min left |

Calibration — stated confidence against the rate that materialized:

| Bucket | n | Stated | Realized | Gap |
|---|---|---|---|---|
| 0.60–0.70 | 6 | 0.667 | 0.500 | **−0.167** |
| 0.70–0.80 | 4 | 0.750 | 1.000 | +0.250 |
| 0.80–0.90 | 4 | 0.828 | 0.750 | −0.078 |
| 0.90–1.00 | 20 | 0.938 | 1.000 | +0.062 |

Read it honestly: well behaved above 0.80, and **over-confident in the 0.60–0.70
bucket** on a small sample. Recall of 1.000 is the easiest possible case — the
held-out incidents are complete precursor windows, and eight synthetic archetypes
are far more separable than real telemetry. The number worth trusting is precision.

Two of `make verify`'s nineteen checks are properties of the *seeded world* rather
than of the code — a playbook one success from promotion, and a challenger with
zero trials. Rehearsal consumes both, because the system genuinely learns from
being rehearsed, so on a world that has been demoed against they fail and
`make demo-reset` restores them. `make demo-check` exists to tell you which it is.

### Known gaps, and why

| Gap | Why |
|---|---|
| Region kill not exercised | The Docker daemon was not running on the build machine. `make region-config` proves the configuration; `make region-demo` proves the behaviour and is untested. |
| Birth, mutation, merge produce nothing | No AWS credentials, so Bedrock is unreachable. They log and decline rather than fabricating a playbook. `make lifecycle` substitutes one seam and stamps `proposed_by: "lifecycle-harness"` on every row. |
| AWS stack never deployed | `aws`, `sam` and `ccloud` are not installed here. `sam validate --lint` runs in CI. |
| 10k-row load and TTL-reap checks | Bulk vector writes run at ~2.6 rows/s over this link and the connection drops before 1k rows. Environment-limited, not design-limited. |
| Agent Skills | Pre-decided scope cut. The `ccloud` read-only health check remains. |
| Unprefixed vector index removed | Oracle's neighbourhood query has no category filter by design, so it falls back to a scan and sort. Invisible at 155 snapshots; not at a million. Restoring it costs a second index on every write. |
| Calibration in the low bucket | Real and visible. Fixing it means reweighting the prior against neighbour similarity, and that is a change worth measuring rather than guessing. |

Key locked decisions: **Python 3.12** Lambdas · **AWS SAM** IaC · embedding dim
**1024** (Titan Text Embeddings V2 default — *not* 1536) · `institutional_playbooks`
is a separate **GLOBAL** table (simpler to demo) · vector indexes use
`vector_cosine_ops` so the `<=>` cosine operator is index-accelerated · every
telemetry window is embedded through the single canonical serialization in
`nexus_common.trajectory` · remediation steps are `{action, target, params,
inverse}`, validated by `nexus_common.steps` before anything is written or run.

---

## Prerequisites

```bash
# tooling
brew install aws-sam-cli awscli uv   # or the platform equivalent

# local python deps (Python 3.12) for the migration/seed/verify scripts, plus ruff/pytest
make deps                            # uv sync

cp .env.example .env                 # then fill in COCKROACH_DB_URL + CHANGEFEED_SHARED_SECRET
```

`nexus_common` ships to Lambda as a layer, so it is not installed into the venv;
`pyproject.toml` puts `layers/shared/python` on the path for pytest and the
scripts do the same via `scripts/_env.py`.

The dashboard has its own toolchain:

```bash
make dashboard                       # read API on :8787 (the Lambda handler over HTTP)
cp frontend/.env.example frontend/.env
make ui                              # Vite dev server on :5173
```

`make dashboard` runs `agents/dashboard/app.py` — the same module the deployed
Function URL runs — so the UI is never developed against a different
implementation than it ships against.

`predictions` is populated by Oracle, so run `make pipeline` (or `make live`
alongside the deployed Oracle schedule) to give the dashboard something to show.
Between runs the table empties itself — predictions carry a 6-hour Row-Level TTL
— and the dashboard is built for that: the Overview centre panel falls back to
the most recent prevented row in `incidents`, and every other panel shows a
designed empty state naming the table it consulted. Nothing is faked to fill the
gap — see `frontend/README.md`.

---

## 1. CockroachDB cluster (manual, in CockroachDB Cloud)

These steps happen in the Cloud console / `ccloud` CLI — they can't be scripted from here:

1. Provision a **3-region** cluster. Use regions that also exist in AWS
   (defaults assumed here: `us-east-1`, `eu-west-1`, `ap-south-1`).
2. **Tier check** — confirm your tier supports all of: changefeeds, vector
   indexes, multi-region localities, per-table zone configs, Row-Level TTL.
   `sql/002_zone_configs.sql` (per-table `gc.ttlseconds`) and enterprise
   changefeeds need a Dedicated/Advanced tier. If any is missing, escalate the
   tier now — everything downstream assumes them.
3. Create the `nexus` database and a least-privilege app user (`nexus_app`, no admin).
4. Download the CA cert; verify TLS: `cockroach sql --url "$COCKROACH_DB_URL" -e "SELECT 1"`.
5. `ccloud` CLI: create a **read-only** service account + API key for Guardian
   (RBAC: cluster read, no mutations). Store the key in the `nexus/ccloud` secret.
6. Enable the **Managed MCP Server** for the cluster; confirm read-only + audit
   logging defaults; test from Claude Code/Cursor with a `list tables` query.

Then set up multi-region + the schema from this repo:

```bash
# 000_regions.sql sets PRIMARY REGION + ADD REGION + SURVIVE REGION FAILURE.
# Edit the region names in sql/000_regions.sql to match your cluster first.
make migrate          # applies 000 → 002, idempotently
make seed-smoke       # seed rows + hybrid vector query + AOST query
```

> Single-region / local demo fallback: comment out the `ADD REGION` /
> `SURVIVE REGION FAILURE` lines in `000_regions.sql` and the `LOCALITY …`
> clauses in `001_schema.sql`. `incidents`/`playbooks` are `REGIONAL BY ROW`
> (their region lives in the CockroachDB-managed `crdb_region` column — read it
> as `crdb_region::STRING`); `institutional_playbooks` is `GLOBAL`.

## 2. Schema

`make migrate` applies, in order:

| file | what |
|---|---|
| `000_regions.sql` | primary + 2 regions, `SURVIVE REGION FAILURE` (vector-index and rangefeed cluster settings are on by default in CockroachDB Cloud; the statements are left commented for self-hosted clusters) |
| `001_schema.sql`  | 7 tables (incidents, precursor_snapshots, playbooks, institutional_playbooks GLOBAL, predictions, telemetry_embeddings, evolution_log), vector indexes (`vector_cosine_ops`), secondary indexes, Row-Level TTLs, localities |
| `002_zone_configs.sql` | 7-day `gc.ttlseconds` on `precursor_snapshots` and `predictions` for AOST provenance replay |
| `003_localities.sql` | forces `REGIONAL BY ROW` on `incidents` and `playbooks` — `CREATE TABLE IF NOT EXISTS` drops the `LOCALITY` clause when the table predates `ADD REGION`, leaving them `REGIONAL BY TABLE` with no `crdb_region` column |
| `004_vector_prefix_indexes.sql` | prefixed vector indexes (`(outcome_category, embedding)` and `(outcome_category, status, embedding)`) so the hybrid filtered k-NN queries are served by one index lookup. Each is an index backfill and runs as an async schema-change job — allow a few minutes |

`make seed-smoke` exercises the schema end to end: it seeds throwaway rows, runs a
category-filtered cosine k-NN query, checks the planner picks the vector index, and
runs both an `AS OF SYSTEM TIME` and a follower-read query before cleaning up.

## 3. The memory core

```bash
make seed             # migrate, then build the entire demo world from cold
make verify           # the Phase 2 exit gate (see below)
make test             # unit tests, no database needed
make live             # synthetic fleet + ramp control API on :8000
```

`make seed` writes 150 incidents, 190 precursor snapshots (150 positive, 40
negative), 30 playbooks across four generations, one promoted institutional
playbook, and ~185 `evolution_log` events. It is deterministic: the same seed
rebuilds the identical world, which is what makes `make demo-reset` restore the
exact state the demo was rehearsed against.

Fifty incidents and twenty negatives are **withheld from the database** and
written to `demo/backtest_set.jsonl`. The Phase 7 backtest runs against those, so
its precision and recall numbers are measured on windows the seeded memory has
never seen.

`telemetry_embeddings` is intentionally left empty by the seed — it has a 2-hour
TTL, so seeding it would be seeding something that evaporates. `make live` fills
it through the same ingestion path real telemetry would use:

```bash
curl -XPOST localhost:8000/ramp \
  -d '{"service":"payments","archetype":"connection_pool_exhaustion","speed":1}'
curl localhost:8000/fleet
```

### Embedding provider

Embeddings go through `nexus_common.embeddings`, which has two backends:

| `EMBEDDING_PROVIDER` | Backend |
|---|---|
| `bedrock` | Amazon Titan Text Embeddings V2 — the production path |
| `local` | a deterministic signed feature-hashing embedder over the same canonical text; no network, so a world can be built and tested without AWS credentials |
| `auto` (default) | `bedrock` when AWS credentials resolve, otherwise `local` with a warning |

**They are different vector spaces.** A database seeded with one and queried with
the other yields meaningless distances, so `demo/seed_manifest.json` records which
provider built the current world. Switching providers means re-running `make seed`.

### `make verify` — the Phase 2 exit gate

Seven checks against the live cluster:

1. the hybrid `WHERE category … ORDER BY embedding <=>` query is served by a
   `vector search` node with `prefix spans`, not a scan and sort
2. held-out precursor windows retrieve neighbours of their own archetype
3. a prediction's evidence, re-read `AS OF SYSTEM TIME` its commit timestamp
   after the underlying table has been mutated, is byte-identical — and the
   mutation is verified to have moved the present-tense answer first
4. the dashboard's aggregate resolves against `follower_read_timestamp()`,
   applied with `SET TRANSACTION AS OF SYSTEM TIME` so one timestamp covers the
   whole statement
5. the staged demo beats are properties of the seeded data: two merge-ready
   pairs inside `distance < 0.15`, a promotion candidate at posterior mean
   0.900, a zero-trial challenger, a bad playbook still above the retirement
   line, retired ancestors and merged parents preserved rather than deleted
6. k-NN latency with 10k rows in the sensory tier (`make verify-full`)
7. row-level TTL actually reaps an expired row (`make verify-full`; the TTL job
   cron on that table is `*/5`, so it waits)

## 4. The prevention pipeline

```bash
make pipeline               # ramp → predict → claim → compete → execute → prevented
make pipeline-rollback      # the bad fix wins, degrades the fleet, is rolled back
make pipeline-novel         # a pattern no playbook claims — the cold-start path
make pipeline-concurrency   # five deliveries of one prediction, one execution
```

Step Functions is not deployed on a laptop, so `scripts/pipeline_local.py` plays
its part: it starts the synthetic fleet in-process, ramps a service, feeds the
sensory tier, and calls Sentinel → Diagnostician → Guardian → Chronicler in the
state machine's order. Everything else is real — the same agent code, the same
queries, against the same cluster.

**Oracle** matches the live telemetry window against `precursor_snapshots` (k=14)
and emits a prediction whose confidence is a Beta posterior over the matched
neighbours' outcomes — `alpha` = neighbours that failed + 1, `beta` = neighbours
that recovered + 1. Both are stored, so the credible interval survives the trip.
It stays silent below 5 close neighbours or a 0.60 posterior mean.

**Sentinel** claims the prediction with `SELECT … FOR UPDATE` (duplicate
changefeed deliveries become clean no-ops), retrieves the top-8 candidate
playbooks by vector similarity, and selects by **Thompson sampling** — sampling
each Beta posterior rather than taking the argmax, which is the only reason a
zero-trial challenger ever gets a turn. Every draw is written to `evolution_log`.
The tier then decides: shadow below 0.75 confidence, auto if the winner is
reversible, approval (an `approvals` row) if it is not.

**Guardian** executes against the fleet control API, watches the target metric
for a verification window, and on degradation runs the inverses in reverse —
each one reverting the exact step it undoes rather than being replayed as a
fresh action. Steps are idempotent by construction: the action vocabulary is
declarative, so a retry that re-applies a step already in desired state is a
no-op. "Flat" is reported as inconclusive, never as success.

**Diagnostician** promotes the trailing sensory window into `precursor_snapshots`
— reusing the embedding rather than paying Titan twice — retrieves similar
incidents, and on a genuinely unprecedented pattern asks Bedrock for a playbook.
Anything failing `PlaybookDraft` validation is rejected outright: a malformed
genome is stillborn, logged, never inserted, never executed.

**Chronicler** turns the result into memory. Growth applies the trial and winds
the 90-day disuse clock; a rollback breeds a variant at `generation+1` with a
flat prior while the parent stays active; two siblings inside 0.15 cosine with
both posterior means above 0.5 are replaced by one canonical child that inherits
`min(successes)`/`max(failures)` — the most conservative reading of the evidence
that still transfers, so a merge can never claim more competence than was
demonstrated. Above 0.9 over ten trials a playbook is copied into the GLOBAL
`institutional_playbooks`; below 0.2 over five it retires.

No function in `agents/chronicler/app.py` writes to `evolution_log`. Each returns
the rows it earned and one caller inserts them alongside the mutations in a
single serializable transaction, so a lifecycle change cannot reach the database
without its log row.

## 5. The playbook lifecycle

```bash
make lifecycle              # birth → growth → failure → mutation → merge → promotion
```

`scripts/lifecycle_local.py` drives one family through every transition against
the live cluster in a probe category of its own, asserting `evolution_log` after
each step and deleting the probe on the way out. 36 assertions; all six event
types written.

Mutation and merge need a reasoning model, so the harness substitutes
Chronicler's single `_generate` seam and lets the substitution show: every row it
writes carries `proposed_by: "lifecycle-harness"`, never `"bedrock"`. The genome
still passes the same `PlaybookDraft` validation. Without credentials the
production path degrades honestly — `make pipeline-rollback` reports that no
variant was bred rather than inventing one.

Shadow scoring settles the two halves of a shadow record separately. The
prediction becomes `missed` or `false_alarm`; the playbook is scored only when
there is something real to compare it against, and never merely because the
predicted failure failed to arrive. `success_count` is an integer and a shadow
trial is worth 0.3, so the fraction accrues in the append-only log rather than in
a column and banks a whole trial every 3⅓ observations.

## 6. AWS stack and the changefeed pipeline

```bash
make deploy           # sam build (container) + sam deploy — one command, whole stack
make outputs          # prints the receiver Function URL, bus name, state machine ARN, bucket
make secrets          # prints the commands to populate the 3 placeholder secrets
```

The stack creates: the `nexus-shared` layer, all 7 Lambdas, the `nexus-bus`
EventBridge bus + a `prediction.created` rule → the Step Functions pipeline,
the S3 artifacts bucket, three Secrets Manager placeholders, per-Lambda
least-privilege IAM (no `*` resource policies), and a `NEXUS-<env>` CloudWatch
dashboard. Then wire the changefeed:

```bash
# 1) populate secrets (see `make secrets`) — DB dsn, changefeed shared secret, ccloud key
# 2) edit sql/changefeed.sql: paste the receiver URL from `make outputs` and the shared secret
make changefeed       # CREATE CHANGEFEED FOR TABLE predictions INTO 'webhook-…'
```

**End-to-end pipeline test:**

```sql
INSERT INTO predictions (service_name, causal_pattern, predicted_outcome,
                         predicted_severity, alpha, beta, current_embedding, predicted_eta)
VALUES ('payments','pool-creep','connection_pool_exhaustion',3, 12,2,
        (SELECT trajectory_embedding FROM precursor_snapshots LIMIT 1), now()+INTERVAL '50 min');
```

Within ~2s: changefeed fires → receiver Lambda publishes to `nexus-bus` →
EventBridge rule starts a Step Functions execution → Sentinel→…→Chronicler log
lines in CloudWatch. `make logs-receiver` tails the receiver.

- **Duplicate-delivery check:** POST the same changefeed payload twice; the
  receiver publishes both, each carrying an idempotency key. The `SELECT ... FOR
  UPDATE` claim that turns the second into a no-op belongs to Sentinel and is
  not implemented yet.
- **Plan B:** if the webhook sink misbehaves, enable the `PollerFunction`
  schedule (currently `Enabled: false` in `infra/template.yaml`) — a 1-minute
  poll on `predictions WHERE prevention_status='pending'` that publishes the
  same events.

---

## Security

No credentials in code or git history · secrets in Secrets Manager, read at cold
start · per-Lambda IAM, no wildcard resources · MCP read-only + audit logging ·
ccloud service account read-only · webhook authenticated via `Bearer` shared
secret. Every Bedrock proposal is validated against `nexus_common.steps.PlaybookDraft`
before it can be written or executed: an unknown action, a missing target or a
malformed step is logged with its payload and rejected, never inserted.

## Teardown

```bash
make destroy          # sam delete
# drop the changefeed job first if needed: SHOW CHANGEFEED JOBS; CANCEL JOB <id>;
```
