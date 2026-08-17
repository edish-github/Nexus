import { useEffect, useState } from 'react'
import { usePolled } from './lib/usePolled'
import { apiBase, isConfigured } from './lib/api'
import { Dot, Label } from './components/primitives'
import { DASH, num } from './lib/format'
import { Overview } from './views/Overview'
import { Predictions } from './views/Predictions'
import { Playbooks } from './views/Playbooks'
import { Evolution } from './views/Evolution'
import { Approvals } from './views/Approvals'

const VIEWS = [
  { id: 'overview', label: 'Overview', Component: Overview },
  { id: 'predictions', label: 'Predictions', Component: Predictions },
  { id: 'playbooks', label: 'Playbooks', Component: Playbooks },
  { id: 'evolution', label: 'Evolution', Component: Evolution },
  { id: 'approvals', label: 'Approvals', Component: Approvals },
]

const TIERS = [
  { key: 'sensory', label: 'Sensory', color: 'var(--color-nx-sensory)' },
  { key: 'episodic', label: 'Episodic', color: 'var(--color-nx-accent)' },
  { key: 'semantic', label: 'Semantic', color: 'var(--color-nx-proven)' },
  { key: 'institutional', label: 'Institutional', color: 'var(--color-nx-institutional)' },
]

function useUtcClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  const p = (n) => String(n).padStart(2, '0')
  return `${p(now.getUTCHours())}:${p(now.getUTCMinutes())}:${p(now.getUTCSeconds())}`
}

