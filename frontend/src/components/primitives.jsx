import { DASH } from '../lib/format'

export function Panel({ children, className = '', ...rest }) {
  return (
    <section
      className={`rounded-md border border-nx-line bg-nx-panel ${className}`}
      {...rest}
    >
      {children}
    </section>
  )
}

export function PanelHeader({ label, sub, right, children }) {
  return (
    <header className="flex items-center gap-3 border-b border-nx-line px-4 py-2.5">
      <span className="nx-label shrink-0">{label}</span>
      {sub ? <span className="truncate text-[10px] text-nx-dim">{sub}</span> : null}
      {children}
      <div className="ml-auto flex shrink-0 items-center gap-2">{right}</div>
    </header>
  )
}

export function Label({ children, className = '' }) {
  return <span className={`nx-label ${className}`}>{children}</span>
}

export function Dot({ color, pulse = false, size = 5 }) {
  return (
    <span
      className={`inline-block shrink-0 rounded-full ${pulse ? 'nx-pulse' : ''}`}
      style={{ width: size, height: size, background: color }}
    />
  )
}

export function Pill({ children, color = 'var(--color-nx-muted-3)', tint }) {
  return (
    <span
      className="nx-num shrink-0 rounded-[3px] px-1.5 py-0.5 text-[8.5px] tracking-[0.09em]"
      style={{ color, background: tint ?? `color-mix(in srgb, ${color} 14%, transparent)` }}
    >
      {children}
    </span>
  )
}

/** A labelled figure. `value` is rendered exactly as given; no defaulting. */
export function Stat({ label, value, color = 'var(--color-nx-text)', sub }) {
  return (
    <div className="flex min-w-[82px] flex-col gap-1.5 rounded-md border border-nx-line bg-nx-raised px-3 py-2">
      <Label>{label}</Label>
      <span className="nx-num text-[17px] leading-none" style={{ color }}>
        {value}
      </span>
      {sub ? <span className="text-[9px] text-nx-faint">{sub}</span> : null}
    </div>
  )
}

export function KeyValue({ label, value, color = 'var(--color-nx-text-2)', mono = true }) {
  return (
    <div className="flex flex-col gap-1">
      <Label>{label}</Label>
      <span className={`${mono ? 'nx-num' : ''} text-[12px]`} style={{ color }}>
        {value ?? DASH}
      </span>
    </div>
  )
}

/** Horizontal meter. `value` is 0..1; null renders an empty track, not a full one. */
export function Meter({ value, color, height = 3, track = 'rgba(255,255,255,.06)' }) {
  const width = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, value)) * 100
  return (
    <div className="w-full overflow-hidden rounded-full" style={{ height, background: track }}>
      <div className="h-full rounded-full transition-[width] duration-500" style={{ width: `${width}%`, background: color }} />
    </div>
  )
}

/** A credible interval drawn as a band with a mean marker. */
export function IntervalBar({ low, high, mean, color }) {
  const has = [low, high, mean].every((v) => v !== null && v !== undefined)
  return (
    <div className="relative h-[6px] w-full rounded-full bg-white/[0.06]">
      {has ? (
        <>
          <div
            className="absolute inset-y-0 rounded-full opacity-30"
            style={{ left: `${low * 100}%`, width: `${(high - low) * 100}%`, background: color }}
          />
          <div
            className="absolute -inset-y-[2px] w-[2px] rounded-full"
            style={{ left: `${mean * 100}%`, background: color }}
          />
        </>
      ) : null}
    </div>
  )
}

export function Skeleton({ className = '', rows = 1, height = 12 }) {
  return (
    <div className={`flex flex-col gap-2 ${className}`}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="nx-skeleton" style={{ height, width: `${100 - i * 7}%` }} />
      ))}
    </div>
  )
}

/**
 * The designed empty state. It always says which table was consulted, so an
 * empty panel reads as a fact about the database rather than a broken screen.
 */
export function EmptyState({ title, body, source, action }) {
  return (
    <div className="flex flex-col items-start gap-2.5 px-6 py-10">
      <span className="nx-label text-nx-muted-3">{title}</span>
      <p className="max-w-[60ch] text-[12.5px] leading-relaxed text-nx-dim">{body}</p>
      {source ? (
        <span className="nx-num text-[9.5px] text-nx-faint-2">source: {source}</span>
      ) : null}
      {action}
    </div>
  )
}

/** Renders an ApiError. Never swallowed, never replaced with placeholder data. */
export function ErrorState({ error, what }) {
  return (
    <div className="flex flex-col items-start gap-2 px-6 py-8">
      <span className="nx-label" style={{ color: 'var(--color-nx-failing)' }}>
        {what} could not be read
      </span>
      <p className="max-w-[60ch] text-[12.5px] leading-relaxed text-nx-dim">{error?.message}</p>
      {error?.code ? (
        <span className="nx-num text-[9.5px] text-nx-faint-2">{error.code}</span>
      ) : null}
    </div>
  )
}
