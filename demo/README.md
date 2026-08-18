# Running NEXUS

Start here. Everything below has been run against the live cluster; where
something has not, it says so.

---

## 0 · Once, before anything

```bash
cp .env.example .env         # then fill in COCKROACH_DB_URL — the CockroachDB Cloud connection string
make deps                    # uv sync
cd frontend && npm ci && cd ..
```

`.env` is gitignored and stays that way. `COCKROACH_DB_URL` is the only variable you
must set for the whole backend to work; everything else has a working default.
(That is the name the code reads — `nexus_common.config` falls back to it when
Secrets Manager is not reachable, which is every local run.)

**Embeddings.** With AWS credentials, `EMBEDDING_PROVIDER=auto` uses Amazon Titan
Text Embeddings V2. Without them it falls back to a deterministic local embedder
and says so in every log line and in `demo/seed_manifest.json`. The two are
different vector spaces, so **switching providers requires a full re-seed** —
vectors written by one are meaningless to the other.

---

## 1 · Build the world (~4 minutes)

```bash
make seed                    # migrate, then build the whole demo world
make verify                  # 21 checks against the live cluster
make demo-check              # are the five staged demo beats stageable?
make backtest                # score Oracle on withheld windows, store the run
```

`make seed` is idempotent and destructive in the right way: it `DELETE`s and
rebuilds, so it is also `make demo-reset`. Expect ~220s, almost all of it writing
1024-dimension vectors across three regions.

It uses `DELETE` rather than `TRUNCATE` deliberately, and re-asserts
`sql/002_zone_configs.sql` afterwards. `TRUNCATE` recreates a table under a new ID and
**discards its zone config**, which is how `precursor_snapshots` once ended up
inheriting a 75-minute GC window instead of the configured 7 days — the provenance
replay kept working, because a replay runs seconds after its decision, and nothing
noticed for days. `make verify` now asserts the window on both tables.

---

## 2 · See it work (~3 minutes)

```bash
make demo-run                # the whole three-moment story, headless and graded
```

This is the fastest way to know the system is healthy. It runs the real scripts,
reads their reports, and prints a scorecard of 24 checks. It refuses to start
against a world whose staged beats have worn out — rehearsal genuinely consumes
them — and tells you to `make demo-reset`.

For the graded exit gate, three clean runs from three clean worlds:

```bash
make demo-run-3              # --repeat 3 --reset · about 20 minutes
```

### The individual beats

```bash
make pipeline                # ramp → predict → claim → compete → execute → prevented
make pipeline-rollback       # the bad fix wins, degrades the fleet, is rolled back
make pipeline-approval       # an irreversible fix waits for a human, who approves it
make pipeline-novel          # a pattern no playbook claims — the cold-start path
make pipeline-concurrency    # five deliveries of one prediction, one execution
make lifecycle               # birth → growth → failure → mutation → merge → promotion
make load                    # three concurrent incident ramps; nothing is lost
```

---

## 3 · Watch it on a screen

Three terminals:

```bash
make live                    # the synthetic fleet + ramp control API, :8000
make dashboard               # the dashboard read API, :8787 — the same handler the Lambda runs
make ui                      # Vite dev server, :5173
```

Then open <http://localhost:5173>. Click **Ramp** on a service in the fleet strip
and watch a prediction appear. Everything on screen is a column value or named
arithmetic over column values; where the database has no answer, the panel says
which table it consulted and shows an em dash.

`frontend/.env` needs `VITE_API_BASE_URL=http://localhost:8787`. A missing base URL
is reported by name rather than silently producing 404s that look like an empty
database.

**Note on timing.** Predictions carry a 6-hour Row-Level TTL, so between sessions
the table empties itself. That is correct behaviour, not a bug — run
`make pipeline` to give the dashboard something live to show.

---

## 4 · The region kill

Two halves, because only one can be done on a managed cluster.

```bash
make region-config           # read the survival goal off the real cluster — harms nothing
```

That prints the three database regions, `SURVIVE REGION FAILURE`, which tables are
`REGIONAL BY ROW` versus `LOCALITY GLOBAL`, and the actual replica localities of a
sampled range. 5/5 checks, live, no node harmed.

Watching it survive needs a cluster whose plug is reachable:

```bash
make region-up               # three nodes, one region locality each (needs Docker running)
make region-demo             # open a transaction, kill a region, commit anyway
make region-down
```

