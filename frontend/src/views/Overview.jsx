import { useEffect, useMemo, useRef, useState } from 'react'
import { apiPost } from '../lib/api'
import {
  DASH,
  EVENT_COLOR,
  EVENT_GLYPH,
  FLEET_COLOR,
  ago,
  categoryTag,
  duration,
  fixed,
  humanise,
  num,
  pct,
  predictionLabel,
  shortId,
  signedPct,
  timestamp,
  until,
} from '../lib/format'
import {
  Dot,
  EmptyState,
  ErrorState,
  Label,
  Meter,
  Panel,
  PanelHeader,
  Pill,
  Skeleton,
  Stat,
} from '../components/primitives'
import { PosteriorChart } from '../components/PosteriorChart'
import { Sparkline } from '../components/Sparkline'
import { TrajectoryChart } from '../components/TrajectoryChart'

export function Overview({ overview, onNavigate }) {
  const { data, error, loading, refresh } = overview
  // A ramp the operator started in this session: {service, startedAt, response}
  // or {service, startedAt, error}. It is UI state about a request that was
  // made, not data about the system.
  const [ramp, setRamp] = useState(null)

  if (error && !data) return <ErrorState error={error} what="The overview" />

  return (
    <div className="flex flex-col gap-4 p-5">
      <div className="flex items-start gap-4">
        <div className="min-w-0">
          <h1 className="text-[22px] font-semibold tracking-[-0.01em]">Overview</h1>
          <p className="mt-1 text-[12.5px] text-nx-dim">
            {data ? (
              <>
                {data.fleet.length} services · {num(data.memory?.episodic?.count)} precursor
                snapshots in episodic memory · {num(data.memory?.semantic?.count)} active playbooks
                {data.memory?.institutional?.count ? (
                  <> · {num(data.memory.institutional.count)} promoted to institutional</>
                ) : null}
              </>
            ) : (
              <span className="nx-skeleton inline-block h-3 w-96 align-middle" />
            )}
          </p>
        </div>
        <div className="ml-auto flex shrink-0 gap-2">
          <Stat
            label="Prevented"
            value={data ? num(data.counters?.prevented?.value) : DASH}
            color="var(--color-nx-proven)"
            sub="incidents"
          />
          <Stat
            label="Impacted"
            value={data ? num(data.counters?.impacted?.value) : DASH}
            color="var(--color-nx-failing)"
            sub="incidents"
          />
          <Stat
            label="In flight"
            value={data ? num(data.counters?.in_flight?.value) : DASH}
            color="var(--color-nx-accent)"
            sub="predictions"
          />
          <Stat
            label="Shadow"
            value={data ? num(data.counters?.shadowed?.value) : DASH}
            color="var(--color-nx-institutional)"
            sub="predictions"
          />
        </div>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_360px] gap-4">
        <CentrePanel centre={data?.centre} loading={loading} ramp={ramp} onOpen={onNavigate} />
        <div className="flex flex-col gap-4">
          <BacktestPanel backtest={data?.backtest} loading={loading} />
          <EvolutionFeed
            events={data?.evolution_feed}
            loading={loading}
            onOpen={() => onNavigate('evolution')}
          />
        </div>
      </div>

      <FleetStrip
        fleet={data?.fleet}
        loading={loading}
        ramp={ramp}
        onRamp={async (service) => {
          setRamp({ service, startedAt: Date.now(), pending: true })
          try {
            const response = await apiPost('/fleet/ramp', { service, speed: 4 })
            setRamp({ service, startedAt: Date.now(), response })
            refresh()
          } catch (e) {
            setRamp({ service, startedAt: Date.now(), error: e })
          }
        }}
      />
    </div>
  )
}

/* ------------------------------------------------------------------------ */
/* Centre panel                                                              */
/* ------------------------------------------------------------------------ */

