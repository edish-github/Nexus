# Running NEXUS

Start here. Everything below has been run against the live cluster; where
something has not, it says so.

---

## 0 · Once, before anything

```bash
cp .env.example .env         # then fill in DB_DSN — the CockroachDB Cloud connection string
make deps                    # uv sync
cd frontend && npm ci && cd ..
```

`.env` is gitignored and stays that way. `DB_DSN` is the only variable you must
set for the whole backend to work; everything else has a working default.

**Embeddings.** With AWS credentials, `EMBEDDING_PROVIDER=auto` uses Amazon Titan
Text Embeddings V2. Without them it falls back to a deterministic local embedder
and says so in every log line and in `demo/seed_manifest.json`. The two are
different vector spaces, so **switching providers requires a full re-seed** —
vectors written by one are meaningless to the other.

---

## 1 · Build the world (~4 minutes)

```bash
make seed                    # migrate, then build the whole demo world
make verify                  # 18 checks against the live cluster
make demo-check              # are the five staged demo beats stageable?
make backtest                # score Oracle on withheld windows, store the run
```

`make seed` is idempotent and destructive in the right way: it truncates and
rebuilds, so it is also `make demo-reset`. Expect ~220s, almost all of it writing
1024-dimension vectors across three regions.

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

Complete in the repo, never deployed — `aws`, `sam` and `ccloud` are not installed
on the build machine and the CloudFormation stack has never been created. In order:

```bash
make secrets                 # prints what to put in Secrets Manager
make deploy                  # sam build (container) + sam deploy, one command
make outputs                 # receiver Function URL, bus name, state machine ARNs, bucket
# paste the receiver URL and shared secret into sql/changefeed.sql, then:
make changefeed              # create the changefeed on `predictions`
```

Set `GeneratorUrl` on the stack to wherever the fleet simulator runs, or Guardian
will refuse to act rather than report a fix that never ran.

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
