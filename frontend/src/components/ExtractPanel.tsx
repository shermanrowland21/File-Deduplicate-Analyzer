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
  const [extracting, setExtracting] = useState(false)
  const [job, setJob] = useState<ExtractionJob | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const pollRef = useRef<number | null>(null)

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

    try {
      const res = await fetch(`/api/archives/find?directory=${encodeURIComponent(sourceDir.trim())}`)
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Failed to scan for archives')
      }
      const data = await res.json()
      setArchives(data.archives || [])
      setTotalSize(data.total_size_human || '0 B')
      setScanned(true)

      if (data.archives.length === 0) {
        setError('No archive files found in this directory')
      }
    } catch (err: any) {
      setError(err.message)
    }
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

        <div className="btn-group">
          <button className="btn btn-secondary" onClick={findArchives}>
            Scan for Archives
          </button>
          {scanned && archives.length > 0 && (
            <button className="btn btn-primary" onClick={startExtraction} disabled={extracting}>
              {extracting ? <><span className="spinner" /> Extracting...</> : `Extract ${archives.length} Archives`}
            </button>
          )}
          {extracting && (
            <button className="btn btn-danger" onClick={handleStop}>
              Stop
            </button>
          )}
        </div>
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