function CentrePanel({ centre, loading, ramp, onOpen }) {
  if (loading && !centre) {
    return (
      <Panel>
        <PanelHeader label="Prediction" />
        <div className="p-5">
          <Skeleton rows={5} height={16} />
        </div>
      </Panel>
    )
  }
  if (!centre) return null

  const isActive = centre.kind === 'active_prediction'

  return (
    <Panel className="flex flex-col">
      <PanelHeader
        label={centre.heading}
        sub={
          isActive
            ? 'oracle emitted · pipeline in flight'
            : centre.kind === 'last_prediction'
              ? 'most recent resolved prediction'
              : centre.kind === 'last_prevention'
                ? 'most recent prevented incident'
                : null
        }
        right={
          centre.kind === 'empty' ? null : (
            <button
              type="button"
              onClick={() => onOpen(centre.kind === 'last_prevention' ? 'playbooks' : 'predictions')}
              className="nx-num rounded border border-nx-line px-2 py-1 text-[9px] tracking-[0.08em] text-nx-muted transition-colors hover:border-nx-line-strong hover:text-nx-text"
            >
              OPEN →
            </button>
          )
        }
      >
        <Dot
          color={isActive ? 'var(--color-nx-accent)' : 'var(--color-nx-faint-2)'}
          pulse={isActive}
        />
      </PanelHeader>

      {/* Keyed on the start time so each ramp gets a fresh elapsed counter. */}
      {ramp ? <RampStatus key={ramp.startedAt} ramp={ramp} centre={centre} /> : null}

      {centre.kind === 'empty' ? (
        <EmptyState
          title="No prediction history"
          body="Neither predictions nor incidents contain a row. Seed the demo world with `make seed`, then start the fleet with `make live`."
          source="predictions, incidents"
        />
      ) : centre.kind === 'last_prevention' ? (
        <IncidentEvidence incident={centre.incident} />
      ) : (
        <PredictionEvidence
          prediction={centre.prediction}
          pipeline={centre.pipeline}
          neighbors={centre.neighbors}
          trajectory={centre.trajectory}
        />
      )}
    </Panel>
  )
}

/**
 * The Oracle → Sentinel → Guardian → Chronicler stepper.
 *
 * Every timestamp is the column value the API reported. A stage with no
 * timestamp shows none. Nothing advances on a timer — a stage moves only when
 * the database says it moved.
 */
function Pipeline({ stages }) {
  if (!stages?.length) return null
  return (
    <div className="flex border-t border-nx-line">
      {stages.map((stage, i) => {
        const color =
          stage.state === 'done'
            ? 'var(--color-nx-proven)'
            : stage.state === 'active'
              ? 'var(--color-nx-accent)'
              : 'var(--color-nx-faint-4)'
        return (
          <div
            key={stage.agent}
            className="relative flex-1 px-3 py-2.5"
            style={{
              borderRight: i < stages.length - 1 ? '1px solid var(--color-nx-line-soft)' : 'none',
              background: stage.state === 'active' ? 'rgba(124,158,255,.055)' : 'transparent',
            }}
          >
            <div className="flex items-center gap-2">
              <Dot color={color} pulse={stage.state === 'active'} />
              <span
                className="text-[12px]"
                style={{
                  color:
                    stage.state === 'pending' ? 'var(--color-nx-faint)' : 'var(--color-nx-text)',
                }}
              >
                {stage.agent}
              </span>
            </div>
            <div className="mt-1.5 flex flex-col gap-0.5">
              <span className="nx-num text-[9.5px] text-nx-dim">
                {stage.at ? timestamp(stage.at).slice(11) : DASH}
              </span>
              <span className="truncate text-[9.5px] text-nx-faint-2">
                {stage.detail ?? stage.source_column}
              </span>
            </div>
            <span
              className="absolute bottom-0 left-0 h-px transition-[width] duration-500"
              style={{
                width: stage.state === 'done' ? '100%' : stage.state === 'active' ? '45%' : '0%',
                background: color,
              }}
            />
          </div>
        )
      })}
    </div>
  )
}

