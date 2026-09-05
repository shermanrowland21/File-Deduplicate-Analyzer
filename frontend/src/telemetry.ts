/**
 * Client telemetry — funnels what the browser is doing to POST /api/logs so we can
 * diagnose problems server-side without the user reproducing anything.
 *
 * Ported from the content-creation-platform pattern (app/lib/telemetry.ts).
 * Captures: every /api fetch (method/path/status/duration), clicks, route changes,
 * window errors + unhandled rejections, and explicit track() events.
 * Batched + flushed on interval and page hide (sendBeacon). Never throws into app code.
 */

export interface TelemetryEvent {
  sessionId: string
  ts: string
  level?: 'debug' | 'info' | 'warn' | 'error'
  event: string
  message?: string
  url?: string
  component?: string
  httpStatus?: number
  durationMs?: number
  detail?: unknown
}

const ENDPOINT = '/api/logs'
let sessionId = ''
let queue: Omit<TelemetryEvent, never>[] = []
let started = false
let flushTimer: ReturnType<typeof setInterval> | null = null
let originalFetch: typeof fetch | null = null

function sid(): string {
  if (sessionId) return sessionId
  try {
    const k = 'fda_sid'
    let s = sessionStorage.getItem(k)
    if (!s) {
      s = (crypto.randomUUID?.() ?? String(Math.random()).slice(2)) + ''
      sessionStorage.setItem(k, s)
    }
    sessionId = s
  } catch {
    sessionId = String(Date.now()) + Math.random().toString(36).slice(2)
  }
  return sessionId
}

function enqueue(e: Omit<TelemetryEvent, 'sessionId' | 'ts'>) {
  queue.push({
    ...e,
    sessionId: sid(),
    ts: new Date().toISOString(),
    url: e.url ?? (typeof location !== 'undefined' ? location.pathname + location.search + location.hash : undefined),
  } as TelemetryEvent)
  if (queue.length >= 25) flush()
}

/** Explicit event from app code (e.g. track('view_change', {view})). */
export function track(event: string, detail?: unknown, level: TelemetryEvent['level'] = 'info') {
  enqueue({ event, detail, level })
}

function flush(useBeacon = false) {
  if (queue.length === 0) return
  const batch = queue
  queue = []
  const payload = JSON.stringify({ events: batch })
  try {
    if (useBeacon && navigator.sendBeacon) {
      navigator.sendBeacon(ENDPOINT, new Blob([payload], { type: 'application/json' }))
      return
    }
    ;(originalFetch ?? fetch)(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: payload,
      keepalive: true,
    }).catch(() => {})
  } catch {}
}

function instrumentFetch() {
  if (originalFetch) return
  originalFetch = window.fetch.bind(window)
  window.fetch = async (...args: Parameters<typeof fetch>) => {
    const input = args[0]
    const init = args[1]
    const url =
      typeof input === 'string' ? input : input instanceof URL ? input.href : (input as Request).url
    const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase()
    const isTelemetry = url.includes(ENDPOINT)
    const start = performance.now()
    try {
      const res = await originalFetch!(...args)
      if (!isTelemetry && url.includes('/api/')) {
        const path = new URL(url, location.origin).pathname
        enqueue({
          event: 'api',
          level: res.ok ? 'info' : 'error',
          message: `${method} ${path} -> ${res.status}`,
          httpStatus: res.status,
          durationMs: performance.now() - start,
          detail: { method, path },
        })
      }
      return res
    } catch (err) {
      if (!isTelemetry) {
        enqueue({
          event: 'api',
          level: 'error',
          message: `${method} ${url} -> network error`,
          detail: { method, url, error: String(err) },
        })
        flush()
      }
      throw err
    }
  }
}

function instrumentClicks() {
  document.addEventListener(
    'click',
    (ev) => {
      const el = (ev.target as Element)?.closest?.("button, a, [data-testid], [role='button'], li")
      if (!el) return
      const testid = el.getAttribute('data-testid') ?? undefined
      const text = (el.textContent ?? '').trim().slice(0, 60)
      const tag = el.tagName.toLowerCase()
      enqueue({ event: 'click', message: text || tag, component: testid, detail: { tag, text } })
    },
    { capture: true },
  )
}

function instrumentErrors() {
  window.addEventListener('error', (e) => {
    enqueue({
      event: 'error',
      level: 'error',
      message: e.message,
      detail: { filename: e.filename, lineno: e.lineno, colno: e.colno, stack: (e.error?.stack ?? '').slice(0, 2000) },
    })
    flush()
  })
  window.addEventListener('unhandledrejection', (e: PromiseRejectionEvent) => {
    enqueue({
      event: 'unhandledrejection',
      level: 'error',
      message: String(e.reason?.message ?? e.reason),
      detail: { stack: (e.reason?.stack ?? '').slice(0, 2000) },
    })
    flush()
  })
}

/** Start telemetry once. Safe to call repeatedly. */
export function startTelemetry() {
  if (started || typeof window === 'undefined') return
  started = true
  sid()
  instrumentFetch()
  instrumentClicks()
  instrumentErrors()
  flushTimer = setInterval(() => flush(), 4000)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flush(true)
  })
  window.addEventListener('pagehide', () => flush(true))
  track('session_start', { ua: navigator.userAgent, href: location.href })
}
