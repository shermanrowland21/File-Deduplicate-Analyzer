import { useState } from 'react'
import { api } from '../api'
import { track } from '../telemetry'
import { DirectoryBrowser } from './DirectoryBrowser'
import { NamingBuilder } from './NamingBuilder'
import { NamingConvention } from '../types'

/**
 * Smart Rename — the unified renamer. Crawl a directory; the AI ALWAYS reads the
 * real content of each file (vision + OCR for images, text + embedded-image OCR
 * for docs). You can optionally format the AI's understanding into a naming
 * convention PER FILE TYPE (images vs Word vs Excel …) — AI and your naming
 * format work together, not either/or. Confidence-ranked, evidence-cited,
 * reversible. Risk-weighted auto-approve (rename is reversible → lower bar).
 * Video/audio are flagged for the platform's Media Intelligence, not processed here.
 */
interface Row {
  path: string
  current_name: string
  proposed_name: string
  confidence: number
  evidence: string
  rationale: string
  needs_review: boolean
  needs_media_analysis?: boolean
  content_read?: boolean
  is_change: boolean
  kind: string
  ocr_used?: boolean
  formatted_by_convention?: boolean
  _approved?: boolean
  _edited?: string
}

const RENAME_THRESHOLD = 0.8  // risk-weighted default for a reversible rename

const TYPE_GROUPS = ['default', 'image', 'raw_image', 'video', 'audio', 'document', 'spreadsheet', 'presentation', 'pdf', 'graphic', 'archive']
const TYPE_LABELS: Record<string, string> = {
  default: 'Default (all others)', image: 'Images', raw_image: 'RAW photos', video: 'Video',
  audio: 'Audio', document: 'Word / docs', spreadsheet: 'Excel / sheets',
  presentation: 'Slides', pdf: 'PDF', graphic: 'Graphics (PSD/AI)', archive: 'Archives',
}
// Default to a clean, space-separated style (no underscores). The user can change
// the separator + how spaces are handled in the UI below.
const defaultConv = (): NamingConvention => ({
  template: '{date} {suggested_name}.{ext}',
  date_format: '%Y-%m-%d', separator: ' ', case: 'lower',
  max_length: 255, replace_spaces_with: ' ',
})

// Options for the separator + space-handling pickers.
const SEP_OPTIONS: { value: string; label: string }[] = [
  { value: ' ', label: 'Space  (my report.pdf)' },
  { value: '-', label: 'Hyphen  (my-report.pdf)' },
  { value: '_', label: 'Underscore  (my_report.pdf)' },
  { value: '', label: 'None  (myreport.pdf)' },
]