function PredictionEvidence({ prediction, pipeline, neighbors, trajectory }) {
  if (!prediction) return null
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-start gap-4 px-5 pt-4 pb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Pill color="var(--color-nx-failing)">{categoryTag(prediction.causal_pattern)}</Pill>
            <span className="text-[15px] font-medium tracking-[-0.01em]">
              {humanise(prediction.causal_pattern)}
            </span>
          </div>
          <div className="nx-num mt-1.5 flex items-center gap-1.5 text-[10.5px] text-nx-dim">
            <span>{prediction.service_name}</span>
            <span className="text-nx-faint-3">/</span>
            <span>{prediction.region ?? DASH}</span>
            <span className="text-nx-faint-3">/</span>
            <span>{shortId(prediction.id)}</span>
            <span className="text-nx-faint-3">/</span>
            <span>{prediction.claimed_by ?? 'unclaimed'}</span>
          </div>
        </div>
        <div className="ml-auto flex shrink-0 flex-col items-end gap-1">
          <Label>{prediction.resolved_at ? 'Resolved' : 'Predicted failure in'}</Label>
          <span className="nx-num text-[19px] leading-none text-nx-text">
            {prediction.resolved_at ? ago(prediction.resolved_at) : until(prediction.predicted_eta)}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-[1fr_240px] gap-5 border-t border-nx-line px-5 py-4">
        <div>
          <div className="flex items-center gap-2">
            <Label>Posterior</Label>
            <span className="nx-num text-[10px] text-nx-faint-2">
              Beta(α={fixed(prediction.alpha, 1)}, β={fixed(prediction.beta, 1)})
            </span>
          </div>
          <PosteriorChart
            alpha={prediction.alpha}
            beta={prediction.beta}
            ciLow={prediction.ci_low}
            ciHigh={prediction.ci_high}
            mean={prediction.posterior_mean}
            height={120}
          />
        </div>
        <div className="flex flex-col justify-center gap-3">
          <div>
            <span className="nx-num text-[30px] leading-none text-nx-text">
              {pct(prediction.posterior_mean)}
            </span>
            <div className="nx-num mt-1.5 text-[10px] text-nx-dim">
              90% CI [{fixed(prediction.ci_low, 3)} – {fixed(prediction.ci_high, 3)}]
            </div>
          </div>
          <div className="flex flex-col gap-2 border-t border-nx-line pt-3">
            <Row label="Matched" value={num(prediction.matching_precursor_count)} />
            <Row label="Severity" value={num(prediction.predicted_severity)} />
            <Row label="Status" value={predictionLabel(prediction)} />
          </div>
        </div>
      </div>

      <div className="grid flex-1 grid-cols-[1fr_240px] gap-5 border-t border-nx-line px-5 py-4">
        <div className="flex min-w-0 flex-col">
          {trajectory ? (
            <TrajectoryChart trajectory={trajectory} />
          ) : (
            <p className="mt-2 text-[11px] leading-relaxed text-nx-faint">
              The 2-hour sensory TTL has expired this service&rsquo;s window, or the fleet
              generator is not running. The posterior above was still built from it.
            </p>
          )}
        </div>

        <div className="min-w-0">
          <div className="flex items-baseline gap-2">
            <Label>Retrieved</Label>
            <span className="nx-num text-[10px] text-nx-faint-2">
              k={num(prediction.matching_precursor_count)} · cosine
            </span>
          </div>
          <div className="mt-2 flex flex-col gap-1.5">
            {(neighbors ?? []).length ? (
              neighbors.map((n) => (
                <div key={n.id} className="flex items-center gap-2">
                  <span className="nx-num w-[62px] shrink-0 truncate text-[9.5px] text-nx-dim">
                    {shortId(n.id)}
                  </span>
                  <Meter
                    value={Math.min(1, n.distance / 0.3)}
                    color={
                      n.led_to_incident
                        ? 'var(--color-nx-failing)'
                        : 'var(--color-nx-proven)'
                    }
                  />
                  <span className="nx-num w-[42px] shrink-0 text-right text-[9.5px] text-nx-muted">
                    {fixed(n.distance, 3)}
                  </span>
                </div>
              ))
            ) : (
              <span className="text-[11px] text-nx-faint">
                No neighbours returned for this prediction.
              </span>
            )}
          </div>
        </div>
      </div>

      <Pipeline stages={pipeline} />
    </div>
  )
}

