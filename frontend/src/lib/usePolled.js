import { useCallback, useEffect, useRef, useState } from 'react'
import { apiGet } from './api'

/**
 * Poll one endpoint.
 *
 * Three behaviours matter more than the polling itself:
 *
 * - Requests never stack. A tick that fires while the previous response is
 *   still in flight is skipped, so a slow endpoint degrades into a lower
 *   refresh rate instead of a queue.
 * - Polling stops while the tab is hidden and resumes with an immediate
 *   fetch, so a backgrounded dashboard costs nothing and a foregrounded one is
 *   never showing minutes-old data without saying so.
 * - A failed poll never clears the data. The last good response stays on
 *   screen, `error` is set, and `fetchedAt` tells the UI how old it is.
 */
export function usePolled(path, { intervalMs = 5000, params, enabled = true } = {}) {
  const [state, setState] = useState({
    data: null,
    error: null,
    loading: true,
    fetchedAt: null,
  })

  const inFlight = useRef(false)
  const mounted = useRef(true)
  const key = JSON.stringify(params ?? null)

  const load = useCallback(
    async ({ quiet = false } = {}) => {
      if (inFlight.current || !enabled) return
      inFlight.current = true
      if (!quiet) setState((s) => ({ ...s, loading: s.data === null }))
      try {
        const data = await apiGet(path, { params: params ?? undefined })
        if (mounted.current) {
          setState({ data, error: null, loading: false, fetchedAt: new Date() })
        }
      } catch (error) {
        if (error?.name === 'AbortError') return
        if (mounted.current) setState((s) => ({ ...s, error, loading: false }))
      } finally {
        inFlight.current = false
      }
    },
    // `key` stands in for `params`, which is a fresh object on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [path, key, enabled],
  )

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    if (!enabled) return undefined
    setState((s) => ({ ...s, loading: s.data === null }))
    load()
    let timer = window.setInterval(() => {
      if (!document.hidden) load({ quiet: true })
    }, intervalMs)

    const onVisibility = () => {
      if (!document.hidden) load({ quiet: true })
    }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      window.clearInterval(timer)
      timer = null
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [load, intervalMs, enabled])

  return { ...state, refresh: () => load({ quiet: true }) }
}
