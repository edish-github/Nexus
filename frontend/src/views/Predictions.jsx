import { useState } from 'react'
import { usePolled } from '../lib/usePolled'
import { apiGet } from '../lib/api'
import {
  DASH,
  PREDICTION_COLOR,
  ago,
  duration,
  fixed,
  humanise,
  num,
  pct,
  predictionLabel,
  shortId,
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
} from '../components/primitives'
import { PosteriorChart } from '../components/PosteriorChart'

export function Predictions() {
  const list = usePolled('/predictions', { intervalMs: 5000, params: { limit: 60 } })
  const [selected, setSelected] = useState(null)

  const predictions = list.data?.predictions ?? []
  const activeId = selected && predictions.some((p) => p.id === selected)
    ? selected
    : (predictions[0]?.id ?? null)

  const detail = usePolled(activeId ? `/predictions/${activeId}` : '/predictions', {
    intervalMs: 5000,
    enabled: Boolean(activeId),
  })

  if (list.error && !list.data) return <ErrorState error={list.error} what="Predictions" />

  return (
    <div className="flex h-full min-h-0">
      <div className="flex w-[300px] shrink-0 flex-col border-r border-nx-line">
        <div className="border-b border-nx-line px-4 py-3.5">
          <h1 className="text-[17px] font-semibold tracking-[-0.01em]">Predictions</h1>
          <p className="mt-0.5 text-[11px] text-nx-dim">
            {list.data ? `${list.data.total} rows in predictions` : DASH}
          </p>
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          {list.loading && !list.data ? (
            <div className="p-4">
              <Skeleton rows={6} height={44} />
            </div>
          ) : !predictions.length ? (
            <EmptyState
              title="No predictions"
              body="The predictions table is empty. Oracle writes a row when a live telemetry window matches the precursor memory closely enough — induce a ramp from the Overview fleet strip to drive one."
              source="predictions"
            />
          ) : (
            predictions.map((prediction) => (
              <ListRow
                key={prediction.id}
                prediction={prediction}
                selected={prediction.id === activeId}
                onClick={() => setSelected(prediction.id)}
              />
            ))
          )}
        </div>
      </div>

      <div className="min-w-0 flex-1 overflow-auto">
        {!activeId ? (
          <EmptyState
            title="Nothing to inspect"
            body="Select a prediction to see its retrieved precursors, the SQL that fetched them, the Thompson-sampling competition, and the provenance replay."
            source="predictions"
          />
        ) : detail.error && !detail.data ? (
          <ErrorState error={detail.error} what="This prediction" />
        ) : !detail.data ? (
          <div className="p-6">
            <Skeleton rows={8} height={20} />
          </div>
        ) : (
          <Detail detail={detail.data} />
        )}
      </div>
    </div>
  )
}

function ListRow({ prediction, selected, onClick }) {
  const color = prediction.awaiting_approval
    ? 'var(--color-nx-experimental)'
    : (PREDICTION_COLOR[prediction.prevention_status] ?? 'var(--color-nx-muted)')
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full flex-col items-start gap-1.5 border-b border-nx-line-soft px-4 py-3 text-left transition-colors"
      style={{
        borderLeft: `2px solid ${selected ? color : 'transparent'}`,
        background: selected ? 'rgba(255,255,255,.045)' : 'transparent',
      }}
    >
      <div className="flex w-full items-center gap-2">
        <Pill color={color}>{predictionLabel(prediction)}</Pill>
        <span className="nx-num ml-auto text-[9.5px] text-nx-faint-2">
          {ago(prediction.created_at)}
        </span>
      </div>
      <span className="truncate text-[12.5px] text-nx-text-3">
        {humanise(prediction.causal_pattern)}
      </span>
      <div className="flex w-full items-baseline gap-1.5">
        <span className="nx-num text-[10px] text-nx-dim">{prediction.service_name}</span>
        <span className="text-nx-faint-3">·</span>
        <span className="nx-num text-[10px] text-nx-dim">{prediction.region ?? DASH}</span>
        <span className="nx-num ml-auto text-[11px]" style={{ color }}>
          {pct(prediction.posterior_mean)}
        </span>
      </div>
      <Meter value={prediction.posterior_mean} color={color} />
    </button>
  )
}

