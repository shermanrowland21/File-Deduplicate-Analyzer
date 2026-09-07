import { useState, useEffect } from 'react'
import { api } from '../api'
import { KeeperGuidanceChat } from './KeeperGuidanceChat'

/**
 * -pinned Takeout artifact cleanup (within Organized only).
 *
 * Google Takeout flattened Drive version metadata into filenames as
 * "<name>-at-<timestamp>-pinned.<ext>". This screen removes that clutter safely:
 *   - REDUNDANT (has a byte-identical clean-named twin) -> quarantine (reversible)
 *   - UNIQUE (sole copy of its content) -> rename, stripping the suffix
 * MD5-verified: a file is only quarantined when an identical twin survives.
 */
const num = (n: number) => (n ?? 0).toLocaleString()

export function PinnedCleanupPanel() {
  const [preview, setPreview] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  // Persist manifests so Undo survives a page reload.
  const [qManifest, setQManifestState] = useState<string | null>(() => localStorage.getItem('pinned-q-manifest'))
  const [rManifest, setRManifestState] = useState<string | null>(() => localStorage.getItem('pinned-r-manifest'))
  const setQManifest = (m: string | null) => {
    setQManifestState(m)
    if (m) localStorage.setItem('pinned-q-manifest', m); else localStorage.removeItem('pinned-q-manifest')
  }
  const setRManifest = (m: string | null) => {
    setRManifestState(m)
    if (m) localStorage.setItem('pinned-r-manifest', m); else localStorage.removeItem('pinned-r-manifest')
  }
  const [job, setJob] = useState<any>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const qPoll = useState<{ id: number | null }>({ id: null })[0]

  const load = async () => {
    setLoading(true); setError('')
    try {
      setPreview(await api.pinnedPreview(12))
    } catch (e: any) {
      setError(e.message || 'Preview failed')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [])

  const runQuarantine = async () => {
    if (!window.confirm(
      `Quarantine ${num(preview?.redundant_count)} -pinned files that each have an identical clean twin in Organized?\n\n` +
      `Files are MOVED to a reversible _PinnedQuarantine folder, never deleted. You can Cancel or Undo.`)) return
    setBusy(true); setError(''); setNotice('')
    try {
      const r = await api.pinnedQuarantine()   // { job_id }
      setJobId(r.job_id)
      if (qPoll.id) clearInterval(qPoll.id)
      qPoll.id = window.setInterval(async () => {
        try {
          const j = await api.pinnedQuarantineStatus(r.job_id)
          setJob(j)
          if (j.status === 'completed' || j.status === 'error' || j.status === 'cancelled') {
            if (qPoll.id) { clearInterval(qPoll.id); qPoll.id = null }
            setBusy(false)
            setJobId(null)
            if (j.status !== 'error') {
              setQManifest(j.manifest_file || null)
              const verb = j.status === 'cancelled' ? 'Cancelled —' : 'Quarantined'
              setNotice(`${verb} ${num(j.quarantined)} redundant -pinned files.${j.skipped ? ' ' + num(j.skipped) + ' skipped (safety).' : ''}${j.errors ? ' ' + j.errors + ' errors.' : ''}`)
              await load()
            } else {
              setError(j.error || 'Quarantine failed')
            }
          }
        } catch { /* keep polling */ }
      }, 1200)
    } catch (e: any) {
      setError(e.message || 'Quarantine failed')
      setBusy(false)
    }
  }

  const cancelQuarantine = async () => {
    if (!jobId) return
    try { await api.pinnedQuarantineCancel(jobId) } catch { /* ignore */ }
  }

  const runRename = async () => {
    if (!window.confirm(
      `Rename ${num(preview?.unique_count)} unique -pinned files, stripping the "-at-<timestamp>-pinned" suffix back to the clean name?\n\n` +
      `In-place, collision-safe, reversible.`)) return
    setBusy(true); setError(''); setNotice('')
    try {
      const r = await api.pinnedRename()
      setRManifest(r.manifest_file || null)
      setNotice(`Renamed ${num(r.renamed)} unique -pinned files.${r.errors?.length ? ' ' + r.errors.length + ' errors.' : ''}`)
      await load()
    } catch (e: any) {
      setError(e.message || 'Rename failed')
    } finally {
      setBusy(false)
    }
  }

  const undo = async (which: 'q' | 'r') => {
    const manifest = which === 'q' ? qManifest : rManifest
    if (!manifest) return
    if (!window.confirm('Restore from the last operation?')) return
    try {
      const r = await api.pinnedUndo(manifest)
      setNotice(`Restored ${num(r.restored)} files.`)
      if (which === 'q') setQManifest(null); else setRManifest(null)
      await load()
    } catch (e: any) {
      setError(e.message || 'Undo failed')
    }
  }

  return (
    <div>
      <div className="card">
        <div className="card-header"><h2>Pinned Artifact Cleanup</h2></div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          Google Takeout left version metadata baked into filenames as
          <code> name-at-&lt;timestamp&gt;-pinned.ext</code>. This clears that clutter from Organized:
          redundant copies (identical clean twin exists) are <strong>quarantined</strong>; the rare
          unique ones are <strong>renamed</strong> to the clean name. Nothing is hard-deleted — all
          reversible. Only touches Organized.
        </p>

        {error && <div className="alert alert-error">{error}</div>}
        {notice && (
          <div className="alert alert-success" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
            <span>{notice}</span>
            <span style={{ display: 'flex', gap: 6 }}>
              {qManifest && <button className="btn btn-secondary btn-sm" onClick={() => undo('q')}>Undo quarantine</button>}
              {rManifest && <button className="btn btn-secondary btn-sm" onClick={() => undo('r')}>Undo rename</button>}
            </span>
          </div>
        )}

        {loading && !preview && (
          <div className="empty-state"><span className="spinner" /> Scanning the index for -pinned artifacts…</div>
        )}

        {preview && (
          <>
            <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', margin: '12px 0' }}>
              <Stat label="Redundant (quarantine)" value={num(preview.redundant_count)} color="var(--danger)" />
              <Stat label="Unique (rename)" value={num(preview.unique_count)} color="var(--warning)" />
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button className="btn btn-danger" onClick={runQuarantine} disabled={busy || !preview.redundant_count}>
                {busy && jobId ? <><span className="spinner" /> Quarantining… ({num(job?.quarantined || 0)})</> : busy ? <><span className="spinner" /> Working…</> : `Quarantine ${num(preview.redundant_count)} redundant`}
              </button>
              {jobId && <button className="btn btn-secondary" onClick={cancelQuarantine}>Cancel</button>}
              <button className="btn btn-secondary" onClick={runRename} disabled={busy || !preview.unique_count}>
                Rename {num(preview.unique_count)} unique
              </button>
              {qManifest && !jobId && <button className="btn btn-secondary" onClick={() => undo('q')} title="Restore the last quarantine batch">↩ Undo quarantine</button>}
              {rManifest && <button className="btn btn-secondary" onClick={() => undo('r')} title="Restore the last rename batch">↩ Undo rename</button>}
              <button className="btn btn-secondary btn-sm" onClick={load} disabled={busy || loading}>Refresh</button>
            </div>
          </>
        )}
      </div>

      {preview && (
        <KeeperGuidanceChat
          onRuleApplied={load}
          ambiguousCount={0}
          resolveOpts={{ preferFolder: 'organized', snapshotOnly: false }}
          placeholder="Tell me which copy to keep — e.g. “keep the copy in the more specific folder, not the generic one”…"
        />
      )}

      {preview?.redundant_examples?.length > 0 && (
        <div className="card">
          <div className="card-header"><h3>Examples — redundant (quarantine)</h3></div>
          {preview.redundant_examples.map((r: any, i: number) => (
            <div key={i} style={{ borderBottom: '1px solid var(--border)', padding: '6px 0', fontSize: '0.76rem' }}>
              <div style={{ color: 'var(--danger)', fontFamily: 'monospace', wordBreak: 'break-all' }}>✕ {r.drop}</div>
              <div style={{ color: 'var(--success)', fontFamily: 'monospace', wordBreak: 'break-all' }}>★ keep {r.keeper}</div>
            </div>
          ))}
        </div>
      )}

      {preview?.unique_examples?.length > 0 && (
        <div className="card">
          <div className="card-header"><h3>Examples — unique (rename)</h3></div>
          {preview.unique_examples.map((u: any, i: number) => (
            <div key={i} style={{ borderBottom: '1px solid var(--border)', padding: '6px 0', fontSize: '0.76rem' }}>
              <div style={{ fontFamily: 'monospace', wordBreak: 'break-all', color: 'var(--text-secondary)' }}>{u.path}</div>
              <div style={{ fontFamily: 'monospace', color: 'var(--text-primary)' }}>→ {u.rename_to}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div style={{ fontSize: '1.3rem', fontWeight: 700, color: color || 'var(--text-primary)' }}>{value}</div>
      <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>{label}</div>
    </div>
  )
}
