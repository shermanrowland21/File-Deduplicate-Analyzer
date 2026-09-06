import { useState, useEffect } from 'react'
import { api } from '../api'
import { track } from '../telemetry'
import { DirectoryBrowser } from './DirectoryBrowser'

/**
 * Resolver — sharp, outcome-first dedup. Combines the proven patterns from
 * dupeGuru (keeper = rule-picked, applied in bulk), Czkawka (bulk "select all
 * except" presets + always leave one per group), and Nextcloud (preferred/keep
 * folders). Quarantine is reversible; structural & junk handled automatically.
 */
interface Copy {
  path: string; size: number; size_human: string; source: string
  modified: string; subfolder: string
  is_keeper: boolean; removable: boolean; smart_selected: boolean; structural: boolean
}
interface Card {
  hash: string; category: string; reason: string; structural: boolean
  protected: boolean; wasted_human: string; wasted_space: number
  keeper: string | null; copies: Copy[]
}

const TIEBREAKS = [
  { v: 'biggest', label: 'the biggest file' },
  { v: 'newest', label: 'the newest file' },
  { v: 'oldest', label: 'the oldest file' },
  { v: 'shortest_path', label: 'the shallowest path' },
  { v: 'longest_name', label: 'the most-descriptive name' },
  { v: 'smallest', label: 'the smallest file' },
]