function Detail({ detail }) {
  const { prediction, neighbors, posterior_derivation: derivation, competition, execution } = detail
  const color = prediction.awaiting_approval
    ? 'var(--color-nx-experimental)'
    : (PREDICTION_COLOR[prediction.prevention_status] ?? 'var(--color-nx-muted)')

  return (
    <div className="flex flex-col gap-4 p-5">
      <div className="flex items-start gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Pill color={color}>{predictionLabel(prediction)}</Pill>
            {prediction.awaiting_approval ? <Pill>IRREVERSIBLE PLAYBOOK</Pill> : null}
          </div>
          <h2 className="mt-2 text-[19px] font-semibold tracking-[-0.01em]">
            {humanise(prediction.causal_pattern)}
          </h2>
          <div className="nx-num mt-1.5 flex flex-wrap items-center gap-1.5 text-[10.5px] text-nx-dim">
            <span>{prediction.service_name}</span>
            <span className="text-nx-faint-3">/</span>
            <span>{prediction.region ?? DASH}</span>
            <span className="text-nx-faint-3">/</span>
            <span>{prediction.id}</span>
          </div>
          <div className="nx-num mt-1 text-[10px] text-nx-faint-2">
            claimed by {prediction.claimed_by ?? 'nobody'} · commit ts{' '}
            {prediction.commit_ts ?? DASH}
          </div>
        </div>
        <div className="ml-auto flex shrink-0 gap-2">
          <Kpi label="ETA" value={until(prediction.predicted_eta)} />
          <Kpi label="Matched" value={num(prediction.matching_precursor_count)} />
          <Kpi label="Severity" value={num(prediction.predicted_severity)} />
          <Kpi
            label="Resolved"
            value={prediction.resolved_at ? ago(prediction.resolved_at) : DASH}
          />
        </div>
      </div>

      <div className="grid grid-cols-[380px_minmax(0,1fr)] gap-4">
        <Panel>
          <PanelHeader label="Posterior" sub="Beta(α, β) over matched precursor outcomes" />
          <div className="p-4">
            <PosteriorChart
              alpha={prediction.alpha}
              beta={prediction.beta}
              ciLow={prediction.ci_low}
              ciHigh={prediction.ci_high}
              mean={prediction.posterior_mean}
              height={130}
            />
            <div className="mt-3 flex items-end gap-3">
              <span className="nx-num text-[32px] leading-none">
                {pct(prediction.posterior_mean)}
              </span>
              <div className="mb-0.5 flex flex-col gap-0.5">
                <span className="nx-num text-[10px] text-nx-dim">
                  90% CI [{fixed(prediction.ci_low, 3)} – {fixed(prediction.ci_high, 3)}]
                </span>
                <span className="nx-num text-[10px] text-nx-faint-2">
                  Beta(α={fixed(prediction.alpha, 1)}, β={fixed(prediction.beta, 1)})
                </span>
              </div>
            </div>
            <div className="nx-num mt-3 rounded border border-nx-line-soft bg-nx-sunken px-3 py-2 text-[10.5px] leading-relaxed text-nx-dim">
              <div>
                <span className="text-nx-accent">α</span> = {derivation.alpha_expression}
              </div>
              <div>
                <span className="text-nx-accent">β</span> = {derivation.beta_expression}
              </div>
            </div>
          </div>
        </Panel>

        {/* Keyed on the prediction so switching rows drops any open replay
            rather than showing one prediction's provenance under another. */}
        <Retrieval key={prediction.id} detail={detail} neighbors={neighbors} />
      </div>

      <Competition competition={competition} note={detail.competition_note} />
      <Execution execution={execution} status={prediction.prevention_status} />
    </div>
  )
}

function Kpi({ label, value }) {
  return (
    <div className="flex min-w-[76px] flex-col gap-1.5 rounded-md border border-nx-line bg-nx-raised px-3 py-2">
      <Label>{label}</Label>
      <span className="nx-num text-[15px] leading-none text-nx-text">{value}</span>
    </div>
  )
}

/* ------------------------------------------------------------------------ */
/* Retrieval + provenance replay                                             */
/* ------------------------------------------------------------------------ */

