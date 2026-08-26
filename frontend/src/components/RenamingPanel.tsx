import { useState, useEffect } from 'react'
import { api } from '../api'
import { NamingConvention, RenamePreview, BedrockModel } from '../types'

export function RenamingPanel() {
  const [models, setModels] = useState<BedrockModel[]>([])
  const [selectedModel, setSelectedModel] = useState('anthropic.claude-3-5-sonnet-20241022-v2:0')
  const [filePaths, setFilePaths] = useState('')
  const [convention, setConvention] = useState<NamingConvention>({
    template: '{date}_{category}_{suggested_name}.{ext}',
    date_format: '%Y-%m-%d',
    separator: '_',
    case: 'lower',
    max_length: 255,
    replace_spaces_with: '_',
  })
  const [previews, setPreviews] = useState<RenamePreview[]>([])
  const [loading, setLoading] = useState(false)
  const [applying, setApplying] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  useEffect(() => {
    loadModels()
  }, [])

  const loadModels = async () => {
    try {
      const data = await api.getModels()
      setModels(data)
    } catch (err) {
      // Use defaults
    }
  }

  const handlePreview = async () => {
    const paths = filePaths.split('\n').map((p) => p.trim()).filter(Boolean)
    if (paths.length === 0) {
      setError('Please enter at least one file path')
      return
    }

    setError('')
    setSuccess('')
    setLoading(true)
    setPreviews([])

    try {
      if (paths.length === 1) {
        const result = await api.previewRename(paths[0], convention, selectedModel)
        setPreviews([result])
      } else {
        const result = await api.previewBulkRename(paths, convention, selectedModel)
        setPreviews(result.previews || [])
        if (result.errors && result.errors.length > 0) {
          setError(`Some files had errors: ${result.errors.join(', ')}`)
        }
      }
    } catch (err: any) {
      setError(err.message || 'Preview generation failed')
    } finally {
      setLoading(false)
    }
  }

  const handleApply = async () => {
    if (previews.length === 0) return

    setError('')
    setSuccess('')
    setApplying(true)

    try {
      const result = await api.applyRenames(previews)
      if (result.success) {
        setSuccess(`Successfully renamed ${result.files_renamed} files`)
        setPreviews([])
      } else {
        setError(`Renamed ${result.files_renamed} files, but had errors: ${result.errors?.join(', ')}`)
      }
    } catch (err: any) {
      setError(err.message || 'Rename failed')
    } finally {
      setApplying(false)
    }
  }

  return (
    <div>
      <div className="card">
        <div className="card-header">
          <h2>Smart File Renaming</h2>
        </div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 16 }}>
          Define a naming convention template, and the AI will analyze each file to generate
          descriptive filenames that follow your convention.
        </p>

        {error && <div className="alert alert-error">{error}</div>}
        {success && <div className="alert alert-success">{success}</div>}

        {/* Naming Convention Editor */}
        <div style={{ marginBottom: 24 }}>
          <h3 style={{ fontSize: '1rem', marginBottom: 12 }}>Naming Convention</h3>

          <div className="form-group">
            <label>Template</label>
            <input
              type="text"
              value={convention.template}
              onChange={(e) => setConvention({ ...convention, template: e.target.value })}
              placeholder="{date}_{category}_{suggested_name}.{ext}"
              style={{ fontFamily: 'monospace' }}
            />
          </div>

          <div className="template-help">
            <strong>Available variables:</strong><br />
            <code>{'{date}'}</code> File date &nbsp;|&nbsp;
            <code>{'{category}'}</code> AI category &nbsp;|&nbsp;
            <code>{'{description}'}</code> Short description &nbsp;|&nbsp;
            <code>{'{suggested_name}'}</code> AI-suggested name &nbsp;|&nbsp;
            <code>{'{tags}'}</code> Top tags &nbsp;|&nbsp;
            <code>{'{ext}'}</code> Extension &nbsp;|&nbsp;
            <code>{'{original}'}</code> Original name &nbsp;|&nbsp;
            <code>{'{mime}'}</code> MIME category &nbsp;|&nbsp;
            <code>{'{hash}'}</code> File hash (8 chars) &nbsp;|&nbsp;
            <code>{'{counter}'}</code> Counter
          </div>

          <div className="form-row-3" style={{ marginTop: 16 }}>
            <div className="form-group">
              <label>Date Format</label>
              <select
                value={convention.date_format}
                onChange={(e) => setConvention({ ...convention, date_format: e.target.value })}
              >
                <option value="%Y-%m-%d">2024-01-15</option>
                <option value="%Y%m%d">20240115</option>
                <option value="%m-%d-%Y">01-15-2024</option>
                <option value="%d-%m-%Y">15-01-2024</option>
                <option value="%Y">2024 (year only)</option>
                <option value="%Y-%m">2024-01 (year-month)</option>
              </select>
            </div>
            <div className="form-group">
              <label>Case</label>
              <select
                value={convention.case}
                onChange={(e) => setConvention({ ...convention, case: e.target.value })}
              >
                <option value="lower">lowercase</option>
                <option value="upper">UPPERCASE</option>
                <option value="title">Title Case</option>
                <option value="original">Original</option>
              </select>
            </div>
            <div className="form-group">
              <label>Separator</label>
              <select
                value={convention.separator}
                onChange={(e) => setConvention({ ...convention, separator: e.target.value })}
              >
                <option value="_">Underscore (_)</option>
                <option value="-">Hyphen (-)</option>
                <option value=".">Period (.)</option>
                <option value=" ">Space</option>
              </select>
            </div>
          </div>

          <div className="form-row" style={{ marginTop: 0 }}>
            <div className="form-group">
              <label>Replace Spaces With</label>
              <select
                value={convention.replace_spaces_with}
                onChange={(e) => setConvention({ ...convention, replace_spaces_with: e.target.value })}
              >
                <option value="_">Underscore (_)</option>
                <option value="-">Hyphen (-)</option>
                <option value="">Remove</option>
              </select>
            </div>
            <div className="form-group">
              <label>Max Filename Length</label>
              <input
                type="number"
                value={convention.max_length}
                onChange={(e) => setConvention({ ...convention, max_length: parseInt(e.target.value) || 255 })}
              />
            </div>
          </div>
        </div>

        {/* File Selection */}
        <div style={{ marginBottom: 24 }}>
          <h3 style={{ fontSize: '1rem', marginBottom: 12 }}>Files to Rename</h3>

          <div className="form-group">
            <label>AI Model</label>
            <select value={selectedModel} onChange={(e) => setSelectedModel(e.target.value)}>
              {models.length > 0 ? (
                models.map((model) => (
                  <option key={model.model_id} value={model.model_id}>
                    {model.model_name} ({model.provider})
                  </option>
                ))
              ) : (
                <>
                  <option value="anthropic.claude-3-5-sonnet-20241022-v2:0">Claude 3.5 Sonnet v2</option>
                  <option value="anthropic.claude-3-5-haiku-20241022-v1:0">Claude 3.5 Haiku</option>
                  <option value="anthropic.claude-sonnet-4-20250514-v1:0">Claude Sonnet 4</option>
                  <option value="amazon.nova-pro-v1:0">Amazon Nova Pro</option>
                </>
              )}
            </select>
          </div>

          <div className="form-group">
            <label>File Paths (one per line)</label>
            <textarea
              value={filePaths}
              onChange={(e) => setFilePaths(e.target.value)}
              placeholder={"C:\\Users\\username\\Documents\\IMG_4523.jpg\nC:\\Users\\username\\Documents\\document1.pdf\nC:\\Users\\username\\Documents\\screenshot_2024.png"}
              style={{ minHeight: 120 }}
            />
          </div>

          <div className="btn-group">
            <button
              className="btn btn-primary"
              onClick={handlePreview}
              disabled={loading}
            >
              {loading ? (
                <>
                  <span className="spinner" /> Generating Previews...
                </>
              ) : (
                'Preview Renames'
              )}
            </button>
          </div>
        </div>
      </div>

      {/* Preview Results */}
      {previews.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Rename Preview</h3>
            <button
              className="btn btn-primary"
              onClick={handleApply}
              disabled={applying}
            >
              {applying ? (
                <>
                  <span className="spinner" /> Applying...
                </>
              ) : (
                `Apply ${previews.length} Renames`
              )}
            </button>
          </div>

          <div className="alert alert-warning">
            Review the previews below before applying. Renames are performed on the actual files.
          </div>

          {previews.map((preview, idx) => (
            <div key={idx} className="rename-preview">
              <div className="original">
                {preview.original_name}
              </div>
              <div className="new-name">
                → {preview.new_name}
              </div>
              <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: 4 }}>
                {preview.new_path}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
