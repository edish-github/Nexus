import { useEffect, useRef, useState } from 'react'
import { usePolled } from './lib/usePolled'
import { apiBase, isConfigured } from './lib/api'
import { Dot, Label } from './components/primitives'
import { DASH, ago, num } from './lib/format'
import { Overview } from './views/Overview'
import { Predictions } from './views/Predictions'
import { Playbooks } from './views/Playbooks'
import { Evolution } from './views/Evolution'
import { Approvals } from './views/Approvals'

const VIEWS = [
  { id: 'overview', label: 'Overview', Component: Overview, count: null },
  { id: 'predictions', label: 'Predictions', Component: Predictions, count: 'predictions' },
  { id: 'playbooks', label: 'Playbooks', Component: Playbooks, count: 'playbooks' },
  { id: 'evolution', label: 'Evolution', Component: Evolution, count: 'evolution' },
  { id: 'approvals', label: 'Approvals', Component: Approvals, count: 'approvals' },
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
  const [openRegion, setOpenRegion] = useState(null)

  // One shared overview poll drives the header, the sidebar and the Overview
  // screen, so switching tabs does not open a second stream of the same data.
  const overview = usePolled('/overview', { intervalMs: 5000 })
  const clock = useUtcClock()

  const data = overview.data
  const cluster = data?.cluster
  const stale = overview.error && data
  const Active = VIEWS.find((v) => v.id === view)?.Component ?? Overview

  return (
    <div className="flex h-screen min-w-[1240px] flex-col bg-nx-bg">
      <header className="relative flex h-[54px] shrink-0 items-stretch border-b border-nx-line bg-nx-panel">
        <div className="flex w-[236px] shrink-0 items-center gap-2.5 border-r border-nx-line px-4">
          <span className="relative block h-[19px] w-[19px]">
            <span className="absolute inset-0 rotate-45 rounded-[2px] border-[1.5px] border-nx-accent" />
            <span className="absolute inset-[6px] rotate-45 rounded-[1px] bg-nx-accent" />
          </span>
          <span className="text-[15px] font-semibold tracking-[0.16em]">NEXUS</span>
          <span className="nx-num ml-auto text-[9px] tracking-[0.04em] text-nx-faint">
            {cluster?.survival_goal ? `survive ${cluster.survival_goal}` : DASH}
          </span>
        </div>

        <div className="flex min-w-0 flex-1 items-center px-1">
          <div className="flex shrink-0 items-center gap-2 px-3">
            <Label>Cluster</Label>
            <span className="nx-num text-[11px] text-nx-muted">{cluster?.database ?? DASH}</span>
          </div>
          <span className="h-[22px] w-px shrink-0 bg-nx-line" />

          <div className="flex h-full shrink-0 items-stretch">
            {(cluster?.regions ?? []).map((region) => (
              <RegionChip
                key={region.region}
                region={region}
                open={openRegion === region.region}
                onToggle={() =>
                  setOpenRegion((current) =>
                    current === region.region ? null : region.region,
                  )
                }
                onClose={() => setOpenRegion(null)}
                survivalGoal={cluster?.survival_goal}
              />
            ))}
            {!cluster?.regions?.length && overview.loading ? (
              <div className="flex items-center gap-3 px-3.5">
                <span className="nx-skeleton block h-3.5 w-28" />
                <span className="nx-skeleton block h-3.5 w-28" />
              </div>
            ) : null}
          </div>

          <span className="h-[22px] w-px shrink-0 bg-nx-line" />

          {/* Measured, not reported: every figure here is something this
              process observed or a row count it just read. */}
          <div className="flex shrink-0 items-center gap-5 px-4">
            <HeaderStat
              label="Memory rows"
              value={
                data
                  ? num(
                      TIERS.reduce(
                        (total, tier) => total + (data.memory?.[tier.key]?.count ?? 0),
                        0,
                      ),
                    )
                  : DASH
              }
            />
            <HeaderStat
              label="Vector p50"
              value={
                cluster?.vector_search?.p50_ms == null
                  ? DASH
                  : `${cluster.vector_search.p50_ms} ms`
              }
              hint={
                cluster?.vector_search?.samples
                  ? `${cluster.vector_search.samples} retrievals`
                  : 'no retrieval yet'
              }
            />
            <HeaderStat
              label="Follower lag"
              value={
                cluster?.follower_staleness_seconds == null
                  ? DASH
                  : `${cluster.follower_staleness_seconds.toFixed(1)}s`
              }
            />
            <HeaderStat
              label="Logical ts"
              value={cluster?.logical_ts ? String(cluster.logical_ts).slice(0, 13) : DASH}
            />
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
        <aside className="flex w-[236px] shrink-0 flex-col overflow-auto border-r border-nx-line bg-nx-panel">
          <nav className="flex flex-col gap-0.5 p-3">
            {VIEWS.map((item) => {
              const active = item.id === view
              const badge = item.count ? data?.nav_counts?.[item.count] : null
              const urgent = item.id === 'approvals' && badge > 0
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setView(item.id)}
                  className={`flex w-full items-center gap-2.5 rounded px-2 py-[7px] text-left text-[13px] transition-colors ${
                    active
                      ? 'bg-white/[0.055] font-medium text-nx-text'
                      : 'text-nx-muted-2 hover:bg-white/[0.03] hover:text-nx-text-3'
                  }`}
                >
                  <span
                    className="h-[13px] w-[2px] shrink-0 rounded-full"
                    style={{ background: active ? 'var(--color-nx-accent)' : 'transparent' }}
                  />
                  {item.label}
                  {badge === null || badge === undefined ? null : (
                    <span
                      className="nx-num ml-auto rounded-[3px] px-1.5 py-px text-[10px]"
                      style={{
                        color: urgent
                          ? 'var(--color-nx-experimental)'
                          : active
                            ? 'var(--color-nx-muted)'
                            : 'var(--color-nx-faint)',
                        background: urgent
                          ? 'color-mix(in srgb, var(--color-nx-experimental) 13%, transparent)'
                          : 'transparent',
                      }}
                    >
                      {num(badge)}
                    </span>
                  )}
                </button>
              )
            })}
          </nav>

          <div className="mx-3 h-px bg-nx-line" />

          {/* An agent's invocation count lives in CloudWatch. What the memory
              layer knows is how many rows each one has written and when the
              last one landed, which is what says whether the pipeline moved. */}
          <div className="p-4">
            <div className="mb-2.5 flex items-baseline">
              <Label>Agents</Label>
              <span className="nx-num ml-auto text-[9px] text-nx-faint-2">rows written</span>
            </div>
            <div className="flex flex-col gap-px">
              {(data?.agents ?? []).map((agent) => {
                const idle = !agent.rows
                return (
                  <div
                    key={agent.name}
                    className="group flex items-center gap-2 rounded px-1.5 py-1 hover:bg-white/[0.03]"
                    title={`${agent.name} → ${agent.writes}${
                      agent.last_write_at ? ` · last ${ago(agent.last_write_at)}` : ''
                    }`}
                  >
                    <Dot
                      color={idle ? 'var(--color-nx-faint-4)' : 'var(--color-nx-proven)'}
                      size={5}
                    />
                    <span
                      className="text-[11.5px]"
                      style={{
                        color: idle ? 'var(--color-nx-faint-2)' : 'var(--color-nx-muted)',
                      }}
                    >
                      {agent.name}
                    </span>
                    <span className="nx-num ml-auto text-[11px] text-nx-text-3">
                      {num(agent.rows)}
                    </span>
                    <span className="nx-num w-[34px] shrink-0 text-right text-[9px] text-nx-faint-2">
                      {agent.last_write_at ? ago(agent.last_write_at).replace(' ago', '') : DASH}
                    </span>
                  </div>
                )
              })}
              {!data?.agents?.length && overview.loading ? (
                <span className="nx-skeleton block h-16 w-full" />
              ) : null}
            </div>
          </div>

          <div className="mx-3 h-px bg-nx-line" />

          <div className="p-4">
            <Label className="mb-3 block">Memory</Label>
            <div className="flex flex-col gap-3.5">
              {TIERS.map((tier) => {
                const entry = data?.memory?.[tier.key]
                // Bars are relative to the largest tier, which is the only
                // denominator that exists — a row count has no capacity.
                const largest = Math.max(
                  1,
                  ...TIERS.map((t) => data?.memory?.[t.key]?.count ?? 0),
                )
                const share = entry?.count == null ? null : entry.count / largest
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
                    <div className="h-[3px] w-full overflow-hidden rounded-full bg-white/[0.06]">
                      <div
                        className="h-full rounded-full transition-[width] duration-700"
                        style={{
                          width: `${(share ?? 0) * 100}%`,
                          background: tier.color,
                          opacity: share === null ? 0 : 0.85,
                        }}
                      />
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
              Live row counts, not gauges. Bars are relative to the largest tier. A null sensory
              count means that tier could not be read.
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

function HeaderStat({ label, value, hint }) {
  return (
    <div className="flex flex-col gap-px leading-none" title={hint}>
      <Label>{label}</Label>
      <span className="nx-num text-[11px] text-nx-muted">{value}</span>
    </div>
  )
}

/**
 * A region in the header. Click opens what the cluster actually reports about
 * it: its availability zones, and how many rows of the two REGIONAL BY ROW
 * tables are homed there. There is no simulate-failure control, because the
 * dashboard cannot take a region down and a button that pretends to would be
 * the one lie on the page.
 */
function RegionChip({ region, open, onToggle, onClose, survivalGoal }) {
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const onDocClick = (event) => {
      if (!ref.current?.contains(event.target)) onClose()
    }
    const onKey = (event) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, onClose])

  const rows = (region.playbooks_homed ?? 0) + (region.incidents_homed ?? 0)

  return (
    <div
      ref={ref}
      className="relative flex h-full"
      onMouseEnter={() => !open && onToggle()}
      onMouseLeave={() => open && onClose()}
    >
      <button
        type="button"
        onClick={onToggle}
        className="flex h-full items-center gap-2.5 border-r border-nx-line px-3.5 transition-colors hover:bg-white/[0.06]"
        style={{ background: open ? 'rgba(255,255,255,.06)' : 'transparent' }}
      >
        <Dot color="var(--color-nx-proven)" />
        <span className="flex flex-col items-start gap-px leading-none">
          <span className="text-[11.5px] font-medium text-nx-text-2">{region.region}</span>
          <span className="nx-num text-[9px] text-nx-dim">
            {region.zones?.length ? `${region.zones.length} zones` : 'zones —'}
            {region.playbooks_homed === null ? '' : ` · ${num(rows)} rows`}
          </span>
        </span>
        <span
          className="nx-num rounded-[3px] px-1.5 py-0.5 text-[8px] tracking-[0.1em]"
          style={
            region.primary
              ? {
                  color: 'var(--color-nx-accent)',
                  background: 'color-mix(in srgb, var(--color-nx-accent) 14%, transparent)',
                }
              : { color: 'var(--color-nx-faint)', background: 'rgba(255,255,255,.05)' }
          }
        >
          {region.primary ? 'PRIMARY' : 'REGION'}
        </span>
      </button>

      {open ? (
        <div className="absolute top-[54px] left-0 z-50 w-[330px] overflow-hidden rounded-lg border border-white/[0.11] bg-nx-elevated shadow-[0_18px_48px_rgba(0,0,0,.6)]">
          <div className="border-b border-nx-line px-4 py-3">
            <div className="flex items-center gap-2">
              <Dot color="var(--color-nx-proven)" size={6} />
              <span className="text-[13px] font-semibold">{region.region}</span>
              <span className="nx-num rounded-[3px] bg-white/[0.06] px-1.5 py-0.5 text-[8px] tracking-[0.1em] text-nx-muted-3">
                {region.primary ? 'PRIMARY' : 'REGION'}
              </span>
            </div>
            <div className="nx-num mt-1.5 text-[10px] text-nx-dim-2">
              survival_goal={survivalGoal ?? DASH}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-x-4 gap-y-3 px-4 py-3">
            <PopoverStat
              label="Playbooks homed"
              value={num(region.playbooks_homed)}
              color="var(--color-nx-proven)"
            />
            <PopoverStat
              label="Incidents homed"
              value={num(region.incidents_homed)}
              color="var(--color-nx-text-2)"
            />
          </div>

          <div className="border-t border-nx-line px-4 py-3">
            <Label>Availability zones</Label>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {region.zones?.length ? (
                region.zones.map((zone) => (
                  <span
                    key={zone}
                    className="nx-num rounded border border-nx-proven/25 bg-nx-proven/[0.07] px-2 py-1 text-[10px] text-nx-proven"
                  >
                    {zone}
                  </span>
                ))
              ) : (
                <span className="text-[11px] text-nx-faint">
                  SHOW REGIONS could not be read on this connection.
                </span>
              )}
            </div>
          </div>

          <p className="border-t border-nx-line px-4 py-3 text-[10.5px] leading-relaxed text-nx-faint-2">
            <span className="nx-num">playbooks</span> and{' '}
            <span className="nx-num">incidents</span> are REGIONAL BY ROW, so these counts are the
            rows this region owns and serves locally — not a copy of the whole table.
          </p>
        </div>
      ) : null}
    </div>
  )
}

function PopoverStat({ label, value, color }) {
  return (
    <div className="flex flex-col gap-1">
      <Label>{label}</Label>
      <span className="nx-num text-[15px] leading-none" style={{ color }}>
        {value}
      </span>
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
