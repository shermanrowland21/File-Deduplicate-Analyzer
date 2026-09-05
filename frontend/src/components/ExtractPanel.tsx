import { useState, useRef, useCallback } from 'react'
import { DirectoryBrowser } from './DirectoryBrowser'

interface ArchiveInfo {
  path: string
  filename: string
  size: number
  size_human: string
  extension: string
}

interface ExtractionJob {
  status: string
  source_dir: string
  output_dir: string
  phase: string
  total_archives: number
  processed_archives: number
  current_archive: string
  files_extracted: number
  errors: string[]
  progress: number
  elapsed_seconds: number
}

export function ExtractPanel() {
  const [sourceDir, setSourceDir] = useState('')
  const [outputDir, setOutputDir] = useState('')
  const [showBrowser, setShowBrowser] = useState(false)
  const [browserTarget, setBrowserTarget] = useState<'source' | 'output'>('source')
  const [archives, setArchives] = useState<ArchiveInfo[]>([])
  const [totalSize, setTotalSize] = useState('')
  const [scanned, setScanned] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [extracting, setExtracting] = useState(false)
  const [job, setJob] = useState<ExtractionJob | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [findJobId, setFindJobId] = useState<string | null>(null)
  const [sxRunning, setSxRunning] = useState(false)
  const [sxJob, setSxJob] = useState<any>(null)
  const [sxJobId, setSxJobId] = useState<string | null>(null)
  const [maxWorkers, setMaxWorkers] = useState(2)
  const [moveProcessed, setMoveProcessed] = useState(true)
  const [movingProcessed, setMovingProcessed] = useState(false)
  const [reconciling, setReconciling] = useState(false)
  const [reconcileResult, setReconcileResult] = useState<any>(null)
  const [onlyDrive, setOnlyDrive] = useState(true)
  const [cleaning, setCleaning] = useState(false)
  const [cleanupJob, setCleanupJob] = useState<any>(null)
  const [flattening, setFlattening] = useState(false)
  const [flattenJob, setFlattenJob] = useState<any>(null)
  const pollRef = useRef<number | null>(null)
  const sxPollRef = useRef<number | null>(null)

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  const findArchives = async () => {
    if (!sourceDir.trim()) {
      setError('Select a directory containing your archive files')
      return
    }
    setError('')
    setArchives([])
    setScanned(false)
    setScanning(true)

    try {
      const res = await fetch(`/api/archives/find?directory=${encodeURIComponent(sourceDir.trim())}`)
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Failed to start archive scan')
      }
      const data = await res.json()
      const findJob = data.job_id
      setFindJobId(findJob)

      // Poll for results
      const pollFind = setInterval(async () => {
        try {
          const statusRes = await fetch(`/api/archives/find-status/${findJob}`)
          if (!statusRes.ok) { clearInterval(pollFind); setScanning(false); return }
          const status = await statusRes.json()

          // Update UI with progress
          setArchives(status.archives || [])
          setTotalSize(status.total_size_human || '0 B')

          if (status.status === 'completed') {
            clearInterval(pollFind)
            setScanning(false)
            setScanned(true)
            if ((status.archives || []).length === 0) {
              setError('No archive files (zip, tar.gz, tar) found in this directory or its subdirectories.')
            }
          } else if (status.status === 'error') {
            clearInterval(pollFind)
            setScanning(false)
            setError(status.error || 'Archive scan failed')
          }
        } catch {
          clearInterval(pollFind)
          setScanning(false)
        }
      }, 500)

    } catch (err: any) {
      setError(err.message)
      setScanning(false)
    }
  }

  const runReconcile = async () => {
    if (!sourceDir.trim()) { setError('Set source directory first'); return }
    setError('')
    setReconciling(true)
    setReconcileResult(null)
    try {
      const res = await fetch(`/api/archives/reconcile`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_dir: sourceDir.trim() }),
      })
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Reconcile failed')
      }
      const data = await res.json()
      const jobId = data.job_id

      const poll = setInterval(async () => {
        try {
          const s = await fetch(`/api/archives/reconcile-status/${jobId}`)
          if (!s.ok) { clearInterval(poll); setReconciling(false); return }
          const status = await s.json()
          setReconcileResult(status)
          if (status.status === 'completed' || status.status === 'error' || status.status === 'cancelled') {
            clearInterval(poll)
            setReconciling(false)
          }
        } catch { clearInterval(poll); setReconciling(false) }
      }, 500)
    } catch (err: any) {
      setError(err.message)
      setReconciling(false)
    }
  }

  const runCleanup = async () => {
    // Clean the extracted output. Default to source's extracted folder, or ask.
    const target = outputDir.trim() || (sourceDir.trim() ? sourceDir.trim().replace(/[\\/]$/, '') + '/extracted' : '')
    const cleanTarget = prompt('Folder to clean up (removes -info.json, archive_browser.html, _MACOSX, empty folders):', target || 'E:/Google Drive Files/extracted')
    if (!cleanTarget) return
    setError('')
    setCleaning(true)
    setCleanupJob(null)
    try {
      const res = await fetch(`/api/archives/cleanup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_dir: cleanTarget }),
      })
      if (!res.ok) { const e = await res.json(); throw new Error(e.detail || 'Cleanup failed') }
      const data = await res.json()
      const jobId = data.job_id
      const poll = setInterval(async () => {
        try {
          const s = await fetch(`/api/archives/cleanup-status/${jobId}`)
          if (!s.ok) { clearInterval(poll); setCleaning(false); return }
          const st = await s.json()
          setCleanupJob(st)
          if (st.status === 'completed' || st.status === 'error' || st.status === 'cancelled') {
            clearInterval(poll); setCleaning(false)
          }
        } catch { clearInterval(poll); setCleaning(false) }
      }, 1000)
    } catch (err: any) {
      setError(err.message); setCleaning(false)
    }
  }

  const runFlatten = async () => {
    const src = prompt('Extracted folder to reorganize (personal accounts + shared drives become clean top-level folders):', 'E:/Google Drive Files/extracted')
    if (!src) return
    const dest = prompt('Destination for organized output:', 'E:/Google Drive Files/Organized')
    if (!dest) return
    setError('')
    setFlattening(true)
    setFlattenJob(null)
    try {
      const res = await fetch(`/api/archives/flatten`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ extracted_root: src, dest_root: dest }),
      })
      if (!res.ok) { const e = await res.json(); throw new Error(e.detail || 'Flatten failed') }
      const data = await res.json()
      const jobId = data.job_id
      const poll = setInterval(async () => {
        try {
          const s = await fetch(`/api/archives/flatten-status/${jobId}`)
          if (!s.ok) { clearInterval(poll); setFlattening(false); return }
          const st = await s.json()
          setFlattenJob(st)
          if (st.status === 'completed' || st.status === 'error' || st.status === 'cancelled') {
            clearInterval(poll); setFlattening(false)
          }
        } catch { clearInterval(poll); setFlattening(false) }
      }, 1000)
    } catch (err: any) {
      setError(err.message); setFlattening(false)
    }
  }

  const startScanExtract = async () => {
    if (!sourceDir.trim()) {
      setError('Select a directory containing your archive files')
      return
    }
    setError('')
    setSxRunning(true)
    setSxJob(null)

    try {
      // ALWAYS drive-only + delete-zip-after-extract (frees space) + auto-skip.
      // Hardcoded so they can never be accidentally left off.
      const body: any = { source_dir: sourceDir.trim(), max_workers: maxWorkers, delete_after: true, move_processed: false, only_drive: true }
      if (outputDir.trim()) body.output_dir = outputDir.trim()

      const res = await fetch(`/api/archives/scan-extract`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Failed to start')
      }
      const data = await res.json()
      setSxJobId(data.job_id)

      sxPollRef.current = window.setInterval(async () => {
        try {
          const s = await fetch(`/api/archives/scan-extract-status/${data.job_id}`)
          if (!s.ok) return
          const jobStatus = await s.json()
          setSxJob(jobStatus)
          if (jobStatus.status === 'completed' || jobStatus.status === 'error' || jobStatus.status === 'cancelled') {
            if (sxPollRef.current) clearInterval(sxPollRef.current)
            setSxRunning(false)
          }
        } catch {}
      }, 700)
    } catch (err: any) {
      setError(err.message)
      setSxRunning(false)
    }
  }

  const stopScanExtract = async () => {
    if (sxJobId) {
      await fetch(`/api/archives/scan-extract-stop/${sxJobId}`, { method: 'POST' })
    }
    if (sxPollRef.current) clearInterval(sxPollRef.current)
    setSxRunning(false)
  }

  const startExtraction = async () => {
    setError('')
    setExtracting(true)
    setJob(null)
    stopPolling()

    try {
      const body: any = { source_dir: sourceDir.trim() }
      if (outputDir.trim()) {
        body.output_dir = outputDir.trim()
      }

      const res = await fetch('/api/archives/extract', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Failed to start extraction')
      }
      const data = await res.json()
      setJobId(data.job_id)
      pollJob(data.job_id)
    } catch (err: any) {
      setError(err.message)
      setExtracting(false)
    }
  }

  const pollJob = (id: string) => {
    const poll = async () => {
      try {
        const res = await fetch(`/api/archives/status/${id}`)
        if (!res.ok) return
        const data: ExtractionJob = await res.json()
        setJob(data)
        if (data.status === 'completed' || data.status === 'error' || data.status === 'cancelled') {
          stopPolling()
          setExtracting(false)
        }
      } catch {}
    }
    pollRef.current = window.setInterval(poll, 500)
    poll()
  }

  const handleStop = async () => {
    if (jobId) {
      await fetch(`/api/archives/stop/${jobId}`, { method: 'POST' })
      stopPolling()
      setExtracting(false)
    }
  }

  const formatElapsed = (seconds: number): string => {
    if (seconds < 60) return `${Math.round(seconds)}s`
    const mins = Math.floor(seconds / 60)
    const secs = Math.round(seconds % 60)
    return `${mins}m ${secs}s`
  }

  return (
    <div>
      <div className="card">
        <div className="card-header">
          <h2>Extract Archives</h2>
        </div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 16 }}>
          Point to a folder containing Google Takeout zips (or any archive files) and extract them all.
          Supports zip, tar.gz, and tar files. Handles multi-part Takeout exports automatically.
        </p>

        {error && <div className="alert alert-error">{error}</div>}

        <div className="form-group">
          <label>Source Directory (where your archive files are)</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="text"
              value={sourceDir}
              onChange={(e) => { setSourceDir(e.target.value); setScanned(false) }}
              placeholder="E:/Google-Takeout-Downloads"
              style={{ flex: 1 }}
            />
            <button
              className="btn btn-secondary"
              onClick={() => { setBrowserTarget('source'); setShowBrowser(true) }}
            >
              Browse
            </button>
          </div>
        </div>

        <div className="form-group">
          <label>Output Directory (where to extract, optional)</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="text"
              value={outputDir}
              onChange={(e) => setOutputDir(e.target.value)}
              placeholder="Default: source_dir/extracted"
              style={{ flex: 1 }}
            />
            <button
              className="btn btn-secondary"
              onClick={() => { setBrowserTarget('output'); setShowBrowser(true) }}
            >
              Browse
            </button>
          </div>
        </div>

        <div className="form-group">
          <label>Parallel Workers (HDD: use 1-2, SSD/NVMe: use 6-8)</label>
          <select
            value={maxWorkers}
            onChange={(e) => setMaxWorkers(parseInt(e.target.value))}
            disabled={sxRunning}
            style={{ maxWidth: 300 }}
          >
            <option value={1}>1 - Best for HDD (sequential, no seek thrash)</option>
            <option value={2}>2 - Good for HDD</option>
            <option value={3}>3 - Balanced</option>
            <option value={4}>4 - SSD</option>
            <option value={6}>6 - Fast SSD</option>
            <option value={8}>8 - NVMe</option>
          </select>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 4 }}>
            You have an HDD — 1 or 2 workers will be fastest. More workers cause the drive head to thrash and slow down.
          </div>
        </div>

        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', padding: '10px 12px', background: 'rgba(239,68,68,0.08)', border: '1px solid var(--danger)', borderRadius: 6 }}>
            Extracts <strong>Google Drive files only</strong>. Each zip is <strong style={{ color: 'var(--danger)' }}>DELETED after its Drive files are extracted</strong> to free disk space.
            Email/Mail data inside those zips will be gone (re-pull via Takeout later if needed). Already-extracted archives are skipped automatically.
          </div>
        </div>

        <div className="btn-group">
          <button className="btn btn-primary" onClick={startScanExtract} disabled={sxRunning}>
            {sxRunning ? <><span className="spinner" /> Extracting Drive Files...</> : 'Scan & Extract Drive Files'}
          </button>
          {sxRunning && (
            <button className="btn btn-danger" onClick={stopScanExtract}>
              Stop
            </button>
          )}
          <button
            className="btn btn-secondary"
            onClick={runCleanup}
            disabled={sxRunning || cleaning || flattening}
            title="Removes -info.json metadata, archive_browser.html, _MACOSX folders, and empty folders"
          >
            {cleaning ? <><span className="spinner" /> Cleaning clutter...</> : 'Clean Up Clutter'}
          </button>
          <button
            className="btn btn-secondary"
            onClick={runFlatten}
            disabled={sxRunning || flattening || cleaning}
            title="Reorganizes into clean structure: personal accounts and each shared drive as top-level folders"
          >
            {flattening ? <><span className="spinner" /> Organizing...</> : 'Flatten & Organize'}
          </button>
        </div>

        {cleanupJob && (
          <div style={{ marginTop: 12, padding: 12, background: 'var(--bg-tertiary)', borderRadius: 6, fontSize: '0.85rem' }}>
            <strong>{cleanupJob.status === 'completed' ? 'Cleanup complete' : 'Cleaning...'}</strong>
            <div style={{ marginTop: 4, color: 'var(--text-secondary)' }}>
              Removed: {cleanupJob.json_deleted?.toLocaleString()} metadata files, {cleanupJob.html_deleted} browser pages,
              {' '}{cleanupJob.macosx_deleted} _MACOSX folders, {cleanupJob.empty_dirs_removed?.toLocaleString()} empty folders
              {cleanupJob.bytes_freed_human && <span> — freed {cleanupJob.bytes_freed_human}</span>}
            </div>
          </div>
        )}

        {flattenJob && (
          <div style={{ marginTop: 12, padding: 12, background: 'var(--bg-tertiary)', borderRadius: 6, fontSize: '0.85rem' }}>
            <strong>{flattenJob.status === 'completed' ? 'Organized' : 'Organizing...'}</strong>
            <div style={{ marginTop: 4, color: 'var(--text-secondary)' }}>
              {flattenJob.personal_accounts} personal accounts, {flattenJob.shared_drives} shared drives reorganized.
              {' '}{flattenJob.items_moved?.toLocaleString()} items moved to <code>{flattenJob.dest_root}</code>
            </div>
          </div>
        )}

        {/* Scan+Extract Progress */}
        {sxJob && (
          <div style={{ marginTop: 16, padding: 16, background: 'var(--bg-tertiary)', borderRadius: 8 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
              <strong>
                {sxJob.status === 'completed' ? 'Complete' :
                 sxJob.status === 'cancelled' ? 'Stopped' :
                 sxJob.phase === 'scanning' ? 'Scanning & Extracting...' : 'Finishing extraction...'}
              </strong>
              <span style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                {formatElapsed(sxJob.elapsed_seconds || 0)}
              </span>
            </div>
            <div className="stats-grid">
              <div className="stat-card">
                <div className="value">{sxJob.archives_found}</div>
                <div className="label">Archives Found</div>
              </div>
              <div className="stat-card">
                <div className="value" style={{ color: 'var(--success)' }}>{sxJob.archives_extracted}</div>
                <div className="label">Extracted</div>
              </div>
              <div className="stat-card">
                <div className="value">{sxJob.files_extracted.toLocaleString()}</div>
                <div className="label">Files Out</div>
              </div>
              <div className="stat-card">
                <div className="value" style={{ fontSize: '1rem' }}>{sxJob.total_size_human}</div>
                <div className="label">Archive Size</div>
              </div>
            </div>
            {(sxJob.archives_moved > 0 || sxJob.archives_deleted > 0 || sxJob.archives_skipped > 0) && (
              <div style={{ marginTop: 8, fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                {sxJob.archives_deleted > 0 && <span style={{ color: 'var(--danger)' }}>{sxJob.archives_deleted} zips deleted (space freed). </span>}
                {sxJob.archives_moved > 0 && <span>{sxJob.archives_moved} zips moved. </span>}
                {sxJob.archives_skipped > 0 && <span>{sxJob.archives_skipped} skipped (already done).</span>}
              </div>
            )}
            {sxJob.auto_throttled && (
              <div style={{ marginTop: 6, fontSize: '0.78rem', color: 'var(--warning)' }}>
                Auto-throttled: {sxJob.throttle_reason || 'reduced to 1 worker due to disk thrashing'}
              </div>
            )}
            {sxJob.monitor_mbps != null && sxJob.status === 'running' && (
              <div style={{ marginTop: 4, fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                Monitored throughput: {sxJob.monitor_mbps} MB/s
              </div>
            )}
            {sxJob.currently_extracting && sxJob.currently_extracting.length > 0 && (
              <div style={{ marginTop: 8, fontSize: '0.75rem', color: 'var(--text-muted)', fontFamily: 'monospace' }}>
                Archive: {sxJob.currently_extracting.join(', ')}
              </div>
            )}
            {/* Live throughput + current file */}
            {sxRunning && (
              <div style={{ marginTop: 8, display: 'flex', gap: 20, flexWrap: 'wrap', fontSize: '0.8rem' }}>
                <span style={{ color: 'var(--accent)', fontWeight: 600 }}>
                  {sxJob.throughput_human || '—'} write speed
                </span>
                <span style={{ color: 'var(--text-secondary)' }}>
                  {sxJob.bytes_written_human || '0 B'} extracted so far
                </span>
                {sxJob.current_file && (
                  <span style={{ color: 'var(--text-muted)', fontFamily: 'monospace', fontSize: '0.75rem' }}>
                    Writing: {sxJob.current_file}
                  </span>
                )}
              </div>
            )}
            {sxJob.status === 'completed' && (
              <div className="alert alert-success" style={{ marginTop: 12 }}>
                Done. {sxJob.files_extracted.toLocaleString()} files extracted to <code>{sxJob.output_dir}</code>
              </div>
            )}
          </div>
        )}

        {/* Scanning status */}
        {scanning && (
          <div style={{ marginTop: 12, padding: 12, background: 'var(--bg-tertiary)', borderRadius: 6 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
              <span className="spinner" />
              <span style={{ color: 'var(--text-primary)', fontSize: '0.9rem', fontWeight: 500 }}>
                Scanning directories for archive files...
              </span>
              <button
                className="btn btn-danger btn-sm"
                style={{ marginLeft: 'auto' }}
                onClick={async () => {
                  if (findJobId) {
                    await fetch(`/api/archives/find-stop/${findJobId}`, { method: 'POST' })
                  }
                  setScanning(false)
                  setScanned(true)
                }}
              >
                Stop &amp; Use What's Found
              </button>
            </div>
            {archives.length > 0 && (
              <div style={{ color: 'var(--accent)', fontSize: '0.85rem' }}>
                Found {archives.length} archives so far ({totalSize})
              </div>
            )}
          </div>
        )}

        {/* Results summary */}
        {scanned && !scanning && archives.length > 0 && (
          <div className="alert alert-success" style={{ marginTop: 12 }}>
            Found {archives.length} archive files ({totalSize} total). Click "Extract All" to unpack them.
          </div>
        )}
      </div>

      {/* Archives Found */}
      {scanned && archives.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>{archives.length} Archives Found</h3>
            <span style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
              Total: {totalSize}
            </span>
          </div>
          <div style={{ maxHeight: 300, overflowY: 'auto' }}>
            {archives.map((archive, idx) => (
              <div key={idx} style={{
                display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                padding: '6px 0', borderBottom: '1px solid var(--border)', fontSize: '0.85rem',
              }}>
                <span style={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>{archive.filename}</span>
                <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>{archive.size_human}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Extraction Progress */}
      {job && (
        <div className="card">
          <div className="card-header">
            <h3>
              {job.status === 'completed' ? 'Extraction Complete' :
               job.status === 'cancelled' ? 'Extraction Stopped' :
               job.status === 'error' ? 'Extraction Failed' : 'Extracting...'}
            </h3>
            <span style={{
              color: job.status === 'completed' ? 'var(--success)' :
                     job.status === 'error' ? 'var(--danger)' :
                     job.status === 'cancelled' ? 'var(--warning)' : 'var(--accent)',
              fontSize: '0.85rem',
            }}>
              {job.current_archive || job.phase}
            </span>
          </div>

          <div className="progress-bar" style={{ height: 10, borderRadius: 5, marginBottom: 12 }}>
            <div
              className={`fill ${job.status === 'running' ? 'fill-animated' : ''}`}
              style={{
                width: `${job.progress}%`,
                borderRadius: 5,
                background: job.status === 'completed' ? 'var(--success)' : undefined,
              }}
            />
          </div>

          <div className="stats-grid">
            <div className="stat-card">
              <div className="value">{job.processed_archives} / {job.total_archives}</div>
              <div className="label">Archives Processed</div>
            </div>
            <div className="stat-card">
              <div className="value">{job.files_extracted.toLocaleString()}</div>
              <div className="label">Files Extracted</div>
            </div>
            <div className="stat-card">
              <div className="value">{formatElapsed(job.elapsed_seconds)}</div>
              <div className="label">Elapsed</div>
            </div>
          </div>

          {job.status === 'completed' && (
            <div className="alert alert-success" style={{ marginTop: 12 }}>
              Extracted {job.files_extracted.toLocaleString()} files to: <code>{job.output_dir}</code>
              <br />
              <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                You can now add this directory to a dedup scan or media analysis.
              </span>
            </div>
          )}

          {job.errors.length > 0 && (
            <div className="alert alert-warning" style={{ marginTop: 12 }}>
              {job.errors.length} warnings:
              <div style={{ maxHeight: 100, overflowY: 'auto', marginTop: 4, fontSize: '0.75rem' }}>
                {job.errors.map((e, i) => <div key={i}>{e}</div>)}
              </div>
            </div>
          )}
        </div>
      )}

      {showBrowser && (
        <DirectoryBrowser
          onSelect={(path) => {
            if (browserTarget === 'source') setSourceDir(path)
            else setOutputDir(path)
            setShowBrowser(false)
            setScanned(false)
          }}
          onClose={() => setShowBrowser(false)}
        />
      )}
    </div>
  )
}
