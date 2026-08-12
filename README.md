# NEXUS — the memory operating system for AI agents

> Infrastructure that remembers, predicts, heals — and evolves.
> Operational knowledge with a family tree: playbooks that are born, compete, mutate, merge, and die.


---

## Repo layout

```
sql/            numbered, idempotent migrations (000_regions → 003_localities) + changefeed.sql
scripts/        migrate.py (migration runner), smoke_test.py (schema/vector/AOST verification)
layers/shared/  Lambda layer: nexus_common {db, bedrock, log, config} + requirements
agents/         thin Lambda bodies: oracle, sentinel, diagnostician, guardian, chronicler,
                receiver (changefeed webhook), poller (fallback path, disabled)
infra/          SAM template, samconfig, Step Functions ASL (Sentinel→Diagnostician→Guardian→Chronicler)
generator/      synthetic world generator (not yet implemented)
frontend/       dashboard (React + Vite + Tailwind, navigation shell only)
Makefile        one-command ops (deploy/destroy/migrate/seed-smoke/…)
```

## Status

| Area | State |
|---|---|
| Multi-region schema, vector indexes, TTLs, zone configs | Complete |
| Migration runner + schema smoke test | Complete |
| AWS stack: layer, 7 Lambdas, EventBridge, Step Functions, S3, secrets, CloudWatch | Complete |
| Changefeed → receiver → EventBridge → Step Functions pipeline | Complete, pending a live cluster run |
| Agent logic (oracle, sentinel, diagnostician, guardian, chronicler) | Handler stubs only |
| Synthetic world generator and seed data | Not started |
| Dashboard | Navigation shell only |

Key locked decisions: **Python 3.12** Lambdas · **AWS SAM** IaC · embedding dim
**1024** (Titan Text Embeddings V2 default — *not* 1536) · `institutional_playbooks`
is a separate **GLOBAL** table (simpler to demo) · vector indexes use
`vector_cosine_ops` so the `<=>` cosine operator is index-accelerated.

---

## Prerequisites

```bash
# tooling
brew install aws-sam-cli awscli uv   # or the platform equivalent

# local python deps (Python 3.12) for the migration/smoke scripts, plus ruff/pytest
make deps                            # uv sync

cp .env.example .env                 # then fill in COCKROACH_DB_URL + CHANGEFEED_SHARED_SECRET
```

The dashboard has its own toolchain:

```bash
cd frontend && npm install && npm run dev
```

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

`make seed-smoke` exercises the schema end to end: it seeds throwaway rows, runs a
category-filtered cosine k-NN query, checks the planner picks the vector index, and
runs both an `AS OF SYSTEM TIME` and a follower-read query before cleaning up.

## 3. AWS stack and the changefeed pipeline

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
secret. Schema validation of Bedrock output before any DB write is required
before the Diagnostician writes playbooks, and is not implemented yet.

## Teardown

```bash
make destroy          # sam delete
# drop the changefeed job first if needed: SHOW CHANGEFEED JOBS; CANCEL JOB <id>;
```