export default function App() {
  const [view, setView] = useState('overview')

  // One shared overview poll drives the header, the sidebar and the Overview
  // screen, so switching tabs does not open a second stream of the same data.
  const overview = usePolled('/overview', { intervalMs: 5000 })
  const clock = useUtcClock()

  const data = overview.data
  const cluster = data?.cluster
  const stale = overview.error && data
  const Active = VIEWS.find((v) => v.id === view)?.Component ?? Overview

  return (
    <div className="flex h-screen min-w-[1180px] flex-col bg-nx-bg">
      <header className="flex h-[54px] shrink-0 items-stretch border-b border-nx-line bg-nx-panel">
        <div className="flex w-[236px] shrink-0 items-center gap-2.5 border-r border-nx-line px-4">
          <span className="relative block h-[19px] w-[19px]">
            <span className="absolute inset-0 rotate-45 rounded-[2px] border-[1.5px] border-nx-accent" />
            <span className="absolute inset-[6px] rotate-45 rounded-[1px] bg-nx-accent" />
          </span>
          <span className="text-[15px] font-semibold tracking-[0.16em]">NEXUS</span>
        </div>

        <div className="flex min-w-0 flex-1 items-center gap-0 px-1">
          <div className="flex shrink-0 items-center gap-2 px-3">
            <Label>Cluster</Label>
            <span className="nx-num text-[11px] text-nx-muted">{cluster?.database ?? DASH}</span>
          </div>
          <span className="h-[22px] w-px shrink-0 bg-nx-line" />

          <div className="flex h-full shrink-0 items-stretch">
            {(cluster?.regions ?? []).map((region) => (
              <div
                key={region.region}
                className="flex items-center gap-2.5 border-r border-nx-line px-3.5"
              >
                <Dot color="var(--color-nx-proven)" />
                <span className="flex flex-col gap-px leading-none">
                  <span className="text-[11.5px] font-medium text-nx-text-2">{region.region}</span>
                  <span className="nx-num text-[9px] text-nx-dim">
                    {region.primary ? 'primary' : 'region'}
                  </span>
                </span>
              </div>
            ))}
            {!cluster?.regions?.length && overview.loading ? (
              <div className="flex items-center px-3.5">
                <span className="nx-skeleton block h-3 w-40" />
              </div>
            ) : null}
          </div>

          <span className="h-[22px] w-px shrink-0 bg-nx-line" />
          <div className="flex shrink-0 items-center gap-4 px-4">
            <div className="flex flex-col gap-px leading-none">
              <Label>Survival</Label>
              <span className="nx-num text-[11px] text-nx-muted">
                {cluster?.survival_goal ?? DASH}
              </span>
            </div>
            <div className="flex flex-col gap-px leading-none">
              <Label>Logical ts</Label>
              <span className="nx-num text-[11px] text-nx-muted">
                {cluster?.logical_ts ? String(cluster.logical_ts).slice(0, 14) : DASH}
              </span>
            </div>
          </div>

          <div className="flex-1" />

          <div className="flex shrink-0 items-center gap-2.5 px-4">
            <Dot
              color={overview.error ? 'var(--color-nx-failing)' : 'var(--color-nx-proven)'}
              pulse
            />
            <span className="nx-num text-[11px] tracking-[0.02em] text-nx-muted">{clock}</span>
            <Label>UTC</Label>
          </div>
        </div>
      </header>

      {!isConfigured ? (
        <Banner tone="failing">
          <strong className="font-medium">VITE_API_BASE_URL is not set.</strong> The dashboard has
          no API to read from. Set it in <code className="nx-num">frontend/.env</code> and reload —
          nothing on this page is rendered from a local fallback.
        </Banner>
      ) : null}

      {overview.error && isConfigured ? (
        <Banner tone="failing">
          <strong className="font-medium">Memory layer unreachable.</strong>{' '}
          {overview.error.message}
          {stale ? (
            <span className="text-nx-dim">
              {' '}
              Showing the last successful read from{' '}
              <span className="nx-num">{overview.fetchedAt?.toISOString().slice(11, 19)}Z</span>.
            </span>
          ) : null}
          <span className="nx-num ml-2 text-[10px] text-nx-faint-2">{apiBase || 'unset'}</span>
        </Banner>
      ) : null}

      {(data?.degraded ?? []).length ? (
        <Banner tone="experimental">
          <strong className="font-medium">Partial read.</strong> {data.degraded.join(' · ')}
        </Banner>
      ) : null}

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[236px] shrink-0 flex-col border-r border-nx-line bg-nx-panel">
          <nav className="flex flex-col gap-0.5 p-3">
            {VIEWS.map((item) => {
              const active = item.id === view
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setView(item.id)}
                  className={`flex w-full items-center gap-2.5 rounded px-2 py-[7px] text-left text-[13px] transition-colors ${
                    active
                      ? 'bg-white/[0.055] font-medium text-nx-text'
                      : 'text-nx-muted-2 hover:bg-white/[0.03]'
                  }`}
                >
                  <span
                    className="h-[13px] w-[2px] shrink-0 rounded-full"
                    style={{ background: active ? 'var(--color-nx-accent)' : 'transparent' }}
                  />
                  {item.label}
                </button>
              )
            })}
          </nav>

          <div className="mx-3 h-px bg-nx-line" />

          <div className="p-4">
            <Label className="mb-3 block">Memory</Label>
            <div className="flex flex-col gap-3.5">
              {TIERS.map((tier) => {
                const entry = data?.memory?.[tier.key]
                return (
                  <div key={tier.key} className="flex flex-col gap-1.5">
                    <div className="flex items-center gap-2">
                      <Dot color={tier.color} />
                      <span className="text-[12px] text-nx-muted">{tier.label}</span>
                      <span className="nx-num ml-auto text-[12px] text-nx-text-2">
                        {overview.loading && !data ? (
                          <span className="nx-skeleton inline-block h-3 w-8 align-middle" />
                        ) : (
                          num(entry?.count)
                        )}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="nx-num text-[9px] text-nx-faint-2">
                        {entry?.table ?? DASH}
                      </span>
                      <span className="nx-num ml-auto text-[9px] text-nx-faint">
                        {entry?.ttl ?? DASH}
                      </span>
                    </div>
                  </div>
                )
              })}
            </div>
            <p className="mt-4 text-[10.5px] leading-relaxed text-nx-faint-2">
              Counts are live row counts, not gauges. A null sensory count means that tier could
              not be read.
            </p>
          </div>
        </aside>

        <main className="min-w-0 flex-1 overflow-auto">
          <Active overview={overview} onNavigate={setView} />
        </main>
      </div>
    </div>
  )
}

function Banner({ tone, children }) {
  const color = tone === 'failing' ? 'var(--color-nx-failing)' : 'var(--color-nx-experimental)'
  return (
    <div
      className="flex shrink-0 items-center gap-2 border-b px-4 py-2 text-[12px]"
      style={{
        borderColor: `color-mix(in srgb, ${color} 25%, transparent)`,
        background: `color-mix(in srgb, ${color} 8%, transparent)`,
        color: 'var(--color-nx-text-2)',
      }}
    >
      <Dot color={color} pulse />
      <span className="min-w-0">{children}</span>
    </div>
  )
}
