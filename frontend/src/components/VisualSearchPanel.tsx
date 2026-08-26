import { useState, useRef } from 'react'

interface VisualSearchResult {
  frame_path: string
  file_path: string
  filename: string
  timestamp: number
  description: string
  similarity_score?: number
  match_score?: number
  tags?: {
    objects?: { name: string; category?: string; attributes?: string[] }[]
    materials?: string[]
    colors?: string[]
    scene_tags?: string[]
    content_type?: string
  }
}

export function VisualSearchPanel() {
  const [searchMode, setSearchMode] = useState<'image' | 'text' | 'tags'>('text')
  const [textQuery, setTextQuery] = useState('')
  const [results, setResults] = useState<VisualSearchResult[]>([])
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState('')
  const [selectedResults, setSelectedResults] = useState<Set<number>>(new Set())
  const [exporting, setExporting] = useState(false)
  const [previewImage, setPreviewImage] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // Tag search fields
  const [tagObjects, setTagObjects] = useState('')
  const [tagMaterials, setTagMaterials] = useState('')
  const [tagColors, setTagColors] = useState('')
  const [tagScene, setTagScene] = useState('')
  const [tagContentType, setTagContentType] = useState('')

  // Image search
  const [uploadedImage, setUploadedImage] = useState<File | null>(null)
  const [uploadedPreview, setUploadedPreview] = useState<string | null>(null)

  const handleImageUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) {
      setUploadedImage(file)
      setUploadedPreview(URL.createObjectURL(file))
    }
  }

  const handleSearch = async () => {
    setError('')
    setSearching(true)
    setResults([])

    try {
      let res: Response

      if (searchMode === 'image') {
        if (!uploadedImage) {
          setError('Upload a reference image first')
          setSearching(false)
          return
        }
        const formData = new FormData()
        formData.append('image', uploadedImage)
        formData.append('top_k', '30')
        formData.append('min_score', '0.25')
        res = await fetch('/api/visual/search-by-image', { method: 'POST', body: formData })

      } else if (searchMode === 'text') {
        if (!textQuery.trim()) {
          setError('Enter a search description')
          setSearching(false)
          return
        }
        res = await fetch('/api/visual/search-by-text', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query: textQuery.trim(), top_k: 30, min_score: 0.15 }),
        })

      } else {
        // Tag search
        const body: any = { min_match_score: 0.4 }
        if (tagObjects.trim()) body.objects = tagObjects.split(',').map(s => s.trim())
        if (tagMaterials.trim()) body.materials = tagMaterials.split(',').map(s => s.trim())
        if (tagColors.trim()) body.colors = tagColors.split(',').map(s => s.trim())
        if (tagScene.trim()) body.scene_tags = tagScene.split(',').map(s => s.trim())
        if (tagContentType.trim()) body.content_type = tagContentType.trim()

        if (!body.objects && !body.materials && !body.colors && !body.scene_tags && !body.content_type) {
          setError('Enter at least one filter')
          setSearching(false)
          return
        }

        res = await fetch('/api/visual/search-by-tags', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
      }

      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Search failed')
      }

      const data = await res.json()
      setResults(data.results || [])
      if (data.results?.length === 0) {
        setError('No results found. Make sure media has been analyzed and indexed.')
      }
    } catch (err: any) {
      setError(err.message)
    } finally {
      setSearching(false)
    }
  }

  const toggleSelect = (idx: number) => {
    const next = new Set(selectedResults)
    if (next.has(idx)) next.delete(idx)
    else next.add(idx)
    setSelectedResults(next)
  }

  const handleExportSelected = async () => {
    if (selectedResults.size === 0) return
    setExporting(true)

    const clips = Array.from(selectedResults).map(idx => {
      const r = results[idx]
      return {
        source_path: r.file_path,
        start_time: Math.max(0, r.timestamp - 5),
        end_time: r.timestamp + 10,
        label: r.description?.slice(0, 40) || `frame_${r.timestamp}s`,
      }
    })

    try {
      const res = await fetch('/api/media/export-batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clips, hi_res: true, lo_res: true }),
      })
      if (res.ok) {
        const data = await res.json()
        alert(`Exported ${data.successful} clips. Check the clips folder.`)
      }
    } catch (err: any) {
      setError(err.message)
    } finally {
      setExporting(false)
    }
  }

  const formatTime = (seconds: number): string => {
    if (!seconds && seconds !== 0) return ''
    const h = Math.floor(seconds / 3600)
    const m = Math.floor((seconds % 3600) / 60)
    const s = Math.floor(seconds % 60)
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
    return `${m}:${s.toString().padStart(2, '0')}`
  }

  return (
    <div>
      {/* Mode Selector */}
      <div className="card">
        <div className="card-header">
          <h2>Visual Search</h2>
        </div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 16 }}>
          Find frames and images across all your media using visual similarity, natural language, or structured filters.
        </p>

        <div style={{ display: 'flex', gap: 4, marginBottom: 16 }}>
          <button
            className={`btn ${searchMode === 'image' ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => setSearchMode('image')}
          >
            Image Match
          </button>
          <button
            className={`btn ${searchMode === 'text' ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => setSearchMode('text')}
          >
            Describe It
          </button>
          <button
            className={`btn ${searchMode === 'tags' ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => setSearchMode('tags')}
          >
            Filter by Tags
          </button>
        </div>

        {error && <div className="alert alert-error">{error}</div>}

        {/* Image Upload Mode */}
        {searchMode === 'image' && (
          <div>
            <div className="form-group">
              <label>Upload Reference Image</label>
              <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: 8 }}>
                Upload a photo and find all frames across your media that look similar.
              </p>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                onChange={handleImageUpload}
                style={{ display: 'none' }}
              />
              <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
                {uploadedPreview && (
                  <img
                    src={uploadedPreview}
                    alt="Reference"
                    style={{ width: 120, height: 90, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--border)' }}
                  />
                )}
                <button
                  className="btn btn-secondary"
                  onClick={() => fileInputRef.current?.click()}
                >
                  {uploadedImage ? 'Change Image' : 'Choose Image'}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Text Description Mode */}
        {searchMode === 'text' && (
          <div className="form-group">
            <label>Describe what you're looking for</label>
            <input
              type="text"
              value={textQuery}
              onChange={(e) => setTextQuery(e.target.value)}
              placeholder="e.g. granite rock formation near water, whiteboard with flowchart, person holding product..."
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            />
          </div>
        )}

        {/* Tag Filter Mode */}
        {searchMode === 'tags' && (
          <div>
            <div className="form-row">
              <div className="form-group">
                <label>Objects (comma-separated)</label>
                <input
                  type="text"
                  value={tagObjects}
                  onChange={(e) => setTagObjects(e.target.value)}
                  placeholder="rock, tree, car, person..."
                />
              </div>
              <div className="form-group">
                <label>Materials</label>
                <input
                  type="text"
                  value={tagMaterials}
                  onChange={(e) => setTagMaterials(e.target.value)}
                  placeholder="granite, wood, metal, glass..."
                />
              </div>
            </div>
            <div className="form-row">
              <div className="form-group">
                <label>Colors</label>
                <input
                  type="text"
                  value={tagColors}
                  onChange={(e) => setTagColors(e.target.value)}
                  placeholder="red, blue, grey..."
                />
              </div>
              <div className="form-group">
                <label>Scene Type</label>
                <input
                  type="text"
                  value={tagScene}
                  onChange={(e) => setTagScene(e.target.value)}
                  placeholder="outdoor, mountain, urban..."
                />
              </div>
            </div>
            <div className="form-group">
              <label>Content Type</label>
              <input
                type="text"
                value={tagContentType}
                onChange={(e) => setTagContentType(e.target.value)}
                placeholder="landscape, presentation, product..."
              />
            </div>
          </div>
        )}

        <button className="btn btn-primary" onClick={handleSearch} disabled={searching}>
          {searching ? <><span className="spinner" /> Searching...</> : 'Search'}
        </button>
      </div>

      {/* Results Grid */}
      {results.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>{results.length} Visual Matches</h3>
            <div className="btn-group">
              {selectedResults.size > 0 && (
                <button
                  className="btn btn-primary btn-sm"
                  onClick={handleExportSelected}
                  disabled={exporting}
                >
                  {exporting ? 'Exporting...' : `Export ${selectedResults.size} Clips`}
                </button>
              )}
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => {
                  const all = new Set<number>()
                  results.forEach((_, i) => all.add(i))
                  setSelectedResults(all)
                }}
              >
                Select All
              </button>
            </div>
          </div>

          <div className="visual-results-grid">
            {results.map((result, idx) => (
              <div
                key={idx}
                className={`visual-result-card ${selectedResults.has(idx) ? 'selected' : ''}`}
                onClick={() => toggleSelect(idx)}
              >
                {/* Thumbnail */}
                <div className="visual-result-thumb">
                  <img
                    src={`/api/visual/frame-image?path=${encodeURIComponent(result.frame_path)}`}
                    alt={result.description || 'Frame'}
                    loading="lazy"
                    onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
                  />
                  {result.timestamp > 0 && (
                    <span className="visual-result-time">{formatTime(result.timestamp)}</span>
                  )}
                  {(result.similarity_score || result.match_score) && (
                    <span className="visual-result-score">
                      {Math.round((result.similarity_score || result.match_score || 0) * 100)}%
                    </span>
                  )}
                </div>

                {/* Info */}
                <div className="visual-result-info">
                  <div className="visual-result-filename">{result.filename}</div>
                  {result.description && (
                    <div className="visual-result-desc">{result.description.slice(0, 80)}</div>
                  )}
                  {result.tags && result.tags.objects && result.tags.objects.length > 0 && (
                    <div className="tags" style={{ marginTop: 4 }}>
                      {result.tags.objects.slice(0, 4).map((obj, i) => (
                        <span key={i} className="tag">{obj.name}</span>
                      ))}
                    </div>
                  )}
                </div>

                {/* Selection indicator */}
                <div className="visual-result-check">
                  <input
                    type="checkbox"
                    checked={selectedResults.has(idx)}
                    readOnly
                    style={{ accentColor: 'var(--accent)' }}
                  />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Image Preview Modal */}
      {previewImage && (
        <div className="dir-browser-overlay" onClick={() => setPreviewImage(null)}>
          <img
            src={previewImage}
            alt="Preview"
            style={{ maxWidth: '80vw', maxHeight: '80vh', borderRadius: 8 }}
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </div>
  )
}