// The four trajectory channels, in the mockup's order: the alarming one first.
const CHANNEL_COLORS = [
  'var(--color-nx-failing)',
  'var(--color-nx-accent)',
  'var(--color-nx-experimental)',
  'var(--color-nx-proven)',
]

/**
 * The fallback the Overview lands on almost all of the time: the most recent
 * incident the system prevented, with the precursor window that preceded it.
 * Every field is a column on `incidents` or its `precursor_snapshots` row.
 */
function IncidentEvidence({ incident }) {
  if (!incident) return null
  const metrics = incident.precursor?.metrics ?? {}
  const names = Object.keys(metrics).slice(0, 4)
  const colors = [
    'var(--color-nx-failing)',
    'var(--color-nx-accent)',
    'var(--color-nx-experimental)',
    'var(--color-nx-proven)',
  ]

  return (
    <>
      <div className="flex items-start gap-4 px-5 pt-4 pb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Pill color="var(--color-nx-proven)">PREVENTED</Pill>
            <span className="text-[15px] font-medium tracking-[-0.01em]">{incident.title}</span>
          </div>
          <div className="nx-num mt-1.5 flex flex-wrap items-center gap-1.5 text-[10.5px] text-nx-dim">
            <span>{(incident.affected_services ?? []).join(', ') || DASH}</span>
            <span className="text-nx-faint-3">/</span>
            <span>{incident.region ?? DASH}</span>
            <span className="text-nx-faint-3">/</span>
            <span>{shortId(incident.id)}</span>
            <span className="text-nx-faint-3">/</span>
            <span>severity {incident.severity}</span>
          </div>
        </div>
        <div className="ml-auto flex shrink-0 flex-col items-end gap-1">
          <Label>Detected</Label>
          <span className="nx-num text-[19px] leading-none text-nx-text">
            {ago(incident.detected_at)}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-[1fr_240px] gap-5 border-t border-nx-line px-5 py-4">
        <div>
          {incident.precursor?.metrics ? (
            <TrajectoryChart trajectory={{ metrics: incident.precursor.metrics }} />
          ) : (
            <p className="mt-3 text-[11.5px] text-nx-faint">
              This incident has no row in <span className="nx-num">precursor_snapshots</span>, so
              there is no trajectory to draw.
            </p>
          )}
        </div>

        <div className="flex min-w-0 flex-col gap-2.5">
          <Row label="Root cause" value={incident.root_cause ?? DASH} wrap />
          <Row label="MTTR" value={duration(incident.mttr_seconds)} />
          <Row label="Resolved" value={ago(incident.resolved_at)} />
          <Row label="Predicted" value={incident.was_predicted ? 'yes' : 'no'} />
          <Row label="Auto-resolved" value={incident.was_auto_resolved ? 'yes' : 'no'} />
          <Row
            label="Playbook"
            value={incident.playbook_used ? shortId(incident.playbook_used) : DASH}
          />
        </div>
      </div>

      <p className="border-t border-nx-line px-5 py-2.5 text-[10.5px] text-nx-faint-2">
        No prediction is in flight. This is the most recent row in{' '}
        <span className="nx-num">incidents</span> with{' '}
        <span className="nx-num">was_prevented = true</span> — history, not a live pipeline.
      </p>
    </>
  )
}

function Row({ label, value, wrap = false }) {
  return (
    <div className="flex items-baseline gap-3">
      <Label className="w-[86px] shrink-0">{label}</Label>
      <span
        className={`nx-num min-w-0 text-[11px] text-nx-text-2 ${
          wrap ? 'leading-relaxed break-words' : 'truncate'
        }`}
      >
        {value}
      </span>
    </div>
  )
}

/* ------------------------------------------------------------------------ */
/* Ramp                                                                      */
/* ------------------------------------------------------------------------ */

/**
 * Narration for the 60-90 seconds between starting a ramp and a prediction
 * appearing. It reports elapsed time and what the database currently shows —
 * it does not claim progress the pipeline has not made.
 */
