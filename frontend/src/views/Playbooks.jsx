import { useState } from 'react'
import { usePolled } from '../lib/usePolled'
import {
  CLASS_COLOR,
  DASH,
  ago,
  fixed,
  num,
  playbookClass,
  shortId,
  timestamp,
} from '../lib/format'
import {
  Dot,
  EmptyState,
  ErrorState,
  IntervalBar,
  Label,
  Pill,
  Skeleton,
} from '../components/primitives'
import { PosteriorChart } from '../components/PosteriorChart'
import { EventRow } from './Overview'

const FILTERS = [
  { id: 'active', label: 'Active', params: { status: 'active' } },
  { id: 'institutional', label: 'Institutional', params: { tier: 'institutional' } },
  { id: 'retired', label: 'Retired', params: { tier: 'retired' } },
  { id: 'all', label: 'All', params: {} },
]

export function Playbooks() {
  const [filter, setFilter] = useState('active')
  const [selected, setSelected] = useState(null)

  const active = FILTERS.find((f) => f.id === filter) ?? FILTERS[0]
  const list = usePolled('/playbooks', {
    intervalMs: 30000,
    params: { limit: 200, ...active.params },
  })

  const playbooks = list.data?.playbooks ?? []
  const activeId =
    selected && playbooks.some((p) => p.id === selected) ? selected : (playbooks[0]?.id ?? null)

  const detail = usePolled(activeId ? `/playbooks/${activeId}` : '/playbooks', {
    intervalMs: 30000,
    enabled: Boolean(activeId),
  })

  if (list.error && !list.data) return <ErrorState error={list.error} what="Playbooks" />

  const counts = list.data?.counts

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-start gap-4 border-b border-nx-line px-5 py-3.5">
          <div>
            <h1 className="text-[19px] font-semibold tracking-[-0.01em]">Playbooks</h1>
            <p className="mt-0.5 text-[11.5px] text-nx-dim">
              {counts
                ? `${counts.active} active · ${counts.institutional} institutional · ${counts.retired_or_merged} retired or merged · ${counts.all} total`
                : DASH}
            </p>
          </div>
          <div className="ml-auto flex gap-1.5">
            {FILTERS.map((option) => (
              <button
                key={option.id}
                type="button"
                onClick={() => setFilter(option.id)}
                className="rounded px-2.5 py-1 text-[11.5px] transition-colors"
                style={{
                  border: `1px solid ${filter === option.id ? 'var(--color-nx-line-strong)' : 'var(--color-nx-line)'}`,
                  background: filter === option.id ? 'rgba(255,255,255,.06)' : 'transparent',
                  color: filter === option.id ? 'var(--color-nx-text)' : 'var(--color-nx-muted-3)',
                }}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-[minmax(0,1fr)_150px_46px_110px_170px_70px_80px] gap-2 border-b border-nx-line px-5 py-2">
          <Label>Playbook</Label>
          <Label>Category</Label>
          <Label>Gen</Label>
          <Label>Tier</Label>
          <Label>Posterior · 90% CI</Label>
          <Label>Trials</Label>
          <Label>Locality</Label>
        </div>

        <div className="min-h-0 flex-1 overflow-auto">
          {list.loading && !list.data ? (
            <div className="p-5">
              <Skeleton rows={8} height={16} />
            </div>
          ) : !playbooks.length ? (
            <EmptyState
              title="No playbooks match"
              body="No row in the playbooks table satisfies this filter."
              source="playbooks"
            />
          ) : (
            playbooks.map((playbook) => (
              <Row
                key={playbook.id}
                playbook={playbook}
                selected={playbook.id === activeId}
                onClick={() => setSelected(playbook.id)}
              />
            ))
          )}
        </div>
      </div>

      <aside className="w-[400px] shrink-0 overflow-auto border-l border-nx-line">
        {!activeId ? null : detail.error && !detail.data ? (
          <ErrorState error={detail.error} what="This playbook" />
        ) : !detail.data ? (
          <div className="p-5">
            <Skeleton rows={6} height={18} />
          </div>
        ) : (
          <PlaybookDetail detail={detail.data} onSelect={setSelected} />
        )}
      </aside>
    </div>
  )
}

function Row({ playbook, selected, onClick }) {
  const color = CLASS_COLOR[playbookClass(playbook)]
  return (
    <button
      type="button"
      onClick={onClick}
      className="grid w-full grid-cols-[minmax(0,1fr)_150px_46px_110px_170px_70px_80px] items-center gap-2 border-b border-nx-line-soft px-5 py-2.5 text-left transition-colors"
      style={{
        borderLeft: `2px solid ${selected ? color : 'transparent'}`,
        background: selected ? 'rgba(255,255,255,.04)' : 'transparent',
      }}
    >
      <div className="flex min-w-0 items-center gap-2.5">
        <Dot color={color} />
        <div className="min-w-0">
          <div className="truncate text-[12.5px] text-nx-text-3">{playbook.name}</div>
          <div className="nx-num truncate text-[9px] text-nx-faint-2">
            {shortId(playbook.id, 13)} ·{' '}
            {playbook.ancestor_count ? `${playbook.ancestor_count} ancestors` : 'root'}
          </div>
        </div>
      </div>
      <span className="nx-num truncate text-[10.5px] text-nx-dim">{playbook.outcome_category}</span>
      <span className="nx-num text-[11px] text-nx-dim">g{playbook.generation}</span>
      <span>
        <Pill color={color}>{playbook.memory_tier.toUpperCase()}</Pill>
      </span>
      <div className="flex items-center gap-2">
        <IntervalBar
          low={playbook.ci_low}
          high={playbook.ci_high}
          mean={playbook.posterior_mean}
          color={color}
        />
        <span className="nx-num w-[38px] shrink-0 text-right text-[11px] text-nx-text-3">
          {fixed(playbook.posterior_mean, 3)}
        </span>
      </div>
      <span className="nx-num text-[11px] text-nx-dim">
        {playbook.success_count} / {playbook.trials}
      </span>
      <span className="nx-num text-[10px] text-nx-faint">{playbook.locality}</span>
    </button>
  )
}

function PlaybookDetail({ detail, onSelect }) {
  const playbook = detail.playbook
  const color = CLASS_COLOR[playbookClass(playbook)]

  return (
    <div className="flex flex-col">
      <div className="border-b border-nx-line px-5 py-4">
        <div className="flex flex-wrap items-center gap-1.5">
          <Pill color={color}>{playbook.memory_tier.toUpperCase()}</Pill>
          <Pill>{playbook.status.toUpperCase()}</Pill>
          <Pill
            color={
              playbook.reversible ? 'var(--color-nx-accent)' : 'var(--color-nx-failing)'
            }
          >
            {playbook.reversible ? 'REVERSIBLE' : 'IRREVERSIBLE'}
          </Pill>
        </div>
        <h2 className="mt-2.5 text-[16px] font-semibold tracking-[-0.01em]">{playbook.name}</h2>
        <div className="nx-num mt-1 text-[10px] text-nx-faint-2">
          {playbook.id} · generation {playbook.generation} · {playbook.outcome_category}
        </div>
      </div>

      <div className="border-b border-nx-line px-5 py-4">
        <Label>Fitness</Label>
        <PosteriorChart
          alpha={playbook.success_count + 1}
          beta={playbook.failure_count + 1}
          ciLow={playbook.ci_low}
          ciHigh={playbook.ci_high}
          mean={playbook.posterior_mean}
          height={80}
          color={color}
        />
        <div className="mt-2 flex items-baseline gap-3">
          <span className="nx-num text-[22px] leading-none">
            {fixed(playbook.posterior_mean, 3)}
          </span>
          <span className="nx-num text-[10px] text-nx-dim">
            90% CI [{fixed(playbook.ci_low, 3)} – {fixed(playbook.ci_high, 3)}]
          </span>
        </div>
        <div className="mt-3 grid grid-cols-4 gap-2">
          <Metric label="Success" value={num(playbook.success_count)} color="var(--color-nx-proven)" />
          <Metric label="Failure" value={num(playbook.failure_count)} color="var(--color-nx-failing)" />
          <Metric label="Trials" value={num(playbook.trials)} />
          <Metric label="Home" value={playbook.region ?? DASH} />
        </div>
        <p className="mt-3 text-[10.5px] leading-relaxed text-nx-faint-2">
          No fitness float is stored. It is Beta(success + 1, failure + 1), derived at read time
          from <span className="nx-num">success_count</span> and{' '}
          <span className="nx-num">failure_count</span>, so two readers never disagree.
        </p>
      </div>

      {detail.institutional ? (
        <div
          className="border-b border-nx-line px-5 py-3"
          style={{
            background: 'color-mix(in srgb, var(--color-nx-institutional) 7%, transparent)',
          }}
        >
          <Label>Institutional copy</Label>
          <p className="mt-1.5 text-[11.5px] leading-relaxed text-nx-muted">
            Promoted {ago(detail.institutional.promoted_at)} into{' '}
            <span className="nx-num">institutional_playbooks</span>, a{' '}
            <span className="nx-num">LOCALITY GLOBAL</span> table every region reads locally.
          </p>
        </div>
      ) : null}

      <Section label="Lineage">
        {!detail.lineage.length ? (
          <p className="px-5 py-3 text-[11.5px] text-nx-faint">Root playbook — no ancestors.</p>
        ) : (
          <div className="flex flex-col px-5 py-2">
            {detail.lineage.map((ancestor, i) => (
              <button
                key={ancestor.id}
                type="button"
                onClick={() => onSelect(ancestor.id)}
                className="relative flex items-center gap-3 py-1.5 text-left"
              >
                <span
                  className="absolute left-[3px] w-px bg-nx-institutional/30"
                  style={{
                    top: i === 0 ? '50%' : 0,
                    bottom: i === detail.lineage.length - 1 ? '50%' : 0,
                  }}
                />
                <span
                  className="z-10 h-[7px] w-[7px] shrink-0 rounded-full"
                  style={{
                    background:
                      ancestor.id === playbook.id
                        ? 'var(--color-nx-institutional)'
                        : 'var(--color-nx-faint-2)',
                    boxShadow: '0 0 0 3px var(--color-nx-panel)',
                  }}
                />
                <div className="min-w-0">
                  <div className="truncate text-[11.5px] text-nx-text-3">{ancestor.name}</div>
                  <div className="nx-num text-[9.5px] text-nx-faint-2">
                    g{ancestor.generation} · {fixed(ancestor.posterior_mean, 3)}
                  </div>
                </div>
              </button>
            ))}
          </div>
        )}
      </Section>

      <Section label="Remediation steps">
        {detail.steps.map((step) => (
          <div key={step.index} className="border-b border-nx-line-soft px-5 py-2.5 last:border-b-0">
            <div className="flex items-baseline gap-2">
              <span className="nx-num text-[9.5px] text-nx-faint">{step.index}</span>
              <span className="nx-num text-[12px] text-nx-text-2">{step.action}</span>
            </div>
            <div className="nx-num mt-1 text-[10px] text-nx-dim">
              {step.target ? `${step.target} · ` : ''}
              {JSON.stringify(step.params)}
            </div>
            <div className="nx-num mt-1 flex items-baseline gap-1.5 text-[10px]">
              <span className="text-nx-faint-2">↩</span>
              <span className="text-nx-muted-3">
                {step.inverse ? step.inverse.action : 'no inverse declared'}
              </span>
            </div>
          </div>
        ))}
      </Section>

      <Section label="Life timeline">
        {!detail.timeline.length ? (
          <p className="px-5 py-3 text-[11.5px] text-nx-faint">
            No rows in <span className="nx-num">evolution_log</span> reference this playbook.
          </p>
        ) : (
          detail.timeline.map((event) => <EventRow key={event.id} event={event} />)
        )}
      </Section>

      <div className="px-5 py-4">
        <div className="grid grid-cols-2 gap-3">
          <Metric label="Created" value={timestamp(playbook.created_at).slice(0, 10)} />
          <Metric label="Last used" value={ago(playbook.last_used_at)} />
          <Metric label="TTL expires" value={timestamp(playbook.expires_at).slice(0, 10)} />
          <Metric label="Retired" value={playbook.retired_at ? ago(playbook.retired_at) : DASH} />
        </div>
      </div>
    </div>
  )
}

function Section({ label, children }) {
  return (
    <div className="border-b border-nx-line">
      <div className="px-5 pt-4 pb-2">
        <Label>{label}</Label>
      </div>
      {children}
    </div>
  )
}

function Metric({ label, value, color = 'var(--color-nx-text-2)' }) {
  return (
    <div className="flex flex-col gap-1">
      <Label>{label}</Label>
      <span className="nx-num truncate text-[11.5px]" style={{ color }}>
        {value}
      </span>
    </div>
  )
}