export function ResolverPanel() {
  const [scans, setScans] = useState<any[]>([])
  const [scanId, setScanId] = useState<string | null>(null)
  const [withinSource, setWithinSource] = useState(true)
  const [preferFolder, setPreferFolder] = useState<string>('')
  const [tiebreak, setTiebreak] = useState('biggest')

  const [review, setReview] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [executing, setExecuting] = useState(false)
  const [result, setResult] = useState<any>(null)
  const [error, setError] = useState('')
  const [showBrowser, setShowBrowser] = useState(false)
  const [filter, setFilter] = useState('')
  const [showProtected, setShowProtected] = useState(false)

  const [selected, setSelected] = useState<Record<string, boolean>>({})
  const [keeperOverride, setKeeperOverride] = useState<Record<string, string>>({})

  useEffect(() => {
    api.listScans().then((r) => { setScans(r.scans || []); if (r.latest) setScanId(r.latest) }).catch(() => {})
  }, [])

  const runReview = async () => {
    setError(''); setResult(null); setReview(null)
    if (!scanId) { setError('Pick a scan first'); return }
    setLoading(true)
    try {
      const r = await api.resolverReview(scanId, {
        source_of_truth: preferFolder ? [preferFolder] : [],
        within_source: withinSource, tiebreak,
      })
      setReview(r)
      const sel: Record<string, boolean> = {}
      for (const c of r.cards || []) for (const cp of c.copies) if (cp.smart_selected) sel[cp.path] = true
      setSelected(sel); setKeeperOverride({})
      track('resolver_review', { scan_id: scanId, within: withinSource, prefer: !!preferFolder, tiebreak, groups: r.summary?.total_groups })
    } catch (e: any) {
      setError(e.message || 'Review failed')
    } finally { setLoading(false) }
  }

  const cards: Card[] = review?.cards || []
  const actionable = cards.filter((c) => !c.protected)
  const protectedCards = cards.filter((c) => c.protected)
  const q = filter.trim().toLowerCase()
  const shown = q ? actionable.filter((c) => c.copies.some((cp) => cp.path.toLowerCase().includes(q))) : actionable

  const keeperOf = (c: Card) => keeperOverride[c.hash] || c.keeper
  const makeKeeper = (hash: string, path: string) => {
    setKeeperOverride((k) => ({ ...k, [hash]: path }))
    setSelected((s) => ({ ...s, [path]: false }))
  }
  const toggleCopy = (path: string) => setSelected((s) => ({ ...s, [path]: !s[path] }))

  // --- bulk presets (Czkawka), always leaving the keeper (one per group) ---
  const selectAllExceptKeeper = () => {
    const sel: Record<string, boolean> = {}
    for (const c of actionable) {
      const k = keeperOf(c)
      for (const cp of c.copies) if (cp.path !== k && !cp.structural) sel[cp.path] = true
    }
    setSelected(sel)
  }
  const clearAll = () => setSelected({})

  const selectedPaths = () => {
    const out: string[] = []
    for (const c of actionable) {
      const k = keeperOf(c)
      for (const cp of c.copies) {
        if (cp.path === k || cp.structural) continue
        if (selected[cp.path]) out.push(cp.path)
      }
    }
    return out
  }
  const selInfo = () => {
    let count = 0, bytes = 0
    for (const c of actionable) {
      const k = keeperOf(c)
      for (const cp of c.copies) {
        if (cp.path === k || cp.structural) continue
        if (selected[cp.path]) { count++; bytes += cp.size || 0 }
      }
    }
    return { count, bytes }
  }
  const { count: selCount, bytes: selBytes } = selInfo()

  const apply = async () => {
    const paths = selectedPaths()
    if (paths.length === 0) { setError('Nothing selected to quarantine'); return }
    if (!window.confirm(`Quarantine ${paths.length} file(s)? Reversible — moved to a dated quarantine with a manifest, never deleted.`)) return
    setExecuting(true); setError('')
    try {
      const r = await api.resolverExecute(scanId, {
        source_of_truth: preferFolder ? [preferFolder] : [],
        within_source: withinSource, tiebreak, only_paths: paths,
      })
      setResult(r)
      track('resolver_execute', { removed: r.removed, freed: r.freed_human })
      await runReview()
    } catch (e: any) {
      setError(e.message || 'Quarantine failed')
    } finally { setExecuting(false) }
  }

  const undo = async () => {
    if (!result?.manifest_path && !result?.manifest) return
    // (undo endpoint reuse omitted for brevity; manifest is on disk)
    setError('Undo: restore from the quarantine manifest on disk.')
  }

  const s = review?.summary
  const confColor = (structural: boolean, keeper: boolean) =>
    keeper ? 'var(--success)' : structural ? 'var(--warning)' : 'var(--border)'
  const human = (n: number) => {
    if (!n) return '0 B'; const u = ['B','KB','MB','GB','TB']; let i = 0; let x = n
    while (x >= 1024 && i < u.length - 1) { x /= 1024; i++ } return `${x.toFixed(1)} ${u[i]}`
  }

  return (
    <div>
      {/* ============ Setup ============ */}
      <div className="card" style={{ padding: 20 }}>
        <h2 style={{ margin: '0 0 4px' }}>Resolver</h2>
        <p style={{ color: 'var(--text-muted)', fontSize: '0.82rem', margin: '0 0 16px' }}>
          Keep one copy of each duplicate, quarantine the rest. Reversible. Website/framework files and
          junk (desktop.ini etc.) are handled for you.
        </p>

        {error && <div className="alert alert-error">{error}</div>}

        <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 14 }}>
          <div className="form-group" style={{ margin: 0 }}>
            <label>Scan</label>
            <select value={scanId || ''} onChange={(e) => { setScanId(e.target.value); setReview(null) }}>
              <option value="">— pick a scan —</option>
              {scans.map((sc) => (
                <option key={sc.scan_id} value={sc.scan_id}>
                  {(Array.isArray(sc.directories) ? sc.directories.join(' + ') : sc.directories) || sc.scan_id}
                  {' — '}{(sc.duplicate_groups ?? 0).toLocaleString()} groups
                </option>
              ))}
            </select>
          </div>

          {/* Keeper rule bar — the sharp centerpiece (dupeGuru model) */}
          <div style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: 10, padding: 14 }}>
            <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: 8 }}>Which copy do you KEEP?</div>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', fontSize: '0.9rem' }}>
              <span>Prefer files in</span>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <input type="text" value={preferFolder} onChange={(e) => setPreferFolder(e.target.value)}
                  placeholder="(any folder)" style={{ width: 320, fontFamily: 'monospace', fontSize: '0.8rem' }} />
                <button className="btn btn-secondary btn-sm" onClick={() => setShowBrowser(true)}>Browse</button>
                {preferFolder && <button className="btn btn-secondary btn-sm" onClick={() => setPreferFolder('')}>✕</button>}
              </div>
              <span>· otherwise keep</span>
              <select value={tiebreak} onChange={(e) => setTiebreak(e.target.value)} style={{ width: 'auto' }}>
                {TIEBREAKS.map((t) => <option key={t.v} value={t.v}>{t.label}</option>)}
              </select>
            </div>
            <label className="checkbox-label" style={{ margin: '10px 0 0', fontSize: '0.82rem' }}>
              <input type="radio" checked={withinSource} onChange={() => setWithinSource(true)} /> Clean inside one folder
              <input type="radio" checked={!withinSource} onChange={() => setWithinSource(false)} style={{ marginLeft: 16 }} /> Compare two sources
            </label>
          </div>

          <button className="btn btn-primary" onClick={runReview} disabled={loading || !scanId} style={{ alignSelf: 'flex-start' }}>
            {loading ? <><span className="spinner" /> Reviewing...</> : 'Review duplicates'}
          </button>
        </div>
      </div>

      {result && (
        <div className="alert alert-success">
          Quarantined {result.removed} file(s), freed {result.freed_human || human(result.freed || 0)}. Reversible (manifest saved).
        </div>
      )}

      {/* ============ Summary band ============ */}
      {s && (
        <div className="card" style={{ padding: 18 }}>
          <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <Stat big value={human(selBytes)} label={`reclaimable (${selCount} selected)`} color="var(--success)" />
            <Stat value={s.total_groups.toLocaleString()} label="duplicate groups" />
            <Stat value={s.protected_files.toLocaleString()} label="protected / kept" color="var(--warning)" />
            <div style={{ flex: 1 }} />
            <button className="btn btn-primary" onClick={apply} disabled={executing || selCount === 0}>
              {executing ? <><span className="spinner" /> Quarantining...</> : `Quarantine ${selCount} selected`}
            </button>
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 14, alignItems: 'center', flexWrap: 'wrap' }}>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Bulk:</span>
            <button className="btn btn-secondary btn-sm" onClick={selectAllExceptKeeper}>Select all except the keeper</button>
            <button className="btn btn-secondary btn-sm" onClick={clearAll}>Clear selection</button>
            <span style={{ fontSize: '0.72rem', color: 'var(--success)', marginLeft: 4 }}>✓ always leaves one copy per group</span>
            {s.capped && <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginLeft: 'auto' }}>top {cards.length} groups by size</span>}
          </div>
        </div>
      )}

      {/* ============ Review cards ============ */}
      {actionable.length > 0 && (
        <div className="card" style={{ padding: 18 }}>
          <div className="card-header" style={{ marginBottom: 12 }}>
            <h3 style={{ margin: 0 }}>Review — {shown.length} group{shown.length === 1 ? '' : 's'}</h3>
            <input type="text" value={filter} onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter by path…" style={{ maxWidth: 240 }} />
          </div>
          {shown.map((c) => {
            const keeper = keeperOf(c)
            return (
              <div key={c.hash} style={{ borderRadius: 8, padding: '10px 12px', marginBottom: 10, background: 'var(--bg-secondary)', border: '1px solid var(--border)' }}>
                <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginBottom: 6 }}>
                  {c.copies.length} copies · {c.wasted_human} reclaimable
                </div>
                {c.copies.map((cp) => {
                  const isKeeper = cp.path === keeper
                  const parts = cp.path.split('/')
                  const name = parts.pop(); const dir = parts.join('/')
                  return (
                    <div key={cp.path} style={{
                      display: 'flex', alignItems: 'center', gap: 10, padding: '5px 8px', borderRadius: 6,
                      background: isKeeper ? 'rgba(46,160,67,0.08)' : 'transparent',
                      borderLeft: `3px solid ${confColor(cp.structural, isKeeper)}`,
                    }}>
                      {isKeeper ? <span title="kept" style={{ color: 'var(--success)', fontWeight: 700, width: 18, textAlign: 'center' }}>★</span>
                        : cp.structural ? <span title="protected" style={{ width: 18, textAlign: 'center' }}>🔒</span>
                        : <input type="checkbox" checked={!!selected[cp.path]} onChange={() => toggleCopy(cp.path)} style={{ width: 18 }} />}
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: '0.82rem', fontWeight: isKeeper ? 600 : 400, color: isKeeper ? 'var(--success)' : 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</div>
                        <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)', fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{dir}</div>
                      </div>
                      <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
                        {cp.size_human}{cp.modified ? ' · ' + cp.modified.slice(0, 10) : ''}
                      </div>
                      {isKeeper ? <span style={{ fontSize: '0.66rem', color: 'var(--success)', width: 84, textAlign: 'right' }}>KEEP</span>
                        : cp.structural ? <span style={{ fontSize: '0.66rem', color: 'var(--warning)', width: 84, textAlign: 'right' }}>protected</span>
                        : <button className="btn btn-secondary btn-sm" style={{ width: 84 }} onClick={() => makeKeeper(c.hash, cp.path)}>Keep this</button>}
                    </div>
                  )
                })}
              </div>
            )
          })}
        </div>
      )}

      {/* ============ Protected (collapsed) ============ */}
      {protectedCards.length > 0 && (
        <div className="card" style={{ padding: 18 }}>
          <div style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8 }} onClick={() => setShowProtected(!showProtected)}>
            <span style={{ color: 'var(--warning)' }}>🔒</span>
            <strong style={{ fontSize: '0.9rem' }}>Protected — won't touch ({protectedCards.length} groups)</strong>
            <span style={{ color: 'var(--text-muted)' }}>{showProtected ? '▾' : '▸'}</span>
          </div>
          {showProtected && (
            <div style={{ marginTop: 10 }}>
              <p style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                Framework/template/website-internal files each project legitimately needs its own copy of.
              </p>
              {protectedCards.slice(0, 80).map((c) => (
                <div key={c.hash} style={{ fontSize: '0.72rem', padding: '3px 0', borderBottom: '1px solid var(--border)' }}>
                  <span style={{ color: 'var(--warning)' }}>{c.reason}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {review && actionable.length === 0 && (
        <div className="empty-state">
          <h3>Nothing to quarantine</h3>
          <p>No safely-removable duplicates for this rule. Try "Compare two sources", or set a preferred folder.</p>
        </div>
      )}

      {showBrowser && (
        <DirectoryBrowser onSelect={(p) => { setPreferFolder(p); setShowBrowser(false) }} onClose={() => setShowBrowser(false)} />
      )}
    </div>
  )
}

function Stat({ value, label, color, big }: { value: string; label: string; color?: string; big?: boolean }) {
  return (
    <div>
      <div style={{ fontSize: big ? '2rem' : '1.3rem', fontWeight: 700, lineHeight: 1, color: color || 'var(--text-primary)' }}>{value}</div>
      <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: 4 }}>{label}</div>
    </div>
  )
}
