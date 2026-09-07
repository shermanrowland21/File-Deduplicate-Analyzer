import { useState, useEffect } from 'react'
import { api } from '../api'

/**
 * Cross-Folder Dedup review UI.
 *
 * Dedupe identical files (same MD5) across Organized (master/keeper) and
 * Dropbox-Snapshot (throwaway). "Cross-folder only" (default ON) quarantines
 * ONLY files that exist in BOTH folders — the Snapshot copy is removed, the
 * Organized copy is kept. Files unique to Snapshot are never touched. Within-
 * folder dupes are left alone.
 *
 * You SEE exactly what's queued (keeper vs. removed) before anything moves.
 * Quarantine is reversible (manifest + Undo). Nothing is hard-deleted.
 */
const fmtGB = (bytes: number) => `${(bytes / 1e9).toLocaleString(undefined, { maximumFractionDigits: 1 })} GB`
const num = (n: number) => (n ?? 0).toLocaleString()
const PAGE = 100

export function CrossDedupPanel() {
  // Strict, master-preserving mode: only Snapshot files that also exist in
  // Organized are ever removed. Organized is NEVER touched.
  const [snapshotOnly, setSnapshotOnly] = useState(true)
  const [preferFolder] = useState<'organized' | 'snapshot'>('organized')
  const [summary, setSummary] = useState<any>(null)
  const [groups, setGroups] = useState<any[]>([])
  const [totalGroups, setTotalGroups] = useState(0)
  const [offset, setOffset] = useState(0)
  const [filter, setFilter] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [job, setJob] = useState<any>(null)
  const [manifest, setManifest] = useState<string | null>(null)
  const pollRef = useState<{ id: number | null }>({ id: null })[0]

  const load = async (newOffset = 0, ff = filter) => {
    setLoading(true); setError('')
    try {
      const r = await api.crossdedupGroups({ preferFolder, snapshotOnly, offset: newOffset, limit: PAGE, folderFilter: ff })
      setSummary(r.summary)
      setGroups(r.groups || [])
      setTotalGroups(r.total_groups || 0)
      setOffset(newOffset)
    } catch (e: any) {
      setError(e.message || 'Failed to load groups')
    } finally {
      setLoading(false)
    }
  }

  // First build can take ~30s (hashes 1.3M rows into groups); reload on toggle.
  useEffect(() => { load(0, filter) }, [snapshotOnly])

  // In snapshot_only mode the summary's redundant_files IS the snapshot-only set.
  const queuedFiles = summary?.redundant_files
  const queuedBytes = summary?.reclaimable_bytes
  const running = job && job.status === 'running'

  const runPurge = async () => {
    if (!window.confirm(
      `Quarantine ${num(queuedFiles)} Snapshot files that are duplicated in Organized?\n\n` +
      `Organized (the master) is NEVER touched. Files are MOVED to a reversible ` +
      `_CrossDedupQuarantine folder in Snapshot, never deleted. You can Undo.`)) return
    setError(''); setNotice('')
    try {
      const r = await api.crossdedupPurge(preferFolder, snapshotOnly)
      pollJob(r.job_id)
    } catch (e: any) {
      setError(e.message || 'Purge failed to start')
    }
  }

  const pollJob = (jobId: string) => {
    if (pollRef.id) clearInterval(pollRef.id)
    pollRef.id = window.setInterval(async () => {
      try {
        const j = await api.crossdedupStatus(jobId)
        setJob(j)
        if (j.status === 'completed' || j.status === 'error') {
          if (pollRef.id) { clearInterval(pollRef.id); pollRef.id = null }
          if (j.status === 'completed') {
            setManifest(j.manifest_file || null)
            setNotice(`Quarantined ${num(j.quarantined)} files (${fmtGB(j.reclaimed_bytes)}). ${j.errors || 0} errors.`)
            load(0, filter)
          }
        }
      } catch { /* keep polling */ }
    }, 1500)
  }

  const undoLast = async () => {
    if (!manifest) return
    if (!window.confirm('Restore all files from the last cross-dedup quarantine?')) return
    try {
      const r = await api.crossdedupUndo(manifest)
      setNotice(`Restored ${num(r.restored)} files.`)
      setManifest(null)
      load(0, filter)
    } catch (e: any) {
      setError(e.message || 'Undo failed')
    }
  }

  return (
    <div>
      <div className="card">
        <div className="card-header"><h2>Cross-Folder Dedup</h2></div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          Removes only <strong>Dropbox-Snapshot</strong> files that are byte-identical to a file already
          in <strong>Organized</strong> (the master). <strong>Organized is never touched.</strong> Files
          unique to Snapshot are kept. Review below, then quarantine — <strong>reversible</strong>, never
          a hard delete.
        </p>

        <label className="checkbox-label" style={{ marginBottom: 8 }}>
          <input type="checkbox" checked={snapshotOnly} onChange={e => setSnapshotOnly(e.target.checked)} disabled={running} />
          Snapshot-only removal (never touch Organized) — recommended
        </label>

        {error && <div className="alert alert-error">{error}</div>}
        {notice && (
          <div className="alert alert-success" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>{notice}</span>
            {manifest && <button className="btn btn-secondary btn-sm" onClick={undoLast}>Undo</button>}
          </div>
        )}

        {summary && (
          <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', margin: '12px 0' }}>
            <Stat label="Snapshot files queued" value={num(queuedFiles)} color="var(--danger)" />
            <Stat label="Space reclaimed" value={fmtGB(queuedBytes)} color="var(--warning)" />
            <Stat label="Duplicate groups" value={num(totalGroups)} />
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <input type="text" value={filter} onChange={e => setFilter(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') load(0, filter) }}
            placeholder="Filter by path (e.g. Marketing, a person's folder)…"
            style={{ flex: 1, minWidth: 240 }} disabled={running} />
          <button className="btn btn-secondary btn-sm" onClick={() => load(0, filter)} disabled={loading || running}>
            {loading ? <><span className="spinner" /> Building…</> : 'Apply filter'}
          </button>
          <button className="btn btn-danger" onClick={runPurge} disabled={running || !summary || !queuedFiles}>
            {running ? <><span className="spinner" /> Quarantining… ({num(job.quarantined)})</> : `Quarantine ${num(queuedFiles)} files`}
          </button>
        </div>
      </div>

      {loading && groups.length === 0 && (
        <div className="empty-state">
          <span className="spinner" /> Building duplicate groups from the MD5 index (first load ~30s)…
        </div>
      )}

      {groups.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Review — {num(totalGroups)} groups{summary?.snapshot_only ? ' (Snapshot-only removal)' : ''}</h3>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: '0.8rem' }}>
              <button className="btn btn-secondary btn-sm" onClick={() => load(Math.max(0, offset - PAGE), filter)} disabled={offset === 0 || loading}>‹ Prev</button>
              <span style={{ color: 'var(--text-muted)' }}>{offset + 1}–{Math.min(offset + PAGE, totalGroups)}</span>
              <button className="btn btn-secondary btn-sm" onClick={() => load(offset + PAGE, filter)} disabled={offset + PAGE >= totalGroups || loading}>Next ›</button>
            </div>
          </div>

          {groups.map((g, i) => (
            <div key={g.md5 + i} style={{ borderBottom: '1px solid var(--border)', padding: '8px 0', fontSize: '0.78rem' }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
                <span style={{ color: 'var(--success)', fontWeight: 600, flexShrink: 0 }}>★ KEEP</span>
                <span style={{ fontFamily: 'monospace', wordBreak: 'break-all', color: 'var(--text-primary)' }}>
                  [{g.keeper_folder}] {g.keeper}
                </span>
                <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', flexShrink: 0 }}>{g.reclaim_mb} MB</span>
              </div>
              {g.removes.map((r: any, j: number) => (
                <div key={j} style={{ display: 'flex', gap: 8, alignItems: 'baseline', paddingLeft: 14 }}>
                  <span style={{ color: 'var(--danger)', flexShrink: 0 }}>✕ drop</span>
                  <span style={{ fontFamily: 'monospace', wordBreak: 'break-all', color: 'var(--text-secondary)' }}>
                    [{r.folder}] {r.path}
                  </span>
                  <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', flexShrink: 0 }}>{r.size_mb} MB</span>
                </div>
              ))}
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
