// Presentation helpers. Everything here is arithmetic or string formatting on
// a value the API supplied; nothing invents a value when one is missing. An
// absent value formats as an em dash, which is how the UI says "the database
// has no answer" without saying "zero".

export const DASH = '—'

export function num(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function fixed(value, digits = 3) {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH
  return Number(value).toFixed(digits)
}

export function pct(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH
  return `${(Number(value) * 100).toFixed(digits)}%`
}

export function signedPct(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH
  const n = Number(value)
  return `${n >= 0 ? '+' : ''}${n.toFixed(digits)}%`
}

export function ago(iso, now = Date.now()) {
  if (!iso) return DASH
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return DASH
  const seconds = Math.max(0, Math.round((now - then) / 1000))
  if (seconds < 45) return 'now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export function until(iso, now = Date.now()) {
  if (!iso) return DASH
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return DASH
  const seconds = Math.round((then - now) / 1000)
  if (seconds <= 0) return 'elapsed'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  return `${Math.round(minutes / 60)}h`
}

export function clock(iso) {
  if (!iso) return DASH
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return DASH
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`
}

export function timestamp(iso) {
  if (!iso) return DASH
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return DASH
  return d.toISOString().replace('T', ' ').replace('Z', 'Z')
}

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return DASH
  const s = Math.round(Number(seconds))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

export function shortId(id, length = 8) {
  if (!id) return DASH
  return String(id).slice(0, length)
}

/** `connection_pool_exhaustion` -> `Connection pool exhaustion`. */
export function humanise(key) {
  if (!key) return DASH
  const text = String(key).replace(/_/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** `connection_pool_exhaustion` -> `POOL`, for the category chip. */
export function categoryTag(key) {
  if (!key) return DASH
  return String(key).split('_')[0].toUpperCase().slice(0, 6)
}

// The five status colours, resolved from what the API sends. The evolution
// endpoint already sends a resolved `class` for playbooks; these cover the
// places where the UI classifies a raw column value.
export const CLASS_COLOR = {
  proven: 'var(--color-nx-proven)',
  experimental: 'var(--color-nx-experimental)',
  failing: 'var(--color-nx-failing)',
  institutional: 'var(--color-nx-institutional)',
  retired: 'var(--color-nx-retired)',
}

export function playbookClass(playbook) {
  if (!playbook) return 'retired'
  if (playbook.status !== 'active' || playbook.memory_tier === 'retired') return 'retired'
  if (playbook.memory_tier === 'institutional') return 'institutional'
  const mean = playbook.posterior_mean
  if (mean === null || mean === undefined) return 'experimental'
  if (mean >= 0.75) return 'proven'
  if (mean >= 0.45) return 'experimental'
  return 'failing'
}

/** Fleet tile colour. `unknown` is its own grey — an unobserved service is not healthy. */
export const FLEET_COLOR = {
  healthy: 'var(--color-nx-proven)',
  recovered: 'var(--color-nx-proven)',
  drifting: 'var(--color-nx-experimental)',
  degrading: 'var(--color-nx-experimental)',
  failing: 'var(--color-nx-failing)',
  unknown: 'var(--color-nx-faint-2)',
}

export const EVENT_COLOR = {
  birth: 'var(--color-nx-accent)',
  growth: 'var(--color-nx-proven)',
  mutation: 'var(--color-nx-institutional)',
  competition: 'var(--color-nx-muted-3)',
  merge: 'var(--color-nx-sensory)',
  promotion: 'var(--color-nx-experimental)',
  retirement: 'var(--color-nx-dim-2)',
  rollback: 'var(--color-nx-failing)',
}

export const EVENT_GLYPH = {
  birth: '+',
  growth: '↑',
  mutation: '⋔',
  competition: '⇄',
  merge: '⋈',
  promotion: '★',
  retirement: '⌀',
  rollback: '↺',
}

export const PREDICTION_COLOR = {
  prevented: 'var(--color-nx-proven)',
  preventing: 'var(--color-nx-accent)',
  pending: 'var(--color-nx-accent)',
  missed: 'var(--color-nx-failing)',
  false_alarm: 'var(--color-nx-experimental)',
  shadowed: 'var(--color-nx-institutional)',
}

export function predictionLabel(prediction) {
  if (!prediction) return DASH
  if (prediction.awaiting_approval) return 'AWAITING APPROVAL'
  return String(prediction.prevention_status || '').replace(/_/g, ' ').toUpperCase()
}
