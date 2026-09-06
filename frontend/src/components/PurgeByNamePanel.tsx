import { useState, useEffect } from 'react'
import { api } from '../api'
import { track } from '../telemetry'
import { DirectoryBrowser } from './DirectoryBrowser'

/**
 * Find & Purge by name. Search file AND folder names by keyword(s) under a
 * chosen root, review every hit, then quarantine the selected ones (reversible,
 * manifest — never a hard delete). Built for decommission cleanup: purge an old
 * client's material (e.g. "Brandon Dawson" / "BDawson") before migrating the
 * rest to S3, whether or not the files are duplicated.
 *
 * SAFETY: run this on a local/sandbox copy (e.g. the Dropbox snapshot), not a
 * live-syncing folder, or the quarantine moves will sync to the cloud.
 */
interface FolderHit { path: string; keyword: string; files: number; bytes: number; _sel?: boolean }
interface FileHit { path: string; keyword: string; bytes: number; _sel?: boolean }

const fmtBytes = (b: number) => {
  if (!b) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; let n = b
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`
}

export function PurgeByNamePanel() {
  const [root, setRoot] = useState('')
  const [keywordText, setKeywordText] = useState('')
  const [wholeWord, setWholeWord] = useState(false)
  const [showBrowser, setShowBrowser] = useState(false)
  const [job, setJob] = useState<any>(null)
  const [folders, setFolders] = useState<FolderHit[]>([])
  const [files, setFiles] = useState<FileHit[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [lastManifest, setLastManifest] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const pollRef = useState<{ id: number | null }>({ id: null })[0]

  // Unified catalog state — registered roots + refresh progress.
  const [catRoots, setCatRoots] = useState<any[]>([])
  const [catJob, setCatJob] = useState<any>(null)
  const catPoll = useState<{ id: number | null }>({ id: null })[0]

  const loadCatRoots = async () => {
    try { const r = await api.catalogRoots(); setCatRoots(r.roots || []) } catch { /* ignore */ }
  }
  useEffect(() => { loadCatRoots() }, [])

  const registerCurrentRoot = async () => {
    if (!root.trim()) { setError('Pick a folder first'); return }
    try { await api.catalogRegister(root.trim()); await loadCatRoots(); setNotice(`Registered ${root.trim()} in the catalog. Click Refresh to crawl it.`) }
    catch (e: any) { setError(e.message || 'Register failed') }
  }

  const refreshCatalog = async (which?: string) => {
    try {
      const r = await api.catalogRefresh(which)
      if (catPoll.id) clearInterval(catPoll.id)
      catPoll.id = window.setInterval(async () => {
        try {
          const j = await api.catalogRefreshStatus(r.job_id)
          setCatJob(j)
          if (j.status === 'completed' || j.status === 'error' || j.status === 'cancelled') {
            if (catPoll.id) { clearInterval(catPoll.id); catPoll.id = null }
            await loadCatRoots()
          }
        } catch { /* keep polling */ }
      }, 1000)
    } catch (e: any) { setError(e.message || 'Refresh failed') }
  }

  const ago = (ts?: number) => {
    if (!ts) return 'never'
    const s = Math.floor(Date.now() / 1000 - ts)
    if (s < 90) return `${s}s ago`
    if (s < 5400) return `${Math.floor(s / 60)}m ago`
    if (s < 129600) return `${Math.floor(s / 3600)}h ago`
    return `${Math.floor(s / 86400)}d ago`
  }

  const parseKeywords = () =>
    keywordText.split(/[\n,]/).map(s => s.trim()).filter(Boolean)

  const startSearch = async () => {
    setError(''); setNotice(''); setFolders([]); setFiles([])
    if (!root.trim()) { setError('Pick a folder to search'); return }
    const kws = parseKeywords()
    if (kws.length === 0) { setError('Enter at least one keyword'); return }
    try {
      const r = await api.purgeSearch(root.trim(), kws, wholeWord)
      track('purge_search', { root: root.trim(), keywords: kws.length, whole_word: wholeWord })
      if (pollRef.id) clearInterval(pollRef.id)
      pollRef.id = window.setInterval(async () => {
        try {
          const j = await api.purgeStatus(r.job_id)
          setJob(j)
          if (j.status === 'completed' && j.result) {
            setFolders(j.result.matched_folders.map((x: FolderHit) => ({ ...x, _sel: true })))
            setFiles(j.result.matched_files.map((x: FileHit) => ({ ...x, _sel: true })))
          }
          if (j.status === 'completed' || j.status === 'error') {
            if (pollRef.id) { clearInterval(pollRef.id); pollRef.id = null }
          }
        } catch { /* keep polling */ }
      }, 1000)
    } catch (e: any) {
      setError(e.message || 'Search failed to start')
    }
  }

  const selectedPaths = [
    ...folders.filter(f => f._sel).map(f => f.path),
    ...files.filter(f => f._sel).map(f => f.path),
  ]
  const selectedBytes =
    folders.filter(f => f._sel).reduce((s, f) => s + f.bytes, 0) +
    files.filter(f => f._sel).reduce((s, f) => s + f.bytes, 0)

  const toggleFolder = (path: string, v: boolean) =>
    setFolders(prev => prev.map(f => f.path === path ? { ...f, _sel: v } : f))
  const toggleFile = (path: string, v: boolean) =>
    setFiles(prev => prev.map(f => f.path === path ? { ...f, _sel: v } : f))
  const selectAll = (v: boolean) => {
    setFolders(prev => prev.map(f => ({ ...f, _sel: v })))
    setFiles(prev => prev.map(f => ({ ...f, _sel: v })))
  }

  const doQuarantine = async () => {
    if (selectedPaths.length === 0) { setError('Nothing selected'); return }
    if (!window.confirm(
      `Quarantine ${selectedPaths.length} item(s) totaling ${fmtBytes(selectedBytes)}?\n\n` +
      `They are MOVED to a _PurgeQuarantine folder (reversible), not deleted.`)) return
    setBusy(true); setError('')
    try {
      const res = await api.purgeQuarantine(root.trim(), selectedPaths)
      track('purge_quarantine', { count: res.quarantined, bytes: res.quarantined_bytes })
      setLastManifest(res.manifest_file || null)
      setNotice(`Quarantined ${res.quarantined} item(s), ${fmtBytes(res.quarantined_bytes)} reclaimed.` +
        (res.errors?.length ? ` ${res.errors.length} error(s).` : ''))
      // drop quarantined rows from the lists
      const done = new Set(selectedPaths)
      setFolders(prev => prev.filter(f => !done.has(f.path)))
      setFiles(prev => prev.filter(f => !done.has(f.path)))
    } catch (e: any) {
      setError(e.message || 'Quarantine failed')
    } finally {
      setBusy(false)
    }
  }

  const undoLast = async () => {
    if (!lastManifest) return
    if (!window.confirm('Restore the last purge from quarantine?')) return
    try {
      const res = await api.purgeUndo(lastManifest)
      setNotice(`Undo: restored ${res.restored} item(s).`)
      setLastManifest(null)
    } catch (e: any) {
      setError(e.message || 'Undo failed')
    }
  }

  const running = job && job.status === 'running'
  const hasResults = folders.length > 0 || files.length > 0

  return (
    <div>
      {/* Catalog bar: crawl once, search from the DB (instant). Incremental refresh. */}
      <div className="card" style={{ paddingBottom: 10 }}>
        <div className="card-header" style={{ marginBottom: 6 }}>
          <h3 style={{ fontSize: '0.95rem' }}>Filesystem Catalog</h3>
          <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
            crawl once → searches are instant; refresh is incremental (no full re-scan)
          </span>
        </div>
        {catRoots.length === 0 && (
          <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginBottom: 6 }}>
            No roots cataloged yet. Set a folder below, then <strong>Add to catalog</strong> and{' '}
            <strong>Refresh</strong>. Until then, searches fall back to a live disk walk (slow).
          </div>
        )}
        {catRoots.map((r: any) => (
          <div key={r.root} style={{
            display: 'flex', alignItems: 'center', gap: 10, fontSize: '0.76rem',
            padding: '3px 0', borderBottom: '1px solid var(--border)',
          }}>
            <span style={{ flex: 1, fontFamily: 'monospace' }}>{r.root}</span>
            <span style={{ color: 'var(--text-muted)' }}>
              {r.file_count?.toLocaleString() || 0} files
            </span>
            <span style={{ color: r.last_scan_at ? 'var(--success)' : 'var(--warning)' }}>
              {r.last_scan_at ? `updated ${ago(r.last_scan_at)}` : 'never crawled'}
            </span>
            <button className="btn btn-secondary btn-sm" onClick={() => refreshCatalog(r.root)}>Refresh</button>
          </div>
        ))}
        <div style={{ display: 'flex', gap: 8, marginTop: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <button className="btn btn-secondary btn-sm" onClick={registerCurrentRoot}>Add current folder to catalog</button>
          {catRoots.length > 0 && (
            <button className="btn btn-secondary btn-sm" onClick={() => refreshCatalog()}>Refresh all</button>
          )}
          {catJob && catJob.status === 'running' && (
            <span style={{ fontSize: '0.74rem', color: 'var(--text-secondary)' }}>
              <span className="spinner" style={{ width: 10, height: 10, verticalAlign: 'middle', marginRight: 6 }} />
              crawling: {catJob.files_seen?.toLocaleString()} files, {catJob.dirs_seen?.toLocaleString()} dirs
              {catJob.pruned ? ` (${catJob.pruned} removed)` : ''}
            </span>
          )}
          {catJob && catJob.status === 'completed' && (
            <span style={{ fontSize: '0.74rem', color: 'var(--success)' }}>
              ✓ catalog updated ({catJob.files_seen?.toLocaleString()} files, {catJob.pruned || 0} pruned)
            </span>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header"><h2>Find &amp; Purge by Name</h2></div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          Search file and folder names for keyword(s) — e.g. an old client like{' '}
          <code>Brandon Dawson</code> or <code>BDawson</code> — and quarantine everything that matches,
          whether or not it's duplicated. Great for decommission cleanup before an S3 migration. Matches
          are <strong>moved to a quarantine folder (reversible)</strong>, never hard-deleted.
        </p>
        <div className="alert" style={{ background: 'var(--bg-tertiary)', border: '1px solid var(--warning)', fontSize: '0.78rem' }}>
          ⚠ Run this on a <strong>local/sandbox copy</strong> (e.g. the Dropbox snapshot), not a live-syncing
          Dropbox folder — quarantine moves in a synced folder would propagate to the cloud.
        </div>

        {error && <div className="alert alert-error">{error}</div>}
        {notice && (
          <div className="alert alert-success" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>{notice}</span>
            {lastManifest && <button className="btn btn-secondary btn-sm" onClick={undoLast}>Undo</button>}
          </div>
        )}

        <div className="form-group">
          <label>Folder to search</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <input type="text" value={root} onChange={e => setRoot(e.target.value)}
              placeholder="e.g. E:/Dropbox-Snapshot" style={{ flex: 1 }} />
            <button className="btn btn-secondary" onClick={() => setShowBrowser(true)}>Browse</button>
          </div>
        </div>

        <div className="form-group">
          <label>Keywords (one per line or comma-separated — matches file AND folder names)</label>
          <textarea value={keywordText} onChange={e => setKeywordText(e.target.value)}
            placeholder={"Brandon Dawson\nBDawson\nRachelle Mesquit"}
            rows={3} style={{ width: '100%', fontFamily: 'monospace', fontSize: '0.85rem' }} />
        </div>

        <label className="checkbox-label" style={{ marginBottom: 12 }}>
          <input type="checkbox" checked={wholeWord} onChange={e => setWholeWord(e.target.checked)} />
          Whole-word match only (avoid over-matching partial words)
        </label>

        <button className="btn btn-primary" onClick={startSearch} disabled={running}>
          {running ? <><span className="spinner" /> Searching… ({job.folders_scanned} folders, {job.matched_folders + job.matched_files} hits)</> : 'Search'}
        </button>
        {running && job.current && (
          <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: 8 }}>
            scanning: {job.current}
          </div>
        )}
      </div>

      {hasResults && (
        <div className="card">
          <div className="card-header">
            <h3>Matches</h3>
          </div>
          <div style={{ display: 'flex', gap: 20, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
            <div>
              <span style={{ fontSize: '1.4rem', fontWeight: 700, color: 'var(--danger)' }}>{selectedPaths.length}</span>
              <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginLeft: 6 }}>selected</span>
            </div>
            <div>
              <span style={{ fontSize: '1.4rem', fontWeight: 700 }}>{fmtBytes(selectedBytes)}</span>
              <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginLeft: 6 }}>to reclaim</span>
            </div>
            <button className="btn btn-secondary btn-sm" onClick={() => selectAll(true)}>Select all</button>
            <button className="btn btn-secondary btn-sm" onClick={() => selectAll(false)}>Clear all</button>
            <button className="btn btn-danger btn-sm" onClick={doQuarantine} disabled={busy || selectedPaths.length === 0}>
              {busy ? <><span className="spinner" /> Quarantining…</> : `Quarantine selected (${selectedPaths.length})`}
            </button>
          </div>

          {folders.length > 0 && (
            <>
              <h4 style={{ fontSize: '0.85rem', margin: '8px 0', color: 'var(--text-secondary)' }}>
                Matched folders ({folders.length}) — whole subtree
              </h4>
              {folders.map(f => (
                <label key={f.path} style={{
                  display: 'flex', alignItems: 'center', gap: 10, padding: '6px 10px',
                  borderBottom: '1px solid var(--border)', fontSize: '0.8rem', cursor: 'pointer',
                }}>
                  <input type="checkbox" checked={!!f._sel} onChange={e => toggleFolder(f.path, e.target.checked)} />
                  <span style={{ width: 70, textAlign: 'right', color: 'var(--text-primary)' }}>{f.files} files</span>
                  <span style={{ width: 70, textAlign: 'right', color: 'var(--warning)' }}>{fmtBytes(f.bytes)}</span>
                  <span style={{ flex: 1, fontFamily: 'monospace', wordBreak: 'break-all' }}>📁 {f.path}</span>
                </label>
              ))}
            </>
          )}

          {files.length > 0 && (
            <>
              <h4 style={{ fontSize: '0.85rem', margin: '12px 0 8px', color: 'var(--text-secondary)' }}>
                Matched loose files ({files.length})
              </h4>
              {files.map(f => (
                <label key={f.path} style={{
                  display: 'flex', alignItems: 'center', gap: 10, padding: '6px 10px',
                  borderBottom: '1px solid var(--border)', fontSize: '0.8rem', cursor: 'pointer',
                }}>
                  <input type="checkbox" checked={!!f._sel} onChange={e => toggleFile(f.path, e.target.checked)} />
                  <span style={{ width: 70, textAlign: 'right', color: 'var(--warning)' }}>{fmtBytes(f.bytes)}</span>
                  <span style={{ flex: 1, fontFamily: 'monospace', wordBreak: 'break-all' }}>{f.path}</span>
                </label>
              ))}
            </>
          )}
        </div>
      )}

      {job && job.status === 'completed' && !hasResults && (
        <div className="empty-state">
          <h3>No matches</h3>
          <p>No files or folders matched those keywords under {root}.</p>
        </div>
      )}

      {showBrowser && (
        <DirectoryBrowser onSelect={(p) => { setRoot(p); setShowBrowser(false) }} onClose={() => setShowBrowser(false)} />
      )}
    </div>
  )
}
