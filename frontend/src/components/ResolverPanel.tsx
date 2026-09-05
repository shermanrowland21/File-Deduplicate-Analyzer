import { useState, useEffect } from 'react'
import { api } from '../api'
import { track } from '../telemetry'
import { DirectoryBrowser } from './DirectoryBrowser'

/**
 * Directory-level dedup resolver (Phase A). Pick source-of-truth folder(s),
 * see how duplicates classify (safe cross-source vs protected structural vs
 * single-source), preview the SAFE removal plan, and execute to quarantine.
 */
export function ResolverPanel() {
  const [scans, setScans] = useState<any[]>([])
  const [scanId, setScanId] = useState<string | null>(null)
  const [sot, setSot] = useState<string[]>([])
  const [sotInput, setSotInput] = useState('')
  const [analysis, setAnalysis] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [executing, setExecuting] = useState(false)
  const [result, setResult] = useState<any>(null)
  const [error, setError] = useState('')
  const [showBrowser, setShowBrowser] = useState(false)

  // --- LLM advisor state ---
  const [advJob, setAdvJob] = useState<any>(null)
  const [advJobId, setAdvJobId] = useState<string | null>(null)
  const [advMax, setAdvMax] = useState('100')
  const [advMinMB, setAdvMinMB] = useState('1')
  const advPollRef = useState<{ id: number | null }>({ id: null })[0]

  const startAdvisor = async () => {
    setError('')
    try {
      const r = await api.adviseStart(scanId, {
        max_groups: parseInt(advMax) || 100,
        min_size: (parseInt(advMinMB) || 0) * 1024 * 1024,
      })
      setAdvJobId(r.job_id)
      track('advisor_start', { scan_id: scanId, job: r.job_id })
      if (advPollRef.id) clearInterval(advPollRef.id)
      advPollRef.id = window.setInterval(async () => {
        try {
          const j = await api.adviseStatus(r.job_id)
          setAdvJob(j)
          if (j.status === 'completed' || j.status === 'error' || j.status === 'cancelled') {
            if (advPollRef.id) { clearInterval(advPollRef.id); advPollRef.id = null }
          }
        } catch {}
      }, 1500)
    } catch (e: any) {
      setError(e.message || 'Advisor failed to start')
    }
  }

  const stopAdvisor = async () => {
    if (advJobId) { try { await api.adviseStop(advJobId) } catch {} }
    if (advPollRef.id) { clearInterval(advPollRef.id); advPollRef.id = null }
  }

  const riskColor = (risk: string) =>
    risk === 'low' ? 'var(--success)' : risk === 'high' ? 'var(--danger)' : 'var(--warning)'
  const classLabel: Record<string, string> = {
    redundant_backup: 'Redundant backup (safe)',
    project_internal: 'Project internal (keep all)',
    versioned_content: 'Versioned content (review)',
    needs_human_review: 'Needs human review',
    needs_more_content: 'Needs more content',
  }

  useEffect(() => {
    api.listScans().then((r) => {
      setScans(r.scans || [])
      setScanId(r.latest || null)
    }).catch(() => {})
  }, [])

  const runAnalyze = async () => {
    setError(''); setLoading(true); setResult(null)
    try {
      const a = await api.resolverAnalyze(scanId, sot)
      setAnalysis(a)
      track('resolver_analyze', { scan_id: scanId, sot, plan: a.plan_remove_count, space: a.plan_remove_space_human })
    } catch (e: any) {
      setError(e.message || 'Analyze failed')
    } finally {
      setLoading(false)
    }
  }

  const runExecute = async () => {
    if (!sot.length) { setError('Add at least one source-of-truth folder first'); return }
    if (!confirm(`This will move ${analysis?.plan_remove_count ?? 0} duplicate files to quarantine (reversible). Continue?`)) return
    setError(''); setExecuting(true)
    try {
      const r = await api.resolverExecute(scanId, sot)
      setResult(r)
      track('resolver_execute', { removed: r.removed, freed: r.freed_human })
      const a = await api.resolverAnalyze(scanId, sot)
      setAnalysis(a)
    } catch (e: any) {
      setError(e.message || 'Execute failed')
    } finally {
      setExecuting(false)
    }
  }

  const addSot = (p: string) => {
    const v = p.trim()
    if (v && !sot.includes(v)) setSot([...sot, v])
    setSotInput('')
  }

  const cat = analysis?.by_category || {}
  const catCard = (key: string, label: string, color: string, note: string) => {
    const c = cat[key]
    if (!c) return null
    return (
      <div className="stat-card" style={{ borderLeft: `3px solid ${color}` }}>
        <div className="value" style={{ color, fontSize: '1.3rem' }}>{c.groups.toLocaleString()}</div>
        <div className="label">{label}</div>
        <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: 4 }}>
          {c.files.toLocaleString()} files · {c.wasted_human} · {note}
        </div>
      </div>
    )
  }

  return (
    <div>
      <div className="card">
        <div className="card-header"><h2>Duplicate Resolver</h2></div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          Pick your <strong>source of truth</strong> (the folder whose copies win). The resolver safely
          marks only <strong>cross-source</strong> duplicates elsewhere for removal, and <strong>protects</strong>
          {' '}project/website internal files (templates, node_modules, .git, etc.) from bulk deletion.
        </p>

        {error && <div className="alert alert-error">{error}</div>}

        <div className="form-group">
          <label>Scan</label>
          <select value={scanId || ''} onChange={(e) => setScanId(e.target.value)}
            style={{ width: '100%', padding: '8px', background: 'var(--bg-tertiary)', border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text-primary)' }}>
            {scans.length === 0 && <option value="">No completed scans yet</option>}
            {scans.map((s) => (
              <option key={s.scan_id} value={s.scan_id}>
                {s.duplicate_groups?.toLocaleString()} groups — {Array.isArray(s.directories) ? s.directories.join(', ') : s.directories} ({new Date((s.saved_at || 0) * 1000).toLocaleString()})
              </option>
            ))}
          </select>
        </div>

        <div className="form-group">
          <label>Source of Truth — folders to KEEP (winners)</label>
          {sot.map((d, i) => (
            <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 4, background: 'var(--bg-tertiary)', padding: '6px 12px', borderRadius: 6, fontFamily: 'monospace', fontSize: '0.8rem' }}>
              <span style={{ color: 'var(--success)' }}>KEEP</span>
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}>{d}</span>
              <button className="btn btn-secondary btn-sm" onClick={() => setSot(sot.filter((_, j) => j !== i))} style={{ padding: '2px 8px' }}>✕</button>
            </div>
          ))}
          <div style={{ display: 'flex', gap: 8 }}>
            <input type="text" value={sotInput} onChange={(e) => setSotInput(e.target.value)}
              placeholder="e.g. E:/Google Drive Files/Organized"
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addSot(sotInput) } }}
              style={{ flex: 1 }} />
            <button className="btn btn-secondary" onClick={() => addSot(sotInput)} disabled={!sotInput.trim()}>Add</button>
            <button className="btn btn-secondary" onClick={() => setShowBrowser(true)}>Browse</button>
          </div>
        </div>

        <button className="btn btn-primary" onClick={runAnalyze} disabled={loading || !scanId}>
          {loading ? <><span className="spinner" /> Analyzing...</> : 'Analyze'}
        </button>
      </div>

      {analysis && (
        <>
          <div className="card">
            <div className="card-header"><h3>How your duplicates classify</h3></div>
            <div className="stats-grid">
              {catCard('cross_source_redundant', 'Cross-source (safe to dedup)', 'var(--success)', 'same file in 2+ backup locations')}
              {catCard('structural_internal', 'Project/website internal (protected)', 'var(--warning)', 'kept — deleting could break projects')}
              {catCard('single_source', 'Single-source (review)', 'var(--accent)', 'copies within one location')}
            </div>
          </div>

          <div className="card">
            <div className="card-header"><h3>Safe removal plan</h3></div>
            {sot.length === 0 ? (
              <div className="alert alert-info">Add a source-of-truth folder above and re-analyze to build a removal plan.</div>
            ) : (
              <>
                <div className="stats-grid">
                  <div className="stat-card">
                    <div className="value" style={{ color: 'var(--danger)' }}>{analysis.plan_remove_count?.toLocaleString()}</div>
                    <div className="label">Files to remove</div>
                  </div>
                  <div className="stat-card">
                    <div className="value" style={{ color: 'var(--success)' }}>{analysis.plan_remove_space_human}</div>
                    <div className="label">Space reclaimed</div>
                  </div>
                  <div className="stat-card">
                    <div className="value" style={{ color: 'var(--warning)' }}>{analysis.protected_files?.toLocaleString()}</div>
                    <div className="label">Protected (kept)</div>
                  </div>
                </div>

                {analysis.removable_by_source && Object.keys(analysis.removable_by_source).length > 0 && (
                  <div style={{ margin: '12px 0' }}>
                    <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: 6 }}>Removals by source:</div>
                    {Object.entries<any>(analysis.removable_by_source).map(([src, v]) => (
                      <div key={src} style={{ fontSize: '0.8rem', fontFamily: 'monospace' }}>
                        {src}: {v.files.toLocaleString()} files · {v.space_human}
                      </div>
                    ))}
                  </div>
                )}

                <details style={{ margin: '8px 0' }}>
                  <summary style={{ cursor: 'pointer', fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                    Preview first {analysis.plan_sample?.length || 0} removals
                  </summary>
                  <div style={{ maxHeight: 260, overflowY: 'auto', marginTop: 8 }}>
                    {(analysis.plan_sample || []).map((p: any, i: number) => (
                      <div key={i} style={{ fontSize: '0.72rem', fontFamily: 'monospace', padding: '3px 0', borderBottom: '1px solid var(--border)' }}>
                        <div style={{ color: 'var(--danger)' }}>REMOVE {p.path}</div>
                        <div style={{ color: 'var(--success)' }}>  KEEP  {p.keep}</div>
                      </div>
                    ))}
                  </div>
                </details>

                {result && (
                  <div className="alert alert-success">
                    Quarantined {result.removed?.toLocaleString()} files, freed {result.freed_human}.
                    {result.error_count > 0 && ` (${result.error_count} errors)`}
                    <div style={{ fontSize: '0.7rem', marginTop: 4 }}>Quarantine: {result.quarantine}</div>
                  </div>
                )}

                <button className="btn btn-danger" onClick={runExecute}
                  disabled={executing || !analysis.plan_remove_count}>
                  {executing ? <><span className="spinner" /> Quarantining...</> : `Quarantine ${analysis.plan_remove_count?.toLocaleString()} files (reversible)`}
                </button>
              </>
            )}
          </div>
        </>
      )}

      {/* ---------------- AI Advisor ---------------- */}
      <div className="card">
        <div className="card-header"><h3>AI Advisor — reads the actual files</h3></div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.82rem' }}>
          For the ambiguous cases (like framework templates), the AI <strong>reads the real file
          content</strong> (not the filename) and recommends keep/remove with cited evidence. It refuses
          to guess — anything it can't read or judge is flagged for your review. It never deletes; you decide.
        </p>
        <div className="form-row">
          <div className="form-group">
            <label>Max groups to advise (biggest-waste first)</label>
            <input type="number" value={advMax} onChange={(e) => setAdvMax(e.target.value)} />
          </div>
          <div className="form-group">
            <label>Min file size (MB)</label>
            <input type="number" value={advMinMB} onChange={(e) => setAdvMinMB(e.target.value)} />
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn btn-primary" onClick={startAdvisor}
            disabled={!scanId || (advJob && advJob.status === 'running')}>
            {advJob && advJob.status === 'running'
              ? <><span className="spinner" /> Advising {advJob.done}/{advJob.total}...</>
              : 'Get AI Advice'}
          </button>
          {advJob && advJob.status === 'running' && (
            <button className="btn btn-danger" onClick={stopAdvisor}>Stop</button>
          )}
        </div>

        {advJob && (advJob.results || []).length > 0 && (
          <div style={{ marginTop: 16 }}>
            <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: 8 }}>
              {advJob.results.length} groups advised{advJob.status === 'running' ? ' (updating…)' : ''} — ranked by wasted space
            </div>
            {[...advJob.results]
              .sort((a: any, b: any) => (b.wasted_space || 0) - (a.wasted_space || 0))
              .map((r: any, i: number) => (
                <div key={i} style={{
                  border: '1px solid var(--border)', borderLeft: `4px solid ${riskColor(r.risk)}`,
                  borderRadius: 6, padding: '10px 12px', marginBottom: 8, background: 'var(--bg-tertiary)',
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
                    <span style={{ fontWeight: 600, color: riskColor(r.risk) }}>
                      {classLabel[r.classification] || r.classification}
                    </span>
                    <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                      {r.copies} copies · {r.wasted_human} · action: {r.recommended_action} · conf {(r.confidence ?? 0).toFixed?.(2) ?? r.confidence}
                      {' '}· {r.content_read ? `read ${r.content_kind}` : '⚠ content not read'}
                    </span>
                  </div>
                  <div style={{ fontSize: '0.8rem', marginTop: 6 }}>{r.rationale}</div>
                  {r.evidence && (
                    <div style={{ fontSize: '0.72rem', fontFamily: 'monospace', color: 'var(--text-secondary)', marginTop: 4, background: 'var(--bg-primary)', padding: '4px 8px', borderRadius: 4 }}>
                      evidence: {String(r.evidence).slice(0, 240)}
                    </div>
                  )}
                  <details style={{ marginTop: 4 }}>
                    <summary style={{ cursor: 'pointer', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                      {r.distinct_locations?.length || 0} locations
                    </summary>
                    {(r.distinct_locations || []).map((d: string, j: number) => (
                      <div key={j} style={{ fontSize: '0.68rem', fontFamily: 'monospace', color: 'var(--text-muted)' }}>{d}</div>
                    ))}
                  </details>
                </div>
              ))}
          </div>
        )}
      </div>

      {showBrowser && (
        <DirectoryBrowser onSelect={(p) => { addSot(p); setShowBrowser(false) }} onClose={() => setShowBrowser(false)} />
      )}
    </div>
  )
}
