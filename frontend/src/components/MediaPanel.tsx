import { useState, useEffect, useRef, useCallback } from 'react'
import { DirectoryBrowser } from './DirectoryBrowser'

interface PipelineJob {
  status: string
  file_path: string
  phase: string
  progress: number
  steps_completed: string[]
  error: string | null
  transcript_segments?: number
  frames_extracted?: number
  topics_found?: number
  keywords_found?: number
  elapsed_seconds?: number
  summary?: any
  media_info?: any
  transcription_note?: string
}

interface SearchResult {
  query: string
  transcript_hits: any[]
  topic_hits: any[]
  visual_hits: any[]
  total_hits: number
}

export function MediaPanel() {
  const [tab, setTab] = useState<'analyze' | 'search' | 'library'>('analyze')
  const [filePath, setFilePath] = useState('')
  const [showBrowser, setShowBrowser] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [job, setJob] = useState<PipelineJob | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState('')

  // Analysis options
  const [doTranscribe, setDoTranscribe] = useState(true)
  const [doVisual, setDoVisual] = useState(true)
  const [doTopics, setDoTopics] = useState(true)
  const [frameInterval, setFrameInterval] = useState(30)

  // Search
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<SearchResult | null>(null)
  const [searching, setSearching] = useState(false)

  // Library
  const [analyzedFiles, setAnalyzedFiles] = useState<any[]>([])
  const [selectedFileAnalysis, setSelectedFileAnalysis] = useState<any>(null)

  const pollRef = useRef<number | null>(null)

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  useEffect(() => {
    if (tab === 'library') loadLibrary()
    return () => stopPolling()
  }, [tab, stopPolling])

  const loadLibrary = async () => {
    try {
      const res = await fetch('/api/media/files')
      if (res.ok) {
        const data = await res.json()
        setAnalyzedFiles(data.files || [])
      }
    } catch {}
  }

  const startAnalysis = async () => {
    if (!filePath.trim()) {
      setError('Select a file to analyze')
      return
    }
    setError('')
    setAnalyzing(true)
    setJob(null)
    stopPolling()

    try {
      const res = await fetch('/api/media/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          file_path: filePath.trim(),
          transcribe: doTranscribe,
          visual: doVisual,
          topics: doTopics,
          frame_interval: frameInterval,
        }),
      })
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Failed to start analysis')
      }
      const data = await res.json()
      setJobId(data.job_id)
      pollJob(data.job_id)
    } catch (err: any) {
      setError(err.message)
      setAnalyzing(false)
    }
  }

  const pollJob = (id: string) => {
    const poll = async () => {
      try {
        const res = await fetch(`/api/media/job/${id}`)
        if (!res.ok) return
        const data: PipelineJob = await res.json()
        setJob(data)
        if (data.status === 'completed' || data.status === 'error') {
          stopPolling()
          setAnalyzing(false)
          if (data.status === 'error') setError(data.error || 'Analysis failed')
        }
      } catch {}
    }
    pollRef.current = window.setInterval(poll, 1000)
    poll()
  }

  const handleSearch = async () => {
    if (!searchQuery.trim()) return
    setSearching(true)
    setSearchResults(null)
    try {
      const res = await fetch(`/api/media/search?q=${encodeURIComponent(searchQuery.trim())}&limit=50`)
      if (res.ok) {
        const data = await res.json()
        setSearchResults(data)
      }
    } catch (err: any) {
      setError(err.message)
    } finally {
      setSearching(false)
    }
  }

  const loadFileAnalysis = async (filePath: string) => {
    try {
      const res = await fetch(`/api/media/file-analysis?file_path=${encodeURIComponent(filePath)}`)
      if (res.ok) {
        const data = await res.json()
        setSelectedFileAnalysis(data)
      }
    } catch {}
  }

  const formatTime = (seconds: number): string => {
    const h = Math.floor(seconds / 3600)
    const m = Math.floor((seconds % 3600) / 60)
    const s = Math.floor(seconds % 60)
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
    return `${m}:${s.toString().padStart(2, '0')}`
  }

  const getPhaseLabel = (phase: string) => {
    const labels: Record<string, string> = {
      initializing: 'Initializing...',
      media_info: 'Reading media info',
      extracting_audio: 'Extracting audio track',
      transcribing: 'Transcribing speech',
      extracting_frames: 'Extracting keyframes',
      analyzing_frames: 'Analyzing frames (AI)',
      extracting_topics: 'Extracting topics',
      generating_summary: 'Generating summary',
      complete: 'Complete',
    }
    return labels[phase] || phase
  }

  return (
    <div>
      {/* Tab Navigation */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 16 }}>
        {(['analyze', 'search', 'library'] as const).map((t) => (
          <button
            key={t}
            className={`btn ${tab === t ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => setTab(t)}
            style={{ textTransform: 'capitalize' }}
          >
            {t === 'analyze' ? 'Analyze Media' : t === 'search' ? 'Search Content' : 'Library'}
          </button>
        ))}
      </div>

      {/* ANALYZE TAB */}
      {tab === 'analyze' && (
        <div>
          <div className="card">
            <div className="card-header">
              <h2>Media Analysis Pipeline</h2>
            </div>
            <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 16 }}>
              Analyze video/audio files: extract speech transcripts, analyze visual frames,
              extract topics and keywords — all timestamped and searchable.
            </p>

            {error && <div className="alert alert-error">{error}</div>}

            <div className="form-group">
              <label>Media File Path</label>
              <div style={{ display: 'flex', gap: 8 }}>
                <input
                  type="text"
                  value={filePath}
                  onChange={(e) => setFilePath(e.target.value)}
                  placeholder="E:/Videos/meeting_recording.mp4"
                  style={{ flex: 1 }}
                />
                <button className="btn btn-secondary" onClick={() => setShowBrowser(true)}>
                  Browse
                </button>
              </div>
            </div>

            <div style={{ display: 'flex', gap: 24, marginBottom: 16, flexWrap: 'wrap' }}>
              <label className="checkbox-label">
                <input type="checkbox" checked={doTranscribe} onChange={(e) => setDoTranscribe(e.target.checked)} />
                Transcribe Audio
              </label>
              <label className="checkbox-label">
                <input type="checkbox" checked={doVisual} onChange={(e) => setDoVisual(e.target.checked)} />
                Analyze Frames
              </label>
              <label className="checkbox-label">
                <input type="checkbox" checked={doTopics} onChange={(e) => setDoTopics(e.target.checked)} />
                Extract Topics
              </label>
              <div className="form-group" style={{ marginBottom: 0, width: 160 }}>
                <label style={{ marginBottom: 2 }}>Frame Interval (sec)</label>
                <input
                  type="number"
                  value={frameInterval}
                  onChange={(e) => setFrameInterval(parseInt(e.target.value) || 30)}
                  min={5} max={300}
                  style={{ padding: '6px 8px' }}
                />
              </div>
            </div>

            <button className="btn btn-primary" onClick={startAnalysis} disabled={analyzing}>
              {analyzing ? <><span className="spinner" /> Analyzing...</> : 'Start Analysis'}
            </button>
          </div>

          {/* Pipeline Progress */}
          {job && (
            <div className="card">
              <div className="card-header">
                <h3>{job.status === 'completed' ? 'Analysis Complete' : 'Processing...'}</h3>
                <span style={{
                  color: job.status === 'completed' ? 'var(--success)' : job.status === 'error' ? 'var(--danger)' : 'var(--accent)',
                  fontSize: '0.85rem',
                }}>
                  {getPhaseLabel(job.phase)}
                </span>
              </div>

              {/* Progress Bar */}
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

              {/* Steps */}
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
                {['media_info', 'audio_extraction', 'transcription', 'frame_extraction', 'visual_analysis', 'topic_extraction', 'summary'].map((step) => (
                  <span key={step} style={{
                    fontSize: '0.7rem', padding: '3px 8px', borderRadius: 4,
                    background: job.steps_completed.includes(step) ? 'var(--success)' : 'var(--bg-tertiary)',
                    color: job.steps_completed.includes(step) ? 'white' : 'var(--text-muted)',
                  }}>
                    {step.replace('_', ' ')}
                  </span>
                ))}
              </div>

              {/* Stats */}
              <div className="stats-grid">
                {job.transcript_segments != null && (
                  <div className="stat-card">
                    <div className="value">{job.transcript_segments}</div>
                    <div className="label">Transcript Segments</div>
                  </div>
                )}
                {job.frames_extracted != null && (
                  <div className="stat-card">
                    <div className="value">{job.frames_extracted}</div>
                    <div className="label">Frames Analyzed</div>
                  </div>
                )}
                {job.topics_found != null && (
                  <div className="stat-card">
                    <div className="value">{job.topics_found}</div>
                    <div className="label">Topics Found</div>
                  </div>
                )}
                {job.keywords_found != null && (
                  <div className="stat-card">
                    <div className="value">{job.keywords_found}</div>
                    <div className="label">Keywords</div>
                  </div>
                )}
              </div>

              {job.transcription_note && (
                <div className="alert alert-warning" style={{ marginTop: 12 }}>
                  {job.transcription_note}
                </div>
              )}

              {/* Summary */}
              {job.summary && (
                <div className="metadata-section" style={{ marginTop: 12 }}>
                  <h4>AI Summary</h4>
                  {job.summary.title && (
                    <div className="metadata-row">
                      <span className="key">Title:</span>
                      <span className="value" style={{ fontWeight: 600 }}>{job.summary.title}</span>
                    </div>
                  )}
                  {job.summary.category && (
                    <div className="metadata-row">
                      <span className="key">Category:</span>
                      <span className="value">{job.summary.category}</span>
                    </div>
                  )}
                  {job.summary.summary && (
                    <div style={{ marginTop: 8, fontSize: '0.85rem', color: 'var(--text-secondary)', lineHeight: 1.7 }}>
                      {job.summary.summary}
                    </div>
                  )}
                  {job.summary.main_topics && (
                    <div className="tags" style={{ marginTop: 8 }}>
                      {job.summary.main_topics.map((t: string, i: number) => (
                        <span key={i} className="tag">{t}</span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* SEARCH TAB */}
      {tab === 'search' && (
        <div>
          <div className="card">
            <div className="card-header">
              <h2>Search Media Content</h2>
            </div>
            <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 16 }}>
              Search across all analyzed media — transcripts, topics, keywords, and visual descriptions.
              Find the exact moment in a video where something is discussed.
            </p>

            <div className="form-group">
              <label>Search Query</label>
              <div style={{ display: 'flex', gap: 8 }}>
                <input
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="e.g. supply chain, product demo, quarterly results..."
                  onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
                  style={{ flex: 1 }}
                />
                <button className="btn btn-primary" onClick={handleSearch} disabled={searching}>
                  {searching ? <span className="spinner" /> : 'Search'}
                </button>
              </div>
            </div>
          </div>

          {/* Search Results */}
          {searchResults && (
            <div className="card">
              <div className="card-header">
                <h3>Results for "{searchResults.query}"</h3>
                <span style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
                  {searchResults.total_hits} hits
                </span>
              </div>

              {searchResults.total_hits === 0 && (
                <div className="empty-state" style={{ padding: 24 }}>
                  <p>No results found. Try different keywords or analyze more files.</p>
                </div>
              )}

              {/* Transcript Hits */}
              {searchResults.transcript_hits.length > 0 && (
                <div style={{ marginBottom: 16 }}>
                  <h4 style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', marginBottom: 8 }}>
                    Speech / Transcript ({searchResults.transcript_hits.length})
                  </h4>
                  {searchResults.transcript_hits.map((hit, idx) => (
                    <div key={idx} className="search-hit">
                      <div className="search-hit-header">
                        <span className="search-hit-file">{hit.filename}</span>
                        <span className="search-hit-time">
                          {formatTime(hit.start_time)} — {formatTime(hit.end_time)}
                        </span>
                      </div>
                      <div className="search-hit-text">{hit.text}</div>
                      <div className="search-hit-path">{hit.file_path}</div>
                    </div>
                  ))}
                </div>
              )}

              {/* Topic Hits */}
              {searchResults.topic_hits.length > 0 && (
                <div style={{ marginBottom: 16 }}>
                  <h4 style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', marginBottom: 8 }}>
                    Topics & Keywords ({searchResults.topic_hits.length})
                  </h4>
                  {searchResults.topic_hits.map((hit, idx) => (
                    <div key={idx} className="search-hit">
                      <div className="search-hit-header">
                        <span className="search-hit-file">{hit.filename}</span>
                        {hit.start_time != null && (
                          <span className="search-hit-time">
                            {formatTime(hit.start_time)} — {formatTime(hit.end_time)}
                          </span>
                        )}
                      </div>
                      <div className="search-hit-text">
                        <span className="tag" style={{ marginRight: 8 }}>{hit.topic}</span>
                      </div>
                      <div className="search-hit-path">{hit.file_path}</div>
                    </div>
                  ))}
                </div>
              )}

              {/* Visual Hits */}
              {searchResults.visual_hits.length > 0 && (
                <div>
                  <h4 style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', marginBottom: 8 }}>
                    Visual / OCR ({searchResults.visual_hits.length})
                  </h4>
                  {searchResults.visual_hits.map((hit, idx) => (
                    <div key={idx} className="search-hit">
                      <div className="search-hit-header">
                        <span className="search-hit-file">{hit.filename}</span>
                        <span className="search-hit-time">{formatTime(hit.timestamp)}</span>
                      </div>
                      <div className="search-hit-text">{hit.description || hit.ocr_text}</div>
                      <div className="search-hit-path">{hit.file_path}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* LIBRARY TAB */}
      {tab === 'library' && (
        <div>
          <div className="card">
            <div className="card-header">
              <h2>Analyzed Files</h2>
              <button className="btn btn-secondary btn-sm" onClick={loadLibrary}>Refresh</button>
            </div>

            {analyzedFiles.length === 0 ? (
              <div className="empty-state" style={{ padding: 24 }}>
                <p>No files analyzed yet. Go to "Analyze Media" to process a file.</p>
              </div>
            ) : (
              <table className="file-table">
                <thead>
                  <tr>
                    <th>File</th>
                    <th>Type</th>
                    <th>Duration</th>
                    <th>Status</th>
                    <th>Analyzed</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {analyzedFiles.map((file) => (
                    <tr key={file.file_path}>
                      <td className="path-cell" title={file.file_path}>{file.filename}</td>
                      <td>{file.extension}</td>
                      <td>{file.duration_seconds ? formatTime(file.duration_seconds) : '—'}</td>
                      <td>
                        <span style={{
                          fontSize: '0.7rem', padding: '2px 6px', borderRadius: 4,
                          background: file.analysis_status === 'completed' ? 'var(--success)' : 'var(--bg-tertiary)',
                          color: file.analysis_status === 'completed' ? 'white' : 'var(--text-muted)',
                        }}>
                          {file.analysis_status}
                        </span>
                      </td>
                      <td style={{ fontSize: '0.8rem' }}>
                        {file.analyzed_at ? new Date(file.analyzed_at).toLocaleDateString() : '—'}
                      </td>
                      <td>
                        <button
                          className="btn btn-secondary btn-sm"
                          onClick={() => loadFileAnalysis(file.file_path)}
                        >
                          View
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* File Detail View */}
          {selectedFileAnalysis && (
            <div className="card">
              <div className="card-header">
                <h3>{selectedFileAnalysis.file.filename}</h3>
                <button className="btn btn-secondary btn-sm" onClick={() => setSelectedFileAnalysis(null)}>
                  Close
                </button>
              </div>

              {/* Transcript */}
              {selectedFileAnalysis.transcript.length > 0 && (
                <div className="metadata-section">
                  <h4>Transcript ({selectedFileAnalysis.transcript.length} segments)</h4>
                  <div style={{ maxHeight: 300, overflowY: 'auto' }}>
                    {selectedFileAnalysis.transcript.map((seg: any, i: number) => (
                      <div key={i} style={{
                        display: 'flex', gap: 12, padding: '4px 0',
                        borderBottom: '1px solid var(--border)', fontSize: '0.8rem',
                      }}>
                        <span style={{ color: 'var(--accent)', fontFamily: 'monospace', flexShrink: 0, width: 80 }}>
                          {formatTime(seg.start_time)}
                        </span>
                        {seg.speaker && (
                          <span style={{ color: 'var(--warning)', flexShrink: 0, width: 70 }}>
                            {seg.speaker}
                          </span>
                        )}
                        <span style={{ color: 'var(--text-primary)' }}>{seg.text}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Topics */}
              {selectedFileAnalysis.topics.length > 0 && (
                <div className="metadata-section">
                  <h4>Topics</h4>
                  <div className="tags">
                    {selectedFileAnalysis.topics.map((t: any, i: number) => (
                      <span key={i} className="tag">
                        {t.topic}
                        {t.start_time != null && (
                          <span style={{ marginLeft: 4, opacity: 0.6 }}>
                            @ {formatTime(t.start_time)}
                          </span>
                        )}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Keywords */}
              {selectedFileAnalysis.keywords.length > 0 && (
                <div className="metadata-section">
                  <h4>Keywords</h4>
                  <div className="tags">
                    {selectedFileAnalysis.keywords.slice(0, 30).map((k: any, i: number) => (
                      <span key={i} className="tag">{k.keyword} ({k.frequency})</span>
                    ))}
                  </div>
                </div>
              )}

              {/* Visual Segments */}
              {selectedFileAnalysis.visual_segments.length > 0 && (
                <div className="metadata-section">
                  <h4>Visual Analysis ({selectedFileAnalysis.visual_segments.length} frames)</h4>
                  <div style={{ maxHeight: 300, overflowY: 'auto' }}>
                    {selectedFileAnalysis.visual_segments.map((vs: any, i: number) => (
                      <div key={i} style={{
                        padding: '6px 0', borderBottom: '1px solid var(--border)', fontSize: '0.8rem',
                      }}>
                        <span style={{ color: 'var(--accent)', fontFamily: 'monospace', marginRight: 12 }}>
                          {formatTime(vs.timestamp)}
                        </span>
                        <span style={{ color: 'var(--text-primary)' }}>{vs.description}</span>
                        {vs.ocr_text && (
                          <div style={{ marginTop: 2, color: 'var(--text-muted)', fontSize: '0.75rem' }}>
                            OCR: {vs.ocr_text}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {showBrowser && (
        <DirectoryBrowser
          onSelect={(path) => { setFilePath(path); setShowBrowser(false) }}
          onClose={() => setShowBrowser(false)}
        />
      )}
    </div>
  )
}