export function SmartRenamePanel() {
  const [directory, setDirectory] = useState('')
  const [recursive, setRecursive] = useState(true)
  const [onlyIllNamed, setOnlyIllNamed] = useState(true)
  const [maxFiles, setMaxFiles] = useState('300')
  const [job, setJob] = useState<any>(null)
  const [rows, setRows] = useState<Row[]>([])
  const [threshold, setThreshold] = useState(RENAME_THRESHOLD)
  const [error, setError] = useState('')
  const [applying, setApplying] = useState(false)
  const [applyResult, setApplyResult] = useState<any>(null)
  const [showBrowser, setShowBrowser] = useState(false)
  const pollRef = useState<{ id: number | null }>({ id: null })[0]

  // Naming-convention formatting (optional; AI reads content either way).
  const [useConvention, setUseConvention] = useState(false)
  const [conventions, setConventions] = useState<Record<string, NamingConvention>>({ default: defaultConv() })
  const [activeType, setActiveType] = useState('default')
  const convention = conventions[activeType] || conventions.default
  const setConvention = (c: NamingConvention) => setConventions((p) => ({ ...p, [activeType]: c }))

  const start = async () => {
    setError(''); setApplyResult(null); setRows([])
    if (!directory.trim()) { setError('Pick a directory first'); return }
    try {
      const r = await api.fileNamerAnalyze(directory.trim(), {
        recursive, only_illnamed: onlyIllNamed, max_files: parseInt(maxFiles) || 300,
        conventions: useConvention ? conventions : undefined,
      })
      track('smartrename_analyze', { dir: directory.trim(), job: r.job_id, convention: useConvention })
      if (typeof r.rename_threshold === 'number') setThreshold(r.rename_threshold)
      if (pollRef.id) clearInterval(pollRef.id)
      pollRef.id = window.setInterval(async () => {
        try {
          const j = await api.fileNamerStatus(r.job_id)
          setJob(j)
          if (j.results) {
            setRows(j.results.map((x: Row) => ({ ...x, _approved: x.confidence >= threshold && x.is_change })))
          }
          if (['completed', 'error', 'cancelled'].includes(j.status)) {
            if (pollRef.id) { clearInterval(pollRef.id); pollRef.id = null }
          }
        } catch { /* keep polling */ }
      }, 1200)
    } catch (e: any) {
      setError(e.message || 'Analyze failed to start')
    }
  }

  const stop = () => { if (pollRef.id) { clearInterval(pollRef.id); pollRef.id = null } }

  const setApproved = (path: string, v: boolean) =>
    setRows(rows.map(r => r.path === path ? { ...r, _approved: v } : r))
  const setEdited = (path: string, name: string) =>
    setRows(rows.map(r => r.path === path ? { ...r, _edited: name } : r))
  const approveAbove = () =>
    setRows(rows.map(r => ({ ...r, _approved: r.is_change && r.confidence >= threshold })))
  const clearAll = () => setRows(rows.map(r => ({ ...r, _approved: false })))

  const approvedItems = rows.filter(r => r._approved && r.is_change).map(r => ({
    path: r.path, proposed_name: (r._edited ?? r.proposed_name),
  }))

  const apply = async () => {
    if (approvedItems.length === 0) { setError('Nothing approved to apply'); return }
    if (!window.confirm(`Rename ${approvedItems.length} file(s)? Reversible — a manifest is written so it can be undone.`)) return
    setApplying(true); setError('')
    try {
      const res = await api.fileNamerApply(approvedItems)
      setApplyResult(res)
      track('smartrename_apply', { renamed: res.renamed, manifest: res.manifest })
      const done = new Set(res.moves?.map((m: any) => m.from) || [])
      setRows(rows.filter(r => !done.has(r.path)))
    } catch (e: any) {
      setError(e.message || 'Apply failed')
    } finally { setApplying(false) }
  }

  const undo = async () => {
    if (!applyResult?.manifest) return
    if (!window.confirm('Undo the last batch of renames?')) return
    try {
      const res = await api.fileNamerUndo(applyResult.manifest)
      setApplyResult({ ...applyResult, undone: res })
    } catch (e: any) { setError(e.message || 'Undo failed') }
  }

  const confColor = (c: number) => c >= 0.8 ? 'var(--success)' : c >= 0.5 ? 'var(--warning)' : 'var(--danger)'

  // Split proposals: nameable changes vs the "couldn't read" bucket (kept
  // separate + loud, per research: unreadable must never be a silent miss).
  const unreadable = rows.filter(r => r.content_read === false && !r.needs_media_analysis)
  const mediaFlagged = rows.filter(r => r.needs_media_analysis)
  const changed = rows.filter(r => r.is_change && r.content_read !== false)
  const approvedCount = rows.filter(r => r._approved && r.is_change).length

  return (
    <div>
      <div className="card">
        <div className="card-header"><h2>Smart Rename</h2></div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          Crawls a directory and, for each file, the AI <strong>reads the real content</strong> —
          vision + OCR for images, extracted text (and OCR of embedded screenshots) for documents.
          Optionally format the result into a <strong>naming convention per file type</strong>.
          Every proposal shows a confidence score and cited evidence; review, edit, approve. Renames
          are collision-safe and reversible. (Videos/audio are flagged for Media Intelligence.)
        </p>

        {error && <div className="alert alert-error">{error}</div>}

        <div className="form-group">
          <label>Directory</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <input type="text" value={directory} onChange={e => setDirectory(e.target.value)}
              placeholder="e.g. D:/Highland Park Dropbox/3 Rare Earth Trading/1 Agate Photos"
              style={{ flex: 1 }} />
            <button className="btn btn-secondary" onClick={() => setShowBrowser(true)}>Browse</button>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap', marginBottom: 12 }}>
          <label className="checkbox-label" style={{ margin: 0 }}>
            <input type="checkbox" checked={recursive} onChange={e => setRecursive(e.target.checked)} />
            Crawl all subfolders
          </label>
          <label className="checkbox-label" style={{ margin: 0 }}>
            <input type="checkbox" checked={onlyIllNamed} onChange={e => setOnlyIllNamed(e.target.checked)} />
            Only ill-named files
          </label>
          <label style={{ fontSize: '0.85rem' }}>
            Max files: <input type="number" value={maxFiles} onChange={e => setMaxFiles(e.target.value)} style={{ width: 90 }} />
          </label>
        </div>

        {/* Optional naming-convention formatting */}
        <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
          <label className="checkbox-label" style={{ margin: 0, fontWeight: 600 }}>
            <input type="checkbox" checked={useConvention} onChange={e => setUseConvention(e.target.checked)} />
            Format names with a naming convention (per file type)
          </label>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '4px 0 0 24px' }}>
            {useConvention
              ? 'The AI reads the content, then its understanding fills your template below.'
              : 'Off: the AI proposes a plain descriptive name. Turn on to control the format.'}
          </div>

          {useConvention && (
            <div style={{ marginTop: 12 }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
                {TYPE_GROUPS.map((g) => (
                  <button key={g} type="button" onClick={() => {
                    if (!conventions[g]) setConventions((p) => ({ ...p, [g]: { ...defaultConv() } }))
                    setActiveType(g)
                  }}
                    style={{
                      padding: '4px 10px', borderRadius: 6, fontSize: '0.78rem', cursor: 'pointer',
                      background: activeType === g ? 'var(--accent)' : 'var(--bg-tertiary)',
                      color: activeType === g ? 'white' : 'var(--text-secondary)',
                      border: '1px solid var(--border)',
                    }}>
                    {TYPE_LABELS[g]}{conventions[g] && g !== 'default' ? ' ●' : ''}
                  </button>
                ))}
              </div>
              {/* Separator + space handling — this is what controls underscores. */}
              <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 10 }}>
                <label style={{ fontSize: '0.8rem' }}>
                  Separator between parts:{' '}
                  <select value={convention.separator}
                    onChange={e => setConvention({ ...convention, separator: e.target.value })}
                    style={{ marginLeft: 4 }}>
                    {SEP_OPTIONS.map(o => <option key={o.value || 'none'} value={o.value}>{o.label}</option>)}
                  </select>
                </label>
                <label style={{ fontSize: '0.8rem' }}>
                  Spaces in names become:{' '}
                  <select value={convention.replace_spaces_with}
                    onChange={e => setConvention({ ...convention, replace_spaces_with: e.target.value })}
                    style={{ marginLeft: 4 }}>
                    {SEP_OPTIONS.map(o => <option key={o.value || 'none'} value={o.value}>{o.label}</option>)}
                  </select>
                </label>
                <label style={{ fontSize: '0.8rem' }}>
                  Case:{' '}
                  <select value={convention.case}
                    onChange={e => setConvention({ ...convention, case: e.target.value as any })}
                    style={{ marginLeft: 4 }}>
                    <option value="lower">lowercase</option>
                    <option value="upper">UPPERCASE</option>
                    <option value="title">Title Case</option>
                    <option value="none">Keep as-is</option>
                  </select>
                </label>
              </div>
              <NamingBuilder key={activeType + convention.separator} template={convention.template} separator={convention.separator}
                onChange={(tpl) => setConvention({ ...convention, template: tpl })} />
            </div>
          )}
        </div>

        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn btn-primary" onClick={start} disabled={job && job.status === 'running'}>
            {job && job.status === 'running'
              ? <><span className="spinner" /> Analyzing {job.done}/{job.total}...</>
              : 'Analyze Files'}
          </button>
          {job && job.status === 'running' && <button className="btn btn-danger" onClick={stop}>Stop</button>}
        </div>
        {job && job.status === 'running' && job.current && (
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 8 }}>reading: {job.current}</div>
        )}
      </div>

      {applyResult && (
        <div className="alert alert-success" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>
            Renamed {applyResult.renamed} file(s){applyResult.skipped ? `, skipped ${applyResult.skipped}` : ''}.
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
            <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>{approvedCount} approved</span>
          </div>

          <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
            <label style={{ fontSize: '0.8rem' }}>
              Auto-approve confidence ≥ {threshold.toFixed(2)}
              <input type="range" min={0} max={1} step={0.05} value={threshold}
                onChange={e => setThreshold(parseFloat(e.target.value))} style={{ verticalAlign: 'middle', marginLeft: 8 }} />
              <span style={{ color: 'var(--text-muted)', marginLeft: 6, fontSize: '0.72rem' }}>
                (rename is reversible → 0.80 default)
              </span>
            </label>
            <button className="btn btn-secondary btn-sm" onClick={approveAbove}>Approve all ≥ threshold</button>
            <button className="btn btn-secondary btn-sm" onClick={clearAll}>Clear all</button>
            <button className="btn btn-primary btn-sm" onClick={apply} disabled={applying || approvedCount === 0}>
              {applying ? <><span className="spinner" /> Applying...</> : `Apply ${approvedCount} rename(s)`}
            </button>
          </div>

          {changed.map((r) => (
            <div key={r.path} style={{
              border: '1px solid var(--border)', borderLeft: `4px solid ${confColor(r.confidence)}`,
              borderRadius: 6, padding: '10px 12px', marginBottom: 8,
              background: r._approved ? 'var(--bg-tertiary)' : 'var(--bg-secondary)',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <input type="checkbox" checked={!!r._approved} onChange={e => setApproved(r.path, e.target.checked)} />
                <span style={{ color: confColor(r.confidence), fontWeight: 600, fontSize: '0.8rem' }}>{(r.confidence * 100).toFixed(0)}%</span>
                <span style={{ fontSize: '0.68rem', padding: '1px 6px', borderRadius: 4, background: 'var(--bg-primary)', color: 'var(--text-muted)' }}>
                  {r.kind}{r.ocr_used ? ' · OCR' : ''}{r.formatted_by_convention ? ' · convention' : ''}
                </span>
                {r.needs_review && <span style={{ fontSize: '0.68rem', color: 'var(--warning)' }}>⚠ review</span>}
              </div>
              <div style={{ marginTop: 6, fontSize: '0.8rem' }}>
                <div style={{ color: 'var(--text-muted)', fontFamily: 'monospace', textDecoration: 'line-through' }}>{r.current_name}</div>
                <input type="text" value={r._edited ?? r.proposed_name} onChange={e => setEdited(r.path, e.target.value)}
                  style={{ width: '100%', marginTop: 4, fontFamily: 'monospace', fontSize: '0.82rem' }} />
              </div>
              <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', marginTop: 6 }}>{r.rationale}</div>
              {r.evidence && (
                <div style={{ fontSize: '0.7rem', fontFamily: 'monospace', color: 'var(--text-secondary)', marginTop: 4, background: 'var(--bg-primary)', padding: '3px 8px', borderRadius: 4 }}>
                  evidence: {String(r.evidence).slice(0, 260)}
                </div>
              )}
              <div style={{ fontSize: '0.66rem', color: 'var(--text-muted)', marginTop: 4, fontFamily: 'monospace' }}>{r.path}</div>
            </div>
          ))}
        </div>
      )}

      {/* Distinct, LOUD bucket for files the AI could NOT read — never silently
          skipped or blank-named (research learning 1). */}
      {unreadable.length > 0 && (
        <div className="card">
          <div className="card-header"><h3 style={{ color: 'var(--warning)' }}>⚠ {unreadable.length} couldn't be read — needs human review</h3></div>
          <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
            These files had no readable text/content the AI could name from (e.g. raw binary, empty,
            or unreadable). They were NOT renamed and NOT guessed. Review them manually.
          </p>
          {unreadable.map((r) => (
            <div key={r.path} style={{ fontSize: '0.72rem', fontFamily: 'monospace', color: 'var(--text-muted)', padding: '2px 0' }}>
              {r.current_name} — {r.evidence || r.rationale}
            </div>
          ))}
        </div>
      )}

      {mediaFlagged.length > 0 && (
        <div className="alert alert-warning" style={{ fontSize: '0.8rem' }}>
          {mediaFlagged.length} video/audio file(s) need Media Intelligence (transcript/topics) to be
          named — that runs on the CPMS platform, not locally.
        </div>
      )}

      {job && job.status === 'completed' && changed.length === 0 && unreadable.length === 0 && (
        <div className="empty-state">
          <h3>No renames proposed</h3>
          <p>No files had enough content evidence to confidently propose a better name.</p>
        </div>
      )}

      {showBrowser && (
        <DirectoryBrowser onSelect={(p) => { setDirectory(p); setShowBrowser(false) }} onClose={() => setShowBrowser(false)} />
      )}
    </div>
  )
}
