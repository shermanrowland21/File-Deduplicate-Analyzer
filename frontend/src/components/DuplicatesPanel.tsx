import { useState } from 'react'
import { api } from '../api'
import { DuplicatesResponse, DuplicateGroup } from '../types'

interface DuplicatesPanelProps {
  duplicates: DuplicatesResponse | null
  scanId: string | null
}

export function DuplicatesPanel({ duplicates, scanId }: DuplicatesPanelProps) {
  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set())
  const [action, setAction] = useState('move_to_trash')
  const [moveToFolder, setMoveToFolder] = useState('')
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set())
  const [processing, setProcessing] = useState(false)
  const [result, setResult] = useState<any>(null)
  const [error, setError] = useState('')

  if (!duplicates || duplicates.total_groups === 0) {
    return (
      <div className="empty-state">
        <h3>No Duplicates Found</h3>
        <p>Scan a directory first to find duplicate files, or the last scan found no duplicates.</p>
      </div>
    )
  }

  const toggleGroup = (hash: string) => {
    const next = new Set(expandedGroups)
    if (next.has(hash)) {
      next.delete(hash)
    } else {
      next.add(hash)
    }
    setExpandedGroups(next)
  }

  const toggleFile = (path: string) => {
    const next = new Set(selectedFiles)
    if (next.has(path)) {
      next.delete(path)
    } else {
      next.add(path)
    }
    setSelectedFiles(next)
  }

  const selectAllExceptFirst = (group: DuplicateGroup) => {
    const next = new Set(selectedFiles)
    // Keep the first file, select the rest
    group.files.slice(1).forEach((f) => next.add(f.path))
    setSelectedFiles(next)
  }

  const deselectGroup = (group: DuplicateGroup) => {
    const next = new Set(selectedFiles)
    group.files.forEach((f) => next.delete(f.path))
    setSelectedFiles(next)
  }

  const selectAllDuplicates = () => {
    const next = new Set(selectedFiles)
    duplicates.groups.forEach((group) => {
      group.files.slice(1).forEach((f) => next.add(f.path))
    })
    setSelectedFiles(next)
  }

  const handleDeduplicate = async () => {
    if (selectedFiles.size === 0) {
      setError('No files selected for deduplication')
      return
    }

    if (action === 'move_to_folder' && !moveToFolder.trim()) {
      setError('Please specify a destination folder')
      return
    }

    setError('')
    setProcessing(true)
    setResult(null)

    try {
      const res = await api.deduplicate(
        scanId || '',
        Array.from(selectedFiles),
        action,
        action === 'move_to_folder' ? moveToFolder : undefined
      )
      setResult(res)
      if (res.success) {
        setSelectedFiles(new Set())
      }
    } catch (err: any) {
      setError(err.message || 'Deduplication failed')
    } finally {
      setProcessing(false)
    }
  }

  return (
    <div>
      {/* Summary Stats */}
      <div className="stats-grid">
        <div className="stat-card">
          <div className="value">{duplicates.total_groups}</div>
          <div className="label">Duplicate Groups</div>
        </div>
        <div className="stat-card">
          <div className="value">{duplicates.total_duplicate_files}</div>
          <div className="label">Duplicate Files</div>
        </div>
        <div className="stat-card">
          <div className="value" style={{ color: 'var(--warning)' }}>
            {duplicates.total_wasted_space_human}
          </div>
          <div className="label">Wasted Space</div>
        </div>
        <div className="stat-card">
          <div className="value" style={{ color: 'var(--danger)' }}>
            {selectedFiles.size}
          </div>
          <div className="label">Selected for Removal</div>
        </div>
      </div>

      {/* Actions Bar */}
      <div className="card">
        <div className="card-header">
          <h3>Deduplication Actions</h3>
          <button className="btn btn-secondary btn-sm" onClick={selectAllDuplicates}>
            Select All Duplicates (Keep First)
          </button>
        </div>

        {error && <div className="alert alert-error">{error}</div>}
        {result && result.success && (
          <div className="alert alert-success">
            Removed {result.files_removed} files, freed {result.space_freed_human}
          </div>
        )}

        <div className="form-row">
          <div className="form-group">
            <label>Action</label>
            <select value={action} onChange={(e) => setAction(e.target.value)}>
              <option value="move_to_trash">Move to Trash</option>
              <option value="delete">Permanently Delete</option>
              <option value="move_to_folder">Move to Folder</option>
            </select>
          </div>
          {action === 'move_to_folder' && (
            <div className="form-group">
              <label>Destination Folder</label>
              <input
                type="text"
                value={moveToFolder}
                onChange={(e) => setMoveToFolder(e.target.value)}
                placeholder="C:\duplicates_backup"
              />
            </div>
          )}
        </div>

        <button
          className="btn btn-danger"
          onClick={handleDeduplicate}
          disabled={processing || selectedFiles.size === 0}
        >
          {processing ? (
            <>
              <span className="spinner" /> Processing...
            </>
          ) : (
            `Remove ${selectedFiles.size} Selected Files`
          )}
        </button>
      </div>

      {/* Duplicate Groups */}
      <div style={{ marginTop: 16 }}>
        {duplicates.groups.map((group) => (
          <div key={group.hash} className="dup-group">
            <div className="dup-group-header" onClick={() => toggleGroup(group.hash)}>
              <div className="dup-group-info">
                <span className="count">{group.file_count} copies</span>
                <span className="wasted">Wasting: {group.total_wasted_space_human}</span>
                <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                  {group.files[0]?.filename}
                </span>
              </div>
              <div className="btn-group">
                <button
                  className="btn btn-secondary btn-sm"
                  onClick={(e) => { e.stopPropagation(); selectAllExceptFirst(group) }}
                >
                  Select Dupes
                </button>
                <button
                  className="btn btn-secondary btn-sm"
                  onClick={(e) => { e.stopPropagation(); deselectGroup(group) }}
                >
                  Deselect
                </button>
                <span style={{ fontSize: '1.2rem', color: 'var(--text-muted)' }}>
                  {expandedGroups.has(group.hash) ? '▾' : '▸'}
                </span>
              </div>
            </div>

            {expandedGroups.has(group.hash) && (
              <div className="dup-group-body">
                <table className="file-table">
                  <thead>
                    <tr>
                      <th style={{ width: 40 }}>Keep</th>
                      <th>File Path</th>
                      <th>Size</th>
                      <th>Modified</th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.files.map((file, idx) => (
                      <tr key={file.path}>
                        <td>
                          <input
                            type="checkbox"
                            checked={!selectedFiles.has(file.path)}
                            onChange={() => toggleFile(file.path)}
                            title={selectedFiles.has(file.path) ? 'Will be removed' : 'Will be kept'}
                            style={{ accentColor: selectedFiles.has(file.path) ? 'var(--danger)' : 'var(--success)' }}
                          />
                        </td>
                        <td className="path-cell" title={file.path}>
                          {idx === 0 && !selectedFiles.has(file.path) && (
                            <span style={{ color: 'var(--success)', marginRight: 6, fontSize: '0.7rem' }}>
                              KEEP
                            </span>
                          )}
                          {selectedFiles.has(file.path) && (
                            <span style={{ color: 'var(--danger)', marginRight: 6, fontSize: '0.7rem' }}>
                              REMOVE
                            </span>
                          )}
                          {file.path}
                        </td>
                        <td>{file.size_human}</td>
                        <td style={{ fontSize: '0.8rem' }}>
                          {new Date(file.modified_time).toLocaleDateString()}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
