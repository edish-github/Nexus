import { usePolled } from '../lib/usePolled'
import { DASH, ago, fixed, humanise, num, pct, shortId, until } from '../lib/format'
import {
  Dot,
  EmptyState,
  ErrorState,
  Label,
  Panel,
  PanelHeader,
  Pill,
  Skeleton,
} from '../components/primitives'

export function Approvals() {
  const queue = usePolled('/predictions', {
    intervalMs: 5000,
    params: { status: 'awaiting_approval', limit: 20 },
  })

  if (queue.error && !queue.data) return <ErrorState error={queue.error} what="The approval queue" />

  const pending = queue.data?.predictions ?? []

  return (
    <div className="flex flex-col gap-4 p-5">
      <div>
        <h1 className="text-[22px] font-semibold tracking-[-0.01em]">Approvals</h1>
        <p className="mt-1 max-w-[80ch] text-[12.5px] leading-relaxed text-nx-dim">
          {pending.length
            ? `${pending.length} remediation${pending.length > 1 ? 's' : ''} held at the approval gate. The posterior cleared the auto-execute threshold, but the selected playbook declares itself irreversible.`
            : 'Irreversible playbooks pause here. Reversible ones execute and roll themselves back if the metrics disagree.'}
        </p>
      </div>

      {queue.loading && !queue.data ? (
        <Panel>
          <div className="p-5">
            <Skeleton rows={4} height={20} />
          </div>
        </Panel>
      ) : !pending.length ? (
        <Panel>
          <PanelHeader label="Queue empty" />
          <EmptyState
            title="Nothing is waiting"
            body="No prediction is pending against an irreversible playbook. This queue is derived: prevention_status = 'pending' and the applied playbook has reversible = false. It is not a stored status."
            source="predictions × playbooks.reversible"
          />
        </Panel>
      ) : (
        pending.map((prediction) => <ApprovalCard key={prediction.id} prediction={prediction} />)
      )}
    </div>
  )
}