function Retrieval({ detail, neighbors }) {
  const [replay, setReplay] = useState(null)
  const [busy, setBusy] = useState(false)
  const predictionId = detail.prediction.id

  async function toggle() {
    if (replay) {
      setReplay(null)
      return
    }
    setBusy(true)
    try {
      setReplay({ data: await apiGet(`/predictions/${predictionId}/replay`) })
    } catch (error) {
      setReplay({ error })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel className="flex min-w-0 flex-col">
      <PanelHeader
        label="Vector retrieval"
        sub={`episodic tier · precursor_snapshots · k=${detail.retrieval_k}`}
        right={
          <button
            type="button"
            onClick={toggle}
            disabled={busy}
            className="nx-num rounded border px-2 py-1 text-[9px] tracking-[0.08em] transition-colors disabled:opacity-50"
            style={{
              borderColor: 'color-mix(in srgb, var(--color-nx-institutional) 40%, transparent)',
              background: 'color-mix(in srgb, var(--color-nx-institutional) 10%, transparent)',
              color: 'var(--color-nx-institutional)',
            }}
          >
            {busy ? 'REPLAYING…' : replay ? 'CLOSE REPLAY' : 'REPLAY AS OF SYSTEM TIME'}
          </button>
        }
      />

      <pre className="nx-sql overflow-x-auto border-b border-nx-line bg-nx-sunken px-4 py-3 text-[10.5px] leading-relaxed text-nx-muted-3">
        {detail.retrieval_sql}
      </pre>

      {replay ? <ReplayPanel replay={replay} /> : null}

      <div className="grid shrink-0 grid-cols-[1fr_100px_150px_60px] gap-2 border-b border-nx-line px-4 py-2">
        <Label>Snapshot</Label>
        <Label>Outcome</Label>
        <Label>Cosine distance</Label>
        <Label>Lead</Label>
      </div>
      <div className="max-h-[300px] overflow-auto">
        {!neighbors.length ? (
          <EmptyState
            title="No neighbours"
            body="The vector search returned nothing, which means precursor_snapshots is empty."
            source="precursor_snapshots"
          />
        ) : (
          neighbors.map((n, i) => (
            <div
              key={n.id}
              className="grid grid-cols-[1fr_100px_150px_60px] items-center gap-2 border-b border-nx-line-soft px-4 py-1.5"
              style={{ background: i % 2 ? 'rgba(255,255,255,.012)' : 'transparent' }}
            >
              <span className="nx-num truncate text-[10.5px] text-nx-muted">{shortId(n.id, 13)}</span>
              <span
                className="nx-num text-[9.5px] tracking-[0.08em]"
                style={{
                  color: n.led_to_incident
                    ? 'var(--color-nx-failing)'
                    : 'var(--color-nx-proven)',
                }}
              >
                {n.led_to_incident ? 'INCIDENT' : 'BENIGN'}
              </span>
              <div className="flex items-center gap-2">
                <Meter
                  value={Math.min(1, n.distance)}
                  color={
                    n.led_to_incident ? 'var(--color-nx-failing)' : 'var(--color-nx-proven)'
                  }
                />
                <span className="nx-num w-[48px] shrink-0 text-right text-[10.5px] text-nx-text-3">
                  {fixed(n.distance, 4)}
                </span>
              </div>
              <span className="nx-num text-[10.5px] text-nx-dim">
                {n.lead_minutes === null ? DASH : `${num(n.lead_minutes)}m`}
              </span>
            </div>
          ))
        )}
      </div>
    </Panel>
  )
}

/**
 * The provenance replay: the same statement, once pinned to the prediction
 * row's own MVCC commit timestamp and once against current state.
 */
function ReplayPanel({ replay }) {
  if (replay.error) {
    const gc = replay.error.code === 'gc_threshold_exceeded'
    return (
      <div
        className="border-b border-nx-line px-4 py-3"
        style={{
          background: `color-mix(in srgb, var(--color-nx-${gc ? 'experimental' : 'failing'}) 8%, transparent)`,
        }}
      >
        <div
          className="nx-label"
          style={{ color: `var(--color-nx-${gc ? 'experimental' : 'failing'})` }}
        >
          {gc ? 'Outside the MVCC retention window' : 'Replay failed'}
        </div>
        <p className="mt-1 max-w-[70ch] text-[11.5px] leading-relaxed text-nx-muted">
          {replay.error.message}
        </p>
        <span className="nx-num text-[9.5px] text-nx-faint-2">{replay.error.code}</span>
      </div>
    )
  }

  const data = replay.data
  // Divergence is not a failure. Diagnostician promotes the very window this
  // prediction was about into the episodic tier, so the live top-k legitimately
  // gains a neighbour that did not exist at decision time. What would be alarming
  // is the *posterior* moving, and that is called out separately below.
  const posteriorHeld =
    data.panes?.length === 2 && data.panes[0].posterior_mean === data.panes[1].posterior_mean
  const verdictColor = data.identical
    ? 'var(--color-nx-proven)'
    : posteriorHeld
      ? 'var(--color-nx-institutional)'
      : 'var(--color-nx-failing)'

  return (
    <div className="border-b border-nx-line bg-nx-sunken">
      <div className="flex items-center gap-3 border-b border-nx-line px-4 py-2.5">
        <span className="nx-label" style={{ color: 'var(--color-nx-institutional)' }}>
          Provenance replay
        </span>
        <span className="text-[10px] text-nx-dim">
          the evidence this decision was made on, re-read at its commit timestamp
        </span>
        <span className="ml-auto">
          <Pill color={verdictColor}>{data.verdict}</Pill>
        </span>
      </div>

      <div className="grid grid-cols-2">
        {data.panes.map((pane, index) => (
          <div
            key={pane.title}
            className="min-w-0 p-3.5"
            style={{ borderRight: index === 0 ? '1px solid var(--color-nx-line)' : 'none' }}
          >
            <div className="flex items-center gap-2">
              <Dot
                color={
                  index === 0 ? 'var(--color-nx-institutional)' : 'var(--color-nx-accent)'
                }
              />
              <span className="nx-label text-nx-muted">{pane.title}</span>
            </div>
            <div className="nx-num mt-2 truncate rounded bg-black/30 px-2 py-1 text-[10px] text-nx-muted-3">
              {pane.clause}
            </div>
            <div className="mt-2.5 flex items-baseline gap-2">
              <span className="nx-num text-[18px] text-nx-text">{pct(pane.posterior_mean)}</span>
              <span className="nx-num text-[10px] text-nx-faint-2">
                Beta(α={fixed(pane.alpha, 1)}, β={fixed(pane.beta, 1)})
              </span>
            </div>
            <div className="mt-2.5 flex flex-col gap-1">
              {pane.rows.slice(0, 8).map((row, i) => {
                const other = data.panes[index === 0 ? 1 : 0]?.rows?.[i]
                const same = other?.id === row.id
                return (
                  <div key={row.id} className="flex items-center gap-2">
                    <span className="nx-num flex-1 truncate text-[10px] text-nx-dim">
                      {shortId(row.id, 13)}
                    </span>
                    <span
                      className="nx-num text-[9px]"
                      style={{
                        color: row.led_to_incident
                          ? 'var(--color-nx-failing)'
                          : 'var(--color-nx-proven)',
                      }}
                    >
                      {row.led_to_incident ? 'incident' : 'benign'}
                    </span>
                    <span className="nx-num w-[54px] text-right text-[10px] text-nx-muted">
                      {fixed(row.distance, 4)}
                    </span>
                    <span
                      className="nx-num w-[10px] text-right text-[10px]"
                      style={{
                        color: same ? 'var(--color-nx-proven)' : 'var(--color-nx-failing)',
                      }}
                    >
                      {same ? '=' : '≠'}
                    </span>
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="border-t border-nx-line px-4 py-2.5">
        <p className="text-[10.5px] leading-relaxed text-nx-faint-2">
          {num(data.drift?.snapshots_written_since)} snapshots have been written to the episodic
          tier since this decision committed, {duration(data.elapsed_since_commit_seconds)} ago.
          The left pane is pinned to the timestamp Oracle recorded inside the transaction that
          wrote the prediction — <span className="nx-num">{data.commit_ts}</span> — not to the
          row&rsquo;s current version, which has since been rewritten by Sentinel&rsquo;s claim and
          Guardian&rsquo;s outcome.
        </p>
        {data.added_since?.length ? (
          <p className="mt-2 text-[10.5px] leading-relaxed text-nx-faint-2">
            {data.added_since.length} neighbour
            {data.added_since.length > 1 ? 's' : ''} in the live top-k did not exist then:{' '}
            {data.added_since.map((row) => (
              <span key={row.id} className="nx-num text-nx-muted">
                {shortId(row.id, 13)}{' '}
              </span>
            ))}
            — that divergence is the proof the left pane is a real read of the past rather than the
            same query run twice.{' '}
            {posteriorHeld ? (
              <span style={{ color: 'var(--color-nx-proven)' }}>
                The posterior is unchanged regardless: the conclusion did not depend on what came
                after it.
              </span>
            ) : null}
          </p>
        ) : null}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------------ */
/* Competition + execution                                                   */
/* ------------------------------------------------------------------------ */

function Competition({ competition, note }) {
  return (
    <Panel>
      <PanelHeader
        label="Competition"
        sub="Thompson sampling · θ ~ Beta(s+1, f+1) · score = θ × cosine similarity"
      />
      {!competition?.length ? (
        <EmptyState
          title="Sentinel has not claimed this prediction"
          body="No playbook has been selected, so there is no candidate field to show. The competition is reconstructed from the candidates' trial counters once playbook_applied is set."
          source="predictions.playbook_applied"
        />
      ) : (
        <>
          <div className="grid grid-cols-[16px_minmax(0,1fr)_50px_80px_80px_90px_80px] gap-2 border-b border-nx-line px-4 py-2">
            <span />
            <Label>Candidate</Label>
            <Label>Gen</Label>
            <Label>Trials</Label>
            <Label>Sim</Label>
            <Label>Sampled θ</Label>
            <Label>Score</Label>
          </div>
          {competition.map((row) => (
            <div
              key={row.playbook_id}
              className="grid grid-cols-[16px_minmax(0,1fr)_50px_80px_80px_90px_80px] items-center gap-2 border-b border-nx-line-soft px-4 py-2"
              style={{
                background: row.winner ? 'color-mix(in srgb, var(--color-nx-experimental) 5%, transparent)' : 'transparent',
              }}
            >
              <span
                className="text-[9px]"
                style={{ color: row.winner ? 'var(--color-nx-experimental)' : 'transparent' }}
              >
                ★
              </span>
              <div className="min-w-0">
                <div className="truncate text-[12px] text-nx-text-3">{row.name}</div>
                <div className="nx-num truncate text-[9px] text-nx-faint-2">
                  {shortId(row.playbook_id, 13)}
                </div>
              </div>
              <span className="nx-num text-[11px] text-nx-dim">g{row.generation}</span>
              <span className="nx-num text-[11px] text-nx-dim">
                {row.success_count}s / {row.failure_count}f
              </span>
              <span className="nx-num text-[11px] text-nx-muted">{fixed(row.similarity, 3)}</span>
              <span className="nx-num text-[11px] text-nx-faint-2">
                {row.sampled_theta === null ? 'not persisted' : fixed(row.sampled_theta, 3)}
              </span>
              <span
                className="nx-num text-[11px]"
                style={{
                  color: row.winner ? 'var(--color-nx-proven)' : 'var(--color-nx-muted-3)',
                }}
              >
                {fixed(row.score, 3)}
              </span>
            </div>
          ))}
          {note ? (
            <p className="px-4 py-2.5 text-[10.5px] leading-relaxed text-nx-faint-2">{note}</p>
          ) : null}
        </>
      )}
    </Panel>
  )
}

const STEP_STATE_COLOR = {
  applied: 'var(--color-nx-proven)',
  rolled_back: 'var(--color-nx-failing)',
  running: 'var(--color-nx-accent)',
  queued: 'var(--color-nx-faint-3)',
  unknown: 'var(--color-nx-faint-2)',
}

function Execution({ execution, status }) {
  return (
    <Panel>
      <PanelHeader
        label="Execution"
        sub={
          execution.playbook_name
            ? `${execution.playbook_name} · ${execution.steps.length} steps · ${
                execution.reversible ? 'reversible' : 'irreversible'
              }`
            : 'no playbook selected'
        }
      />
      {!execution.steps.length ? (
        <EmptyState
          title="No remediation program"
          body="predictions.playbook_applied is null, so Guardian has no program to run and there is nothing to show."
          source="predictions.playbook_applied"
        />
      ) : (
        <>
          {execution.steps.map((step) => (
            <div
              key={step.index}
              className="flex items-start gap-3 border-b border-nx-line-soft px-4 py-2.5"
            >
              <span
                className="nx-num mt-px flex h-[19px] w-[19px] shrink-0 items-center justify-center rounded text-[9.5px]"
                style={{
                  background: `color-mix(in srgb, ${STEP_STATE_COLOR[step.state]} 14%, transparent)`,
                  color: STEP_STATE_COLOR[step.state],
                }}
              >
                {step.index}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="nx-num text-[12px] text-nx-text-2">{step.action}</span>
                  <span className="nx-num text-[10px] text-nx-dim">
                    {step.target ? `${step.target} · ` : ''}
                    {JSON.stringify(step.params)}
                  </span>
                </div>
                <div className="nx-num mt-1 flex items-baseline gap-1.5 text-[10px] text-nx-faint-2">
                  <span>inverse</span>
                  <span className="text-nx-dim">
                    {step.inverse ? step.inverse.action : 'none declared'}
                  </span>
                </div>
              </div>
              <span
                className="nx-num shrink-0 text-[9.5px] tracking-[0.08em]"
                style={{ color: STEP_STATE_COLOR[step.state] }}
              >
                {step.state.replace('_', ' ').toUpperCase()}
              </span>
            </div>
          ))}
          <p className="px-4 py-2.5 text-[10.5px] leading-relaxed text-nx-faint-2">
            Guardian does not persist a step cursor, so per-step state is only known once the
            prediction resolves. This one is{' '}
            <span className="nx-num">{status}</span>.
          </p>
        </>
      )}
    </Panel>
  )
}
