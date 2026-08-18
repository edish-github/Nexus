// The only place the app talks to the network. Every screen's data comes
// through here; there is no other source, and no fallback if a call fails.

const BASE = (
  import.meta.env.VITE_API_BASE_URL ||
  (import.meta.env.DEV
    ? 'http://127.0.0.1:8787'
    : 'https://pqy4r2l3rghfqhotpftt37ncqu0tcuxh.lambda-url.us-east-1.on.aws')
).replace(/\/+$/, '')

export class ApiError extends Error {
  constructor(message, { code, status, path } = {}) {
    super(message)
    this.name = 'ApiError'
    this.code = code || 'request_failed'
    this.status = status ?? null
    this.path = path ?? null
  }
}

// A missing base URL is a configuration error, and the UI says so by name.
// Silently defaulting to a same-origin path would produce 404s that look like
// an empty database.
export const isConfigured = Boolean(BASE)
export const apiBase = BASE

function url(path, params) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== '') {
      query.set(key, Array.isArray(value) ? value.join(',') : String(value))
    }
  }
  const suffix = query.toString()
  return `${BASE}${path}${suffix ? `?${suffix}` : ''}`
}

export async function apiGet(path, { params, signal } = {}) {
  if (!BASE) {
    throw new ApiError(
      'VITE_API_BASE_URL is not set. Point it at the dashboard API and reload.',
      { code: 'not_configured', path },
    )
  }

  let response
  try {
    response = await fetch(url(path, params), { signal })
  } catch (cause) {
    if (cause?.name === 'AbortError') throw cause
    throw new ApiError(`Cannot reach the dashboard API at ${BASE}.`, {
      code: 'unreachable',
      path,
    })
  }

  const body = await response.json().catch(() => null)

  if (!response.ok) {
    throw new ApiError(body?.detail || `${response.status} ${response.statusText}`, {
      code: body?.error || 'request_failed',
      status: response.status,
      path,
    })
  }
  return body
}

export async function apiPost(path, payload, { signal } = {}) {
  if (!BASE) {
    throw new ApiError('VITE_API_BASE_URL is not set.', { code: 'not_configured', path })
  }
  let response
  try {
    response = await fetch(`${BASE}${path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    })
  } catch (cause) {
    if (cause?.name === 'AbortError') throw cause
    throw new ApiError(`Cannot reach the dashboard API at ${BASE}.`, {
      code: 'unreachable',
      path,
    })
  }
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    throw new ApiError(body?.detail || `${response.status} ${response.statusText}`, {
      code: body?.error || 'request_failed',
      status: response.status,
      path,
    })
  }
  return body
}