function RampStatus({ ramp, centre }) {
  const [elapsed, setElapsed] = useState(0)
  const timer = useRef(null)
  useEffect(() => {
    const started = ramp.startedAt
    timer.current = window.setInterval(
      () => setElapsed(Math.round((Date.now() - started) / 1000)),
      1000,
    )
    return () => window.clearInterval(timer.current)
  }, [ramp.startedAt])

  const gotPrediction = centre?.kind === 'active_prediction'

  if (ramp.error) {
    return (
      <div
        className="flex items-start gap-2.5 border-b border-nx-line px-5 py-3"
        style={{ background: 'color-mix(in srgb, var(--color-nx-failing) 8%, transparent)' }}
      >
        <Dot color="var(--color-nx-failing)" />
        <div className="min-w-0">
          <div className="nx-label" style={{ color: 'var(--color-nx-failing)' }}>
            Ramp not started · {ramp.service}
          </div>
          <p className="mt-1 text-[11.5px] leading-relaxed text-nx-muted">{ramp.error.message}</p>
          <span className="nx-num text-[9.5px] text-nx-faint-2">{ramp.error.code}</span>
        </div>
      </div>
    )
  }

  return (
    <div
      className="flex items-center gap-3 border-b border-nx-line px-5 py-3"
      style={{
        background: `color-mix(in srgb, ${
          gotPrediction ? 'var(--color-nx-proven)' : 'var(--color-nx-accent)'
        } 7%, transparent)`,
      }}
    >
      <Dot
        color={gotPrediction ? 'var(--color-nx-proven)' : 'var(--color-nx-accent)'}
        pulse={!gotPrediction}
      />
      <div className="min-w-0 flex-1">
        <div className="nx-label text-nx-muted">
          {ramp.pending
            ? `Starting ramp on ${ramp.service}`
            : gotPrediction
              ? `Prediction emitted for ${ramp.service}`
              : `Ramp running on ${ramp.service}`}
        </div>
        <p className="mt-1 text-[11.5px] text-nx-dim">
          {gotPrediction
            ? 'Oracle wrote a row into predictions. The stepper below is reading it.'
            : 'Telemetry is landing in the sensory tier. Oracle samples it on a 60-second cadence; the stepper advances when a row appears in predictions, not before.'}
        </p>
      </div>
      <div className="flex shrink-0 flex-col items-end">
        <Label>Elapsed</Label>
        <span className="nx-num text-[17px] leading-none text-nx-text">{elapsed}s</span>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------------ */
/* Fleet                                                                     */
/* ------------------------------------------------------------------------ */

function FleetStrip({ fleet, loading, ramp, onRamp }) {
  if (loading && !fleet) {
    return (
      <Panel>
        <PanelHeader label="Fleet" />
        <div className="p-5">
          <Skeleton rows={2} height={40} />
        </div>
      </Panel>
    )
  }
  if (!fleet?.length) {
    return (
      <Panel>
        <PanelHeader label="Fleet" />
        <EmptyState
          title="No services"
          body="Neither telemetry_embeddings nor precursor_snapshots names a service, so there is no fleet to show."
          source="telemetry_embeddings, precursor_snapshots"
        />
      </Panel>
    )
  }

  return (
    <Panel>
      <PanelHeader
        label="Fleet"
        sub={`${fleet.length} services · telemetry → sensory tier`}
        right={
          <span className="flex items-center gap-2 rounded border border-nx-accent/40 bg-nx-accent/10 px-2.5 py-1 text-[10.5px] font-medium text-nx-accent">
            <span className="text-[12px] leading-none">⚡</span>
            Click a service to induce a ramp
          </span>
        }
      />
      <div className="grid" style={{ gridTemplateColumns: `repeat(${fleet.length}, minmax(0,1fr))` }}>
        {fleet.map((service, i) => (
          <FleetTile
            key={service.service_name}
            service={service}
            last={i === fleet.length - 1}
            ramping={ramp?.service === service.service_name}
            onRamp={() => onRamp(service.service_name)}
          />
        ))}
      </div>
    </Panel>
  )
}

function FleetTile({ service, last, ramping, onRamp }) {
  const color = FLEET_COLOR[service.status] ?? FLEET_COLOR.unknown
  const series = useMemo(() => {
    const points = service.sparkline ?? []
    if (!points.length) return []
    const key =
      'latency_p99_ms' in (points[0] ?? {}) ? 'latency_p99_ms' : Object.keys(points[0] ?? {})[0]
    return key ? points.map((p) => p[key]) : []
  }, [service.sparkline])

  const latencyKey = 'latency_p99_ms'
  const latest = service.latest?.[latencyKey]

  return (
    <button
      type="button"
      onClick={onRamp}
      title={`Induce a load ramp on ${service.service_name}`}
      className="group flex cursor-pointer flex-col items-start gap-1.5 px-3.5 py-3 text-left transition-colors hover:bg-white/[0.04]"
      style={{ borderRight: last ? 'none' : '1px solid var(--color-nx-line-soft)' }}
    >
      <div className="flex w-full items-center gap-2">
        <Dot color={color} pulse={service.status === 'failing' || ramping} />
        <span className="truncate text-[12.5px] font-medium text-nx-text-2">
          {service.service_name}
        </span>
        <span className="nx-num ml-auto text-[9px] text-nx-faint opacity-0 transition-opacity group-hover:opacity-100">
          RAMP →
        </span>
      </div>

      <span className="nx-num text-[9.5px] text-nx-faint">
        {service.region ?? DASH}
        {service.region_derived ? ' (derived)' : ''}
      </span>

      <Sparkline points={series} color={color} />

      <div className="flex w-full items-baseline gap-1.5">
        <span className="nx-num text-[14px] text-nx-text">
          {latest === undefined ? DASH : num(latest, 0)}
        </span>
        <span className="text-[9px] text-nx-faint">ms p99</span>
        <span className="nx-num ml-auto text-[10px] text-nx-dim">
          {signedPct(service.delta_pct)}
        </span>
      </div>

      {/* Each channel's latest sample as a fraction of its declared operating
          range. The scales come from the API, which reads them from the same
          table the embedding quantizes against. */}
      {service.levels?.length ? (
        <div className="flex w-full gap-2 pt-0.5">
          {service.levels.slice(0, 3).map((level) => {
            const bar =
              level.level > 0.72
                ? 'var(--color-nx-failing)'
                : level.level > 0.48
                  ? 'var(--color-nx-experimental)'
                  : 'var(--color-nx-proven)'
            return (
              <div
                key={level.metric}
                className="flex min-w-0 flex-1 flex-col gap-1"
                title={`${level.metric} = ${level.value} ${level.unit}${
                  level.trend ? ` · ${level.trend}` : ''
                }`}
              >
                <div className="h-[3px] w-full overflow-hidden rounded-full bg-white/[0.07]">
                  <div
                    className="h-full rounded-full transition-[width] duration-500"
                    style={{ width: `${level.level * 100}%`, background: bar }}
                  />
                </div>
                <span className="nx-num truncate text-[8px] text-nx-faint-2">
                  {level.metric.replace(/_(pct|ms|utilization|rate)$/, '').slice(0, 9)}
                </span>
              </div>
            )
          })}
        </div>
      ) : null}

      <div className="flex w-full items-center gap-2 pt-0.5">
        <span className="nx-num text-[9px] uppercase tracking-[0.08em]" style={{ color }}>
          {service.status}
        </span>
        <span className="nx-num ml-auto text-[9px] text-nx-faint-2">
          {service.telemetry_samples === null
            ? 'unreadable'
            : `${num(service.telemetry_samples)} samples`}
        </span>
      </div>
      {service.status === 'unknown' ? (
        <span className="text-[9.5px] leading-tight text-nx-faint-2">
          no telemetry inside the 2h sensory TTL
        </span>
      ) : null}
    </button>
  )
}

/* ------------------------------------------------------------------------ */
/* Right column                                                              */
/* ------------------------------------------------------------------------ */

function BacktestPanel({ backtest, loading }) {
  return (
    <Panel>
      <PanelHeader
        label="Backtest"
        sub={backtest ? `k=${backtest.k} · threshold ${backtest.threshold}` : null}
      />
      {loading && !backtest ? (
        <div className="p-4">
          <Skeleton rows={3} height={14} />
        </div>
      ) : !backtest ? (
        <EmptyState
          title="Not computed"
          body="The episodic tier holds no snapshots to score."
          source="precursor_snapshots"
        />
      ) : (
        <div className="flex flex-col gap-3 p-4">
          {[
            {
              label: 'Precision',
              value: fixed(backtest.precision, 3),
              ratio: backtest.precision,
              color: 'var(--color-nx-proven)',
            },
            {
              label: 'Recall',
              value: fixed(backtest.recall, 3),
              ratio: backtest.recall,
              color: 'var(--color-nx-accent)',
            },
            {
              label: 'Median lead',
              value:
                backtest.median_lead_minutes === null
                  ? DASH
                  : `${num(backtest.median_lead_minutes)}m`,
              ratio:
                backtest.median_lead_minutes === null
                  ? null
                  : Math.min(1, backtest.median_lead_minutes / 180),
              color: 'var(--color-nx-experimental)',
            },
          ].map((row) => (
            <div key={row.label} className="flex flex-col gap-1.5">
              <div className="flex items-baseline gap-2">
                <Label>{row.label}</Label>
                <span className="nx-num ml-auto text-[13px] text-nx-text">{row.value}</span>
              </div>
              <Meter value={row.ratio} color={row.color} />
            </div>
          ))}

          {backtest.calibration?.some((bucket) => bucket.n) ? (
            <div className="mt-1 border-t border-nx-line pt-2.5">
              <Label>Calibration · stated vs realized</Label>
              <div className="mt-2 flex flex-col gap-1">
                {backtest.calibration
                  .filter((bucket) => bucket.n)
                  .map((bucket) => (
                    <div key={bucket.bucket} className="nx-num flex items-baseline gap-2 text-[10px]">
                      <span className="w-[74px] shrink-0 text-nx-dim">{bucket.bucket}</span>
                      <span className="w-6 shrink-0 text-nx-faint-2">n{bucket.n}</span>
                      <span className="text-nx-muted">{fixed(bucket.stated, 2)}</span>
                      <span className="text-nx-faint-3">→</span>
                      <span className="text-nx-text-2">{fixed(bucket.realized, 2)}</span>
                      <span
                        className="ml-auto"
                        style={{
                          color:
                            Math.abs(bucket.gap) < 0.1
                              ? 'var(--color-nx-proven)'
                              : 'var(--color-nx-experimental)',
                        }}
                      >
                        {bucket.gap > 0 ? '+' : ''}
                        {fixed(bucket.gap, 2)}
                      </span>
                    </div>
                  ))}
              </div>
            </div>
          ) : null}

          <p className="mt-1 border-t border-nx-line pt-2.5 text-[10.5px] leading-relaxed text-nx-faint-2">
            {backtest.false_positive} false positives, {backtest.false_negative} missed, over{' '}
            {backtest.sample_size} windows. Shown, not hidden. Method:{' '}
            <span className="nx-num">{backtest.method}</span>
            {backtest.out_of_sample === false ? (
              <>
                {' '}
                — <span style={{ color: 'var(--color-nx-experimental)' }}>in-sample</span>, because
                no stored held-out run exists yet. Run{' '}
                <span className="nx-num">make backtest</span> for out-of-sample numbers.
              </>
            ) : (
              <>
                {' '}
                against {num(backtest.memory_size)} remembered snapshots, using windows withheld
                from the database entirely.
              </>
            )}{' '}
            Computed {ago(backtest.computed_at)}.
          </p>
        </div>
      )}
    </Panel>
  )
}

function EvolutionFeed({ events, loading, onOpen }) {
  return (
    <Panel className="flex min-h-0 flex-col">
      <PanelHeader
        label="Evolution"
        sub="append-only"
        right={
          <button
            type="button"
            onClick={onOpen}
            className="nx-num rounded border border-nx-line px-2 py-1 text-[9px] tracking-[0.08em] text-nx-muted transition-colors hover:border-nx-line-strong hover:text-nx-text"
          >
            TREE →
          </button>
        }
      />
      {loading && !events ? (
        <div className="p-4">
          <Skeleton rows={5} height={14} />
        </div>
      ) : !events?.length ? (
        <EmptyState
          title="No lifecycle events"
          body="evolution_log is empty. Every playbook transition writes a row here."
          source="evolution_log"
        />
      ) : (
        <div className="max-h-[380px] overflow-auto">
          {events.map((event) => (
            <EventRow key={event.id} event={event} />
          ))}
        </div>
      )}
    </Panel>
  )
}

export function EventRow({ event, showDelta = true }) {
  const color = EVENT_COLOR[event.event_type] ?? 'var(--color-nx-muted)'
  return (
    <div className="nx-in flex items-start gap-2.5 border-b border-nx-line-soft px-4 py-2.5 last:border-b-0">
      <span
        className="nx-num mt-0.5 flex h-[17px] w-[17px] shrink-0 items-center justify-center rounded text-[10px]"
        style={{ background: `color-mix(in srgb, ${color} 14%, transparent)`, color }}
      >
        {EVENT_GLYPH[event.event_type] ?? '·'}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="nx-num text-[9.5px] tracking-[0.09em]" style={{ color }}>
            {event.event_type.toUpperCase()}
          </span>
          <span className="truncate text-[11.5px] text-nx-text-3">
            {event.playbook_name ?? DASH}
          </span>
          <span className="nx-num ml-auto shrink-0 text-[9.5px] text-nx-faint-2">
            {ago(event.created_at)}
          </span>
        </div>
        {showDelta && event.fitness_before !== null && event.fitness_after !== null ? (
          <div className="nx-num mt-1 flex items-center gap-1.5 text-[10px]">
            <span className="text-nx-dim">{fixed(event.fitness_before, 3)}</span>
            <span className="text-nx-faint-3">→</span>
            <span
              style={{
                color:
                  event.fitness_after > event.fitness_before
                    ? 'var(--color-nx-proven)'
                    : 'var(--color-nx-failing)',
              }}
            >
              {fixed(event.fitness_after, 3)}
            </span>
          </div>
        ) : null}
        <EventDetails details={event.details} />
      </div>
    </div>
  )
}

// `details` is free-form JSONB written by the agents, so the renderer cannot
// assume any key exists. It picks out the one prose key if there is one and
// shows the rest as compact chips, which is the difference between a feed and
// a JSON dump.
const PROSE_KEYS = ['note', 'reason', 'summary', 'message', 'detail']
const HIDDEN_KEYS = ['prediction_id', 'playbook_id', 'incident_id']

function EventDetails({ details }) {
  const entries = Object.entries(details ?? {}).filter(
    ([, value]) => value !== null && value !== undefined && value !== '',
  )
  if (!entries.length) return null

  const proseKey = PROSE_KEYS.find((key) => typeof details[key] === 'string')
  const prose = proseKey ? details[proseKey] : null
  const chips = entries.filter(
    ([key, value]) =>
      key !== proseKey && !HIDDEN_KEYS.includes(key) && typeof value !== 'object',
  )

  return (
    <>
      {prose ? (
        <p className="mt-1 text-[11px] leading-relaxed text-nx-dim">{prose}</p>
      ) : null}
      {chips.length ? (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {chips.map(([key, value]) => (
            <span
              key={key}
              className="nx-num rounded-[3px] bg-white/[0.04] px-1.5 py-0.5 text-[9px] text-nx-faint"
            >
              <span className="text-nx-faint-2">{key.replace(/_/g, ' ')}</span>{' '}
              <span className="text-nx-muted-3">{String(value)}</span>
            </span>
          ))}
        </div>
      ) : null}
    </>
  )
}
