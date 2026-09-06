import { useState } from 'react'
import { api } from '../api'
import { track } from '../telemetry'
import { DirectoryBrowser } from './DirectoryBrowser'

/**
 * Folder Reconciler. Point at ONE parent folder under Organized/ and reconcile
 * its subfolders against each other and (optionally) the authoritative Google
 * Drive structure (via GAM). Three outcome buckets:
 *
 *   1. MERGE — near-name sibling shells (Google-Takeout truncation split one
 *      webinar into e.g. "…Essentials" + "…Essentials_" + an empty shell).
 *      One click consolidates them into a single clean folder; when Drive
 *      validation is on, the canonical name is the AUTHORITATIVE Drive name,
 *      not a guess. Emptied shells are quarantined (reversible).
 *   2. BACKFILL — a folder is empty and has no local content twin. Detect a
 *      source (local Dropbox first) and copy the content back.
 *   3. DELETE — empty folders with nothing to merge or backfill; quarantined
 *      (reversible), never hard-deleted.
 *
 * Nothing is changed until you act on it. Merges/deletes/backfills all write a
 * manifest so they can be undone.
 */
interface Member { name: string; path: string; files: number; bytes: number; empty: boolean }
interface MergeGroup {
  anchor: string
  canonical_name: string
  canonical_from_drive: boolean
  target_exists: boolean
  members: Member[]
  total_files: number
  shell_count: number
  _editedName?: string
  _done?: boolean
}
interface BackfillItem {
  name: string; path: string; anchor: string
  drive_name: string | null
  _detect?: { dropbox: any; drive: any } | null
  _driveJob?: { status: string; phase: string; downloaded: number; total: number; error?: string } | null
  _done?: boolean
}
interface OkItem { name: string; path: string; files: number; bytes: number }

