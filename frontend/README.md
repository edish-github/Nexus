# NEXUS dashboard

A static React bundle that reads the CockroachDB memory layer through the
dashboard Lambda. No SSR, no server runtime — `npm run build` produces something
you can drop on S3 + CloudFront or Vercel.

## The rule this codebase is built around

**The frontend contains zero domain data.** No fallback objects, no seeded
constants, no example arrays, no mock module. Every number, name, id, timestamp
and posterior on screen came from an HTTP response.

Three states, and only three:

| state | what you see |
| --- | --- |
| not loaded yet | a shimmer skeleton |
| loaded, empty | a designed empty state naming the table that was consulted |
| loaded, present | the value, exactly as the API sent it |

A missing value renders as an em dash, never as `0`. A service with no telemetry
reads `unknown`, never `healthy`. If the API is unreachable the last good
response stays on screen behind a banner that says so and timestamps it — the
view is never cleared to zeros, and an error is never swallowed.

The only arithmetic done here is drawing a Beta density from the `alpha` and
`beta` the API sends (`src/lib/beta.js`). The mean and credible interval are
computed server-side and displayed as received, so no two clients can disagree.

## Running it

```bash
cp .env.example .env          # point VITE_API_BASE_URL at the API
npm install
npm run dev                   # http://localhost:5173
```

The API can be either half of the same handler:

```bash
make dashboard                # local: http://127.0.0.1:8787
make outputs                  # deployed: the DashboardApiUrl output
```

For the fleet ramp control to do anything, the synthetic fleet has to be
running and the dashboard Lambda has to know where it is:

```bash
make live                     # generator control API on :8000
GENERATOR_URL=http://127.0.0.1:8000 make dashboard
```

Without it the ramp button returns `generator_not_configured` and the UI says
exactly that. It does not report a ramp it did not start.

## Build

```bash
npm run build                 # -> dist/
npm run preview               # serve dist/ locally
```

`VITE_API_BASE_URL` is baked in at build time, so build once per environment.

## Layout

```
src/
  lib/api.js          the only fetch in the app; typed ApiError
  lib/usePolled.js    polling: no stacked requests, pauses on hidden tab,
                      keeps last good data through a failure
  lib/format.js       formatting + the five status colours. No domain values.
  lib/beta.js         Beta density curve from API-supplied alpha/beta
  lib/layoutTree.js   tidy-tree layout for the genealogy graph
  components/         panels, meters, skeletons, empty states, charts
  views/              one file per screen
```

Polling: 5s for Overview, Predictions, Evolution and Approvals; 30s for
Playbooks. Every interval pauses while the tab is hidden and refetches
immediately on return.

The endpoints, query params and response shapes are specified in
[API_CONTRACT.md](API_CONTRACT.md). That document is the contract between this
bundle and `agents/dashboard/app.py`; change one and change the other.

## Design tokens

Extracted from the mockup, not re-derived by eye — see the `@theme` block in
`src/index.css`. The five status colours are the whole visual grammar:

| colour | meaning |
| --- | --- |
| `#46D39A` green | proven |
| `#F2B457` amber | experimental |
| `#FF6B5E` red | failing |
| `#B98BFF` purple | institutional |
| `#4E5663` grey | retired |

Type is Instrument Sans for prose and JetBrains Mono for every identifier,
number and SQL fragment. Ligatures are disabled in the SQL panel so `<=>` reads
as the operator it is rather than as a single arrow glyph.
