import { useState } from 'react'
import { apiPost } from '../lib/api'
import { usePolled } from '../lib/usePolled'
import { DASH, ago, fixed, humanise, num, pct, shortId, timestamp, until } from '../lib/format'
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
  const queue = usePolled('/approvals', {
    intervalMs: 5000,
    params: { status: 'pending', limit: 20 },
  })
  const settled = usePolled('/approvals', {
    intervalMs: 15000,
    params: { status: 'approved,rejected,expired', limit: 10 },
  })

  if (queue.error && !queue.data) return <ErrorState error={queue.error} what="The approval queue" />

  const pending = queue.data?.approvals ?? []
  const history = settled.data?.approvals ?? []

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
            body="No approval request is open. Sentinel writes one when it is confident and the winning playbook has a step with no inverse — rotating a certificate, pruning a disk."
            source="approvals where status = 'pending'"
          />
        </Panel>
      ) : (
        pending.map((approval) => (
          <ApprovalCard key={approval.id} approval={approval} onDecided={queue.refresh} />
        ))
      )}

      {history.length ? <Decided approvals={history} /> : null}
    </div>
  )
}

function ApprovalCard({ approval, onDecided }) {
  const [pendingDecision, setPendingDecision] = useState(null)
  const [outcome, setOutcome] = useState(null)
  const [failure, setFailure] = useState(null)

  const competition = approval.evidence?.competition ?? []
  const busy = Boolean(pendingDecision)

  async function decide(decision) {
    setPendingDecision(decision)
    setFailure(null)
    try {
      const body = await apiPost(`/approvals/${approval.id}/decide`, { decision })
      setOutcome(body)
      onDecided?.()
    } catch (error) {
      setFailure(error)
    } finally {
      setPendingDecision(null)
    }
  }

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
          waiting {ago(approval.requested_at)} · decide within {until(approval.deadline)}
        </span>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_320px]">
        <div className="border-r border-nx-line p-5">
          <div className="flex items-start gap-4">
            <div className="min-w-0">
              <h2 className="text-[17px] font-semibold tracking-[-0.01em]">
                {humanise(approval.causal_pattern ?? approval.outcome_category)}
              </h2>
              <div className="nx-num mt-1.5 flex flex-wrap items-center gap-1.5 text-[10.5px] text-nx-dim">
                <span>{approval.service_name}</span>
                <span className="text-nx-faint-3">/</span>
                <span>{shortId(approval.prediction_id, 13)}</span>
              </div>
            </div>
            <div className="ml-auto flex shrink-0 flex-col items-end gap-1">
              <Label>Failure in</Label>
              <span className="nx-num text-[19px] leading-none">
                {until(approval.predicted_eta)}
              </span>
            </div>
          </div>

          <div className="mt-4 rounded border border-nx-line-soft bg-nx-sunken px-3.5 py-3">
            <Label>Why this needs a human</Label>
            <p className="mt-1.5 text-[12px] leading-relaxed text-nx-muted">{approval.reason}</p>
          </div>

          <div className="mt-4">
            <Label>Proposed playbook</Label>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className="text-[13px] text-nx-text-2">{approval.playbook_name ?? DASH}</span>
              <span className="nx-num text-[9.5px] text-nx-faint-2">
                gen {num(approval.playbook?.generation)} · {approval.playbook?.memory_tier}
              </span>
              <span className="nx-num text-[9.5px] text-nx-faint-2">
                {shortId(approval.playbook_id, 13)}
              </span>
            </div>
            <div className="mt-2.5 flex flex-col gap-1.5">
              {(approval.steps ?? []).map((step) => {
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
                        irreversible ? 'var(--color-nx-failing)' : 'var(--color-nx-faint)'
                      }
                    >
                      {irreversible ? 'NO INVERSE' : 'REVERSIBLE'}
                    </Pill>
                  </div>
                )
              })}
            </div>
          </div>

          {competition.length ? (
            <div className="mt-4">
              <Label>What it beat</Label>
              <div className="mt-2 flex flex-col gap-1">
                {competition.map((candidate) => (
                  <div
                    key={candidate.playbook_id}
                    className="nx-num flex items-baseline gap-2 text-[10.5px]"
                  >
                    <span
                      className="w-3 shrink-0"
                      style={{
                        color:
                          candidate.playbook_id === approval.playbook_id
                            ? 'var(--color-nx-experimental)'
                            : 'var(--color-nx-faint-3)',
                      }}
                    >
                      {candidate.playbook_id === approval.playbook_id ? '→' : ''}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-nx-muted">{candidate.name}</span>
                    <span className="text-nx-dim">{fixed(candidate.beta_sample, 3)}</span>
                    <span className="text-nx-faint-3">×</span>
                    <span className="text-nx-dim">{fixed(candidate.similarity, 3)}</span>
                    <span className="text-nx-faint-3">=</span>
                    <span className="text-nx-text-2">{fixed(candidate.score, 3)}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>

        <div className="flex flex-col p-5">
          <Label>Evidence bundle</Label>
          <div className="mt-2.5 flex flex-col gap-2.5">
            <Evidence label="Posterior" value={pct(approval.prediction?.posterior_mean)} />
            <Evidence
              label="90% CI"
              value={`[${fixed(approval.prediction?.ci_low, 3)} – ${fixed(approval.prediction?.ci_high, 3)}]`}
            />
            <Evidence
              label="Matched"
              value={`${num(approval.matching_precursor_count)} precursors`}
            />
            <Evidence label="Severity" value={num(approval.predicted_severity)} />
            <Evidence label="Playbook" value={pct(approval.playbook?.posterior_mean)} />
            <Evidence label="Trials" value={num(approval.playbook?.trials)} />
            <Evidence
              label="Commit ts"
              value={approval.evidence?.prediction?.commit_timestamp ?? DASH}
            />
          </div>

          <p className="mt-5 border-t border-nx-line pt-3 text-[11px] leading-relaxed text-nx-faint-2">
            Approving puts an <span className="nx-num">approval.approved</span> event on the bus,
            which starts Guardian against this prediction. Rejecting turns it into a shadow record:
            the playbook stays attached and unexecuted, and Chronicler scores the choice against
            whatever actually happens — so a human disagreeing is evidence too.
          </p>

          {outcome ? (
            <div
              className="mt-4 rounded border px-3 py-2.5"
              style={{
                borderColor:
                  outcome.decision === 'approved'
                    ? 'color-mix(in srgb, var(--color-nx-proven) 35%, transparent)'
                    : 'var(--color-nx-line)',
              }}
            >
              <span
                className="nx-label"
                style={{
                  color:
                    outcome.decision === 'approved'
                      ? 'var(--color-nx-proven)'
                      : 'var(--color-nx-muted-3)',
                }}
              >
                {outcome.decision}
              </span>
              <p className="mt-1.5 text-[11px] leading-relaxed text-nx-muted">{outcome.note}</p>
            </div>
          ) : (
            <>
              <div className="mt-4 flex gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => decide('approved')}
                  className="flex-1 rounded px-3 py-2 text-[12px] font-medium transition-opacity disabled:cursor-wait disabled:opacity-50"
                  style={{
                    border: '1px solid color-mix(in srgb, var(--color-nx-proven) 35%, transparent)',
                    background: 'color-mix(in srgb, var(--color-nx-proven) 10%, transparent)',
                    color: 'var(--color-nx-proven)',
                  }}
                >
                  {pendingDecision === 'approved' ? 'Dispatching…' : 'Approve & execute'}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => decide('rejected')}
                  className="rounded border border-nx-line px-4 py-2 text-[12px] font-medium text-nx-muted-3 transition-opacity disabled:cursor-wait disabled:opacity-50"
                >
                  {pendingDecision === 'rejected' ? 'Recording…' : 'Reject'}
                </button>
              </div>
              {failure ? (
                <p
                  className="mt-2 text-[10.5px] leading-relaxed"
                  style={{ color: 'var(--color-nx-failing)' }}
                >
                  {failure.code === 'already_decided'
                    ? 'Someone else answered this first. The queue is reloading.'
                    : failure.message}
                </p>
              ) : null}
            </>
          )}
        </div>
      </div>
    </Panel>
  )
}

function Decided({ approvals }) {
  return (
    <Panel>
      <PanelHeader label="Decided" sub="the audit trail — who answered, and what happened next" />
      <div className="flex flex-col divide-y divide-nx-line">
        {approvals.map((approval) => (
          <div key={approval.id} className="flex items-baseline gap-3 px-4 py-2.5">
            <Pill
              color={
                approval.status === 'approved'
                  ? 'var(--color-nx-proven)'
                  : approval.status === 'rejected'
                    ? 'var(--color-nx-muted-3)'
                    : 'var(--color-nx-failing)'
              }
            >
              {approval.status}
            </Pill>
            <span className="min-w-0 flex-1 truncate text-[12px] text-nx-muted">
              {approval.playbook_name} on {approval.service_name}
            </span>
            <span className="nx-num text-[10px] text-nx-dim">{approval.decided_by ?? DASH}</span>
            <span className="nx-num text-[10px] text-nx-faint-2">
              {timestamp(approval.decided_at ?? approval.requested_at)}
            </span>
          </div>
        ))}
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
