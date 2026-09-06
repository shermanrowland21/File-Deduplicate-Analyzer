import { useState } from 'react'
import { api } from '../api'
import { track } from '../telemetry'
import { DirectoryBrowser } from './DirectoryBrowser'

/**
 * Folder Re-Naming Advisor UI. Point at a parent folder whose subfolder names
 * were truncated/garbled; the backend reads the FILES INSIDE each folder
 * (descriptive filenames first, then a cheap AWS-Bedrock model reading real
 * content) and proposes corrected names with a CONFIDENCE score + cited
 * evidence. Review low-confidence first, approve/edit, then apply — renames are
 * collision-safe and reversible (never overwrite, never delete).
 */
interface Row {
  folder: string
  current_name: string
  proposed_name: string
  confidence: number
  evidence: string
  rationale: string
  needs_review: boolean
  is_change: boolean
  tier: number
  content_file_used?: string | null
  _approved?: boolean
  _edited?: string
}

export function FolderRenamerPanel() {
  const [parent, setParent] = useState('')
  const [onlySuspected, setOnlySuspected] = useState(true)
  const [job, setJob] = useState<any>(null)
  const [rows, setRows] = useState<Row[]>([])
  const [threshold, setThreshold] = useState(0.85)
  const [error, setError] = useState('')
  const [applying, setApplying] = useState(false)
  const [applyResult, setApplyResult] = useState<any>(null)
  const [showBrowser, setShowBrowser] = useState(false)
  const pollRef = useState<{ id: number | null }>({ id: null })[0]

  const startAnalyze = async () => {
    setError(''); setApplyResult(null); setRows([])
    if (!parent.trim()) { setError('Pick a parent folder first'); return }
    try {
      const r = await api.folderNamerAnalyze(parent.trim(), { only_suspected: onlySuspected })
      track('foldernamer_analyze', { parent: parent.trim(), job: r.job_id, model: r.model })
      if (pollRef.id) clearInterval(pollRef.id)
      pollRef.id = window.setInterval(async () => {
        try {
          const j = await api.folderNamerStatus(r.job_id)
          setJob(j)
          if (j.results) {
            setRows(j.results.map((x: Row) => ({ ...x, _approved: x.confidence >= threshold && x.is_change })))
          }
          if (j.status === 'completed' || j.status === 'error' || j.status === 'cancelled') {
            if (pollRef.id) { clearInterval(pollRef.id); pollRef.id = null }
          }
        } catch { /* keep polling */ }
      }, 1200)
    } catch (e: any) {
      setError(e.message || 'Analyze failed to start')
    }
  }

  const stopAnalyze = async () => {
    if (job?.status === 'running' && job.parent) { /* best-effort */ }
    if (pollRef.id) { clearInterval(pollRef.id); pollRef.id = null }
  }

  const setApproved = (folder: string, v: boolean) =>
    setRows(rows.map(r => r.folder === folder ? { ...r, _approved: v } : r))

  const setEdited = (folder: string, name: string) =>
    setRows(rows.map(r => r.folder === folder ? { ...r, _edited: name } : r))

  const approveAboveThreshold = () =>
    setRows(rows.map(r => ({ ...r, _approved: r.is_change && r.confidence >= threshold })))

  const clearAll = () => setRows(rows.map(r => ({ ...r, _approved: false })))

  const approvedItems = rows.filter(r => r._approved && r.is_change).map(r => ({
    folder: r.folder,
    proposed_name: (r._edited ?? r.proposed_name),
  }))

  const apply = async () => {
    if (approvedItems.length === 0) { setError('Nothing approved to apply'); return }
    if (!window.confirm(`Rename ${approvedItems.length} folder(s)? This is reversible (a manifest is written so it can be undone).`)) return
    setApplying(true); setError('')
    try {
      const res = await api.folderNamerApply(approvedItems)
      setApplyResult(res)
      track('foldernamer_apply', { renamed: res.renamed, manifest: res.manifest })
      // remove applied rows from the list
      const done = new Set(res.moves?.map((m: any) => m.from) || [])
      setRows(rows.filter(r => !done.has(r.folder)))
    } catch (e: any) {
      setError(e.message || 'Apply failed')
    } finally {
      setApplying(false)
    }
  }

  const undo = async () => {
    if (!applyResult?.manifest) return
    if (!window.confirm('Undo the last batch of renames?')) return
    try {
      const res = await api.folderNamerUndo(applyResult.manifest)
      setApplyResult({ ...applyResult, undone: res })
    } catch (e: any) {
      setError(e.message || 'Undo failed')
    }
  }

  const confColor = (c: number) => c >= 0.85 ? 'var(--success)' : c >= 0.5 ? 'var(--warning)' : 'var(--danger)'
  const tierLabel = (t: number) => t === 1 ? 'filename' : t === 2 ? 'AI read content' : 'weak / review'

  const changed = rows.filter(r => r.is_change)
  const approvedCount = rows.filter(r => r._approved && r.is_change).length

  return (
    <div>
      <div className="card">
        <div className="card-header"><h2>Folder Re-Naming Advisor</h2></div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          Recovers folder names that got truncated during migration by <strong>reading the files
          inside</strong> — descriptive filenames first (free), then a cheap AWS-Bedrock model that
          reads the actual document/slide content when filenames aren't enough. Every proposal shows a
          confidence score and the exact evidence it came from. Nothing is renamed until you approve;
          renames are collision-safe and reversible.
        </p>

        {error && <div className="alert alert-error">{error}</div>}

        <div className="form-group">
          <label>Parent folder (its subfolders will be analyzed)</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <input type="text" value={parent} onChange={e => setParent(e.target.value)}
              placeholder="e.g. E:/Google Drive Files/Organized/Marketing Dropbox/HPL/HPLive Webinar-"
              style={{ flex: 1 }} />
            <button className="btn btn-secondary" onClick={() => setShowBrowser(true)}>Browse</button>
          </div>
        </div>

        <label className="checkbox-label" style={{ marginBottom: 12 }}>
          <input type="checkbox" checked={onlySuspected} onChange={e => setOnlySuspected(e.target.checked)} />
          Only analyze folders that look truncated/garbled (faster, cheaper)
        </label>

        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn btn-primary" onClick={startAnalyze}
            disabled={job && job.status === 'running'}>
            {job && job.status === 'running'
              ? <><span className="spinner" /> Analyzing {job.done}/{job.total}...</>
              : 'Analyze Folders'}
          </button>
          {job && job.status === 'running' && (
            <button className="btn btn-danger" onClick={stopAnalyze}>Stop</button>
          )}
        </div>
        {job && job.status === 'running' && job.current && (
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 8 }}>
            reading: {job.current}
          </div>
        )}
      </div>

      {applyResult && (
        <div className="alert alert-success" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>
            Renamed {applyResult.renamed} folder(s){applyResult.skipped ? `, skipped ${applyResult.skipped}` : ''}.
            {applyResult.undone ? ` Undo restored ${applyResult.undone.restored}.` : ''}
          </span>
          {!applyResult.undone && applyResult.manifest && (
            <button className="btn btn-secondary btn-sm" onClick={undo}>Undo</button>
          )}
        </div>
      )}

      {changed.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>{changed.length} proposed rename{changed.length === 1 ? '' : 's'}</h3>
            <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
              {approvedCount} approved
            </span>
          </div>

          <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
            <label style={{ fontSize: '0.8rem' }}>
              Auto-approve confidence ≥ {threshold.toFixed(2)}
              <input type="range" min={0} max={1} step={0.05} value={threshold}
                onChange={e => setThreshold(parseFloat(e.target.value))}
                style={{ verticalAlign: 'middle', marginLeft: 8 }} />
            </label>
            <button className="btn btn-secondary btn-sm" onClick={approveAboveThreshold}>Approve all ≥ threshold</button>
            <button className="btn btn-secondary btn-sm" onClick={clearAll}>Clear all</button>
            <button className="btn btn-primary btn-sm" onClick={apply} disabled={applying || approvedCount === 0}>
              {applying ? <><span className="spinner" /> Applying...</> : `Apply ${approvedCount} rename(s)`}
            </button>
          </div>

          {/* Rows are already ranked low-confidence-first by the backend so you
              review the risky ones. */}
          {changed.map((r) => (
            <div key={r.folder} style={{
              border: '1px solid var(--border)',
              borderLeft: `4px solid ${confColor(r.confidence)}`,
              borderRadius: 6, padding: '10px 12px', marginBottom: 8,
              background: r._approved ? 'var(--bg-tertiary)' : 'var(--bg-secondary)',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <input type="checkbox" checked={!!r._approved}
                  onChange={e => setApproved(r.folder, e.target.checked)} />
                <span style={{ color: confColor(r.confidence), fontWeight: 600, fontSize: '0.8rem' }}>
                  {(r.confidence * 100).toFixed(0)}%
                </span>
                <span style={{
                  fontSize: '0.68rem', padding: '1px 6px', borderRadius: 4,
                  background: 'var(--bg-primary)', color: 'var(--text-muted)',
                }}>{tierLabel(r.tier)}</span>
                {r.needs_review && (
                  <span style={{ fontSize: '0.68rem', color: 'var(--warning)' }}>⚠ review</span>
                )}
              </div>

              <div style={{ marginTop: 6, fontSize: '0.8rem' }}>
                <div style={{ color: 'var(--text-muted)', fontFamily: 'monospace', textDecoration: 'line-through' }}>
                  {r.current_name}
                </div>
                <input type="text" value={r._edited ?? r.proposed_name}
                  onChange={e => setEdited(r.folder, e.target.value)}
                  style={{ width: '100%', marginTop: 4, fontFamily: 'monospace', fontSize: '0.82rem' }} />
              </div>

              <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', marginTop: 6 }}>
                {r.rationale}
              </div>
              {r.evidence && (
                <div style={{
                  fontSize: '0.7rem', fontFamily: 'monospace', color: 'var(--text-secondary)',
                  marginTop: 4, background: 'var(--bg-primary)', padding: '3px 8px', borderRadius: 4,
                }}>
                  evidence: {String(r.evidence).slice(0, 260)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {job && job.status === 'completed' && changed.length === 0 && (
        <div className="empty-state">
          <h3>No rename needed</h3>
          <p>None of the analyzed folders had enough evidence to confidently propose a different name.</p>
        </div>
      )}

      {showBrowser && (
        <DirectoryBrowser onSelect={(p) => { setParent(p); setShowBrowser(false) }} onClose={() => setShowBrowser(false)} />
      )}
    </div>
  )
}