Same engine, same Raft, self-hosted so the plug is reachable. **This half has not
been exercised** — the Docker daemon was not running on the build machine. Start
Docker Desktop and run it; if it fails, the failure is in these scripts, and
`make region-config` still stands on its own.

---

## 5 · Deploy to AWS

**Deployed and verified 18 Aug 2026** — stack `nexus` in `us-east-1`, exit gate 1
closed end to end: `INSERT` → changefeed → receiver → `nexus-bus` → Step Functions
`SUCCEEDED` across Sentinel → Diagnostician → Guardian → Chronicler.

```bash
make secrets                 # prints what to put in Secrets Manager
make deploy                  # sam build (container) + sam deploy, one command
make outputs                 # receiver Function URL, bus name, state machine ARNs, bucket
make changefeed              # create the changefeed on `predictions`
```

Two things about the deployed stack that are not obvious:

**Guardian cannot act in the cloud, on purpose.** The fleet is a local simulator with
no public URL, so `GeneratorUrl` is unset and Guardian reports `no_substrate` rather
than claiming a fix it never ran. Exposing the laptop through a tunnel would make the
beat "work" and make the claim worse.

**The DSN in Secrets Manager is not the DSN in `.env`.** libpq with
`sslmode=verify-full` and no `sslrootcert` looks for `~/.postgresql/root.crt`, which
cannot exist in Lambda, and `sslrootcert=system` fails too because psycopg's manylinux
wheel bundles an OpenSSL whose compiled-in CA path is absent from the Lambda
filesystem. The cluster presents an ordinary Let's Encrypt chain, so the stored DSN
ends with `sslrootcert=/etc/pki/tls/certs/ca-bundle.crt` — Amazon Linux's own bundle.
Keep that parameter when you rotate the DSN or every function will fail to connect.

### Rotating a secret — cycling the functions is part of the rotation, not a note

`config.get_secret` is `@functools.cache`d and the connection pool is a module global,
so a warm execution environment keeps serving the **old** value forever. Writing a new
secret version has no effect on its own. Rotation is two steps:

```bash
# 1. write the new value
aws secretsmanager put-secret-value --secret-id nexus/db --secret-string '{"dsn":"…"}'

# 2. REQUIRED: replace every execution environment, or step 1 did nothing.
#    Updating *any* configuration property does it; --description is the safe one.
for f in oracle sentinel diagnostician guardian chronicler receiver poller dashboard; do
  aws lambda update-function-configuration \
    --function-name "nexus-$f-development" \
    --description "secret rotated $(date -u +%FT%TZ)" >/dev/null
done
```

**Do not** cycle them with `--environment "Variables={SECRET_REVISION=…}"`. That flag
*replaces* the whole variable map rather than merging into it, so it would silently
delete `GENERATOR_URL`, `EVENT_BUS_NAME`, `ARTIFACTS_BUCKET` and everything else the
template set — and the functions would then fail for a reason with no connection to the
rotation you were doing. `--description` changes configuration without touching
variables, which is all that is needed to get new environments.

**All eight functions**, because the layer is shared and any of them may hold a stale
pool. Skipping step 2 produces the worst kind of failure: the old credential keeps
working until the container ages out, so it looks fine and then breaks hours later with
no deploy to blame.

---

## 6 · Record the demo

`DEMO_SCRIPT.md` is the word-for-word script with timings and camera states.
`JUDGE_QA.md` is the Q&A crib. `architecture.svg` is the diagram for the write-up.

Before every take: `make demo-check`. Between takes, if a beat has worn out:
`make demo-reset`.

---

## Troubleshooting

**`make verify` fails on the vector index check.** Index backfills run as
asynchronous schema-change jobs. Wait a couple of minutes after `make seed` and
re-run.

**`make pipeline` reports "no prediction".** The sensory tier has a 2-hour TTL and
Oracle ignores telemetry older than 15 minutes. The pipeline script ramps and
ingests for itself, so this usually means the fleet failed to start — check the
port it printed.

**`make demo-run` refuses to start.** The staged beats have worn out. That is the
system having learned from being rehearsed. `make demo-reset`.

**Mutation and merge report "no proposal produced".** Bedrock is unreachable.
Everything else runs; the birth, mutation and merge paths degrade rather than
inventing a playbook. `make lifecycle` substitutes a deterministic proposer and
stamps `proposed_by: "lifecycle-harness"` on every row it writes.

**A serialization failure escapes.** It should not — that was a real bug found by
`make load` and fixed by moving model calls out of transactions. If you see one,
`make load` is the reproduction.
