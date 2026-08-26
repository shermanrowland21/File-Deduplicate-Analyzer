import { useState, useRef } from 'react'

interface SearchHit {
  file_path: string
  filename: string
  start_time: number
  end_time: number
  text?: string
  topic?: string
  description?: string
  ocr_text?: string
  speaker?: string
  type: 'transcript' | 'topic' | 'visual'
}

interface ExportResult {
  source_path: string
  start_time: number
  end_time: number
  hi_res?: { success: boolean; output_path?: string; file_size_human?: string }
  lo_res?: { success: boolean; output_path?: string; file_size_human?: string }
}

export function MediaSearchPanel() {
  const [query, setQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [results, setResults] = useState<SearchHit[]>([])
  const [totalHits, setTotalHits] = useState(0)
  const [selectedHits, setSelectedHits] = useState<Set<number>>(new Set())
  const [previewHit, setPreviewHit] = useState<SearchHit | null>(null)
  const [proxyReady, setProxyReady] = useState(false)
  const [proxyLoading, setProxyLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [exportResults, setExportResults] = useState<ExportResult[]>([])
  const [exportHiRes, setExportHiRes] = useState(true)
  const [exportLoRes, setExportLoRes] = useState(true)
  const [error, setError] = useState('')
  const videoRef = useRef<HTMLVideoElement>(null)

  const handleSearch = async () => {
    if (!query.trim()) return
    setSearching(true)
    setResults([])
    setSelectedHits(new Set())
    setExportResults([])
    setError('')

    try {
      const res = await fetch(`/api/media/search?q=${encodeURIComponent(query.trim())}&limit=100`)
      if (!res.ok) throw new Error('Search failed')
      const data = await res.json()

      // Flatten all hit types into one list with type labels
      const hits: SearchHit[] = []
      for (const hit of (data.transcript_hits || [])) {
        hits.push({ ...hit, type: 'transcript' })
      }
      for (const hit of (data.topic_hits || [])) {
        hits.push({ ...hit, text: hit.topic, type: 'topic' })
      }
      for (const hit of (data.visual_hits || [])) {
        hits.push({ ...hit, text: hit.description || hit.ocr_text, type: 'visual' })
      }

      // Sort by file then timestamp
      hits.sort((a, b) => {
        if (a.file_path !== b.file_path) return a.file_path.localeCompare(b.file_path)
        return (a.start_time || 0) - (b.start_time || 0)
      })

      setResults(hits)
      setTotalHits(data.total_hits || hits.length)
    } catch (err: any) {
      setError(err.message)
    } finally {
      setSearching(false)
    }
  }

  const toggleSelect = (idx: number) => {
    const next = new Set(selectedHits)
    if (next.has(idx)) next.delete(idx)
    else next.add(idx)
    setSelectedHits(next)
  }

  const selectAll = () => {
    const next = new Set<number>()
    results.forEach((_, i) => next.add(i))
    setSelectedHits(next)
  }

  const previewClip = async (hit: SearchHit) => {
    setPreviewHit(hit)
    setProxyReady(false)
    setProxyLoading(true)

    // Request proxy generation
    try {
      const res = await fetch('/api/media/proxy/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: hit.file_path, quality: 'scrub' }),
      })
      const data = await res.json()

      if (data.status === 'completed') {
        setProxyReady(true)
        setProxyLoading(false)
        // Set video source and seek to timestamp
        setTimeout(() => {
          if (videoRef.current) {
            videoRef.current.src = `/api/media/proxy/stream?file_path=${encodeURIComponent(hit.file_path)}&quality=scrub`
            videoRef.current.currentTime = hit.start_time || 0
          }
        }, 100)
      } else if (data.status === 'running') {
        // Poll until ready
        pollProxy(data, hit)
      }
    } catch {
      setProxyLoading(false)
    }
  }

  const pollProxy = (jobData: any, hit: SearchHit) => {
    const jobId = `proxy_${hit.file_path.split('/').pop()?.split('.')[0]}_scrub`
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`/api/media/proxy/status/${encodeURIComponent(jobId)}`)
        if (!res.ok) { clearInterval(interval); setProxyLoading(false); return }
        const data = await res.json()
        if (data.status === 'completed') {
          clearInterval(interval)
          setProxyReady(true)
          setProxyLoading(false)
          setTimeout(() => {
            if (videoRef.current) {
              videoRef.current.src = `/api/media/proxy/stream?file_path=${encodeURIComponent(hit.file_path)}&quality=scrub`
              videoRef.current.currentTime = hit.start_time || 0
            }
          }, 100)
        } else if (data.status === 'error') {
          clearInterval(interval)
          setProxyLoading(false)
        }
      } catch { clearInterval(interval); setProxyLoading(false) }
    }, 2000)
  }

  const handleExport = async () => {
    if (selectedHits.size === 0) return
    setExporting(true)
    setExportResults([])
    setError('')

    const clips = Array.from(selectedHits).map((idx) => {
      const hit = results[idx]
      return {
        source_path: hit.file_path,
        start_time: Math.max(0, (hit.start_time || 0) - 2),  // 2sec padding before
        end_time: (hit.end_time || hit.start_time || 0) + 2,  // 2sec padding after
        label: hit.text?.slice(0, 50) || '',
      }
    })

    try {
      const res = await fetch('/api/media/export-batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clips, hi_res: exportHiRes, lo_res: exportLoRes }),
      })
      if (!res.ok) throw new Error('Export failed')
      const data = await res.json()
      setExportResults(data.results || [])
    } catch (err: any) {
      setError(err.message)
    } finally {
      setExporting(false)
    }
  }

  const formatTime = (seconds: number): string => {
    if (!seconds && seconds !== 0) return '—'
    const h = Math.floor(seconds / 3600)
    const m = Math.floor((seconds % 3600) / 60)
    const s = Math.floor(seconds % 60)
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
    return `${m}:${s.toString().padStart(2, '0')}`
  }

  const typeColors: Record<string, string> = {
    transcript: 'var(--accent)',
    topic: 'var(--success)',
    visual: 'var(--warning)',
  }

  return (
    <div>
      {/* Search Bar */}
      <div className="card">
        <div className="card-header">
          <h2>Search Across All Media</h2>
        </div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 16 }}>
          Search transcripts, topics, keywords, and visual descriptions across all analyzed files.
          Select results to export as clips.
        </p>

        {error && <div className="alert alert-error">{error}</div>}

        <div className="form-group">
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search for topics, phrases, keywords..."
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              style={{ flex: 1 }}
            />
            <button className="btn btn-primary" onClick={handleSearch} disabled={searching}>
              {searching ? <span className="spinner" /> : 'Search'}
            </button>
          </div>
        </div>
      </div>

      {/* Results + Preview split view */}
      {results.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: previewHit ? '1fr 1fr' : '1fr', gap: 16 }}>
          {/* Results List */}
          <div className="card" style={{ maxHeight: '70vh', overflowY: 'auto' }}>
            <div className="card-header">
              <h3>{totalHits} Results</h3>
              <div className="btn-group">
                <button className="btn btn-secondary btn-sm" onClick={selectAll}>Select All</button>
                <button className="btn btn-secondary btn-sm" onClick={() => setSelectedHits(new Set())}>Clear</button>
              </div>
            </div>

            {results.map((hit, idx) => (
              <div
                key={idx}
                className="search-hit"
                style={{
                  borderLeft: `3px solid ${selectedHits.has(idx) ? 'var(--accent)' : 'transparent'}`,
                  background: selectedHits.has(idx) ? 'var(--bg-tertiary)' : undefined,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={selectedHits.has(idx)}
                    onChange={() => toggleSelect(idx)}
                    style={{ marginTop: 4, accentColor: 'var(--accent)' }}
                  />
                  <div style={{ flex: 1, cursor: 'pointer' }} onClick={() => previewClip(hit)}>
                    <div className="search-hit-header">
                      <span className="search-hit-file">{hit.filename}</span>
                      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                        <span style={{
                          fontSize: '0.65rem', padding: '1px 6px', borderRadius: 3,
                          background: typeColors[hit.type], color: 'white',
                        }}>
                          {hit.type}
                        </span>
                        <span className="search-hit-time">
                          {formatTime(hit.start_time)}
                          {hit.end_time ? ` — ${formatTime(hit.end_time)}` : ''}
                        </span>
                      </div>
                    </div>
                    <div className="search-hit-text">{hit.text}</div>
                    {hit.speaker && (
                      <span style={{ fontSize: '0.7rem', color: 'var(--warning)' }}>Speaker: {hit.speaker}</span>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Preview Panel */}
          {previewHit && (
            <div className="card" style={{ position: 'sticky', top: 24 }}>
              <div className="card-header">
                <h3>Preview</h3>
                <button className="btn btn-secondary btn-sm" onClick={() => setPreviewHit(null)}>Close</button>
              </div>

              {/* Video Player */}
              <div style={{ background: '#000', borderRadius: 6, overflow: 'hidden', marginBottom: 12 }}>
                {proxyLoading && (
                  <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>
                    <span className="spinner" /> Generating proxy for playback...
                  </div>
                )}
                {proxyReady && (
                  <video
                    ref={videoRef}
                    controls
                    style={{ width: '100%', maxHeight: 360 }}
                    preload="auto"
                  />
                )}
                {!proxyLoading && !proxyReady && (
                  <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                    Click a result to preview. First preview generates a proxy (may take a minute).
                  </div>
                )}
              </div>

              {/* Hit Details */}
              <div className="metadata-section">
                <div className="metadata-row">
                  <span className="key">File:</span>
                  <span className="value">{previewHit.filename}</span>
                </div>
                <div className="metadata-row">
                  <span className="key">Time:</span>
                  <span className="value">{formatTime(previewHit.start_time)} — {formatTime(previewHit.end_time)}</span>
                </div>
                <div className="metadata-row">
                  <span className="key">Content:</span>
                  <span className="value">{previewHit.text}</span>
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Export Bar */}
      {selectedHits.size > 0 && (
        <div className="card" style={{ position: 'sticky', bottom: 0, marginTop: 16, borderColor: 'var(--accent)' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div>
              <strong>{selectedHits.size} clips selected</strong>
              <div style={{ display: 'flex', gap: 16, marginTop: 8 }}>
                <label className="checkbox-label">
                  <input type="checkbox" checked={exportHiRes} onChange={(e) => setExportHiRes(e.target.checked)} />
                  Hi-Res (Premiere-ready, lossless)
                </label>
                <label className="checkbox-label">
                  <input type="checkbox" checked={exportLoRes} onChange={(e) => setExportLoRes(e.target.checked)} />
                  Lo-Res (review/sharing, 720p)
                </label>
              </div>
            </div>
            <button className="btn btn-primary" onClick={handleExport} disabled={exporting}>
              {exporting ? <><span className="spinner" /> Exporting...</> : `Export ${selectedHits.size} Clips`}
            </button>
          </div>
        </div>
      )}

      {/* Export Results */}
      {exportResults.length > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-header">
            <h3>Exported Clips</h3>
          </div>
          {exportResults.map((result, idx) => (
            <div key={idx} style={{ padding: '8px 0', borderBottom: '1px solid var(--border)', fontSize: '0.85rem' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span>{formatTime(result.start_time)} — {formatTime(result.end_time)}</span>
                <div className="btn-group">
                  {result.hi_res?.success && (
                    <a
                      href={`/api/media/download-clip?path=${encodeURIComponent(result.hi_res.output_path || '')}`}
                      className="btn btn-primary btn-sm"
                      download
                    >
                      Hi-Res ({result.hi_res.file_size_human})
                    </a>
                  )}
                  {result.lo_res?.success && (
                    <a
                      href={`/api/media/download-clip?path=${encodeURIComponent(result.lo_res.output_path || '')}`}
                      className="btn btn-secondary btn-sm"
                      download
                    >
                      Lo-Res ({result.lo_res.file_size_human})
                    </a>
                  )}
                </div>
              </div>
              <div className="search-hit-path" style={{ marginTop: 2 }}>{result.source_path}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
