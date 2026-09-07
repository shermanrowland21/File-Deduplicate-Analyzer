import { useState, useEffect } from 'react'
import { api } from '../api'
import { KeeperGuidanceChat } from './KeeperGuidanceChat'

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
  const [currentJobId, setCurrentJobId] = useState<string | null>(null)
  const [guidancePending, setGuidancePending] = useState(false)
  // Persist the last purge manifest so the Undo button survives a page reload.
  const [manifest, setManifestState] = useState<string | null>(
    () => localStorage.getItem('crossdedup-last-manifest')
  )
  const setManifest = (m: string | null) => {
    setManifestState(m)
    if (m) localStorage.setItem('crossdedup-last-manifest', m)
    else localStorage.removeItem('crossdedup-last-manifest')
  }
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

  // ---- System-junk cleanup (._ sidecars, .DS_Store, temp) ----
  const [junk, setJunk] = useState<any>(null)
  const [junkJob, setJunkJob] = useState<any>(null)
  const [junkJobId, setJunkJobId] = useState<string | null>(null)
  const [junkManifest, setJunkManifest] = useState<string | null>(() => localStorage.getItem('junk-last-manifest'))
  const junkPoll = useState<{ id: number | null }>({ id: null })[0]

  const loadJunk = async () => { try { setJunk(await api.junkPreview()) } catch { /* */ } }
  useEffect(() => { loadJunk() }, [])

  const runJunkPurge = async () => {
    if (!window.confirm(
      `Quarantine ${(junk?.total ?? 0).toLocaleString()} system-junk files (._ sidecars, .DS_Store, Thumbs.db, temp)?\n\n` +
      `These are OS noise, not your content. Moved to a reversible _JunkQuarantine folder. You can Undo.`)) return
    try {
      const r = await api.junkPurge()
      setJunkJobId(r.job_id)
      if (junkPoll.id) clearInterval(junkPoll.id)
      junkPoll.id = window.setInterval(async () => {
        try {
          const j = await api.junkStatus(r.job_id)
          setJunkJob(j)
          if (j.status === 'completed' || j.status === 'error' || j.status === 'cancelled') {
            if (junkPoll.id) { clearInterval(junkPoll.id); junkPoll.id = null }
            setJunkJobId(null)
            if (j.status !== 'error') {
              setJunkManifest(j.manifest_file || null)
              if (j.manifest_file) localStorage.setItem('junk-last-manifest', j.manifest_file)
              await loadJunk()
              load(0, filter)   // dedup groups are cleaner now
            }
          }
        } catch { /* keep polling */ }
      }, 1200)
    } catch (e: any) { setError(e.message || 'Junk purge failed') }
  }
  const cancelJunk = async () => { if (junkJobId) { try { await api.junkCancel(junkJobId) } catch { /* */ } } }
  const undoJunk = async () => {
    if (!junkManifest) return
    if (!window.confirm('Restore all quarantined junk files?')) return
    try {
      const r = await api.junkUndo(junkManifest)
      setNotice(`Restored ${(r.restored ?? 0).toLocaleString()} junk files.`)
      setJunkManifest(null); localStorage.removeItem('junk-last-manifest')
      await loadJunk()
    } catch (e: any) { setError(e.message || 'Undo failed') }
  }

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

  const cancelPurge = async () => {
    if (!job?.job_id && !currentJobId) return
    try { await api.crossdedupCancel(currentJobId || job.job_id) } catch { /* ignore */ }
  }

  const pollJob = (jobId: string) => {
    setCurrentJobId(jobId)
    if (pollRef.id) clearInterval(pollRef.id)
    pollRef.id = window.setInterval(async () => {
      try {
        const j = await api.crossdedupStatus(jobId)
        setJob(j)
        if (j.status === 'completed' || j.status === 'error' || j.status === 'cancelled') {
          if (pollRef.id) { clearInterval(pollRef.id); pollRef.id = null }
          if (j.status === 'completed' || j.status === 'cancelled') {
            setManifest(j.manifest_file || null)
            const verb = j.status === 'cancelled' ? 'Cancelled —' : 'Quarantined'
            setNotice(`${verb} ${num(j.quarantined)} files (${fmtGB(j.reclaimed_bytes)}). ${j.errors || 0} errors.`)
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

  const junkRunning = !!junkJobId

  return (
    <div>
      {junk && junk.total > 0 && (
        <div className="card" style={{ borderLeft: '3px solid var(--warning)' }}>
          <div className="card-header"><h3>System junk (excluded from dedup)</h3></div>
          <p style={{ color: 'var(--text-secondary)', fontSize: '0.82rem' }}>
            {junk.total.toLocaleString()} OS-noise files (macOS <code>._</code> sidecars, <code>.DS_Store</code>,{' '}
            <code>Thumbs.db</code>, temp) — not your content. These were polluting the dedup view; purge them so
            the duplicate list only shows real files. Reversible.
          </p>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <button className="btn btn-danger" onClick={runJunkPurge} disabled={junkRunning}>
              {junkRunning ? <><span className="spinner" /> Purging junk… ({num(junkJob?.quarantined || 0)})</> : `Purge ${junk.total.toLocaleString()} junk files`}
            </button>
            {junkRunning && <button className="btn btn-secondary" onClick={cancelJunk}>Cancel</button>}
            {junkManifest && !junkRunning && <button className="btn btn-secondary" onClick={undoJunk}>↩ Undo junk purge</button>}
            <span style={{ fontSize: '0.74rem', color: 'var(--text-muted)' }}>
              {junk.by_kind?.appledouble?.toLocaleString() || 0} sidecars · {junk.by_kind?.ds_store || 0} .DS_Store · {junk.by_kind?.temp || 0} temp
            </span>
          </div>
        </div>
      )}

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
          <button className="btn btn-danger" onClick={runPurge} disabled={running || !summary || !queuedFiles || guidancePending}>
            {running ? <><span className="spinner" /> Quarantining… ({num(job.quarantined)})</> : `Quarantine ${num(queuedFiles)} files`}
          </button>
          {running && (
            <button className="btn btn-secondary" onClick={cancelPurge}>Cancel</button>
          )}
          {manifest && !running && (
            <button className="btn btn-secondary" onClick={undoLast} title="Restore the last quarantine batch">
              ↩ Undo last purge
            </button>
          )}
        </div>
      </div>

      {guidancePending && (
        <div className="alert" style={{ background: 'var(--bg-tertiary)', border: '1px solid var(--warning)', fontSize: '0.82rem' }}>
          ⚠ You have unapplied guidance below. <strong>Apply the rule first</strong> — Quarantine is disabled until you do.
        </div>
      )}

      {summary && (
        <KeeperGuidanceChat
          onRuleApplied={() => load(0, filter)}
          onPendingChange={setGuidancePending}
          ambiguousCount={summary.ambiguous_groups || 0}
          resolveOpts={{ preferFolder, snapshotOnly }}
        />
      )}

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
                {g.ai_resolved && <span title="Keeper chosen by AI" style={{ flexShrink: 0, color: 'var(--accent)', fontSize: '0.7rem' }}>AI</span>}
                {g.ambiguous && !g.ai_resolved && <span title="Rule couldn't decide" style={{ flexShrink: 0, color: 'var(--warning)', fontSize: '0.7rem' }}>?</span>}
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
