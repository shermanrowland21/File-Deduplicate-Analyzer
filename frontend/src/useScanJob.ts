import { useState, useRef, useCallback, useEffect } from 'react'
import { api } from './api'

// Shared scan-job state, owned at the App level so it SURVIVES tab switches.
// The scan runs on the backend; this hook keeps polling regardless of which
// view is mounted, and fetches duplicates LIVE (partial results) during scan.

export interface ScanProgress {
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
  hashing_size?: number
}

export interface ScanJob {
  scanning: boolean
  progress: ScanProgress | null
  duplicates: any | null
  error: string
  startScan: (body: any) => Promise<void>
  stopScan: () => Promise<void>
}

export function useScanJob(): ScanJob {
  const [scanning, setScanning] = useState(false)
  const [progress, setProgress] = useState<ScanProgress | null>(null)
  const [duplicates, setDuplicates] = useState<any | null>(null)
  const [error, setError] = useState('')
  const pollRef = useRef<number | null>(null)
  const dupTickRef = useRef(0)

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  // clean up on full app unmount only
  useEffect(() => () => stopPolling(), [stopPolling])

  const pollProgress = useCallback((scanId: string) => {
    stopPolling()
    const poll = async () => {
      try {
        const res = await fetch(`/api/scanner/status/${scanId}`)
        if (!res.ok) return
        const status: ScanProgress = await res.json()
        setProgress(status)

        // Fetch duplicates LIVE every ~5 ticks (partial results now supported).
        dupTickRef.current += 1
        if (status.duplicates_found > 0 && dupTickRef.current % 5 === 0) {
          try {
            const dups = await api.getDuplicates(scanId)
            if (dups) setDuplicates(dups)
          } catch { /* ignore mid-scan */ }
        }

        if (status.status === 'completed') {
          stopPolling()
          setScanning(false)
          try {
            const dups = await api.getDuplicates(scanId)
            if (dups) setDuplicates(dups)
          } catch { /* ignore */ }
        } else if (status.status === 'error' || status.status === 'cancelled') {
          stopPolling()
          setScanning(false)
          if (status.status === 'error') setError(status.error || 'Scan failed')
        }
      } catch {
        // keep polling
      }
    }
    pollRef.current = window.setInterval(poll, 400)
    poll()
  }, [stopPolling])

  const startScan = useCallback(async (body: any) => {
    setError('')
    setScanning(true)
    setProgress(null)
    setDuplicates(null)
    dupTickRef.current = 0
    stopPolling()
    try {
      const response = await fetch('/api/scanner/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!response.ok) {
        const err = await response.json().catch(() => ({}))
        throw new Error(err.detail || `HTTP ${response.status}`)
      }
      const status = await response.json()
      setProgress(status)
      if (status.status === 'running') {
        pollProgress(status.scan_id)
      } else if (status.status === 'completed') {
        setScanning(false)
        try { const dups = await api.getDuplicates(status.scan_id); if (dups) setDuplicates(dups) } catch {}
      } else if (status.status === 'error') {
        setScanning(false)
        setError(status.error || 'Scan failed')
      }
    } catch (err: any) {
      setError(err.message || 'Failed to start scan')
      setScanning(false)
    }
  }, [pollProgress, stopPolling])

  const stopScan = useCallback(async () => {
    if (progress?.scan_id) {
      try { await fetch(`/api/scanner/stop/${progress.scan_id}`, { method: 'POST' }) } catch {}
    }
    stopPolling()
    setScanning(false)
  }, [progress, stopPolling])

  return { scanning, progress, duplicates, error, startScan, stopScan }
}