const fmtBytes = (b: number) => {
  if (!b) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; let n = b
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`
}

export function FolderReconcilerPanel() {
  const [parent, setParent] = useState('')
  const [validateDrive, setValidateDrive] = useState(true)
  const [showBrowser, setShowBrowser] = useState(false)
  const [job, setJob] = useState<any>(null)
  const [report, setReport] = useState<any>(null)
  const [merges, setMerges] = useState<MergeGroup[]>([])
  const [backfills, setBackfills] = useState<BackfillItem[]>([])
  const [oks, setOks] = useState<OkItem[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')          // path/name currently acting on
  const [lastManifest, setLastManifest] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const pollRef = useState<{ id: number | null }>({ id: null })[0]

  const startScan = async () => {
    setError(''); setNotice(''); setReport(null); setMerges([]); setBackfills([]); setOks([])
    if (!parent.trim()) { setError('Pick a folder first'); return }
    try {
      const r = await api.reconcilerScan(parent.trim(), { validate_drive: validateDrive })
      track('reconciler_scan', { parent: parent.trim(), job: r.job_id, validate_drive: validateDrive })
      if (pollRef.id) clearInterval(pollRef.id)
      pollRef.id = window.setInterval(async () => {
        try {
          const j = await api.reconcilerStatus(r.job_id)
          setJob(j)
          if (j.status === 'completed' && j.report) {
            setReport(j.report)
            setMerges(j.report.merge_groups.map((g: MergeGroup) => ({ ...g })))
            setBackfills(j.report.backfill.map((b: BackfillItem) => ({ ...b })))
            setOks(j.report.ok || [])
          }
          if (j.status === 'completed' || j.status === 'error') {
            if (pollRef.id) { clearInterval(pollRef.id); pollRef.id = null }
          }
        } catch { /* keep polling */ }
      }, 1000)
    } catch (e: any) {
      setError(e.message || 'Scan failed to start')
    }
  }

  const doMerge = async (g: MergeGroup) => {
    const canonical = (g._editedName ?? g.canonical_name).trim()
    if (!canonical) { setError('Canonical name cannot be empty'); return }
    const others = g.members.filter(m => m.name !== canonical).map(m => m.name)
    const memberNames = g.members.map(m => m.name)
    if (!window.confirm(
      `Merge ${g.members.length} folder(s) into:\n\n${canonical}\n\n` +
      `Files are moved collision-safe; emptied shells are quarantined (reversible).`)) return
    setBusy(g.anchor); setError('')
    try {
      const res = await api.reconcilerMerge(parent.trim(), canonical, memberNames)
      track('reconciler_merge', { canonical, members: memberNames.length, moved: res.stats?.moved })
      setLastManifest(res.manifest_file || null)
      setNotice(`Merged into "${canonical}": ${res.stats?.moved ?? 0} moved, ` +
        `${res.stats?.skipped_identical ?? 0} identical skipped, ${res.stats?.quarantined_shells ?? 0} shell(s) quarantined.`)
      setMerges(prev => prev.map(x => x.anchor === g.anchor ? { ...x, _done: true } : x))
      void others
    } catch (e: any) {
      setError(e.message || 'Merge failed')
    } finally {
      setBusy('')
    }
  }

  const detectBackfill = async (b: BackfillItem) => {
    setBusy(b.path); setError('')
    try {
      const res = await api.reconcilerBackfillDetect(parent.trim(), [b.name])
      const cand = res.candidates?.[0] || null
      setBackfills(prev => prev.map(x => x.path === b.path ? { ...x, _detect: cand } : x))
    } catch (e: any) {
      setError(e.message || 'Detect failed')
    } finally {
      setBusy('')
    }
  }

  const doBackfillDropbox = async (b: BackfillItem) => {
    const dp = b._detect?.dropbox?.path
    if (!dp) return
    if (!window.confirm(`Copy ${b._detect?.dropbox?.files} file(s) from local Dropbox into "${b.name}"?`)) return
    setBusy(b.path); setError('')
    try {
      const res = await api.reconcilerBackfillDropbox(parent.trim(), b.name, dp)
      track('reconciler_backfill_dropbox', { name: b.name, copied: res.stats?.copied })
      setNotice(`Backfilled "${b.name}" from Dropbox: ${res.stats?.copied ?? 0} file(s) copied.`)
      setBackfills(prev => prev.map(x => x.path === b.path ? { ...x, _done: true } : x))
    } catch (e: any) {
      setError(e.message || 'Backfill failed')
    } finally {
      setBusy('')
    }
  }

  const doBackfillDrive = async (b: BackfillItem) => {
    const count = b._detect?.drive?.file_count
    if (!count) return
    if (!window.confirm(`Download ${count} file(s) from Google Drive into "${b.name}"? Native Google docs are exported to Office formats.`)) return
    setError('')
    try {
      const r = await api.reconcilerBackfillDrive(parent.trim(), b.name)
      track('reconciler_backfill_drive', { name: b.name, job: r.job_id })
      const jobId = r.job_id
      const timer = window.setInterval(async () => {
        try {
          const j = await api.reconcilerBackfillDriveStatus(jobId)
          setBackfills(prev => prev.map(x => x.path === b.path ? { ...x, _driveJob: j } : x))
          if (j.status === 'completed' || j.status === 'error') {
            clearInterval(timer)
            if (j.status === 'completed') {
              const renamed = j.renamed_from && j.canonical_name && j.renamed_from !== j.canonical_name
              setNotice(`Backfilled from Google Drive into "${j.canonical_name || b.name}": ${j.downloaded}/${j.total} file(s) downloaded.` +
                (renamed ? ` Renamed to the authoritative Drive name (old empty shell quarantined).` : '') +
                (j.errors?.length ? ` ${j.errors.length} error(s).` : ''))
              if (j.manifest_file) setLastManifest(j.manifest_file)
              setBackfills(prev => prev.map(x => x.path === b.path ? { ...x, _done: true } : x))
            } else {
              setError(`Drive backfill failed: ${j.error}`)
            }
          }
        } catch { /* keep polling */ }
      }, 1500)
    } catch (e: any) {
      setError(e.message || 'Drive backfill failed to start')
    }
  }

  const deleteEmpty = async (b: BackfillItem) => {
    if (!window.confirm(`Move empty folder "${b.name}" to quarantine? (reversible)`)) return
    setBusy(b.path); setError('')
    try {
      const res = await api.reconcilerDelete([b.path])
      if (res.skipped?.length) {
        setError(`Refused: ${res.skipped[0].reason}`)
      } else {
        setLastManifest(res.manifest_file || null)
        setNotice(`Quarantined "${b.name}" (reversible).`)
        setBackfills(prev => prev.map(x => x.path === b.path ? { ...x, _done: true } : x))
      }
    } catch (e: any) {
      setError(e.message || 'Delete failed')
    } finally {
      setBusy('')
    }
  }

  const undoLast = async () => {
    if (!lastManifest) return
    if (!window.confirm('Undo the last merge/delete? Quarantined shell folders are restored.')) return
    try {
      const res = await api.reconcilerUndo(lastManifest)
      setNotice(`Undo: restored ${res.restored} folder(s). ${res.note || ''}`)
      setLastManifest(null)
    } catch (e: any) {
      setError(e.message || 'Undo failed')
    }
  }

  const running = job && job.status === 'running'
  const activeMerges = merges.filter(m => !m._done)
  const activeBackfills = backfills.filter(b => !b._done)

  return (
    <div>
      <div className="card">
        <div className="card-header"><h2>Folder Reconciler</h2></div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          Reconcile the subfolders of <strong>one folder</strong> under Organized. Google Takeout often
          split a single folder into near-name twins (a truncated one, an <code>_</code>-suffixed one, and
          an empty shell). This finds those, <strong>merges</strong> them into one clean folder, offers to{' '}
          <strong>backfill</strong> genuinely-missing content <strong>from Google Drive</strong> (authoritative
          names + structure via GAM), and <strong>quarantines</strong> folders that are empty everywhere.
          Folder names and structure always come from Google Drive — never guessed. Dropbox is only offered as
          a last-resort fallback when Drive has nothing. Everything is reversible.
        </p>

        {error && <div className="alert alert-error">{error}</div>}
        {notice && (
          <div className="alert alert-success" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>{notice}</span>
            {lastManifest && <button className="btn btn-secondary btn-sm" onClick={undoLast}>Undo</button>}
          </div>
        )}

        <div className="form-group">
          <label>Folder to reconcile (its subfolders are analyzed)</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <input type="text" value={parent} onChange={e => setParent(e.target.value)}
              placeholder="e.g. E:/Google Drive Files/Organized/Marketing Dropbox/HPL/HPLive Webinar-"
              style={{ flex: 1 }} />
            <button className="btn btn-secondary" onClick={() => setShowBrowser(true)}>Browse</button>
          </div>
        </div>

        <label className="checkbox-label" style={{ marginBottom: 12 }}>
          <input type="checkbox" checked={validateDrive} onChange={e => setValidateDrive(e.target.checked)} />
          Validate against Google Drive (GAM) — use authoritative Drive folder names as the clean target
        </label>

        <button className="btn btn-primary" onClick={startScan} disabled={running}>
          {running
            ? <><span className="spinner" /> {job.phase === 'drive_children' || job.phase?.startsWith('drive') ? 'Reading Google Drive…' : 'Scanning…'}</>
            : 'Reconcile Folder'}
        </button>
        {running && job.phase && (
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 8 }}>phase: {job.phase}</div>
        )}
      </div>

      {report && (
        <div className="card">
          <div className="card-header"><h3>Summary</h3></div>
          <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', fontSize: '0.85rem' }}>
            <Stat label="Subfolders" value={report.counts.subfolders} />
            <Stat label="Merge groups" value={report.counts.merge_groups} accent="var(--warning)" />
            <Stat label="Empty shells (fold in)" value={report.counts.shells_to_merge} />
            <Stat label="Backfill / empty" value={report.counts.backfill_candidates} accent="var(--danger)" />
            <Stat label="Already clean" value={report.counts.ok} accent="var(--success)" />
          </div>
          <div style={{ marginTop: 10, fontSize: '0.78rem', color: 'var(--text-muted)' }}>
            {report.drive_validated
              ? '✓ Names validated against Google Drive.'
              : 'Local-only pass (Drive validation off — canonical name = richest local twin).'}
            {report.drive_note && <span style={{ color: 'var(--warning)' }}> — {report.drive_note}</span>}
          </div>
        </div>
      )}

      {activeMerges.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Merge near-name siblings ({activeMerges.length})</h3>
          </div>
          <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginTop: -4 }}>
            Each group is one webinar split across folders. Files move into the canonical folder
            (collision-safe); emptied shells are quarantined.
          </p>
          {activeMerges.map((g) => (
            <div key={g.anchor} style={{
              border: '1px solid var(--border)', borderLeft: '4px solid var(--warning)',
              borderRadius: 6, padding: '10px 12px', marginBottom: 10, background: 'var(--bg-secondary)',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 6 }}>
                <span style={{ fontSize: '0.72rem', fontWeight: 600, color: 'var(--text-secondary)' }}>Clean name:</span>
                {g.canonical_from_drive && (
                  <span style={{ fontSize: '0.66rem', padding: '1px 6px', borderRadius: 4, background: 'var(--bg-primary)', color: 'var(--success)' }}>
                    ✓ Google Drive
                  </span>
                )}
                <input type="text" value={g._editedName ?? g.canonical_name}
                  onChange={e => setMerges(prev => prev.map(x => x.anchor === g.anchor ? { ...x, _editedName: e.target.value } : x))}
                  style={{ flex: 1, minWidth: 260, fontFamily: 'monospace', fontSize: '0.8rem' }} />
                <button className="btn btn-primary btn-sm" disabled={busy === g.anchor}
                  onClick={() => doMerge(g)}>
                  {busy === g.anchor ? <><span className="spinner" /> Merging…</> : `Merge ${g.members.length}`}
                </button>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                {g.members.map((m) => (
                  <div key={m.path} style={{
                    display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.76rem',
                    fontFamily: 'monospace', color: m.files === 0 ? 'var(--text-muted)' : 'var(--text-secondary)',
                  }}>
                    <span style={{ width: 56, textAlign: 'right', color: m.files === 0 ? 'var(--danger)' : 'var(--text-primary)' }}>
                      {m.files === 0 ? 'empty' : `${m.files}`}
                    </span>
                    <span style={{ width: 62, textAlign: 'right', color: 'var(--text-muted)' }}>{m.files ? fmtBytes(m.bytes) : ''}</span>
                    <span style={{ flex: 1 }}>{m.name}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {activeBackfills.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Empty — backfill or remove ({activeBackfills.length})</h3>
          </div>
          <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginTop: -4 }}>
            These folders are empty and have no local content twin. Backfill from <strong>Google Drive</strong>
            {' '}(authoritative name + structure), or quarantine the empty folder (reversible). Dropbox is only a
            fallback if Drive has nothing.
          </p>
          {activeBackfills.map((b) => (
            <div key={b.path} style={{
              border: '1px solid var(--border)', borderLeft: '4px solid var(--danger)',
              borderRadius: 6, padding: '10px 12px', marginBottom: 8, background: 'var(--bg-secondary)',
            }}>
              <div style={{ fontSize: '0.8rem', fontFamily: 'monospace' }}>{b.name}</div>
              {b.drive_name && b.drive_name !== b.name && (
                <div style={{ fontSize: '0.72rem', color: 'var(--success)', marginTop: 2 }}>
                  Drive name: {b.drive_name}
                </div>
              )}
              {b._detect && (
                <div style={{ fontSize: '0.74rem', marginTop: 6 }}>
                  {/* Google Drive is the primary/authoritative source. */}
                  {b._detect.drive
                    ? <div style={{ color: 'var(--success)' }}>
                        ✓ Google Drive source: {b._detect.drive.file_count} file(s) available
                        {b._detect.drive.rename_to && (
                          <div style={{ color: 'var(--text-secondary)', fontSize: '0.72rem' }}>
                            → will save as authoritative Drive name: <span style={{ fontFamily: 'monospace' }}>{b._detect.drive.rename_to}</span>
                          </div>
                        )}
                      </div>
                    : <div style={{ color: 'var(--text-muted)' }}>No matching Google Drive folder found.</div>}
                  {/* Dropbox only shown as a fallback when Drive has nothing. */}
                  {!b._detect.drive && (
                    b._detect.dropbox
                      ? <div style={{ color: 'var(--warning)' }}>Fallback — local Dropbox source: {b._detect.dropbox.files} file(s)</div>
                      : <div style={{ color: 'var(--text-muted)' }}>No local Dropbox fallback found either.</div>
                  )}
                </div>
              )}
              {b._driveJob && b._driveJob.status === 'running' && (
                <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', marginTop: 6 }}>
                  <span className="spinner" style={{ width: 10, height: 10, verticalAlign: 'middle', marginRight: 6 }} />
                  Downloading from Drive: {b._driveJob.downloaded}/{b._driveJob.total || '…'} ({b._driveJob.phase})
                </div>
              )}
              <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
                <button className="btn btn-secondary btn-sm" disabled={busy === b.path} onClick={() => detectBackfill(b)}>
                  {busy === b.path ? <span className="spinner" /> : 'Detect source'}
                </button>
                {/* PRIMARY: Google Drive — authoritative name + structure. */}
                {b._detect?.drive?.file_count > 0 && (
                  <button className="btn btn-primary btn-sm"
                    disabled={b._driveJob?.status === 'running'} onClick={() => doBackfillDrive(b)}>
                    {b._driveJob?.status === 'running' ? 'Downloading…' : `Backfill from Google Drive (${b._detect?.drive?.file_count})`}
                  </button>
                )}
                {/* FALLBACK: Dropbox — only when Drive has nothing. */}
                {b._detect && !b._detect.drive && b._detect.dropbox && (
                  <button className="btn btn-secondary btn-sm" disabled={busy === b.path} onClick={() => doBackfillDropbox(b)}
                    title="Drive has no match — using local Dropbox as a fallback">
                    Fallback: backfill from Dropbox
                  </button>
                )}
                <button className="btn btn-danger btn-sm" disabled={busy === b.path} onClick={() => deleteEmpty(b)}>
                  Quarantine (delete)
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {report && activeMerges.length === 0 && activeBackfills.length === 0 && (
        <div className="empty-state">
          <h3>Nothing to reconcile</h3>
          <p>No near-name sibling groups or empty folders needing action. {oks.length} folder(s) are already clean.</p>
        </div>
      )}

      {showBrowser && (
        <DirectoryBrowser onSelect={(p) => { setParent(p); setShowBrowser(false) }} onClose={() => setShowBrowser(false)} />
      )}
    </div>
  )
}

function Stat({ label, value, accent }: { label: string; value: number; accent?: string }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      <span style={{ fontSize: '1.4rem', fontWeight: 700, color: accent || 'var(--text-primary)' }}>{value}</span>
      <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>{label}</span>
    </div>
  )
}
