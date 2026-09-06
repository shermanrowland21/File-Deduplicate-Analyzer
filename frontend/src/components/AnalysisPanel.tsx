import { useState, useEffect } from 'react'
import { api } from '../api'
import { BedrockModel, FileMetadata } from '../types'
import { DirectoryBrowser } from './DirectoryBrowser'

export function AnalysisPanel() {
  const [models, setModels] = useState<BedrockModel[]>([])
  const [selectedModel, setSelectedModel] = useState('anthropic.claude-3-5-sonnet-20241022-v2:0')
  const [filePath, setFilePath] = useState('')
  const [customPrompt, setCustomPrompt] = useState('')
  const [analyzing, setAnalyzing] = useState(false)
  const [metadata, setMetadata] = useState<FileMetadata | null>(null)
  const [error, setError] = useState('')
  const [showBrowser, setShowBrowser] = useState(false)
  const [folderFiles, setFolderFiles] = useState<{ path: string; name: string }[]>([])
  const [loadingFolder, setLoadingFolder] = useState(false)

  // After picking a folder in the browser, load its files so the user can pick one.
  const pickFolder = async (folder: string) => {
    setShowBrowser(false)
    setError(''); setLoadingFolder(true); setFolderFiles([])
    try {
      const res = await api.listFilesInFolder(folder, false)
      setFolderFiles(res.files || [])
      if (!res.files || res.files.length === 0) {
        setError('No files directly in that folder (subfolders are not listed here).')
      }
    } catch (err: any) {
      setError(err.message || 'Failed to list folder')
    } finally {
      setLoadingFolder(false)
    }
  }

  useEffect(() => {
    loadModels()
  }, [])

  const loadModels = async () => {
    try {
      const data = await api.getModels()
      setModels(data)
    } catch (err) {
      // Models will be empty, use default
    }
  }

  const handleAnalyze = async () => {
    if (!filePath.trim()) {
      setError('Please enter a file path')
      return
    }

    setError('')
    setAnalyzing(true)
    setMetadata(null)

    try {
      const result = await api.analyzeFile(
        filePath.trim(),
        selectedModel,
        customPrompt || undefined
      )
      setMetadata(result)
    } catch (err: any) {
      setError(err.message || 'Analysis failed')
    } finally {
      setAnalyzing(false)
    }
  }

  return (
    <div>
      <div className="card">
        <div className="card-header">
          <h2>AI File Analysis</h2>
        </div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 16 }}>
          Analyze any file using AWS Bedrock. The AI will examine the file contents and generate
          descriptive metadata including category, tags, and a suggested filename.
        </p>

        {error && <div className="alert alert-error">{error}</div>}

        <div className="form-group">
          <label>File Path</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="text"
              value={filePath}
              onChange={(e) => setFilePath(e.target.value)}
              placeholder="Click Browse to pick a file, or paste a path"
              onKeyDown={(e) => e.key === 'Enter' && handleAnalyze()}
              style={{ flex: 1 }}
            />
            <button className="btn btn-secondary" type="button"
              onClick={() => setShowBrowser(true)} disabled={loadingFolder}>
              {loadingFolder ? <><span className="spinner" /> Loading…</> : 'Browse…'}
            </button>
          </div>
          {folderFiles.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                Pick a file from that folder ({folderFiles.length}):
              </label>
              <select value={filePath} onChange={(e) => setFilePath(e.target.value)}>
                <option value="">— select a file —</option>
                {folderFiles.map((f) => (
                  <option key={f.path} value={f.path}>{f.name}</option>
                ))}
              </select>
            </div>
          )}
        </div>

        <div className="form-group">
          <label>AI Model</label>
          <select value={selectedModel} onChange={(e) => setSelectedModel(e.target.value)}>
            {models.length > 0 ? (
              models.map((model) => (
                <option key={model.model_id} value={model.model_id}>
                  {model.model_name} ({model.provider})
                  {model.supports_images ? ' - Images' : ''}
                  {model.supports_video ? ' + Video' : ''}
                </option>
              ))
            ) : (
              <>
                <option value="anthropic.claude-3-5-sonnet-20241022-v2:0">Claude 3.5 Sonnet v2</option>
                <option value="anthropic.claude-3-5-haiku-20241022-v1:0">Claude 3.5 Haiku</option>
                <option value="anthropic.claude-sonnet-4-20250514-v1:0">Claude Sonnet 4</option>
                <option value="amazon.nova-pro-v1:0">Amazon Nova Pro</option>
                <option value="amazon.nova-lite-v1:0">Amazon Nova Lite</option>
              </>
            )}
          </select>
        </div>

        <div className="form-group">
          <label>Custom Analysis Instructions (optional)</label>
          <textarea
            value={customPrompt}
            onChange={(e) => setCustomPrompt(e.target.value)}
            placeholder="e.g. Focus on identifying the document type and key topics. Extract any dates or names mentioned."
          />
        </div>

        <button
          className="btn btn-primary"
          onClick={handleAnalyze}
          disabled={analyzing}
        >
          {analyzing ? (
            <>
              <span className="spinner" /> Analyzing...
            </>
          ) : (
            'Analyze File'
          )}
        </button>
      </div>

      {/* Model Capabilities */}
      {models.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Model Capabilities</h3>
          </div>
          <table className="file-table">
            <thead>
              <tr>
                <th>Model</th>
                <th>Provider</th>
                <th>Images</th>
                <th>Video</th>
              </tr>
            </thead>
            <tbody>
              {models.map((model) => (
                <tr key={model.model_id}>
                  <td>{model.model_name}</td>
                  <td>{model.provider}</td>
                  <td>{model.supports_images ? '✓' : '—'}</td>
                  <td>{model.supports_video ? '✓' : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Analysis Results */}
      {metadata && (
        <div className="card">
          <div className="card-header">
            <h3>Analysis Results</h3>
          </div>

          <div className="metadata-section">
            <h4>File Info</h4>
            <div className="metadata-row">
              <span className="key">Filename:</span>
              <span className="value">{metadata.filename}</span>
            </div>
            <div className="metadata-row">
              <span className="key">Path:</span>
              <span className="value" style={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>
                {metadata.file_path}
              </span>
            </div>
            <div className="metadata-row">
              <span className="key">Type:</span>
              <span className="value">{metadata.mime_type || metadata.extension}</span>
            </div>
          </div>

          <div className="metadata-section">
            <h4>AI Analysis</h4>
            <div className="metadata-row">
              <span className="key">Description:</span>
              <span className="value">{metadata.description}</span>
            </div>
            <div className="metadata-row">
              <span className="key">Category:</span>
              <span className="value">
                <span className="tag" style={{ background: 'var(--accent)', color: 'white', border: 'none' }}>
                  {metadata.category}
                </span>
              </span>
            </div>
            <div className="metadata-row">
              <span className="key">Suggested Name:</span>
              <span className="value" style={{ color: 'var(--success)', fontWeight: 500 }}>
                {metadata.suggested_name}{metadata.extension}
              </span>
            </div>
            <div className="metadata-row">
              <span className="key">Tags:</span>
              <span className="value">
                <div className="tags">
                  {metadata.tags.map((tag, i) => (
                    <span key={i} className="tag">{tag}</span>
                  ))}
                </div>
              </span>
            </div>
          </div>

          {metadata.content_summary && (
            <div className="metadata-section">
              <h4>Content Summary</h4>
              <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', lineHeight: 1.7 }}>
                {metadata.content_summary}
              </p>
            </div>
          )}

          {metadata.additional_metadata && Object.keys(metadata.additional_metadata).length > 0 && (
            <div className="metadata-section">
              <h4>Additional Metadata</h4>
              {Object.entries(metadata.additional_metadata).map(([key, value]) => (
                <div key={key} className="metadata-row">
                  <span className="key">{key}:</span>
                  <span className="value">{String(value)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {showBrowser && (
        <DirectoryBrowser
          onSelect={(path) => pickFolder(path)}
          onClose={() => setShowBrowser(false)}
        />
      )}
    </div>
  )
}
