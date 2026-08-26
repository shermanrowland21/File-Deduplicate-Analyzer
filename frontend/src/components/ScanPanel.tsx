import { useState, useRef, useCallback } from 'react'
import { api } from '../api'
import { ScanStatus, DuplicatesResponse } from '../types'
import { DirectoryBrowser } from './DirectoryBrowser'

interface ScanPanelProps {
  onScanComplete: (status: ScanStatus, duplicates: DuplicatesResponse | null) => void
}

interface ScanProgress {
  scan_id: string
  status: string
  total_files: number
  discovered_files: number
  processed_files: number
  duplicates_found: number
  cache_hits: number
  directory: string
  phase: string
  current_file: string
  current_dir: string
  elapsed_seconds: number
  error?: string
}

export function ScanPanel({ onScanComplete }: ScanPanelProps) {
  const [directory, setDirectory] = useState('')
  const [recursive, setRecursive] = useState(true)
  const [includeHidden, setIncludeHidden] = useState(false)
  const [minSize, setMinSize] = useState('0')
  const [maxSize, setMaxSize] = useState('')
  const [extensions, setExtensions] = useState('')
  const [scanning, setScanning] = useState(false)
  const [progress, setProgress] = useState<ScanProgress | null>(null)
  const [error, setError] = useState('')
  const [showBrowser, setShowBrowser] = useState(false)
  const pollRef = useRef<number | null>(null)

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  const pollProgress = useCallback((scanId: string) => {
    const poll = async () => {
      try {
        const res = await fetch(`/api/scanner/status/${scanId}`)
        if (!res.ok) return
        const status: ScanProgress = await res.json()
        setProgress(status)

        if (status.status === 'completed') {
          stopPolling()
          setScanning(false)
          try {
            const dups = await api.getDuplicates(scanId)
            onScanComplete(status as any, dups)
          } catch {
            onScanComplete(status as any, null)
          }
        } else if (status.status === 'error') {
          stopPolling()
          setScanning(false)
          setError(status.error || 'Scan failed')
        }
      } catch {
        // Keep polling
      }
    }

    pollRef.current = window.setInterval(poll, 400)
    poll()
  }, [stopPolling, onScanComplete])

  const handleScan = async () => {
    if (!directory.trim()) {
      setError('Please enter a directory path')
      return
    }

    setError('')
    setScanning(true)
    setProgress(null)
    stopPolling()

    try {
      const options: any = {
        recursive,
        include_hidden: includeHidden,
        min_file_size: parseInt(minSize) || 0,
      }

      if (maxSize) {
        options.max_file_size = parseInt(maxSize)
      }

      if (extensions.trim()) {
        options.file_extensions = extensions.split(',').map((e: string) => e.trim())
      }

      const status = await api.scanDirectory(directory.trim(), options)
      setProgress(status)

      if (status.status === 'running') {
        pollProgress(status.scan_id)
      } else if (status.status === 'completed') {
        setScanning(false)
        try {
          const dups = await api.getDuplicates(status.scan_id)
          onScanComplete(status, dups)
        } catch {
          onScanComplete(status, null)
        }
      } else if (status.status === 'error') {
        setScanning(false)
        setError(status.error || 'Scan failed')
      }
    } catch (err: any) {
      setError(err.message || 'Failed to start scan')
      setScanning(false)
    }
  }

  const formatElapsed = (seconds: number): string => {
    if (seconds < 60) return `${seconds.toFixed(0)}s`
    const mins = Math.floor(seconds / 60)
    const secs = Math.round(seconds % 60)
    return `${mins}m ${secs}s`
  }

  const getPhaseLabel = (phase: string): string => {
    switch (phase) {
      case 'starting': return 'Starting...'
      case 'scanning': return 'Scanning & Hashing'
      case 'complete': return 'Complete'
      default: return phase
    }
  }

  // Rate: files processed per second
  const rate = progress && progress.elapsed_seconds > 0
    ? Math.round(progress.processed_files / progress.elapsed_seconds)
    : 0

  return (
    <div>
      <div className="card">
        <div className="card-header">
          <h2>Scan Directory for Duplicates</h2>
        </div>

        {error && <div className="alert alert-error">{error}</div>}

        <div className="form-group">
          <label>Directory Path</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="text"
              value={directory}
              onChange={(e) => setDirectory(e.target.value)}
              placeholder="C:\Users\username\Documents or /home/user/files"
              onKeyDown={(e) => e.key === 'Enter' && handleScan()}
              style={{ flex: 1 }}
            />
            <button
              className="btn btn-secondary"
              onClick={() => setShowBrowser(true)}
              type="button"
            >
              Browse
            </button>
          </div>
        </div>

        <div className="form-row">
          <div className="form-group">
            <label>Min File Size (bytes)</label>
            <input
              type="number"
              value={minSize}
              onChange={(e) => setMinSize(e.target.value)}
              placeholder="0"
            />
          </div>
          <div className="form-group">
            <label>Max File Size (bytes, empty = no limit)</label>
            <input
              type="number"
              value={maxSize}
              onChange={(e) => setMaxSize(e.target.value)}
              placeholder="No limit"
            />
          </div>
        </div>

        <div className="form-group">
          <label>File Extensions Filter (comma-separated, empty = all)</label>
          <input
            type="text"
            value={extensions}
            onChange={(e) => setExtensions(e.target.value)}
            placeholder="e.g. jpg, png, pdf, docx"
          />
        </div>

        <div style={{ display: 'flex', gap: 24, marginBottom: 16 }}>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={recursive}
              onChange={(e) => setRecursive(e.target.checked)}
            />
            Scan subdirectories recursively
          </label>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={includeHidden}
              onChange={(e) => setIncludeHidden(e.target.checked)}
            />
            Include hidden files
          </label>
        </div>

        <button
          className="btn btn-primary"
          onClick={handleScan}
          disabled={scanning}
        >
          {scanning ? (
            <>
              <span className="spinner" /> Scanning...
            </>
          ) : (
            'Start Scan'
          )}
        </button>
      </div>

      {/* Live Progress Panel */}
      {progress && (
        <div className="card">
          <div className="card-header">
            <h3>
              {progress.status === 'completed' ? 'Scan Complete' : 'Scanning...'}
            </h3>
            <span style={{
              color: progress.status === 'completed' ? 'var(--success)' :
                     progress.status === 'error' ? 'var(--danger)' : 'var(--accent)',
              fontSize: '0.85rem',
            }}>
              {progress.status === 'running' ? getPhaseLabel(progress.phase) : progress.status}
            </span>
          </div>

          {/* Progress Info */}
          <div className="scan-progress-container">
            <div className="scan-progress-header">
              <span className="scan-progress-pct">
                {progress.processed_files.toLocaleString()} processed
              </span>
              <span className="scan-progress-counts">
                {progress.discovered_files.toLocaleString()} discovered
                {progress.cache_hits > 0 && (
                  <span style={{ color: 'var(--success)', marginLeft: 8 }}>
                    ({progress.cache_hits.toLocaleString()} cached)
                  </span>
                )}
              </span>
              <span className="scan-progress-time">
                {formatElapsed(progress.elapsed_seconds)}
              </span>
            </div>

            {/* Animated progress bar - pulses during scan, fills on complete */}
            <div className="progress-bar" style={{ height: 10, borderRadius: 5 }}>
              <div
                className={`fill ${progress.status === 'running' ? 'fill-animated' : ''}`}
                style={{
                  width: progress.status === 'completed' ? '100%' : '100%',
                  borderRadius: 5,
                  background: progress.status === 'completed'
                    ? 'var(--success)'
                    : undefined,
                }}
              />
            </div>

            {/* Current location being scanned */}
            {progress.status === 'running' && (
              <div className="scan-progress-current">
                <span className="scan-progress-file">
                  {progress.current_file}
                </span>
                {progress.current_dir && (
                  <span className="scan-progress-dir" title={progress.current_dir}>
                    in {progress.current_dir}
                  </span>
                )}
              </div>
            )}

            {/* Large file warning */}
            {progress.status === 'running' && (progress as any).hashing_size > 100 * 1024 * 1024 && (
              <div style={{ fontSize: '0.75rem', color: 'var(--warning)', marginTop: 4 }}>
                ⚠ Hashing large file ({Math.round((progress as any).hashing_size / 1024 / 1024)} MB) — this may take a moment
              </div>
            )}

            {/* Rate */}
            {progress.status === 'running' && rate > 0 && (
              <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 4 }}>
                {rate.toLocaleString()} files/sec
              </div>
            )}
          </div>

          {/* Stats */}
          <div className="stats-grid" style={{ marginTop: 16, marginBottom: 0 }}>
            <div className="stat-card">
              <div className="value">{progress.discovered_files.toLocaleString()}</div>
              <div className="label">Files Found</div>
            </div>
            <div className="stat-card">
              <div className="value">{progress.processed_files.toLocaleString()}</div>
              <div className="label">Hashed</div>
            </div>
            <div className="stat-card">
              <div className="value" style={{ color: progress.duplicates_found > 0 ? 'var(--warning)' : 'var(--success)' }}>
                {progress.duplicates_found.toLocaleString()}
              </div>
              <div className="label">Duplicates</div>
            </div>
            <div className="stat-card">
              <div className="value" style={{ fontSize: '1.1rem' }}>
                {formatElapsed(progress.elapsed_seconds)}
              </div>
              <div className="label">Elapsed</div>
            </div>
          </div>

          {/* Cache info on completion */}
          {progress.status === 'completed' && progress.cache_hits > 0 && (
            <div className="alert alert-success" style={{ marginTop: 12, marginBottom: 0 }}>
              {progress.cache_hits.toLocaleString()} files used cached hashes (skipped re-hashing). 
              Next scan will be even faster for unchanged files.
            </div>
          )}
        </div>
      )}

      {showBrowser && (
        <DirectoryBrowser
          onSelect={(path) => {
            setDirectory(path)
            setShowBrowser(false)
          }}
          onClose={() => setShowBrowser(false)}
        />
      )}
    </div>
  )
}