function ApprovalCard({ prediction }) {
  const detail = usePolled(`/predictions/${prediction.id}`, { intervalMs: 5000 })
  const execution = detail.data?.execution

  return (
    <Panel>
      <div
        className="flex items-center gap-2.5 border-b px-4 py-2.5"
        style={{
          borderColor: 'color-mix(in srgb, var(--color-nx-experimental) 25%, transparent)',
          background: 'color-mix(in srgb, var(--color-nx-experimental) 8%, transparent)',
        }}
      >
        <Dot color="var(--color-nx-experimental)" pulse />
        <span className="nx-label" style={{ color: 'var(--color-nx-experimental)' }}>
          Human approval required
        </span>
        <span className="nx-num ml-auto text-[10px] text-nx-dim">
          waiting {ago(prediction.created_at)}
        </span>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_320px]">
        <div className="border-r border-nx-line p-5">
          <div className="flex items-start gap-4">
            <div className="min-w-0">
              <h2 className="text-[17px] font-semibold tracking-[-0.01em]">
                {humanise(prediction.causal_pattern)}
              </h2>
              <div className="nx-num mt-1.5 flex flex-wrap items-center gap-1.5 text-[10.5px] text-nx-dim">
                <span>{prediction.service_name}</span>
                <span className="text-nx-faint-3">/</span>
                <span>{prediction.region ?? DASH}</span>
                <span className="text-nx-faint-3">/</span>
                <span>{shortId(prediction.id, 13)}</span>
              </div>
            </div>
            <div className="ml-auto flex shrink-0 flex-col items-end gap-1">
              <Label>Failure in</Label>
              <span className="nx-num text-[19px] leading-none">
                {until(prediction.predicted_eta)}
              </span>
            </div>
          </div>

          <div className="mt-4 rounded border border-nx-line-soft bg-nx-sunken px-3.5 py-3">
            <Label>Why this needs a human</Label>
            <p className="mt-1.5 text-[12px] leading-relaxed text-nx-muted">
              The selected playbook declares{' '}
              <span className="nx-num">reversible = false</span>: at least one of its remediation
              steps has no inverse recorded in{' '}
              <span className="nx-num">playbooks.inverse_steps</span>, so Guardian cannot undo it
              inside the verification window.
            </p>
          </div>

          <div className="mt-4">
            <Label>Proposed playbook</Label>
            {!detail.data ? (
              <div className="mt-2">
                <Skeleton rows={3} height={14} />
              </div>
            ) : (
              <>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <span className="text-[13px] text-nx-text-2">
                    {execution?.playbook_name ?? DASH}
                  </span>
                  <span className="nx-num text-[9.5px] text-nx-faint-2">
                    {shortId(execution?.playbook_id, 13)}
                  </span>
                </div>
                <div className="mt-2.5 flex flex-col gap-1.5">
                  {(execution?.steps ?? []).map((step) => {
                    const irreversible = !step.inverse
                    return (
                      <div
                        key={step.index}
                        className="flex items-start gap-3 rounded border border-nx-line-soft px-3 py-2"
                      >
                        <span className="nx-num text-[9.5px] text-nx-faint">{step.index}</span>
                        <div className="min-w-0 flex-1">
                          <span className="nx-num text-[11.5px] text-nx-text-2">{step.action}</span>
                          <div className="nx-num mt-0.5 text-[10px] text-nx-dim">
                            {step.target ? `${step.target} · ` : ''}
                            {JSON.stringify(step.params)}
                          </div>
                        </div>
                        <Pill
                          color={
                            irreversible
                              ? 'var(--color-nx-failing)'
                              : 'var(--color-nx-faint)'
                          }
                        >
                          {irreversible ? 'NO INVERSE' : 'REVERSIBLE'}
                        </Pill>
                      </div>
                    )
                  })}
                </div>
              </>
            )}
          </div>
        </div>

        <div className="flex flex-col p-5">
          <Label>Evidence bundle</Label>
          <div className="mt-2.5 flex flex-col gap-2.5">
            <Evidence label="Posterior" value={pct(prediction.posterior_mean)} />
            <Evidence
              label="90% CI"
              value={`[${fixed(prediction.ci_low, 3)} – ${fixed(prediction.ci_high, 3)}]`}
            />
            <Evidence
              label="Matched"
              value={`${num(prediction.matching_precursor_count)} precursors`}
            />
            <Evidence label="Severity" value={num(prediction.predicted_severity)} />
            <Evidence label="Blast radius" value={`1 service · ${prediction.service_name}`} />
            <Evidence label="Commit ts" value={prediction.commit_ts ?? DASH} />
          </div>

          <p className="mt-5 border-t border-nx-line pt-3 text-[11px] leading-relaxed text-nx-faint-2">
            Approving would resume the Step Functions execution at the Guardian state. Rejecting
            would record the decision in <span className="nx-num">evolution_log</span> and leave
            the prediction to resolve on its own — which is how the system learns that humans
            disagreed.
          </p>

          {/* Not wired up. There is no write endpoint behind these: the dashboard
              Lambda is read-only, and resuming a Step Functions execution needs
              a task token that is not exposed anywhere the UI can reach. They
              are disabled rather than faked — a button that reports success
              without doing anything is worse than no button. */}
          <div className="mt-4 flex gap-2">
            <button
              type="button"
              disabled
              title="No write endpoint exists yet — the dashboard API is read-only."
              className="flex-1 cursor-not-allowed rounded px-3 py-2 text-[12px] font-medium opacity-45"
              style={{
                border: '1px solid color-mix(in srgb, var(--color-nx-proven) 35%, transparent)',
                background: 'color-mix(in srgb, var(--color-nx-proven) 10%, transparent)',
                color: 'var(--color-nx-proven)',
              }}
            >
              Approve &amp; execute
            </button>
            <button
              type="button"
              disabled
              title="No write endpoint exists yet — the dashboard API is read-only."
              className="cursor-not-allowed rounded border border-nx-line px-4 py-2 text-[12px] font-medium text-nx-muted-3 opacity-45"
            >
              Reject
            </button>
          </div>
          <p className="mt-2 text-[10px] leading-relaxed text-nx-faint-2">
            Disabled: the dashboard API is read-only, so neither button has a write path behind it
            yet.
          </p>
        </div>
      </div>
    </Panel>
  )
}

function Evidence({ label, value }) {
  return (
    <div className="flex items-baseline gap-3">
      <Label className="w-[92px] shrink-0">{label}</Label>
      <span className="nx-num truncate text-[11.5px] text-nx-text-2">{value}</span>
    </div>
  )
}
