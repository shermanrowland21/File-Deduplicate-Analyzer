import { useState, useMemo } from 'react'
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
  const [filterSource, setFilterSource] = useState<string>('')
  const [filterSubfolder, setFilterSubfolder] = useState<string>('')

  // Extract unique sources and subfolders from all duplicate files
  const { sources, subfolders } = useMemo(() => {
    if (!duplicates) return { sources: [], subfolders: [] }
    const srcSet = new Set<string>()
    const subSet = new Set<string>()
    for (const group of duplicates.groups) {
      for (const file of group.files) {
        const src = (file as any).source
        const sub = (file as any).subfolder
        if (src) srcSet.add(src)
        if (sub) subSet.add(`${src}/${sub}`)
      }
    }
    return {
      sources: Array.from(srcSet).sort(),
      subfolders: Array.from(subSet).sort(),
    }
  }, [duplicates])

  if (!duplicates || duplicates.total_groups === 0) {
    return (
      <div className="empty-state">
        {duplicates?.in_progress ? (
          <>
            <h3><span className="spinner" style={{ width: 16, height: 16, marginRight: 8, verticalAlign: 'middle' }} />Scan in progress…</h3>
            <p>Duplicates will appear here as they're found. This list updates live.</p>
          </>
        ) : (
          <>
            <h3>No Duplicates Found</h3>
            <p>Scan a directory first to find duplicate files, or the last scan found no duplicates.</p>
          </>
        )}
      </div>
    )
  }

  const toggleGroup = (hash: string) => {
    const next = new Set(expandedGroups)
    if (next.has(hash)) next.delete(hash)
    else next.add(hash)
    setExpandedGroups(next)
  }

  const toggleFile = (path: string) => {
    const next = new Set(selectedFiles)
    if (next.has(path)) next.delete(path)
    else next.add(path)
    setSelectedFiles(next)
  }

  const selectAllExceptFirst = (group: DuplicateGroup) => {
    const next = new Set(selectedFiles)
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

  // Bulk select by source: select all files from a specific source that have duplicates elsewhere
  const selectBySource = (source: string, mode: 'remove' | 'keep') => {
    const next = new Set(selectedFiles)
    for (const group of duplicates.groups) {
      const fromSource = group.files.filter((f: any) => f.source === source)
      const fromOther = group.files.filter((f: any) => f.source !== source)

      if (mode === 'remove') {
        // Remove copies from this source (keep copies from other sources)
        if (fromOther.length > 0) {
          fromSource.forEach((f) => next.add(f.path))
          fromOther.forEach((f) => next.delete(f.path))
        }
      } else {
        // Keep this source, remove from others
        if (fromSource.length > 0) {
          fromOther.forEach((f) => next.add(f.path))
          fromSource.forEach((f) => next.delete(f.path))
        }
      }
    }
    setSelectedFiles(next)
  }

  // Bulk select by subfolder path
  const selectBySubfolder = (sourceSubfolder: string, mode: 'remove' | 'keep') => {
    const next = new Set(selectedFiles)
    for (const group of duplicates.groups) {
      const matching = group.files.filter((f: any) => {
        const key = `${f.source}/${f.subfolder}`
        return key === sourceSubfolder || key.startsWith(sourceSubfolder + '/')
      })
      const others = group.files.filter((f: any) => {
        const key = `${f.source}/${f.subfolder}`
        return key !== sourceSubfolder && !key.startsWith(sourceSubfolder + '/')
      })

      if (mode === 'remove') {
        if (others.length > 0) {
          matching.forEach((f) => next.add(f.path))
          others.forEach((f) => next.delete(f.path))
        }
      } else {
        if (matching.length > 0) {
          others.forEach((f) => next.add(f.path))
          matching.forEach((f) => next.delete(f.path))
        }
      }
    }
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

      {/* Bulk Actions by Source */}
      {sources.length > 1 && (
        <div className="card">
          <div className="card-header">
            <h3>Bulk Actions by Source</h3>
          </div>
          <p style={{ color: 'var(--text-secondary)', fontSize: '0.8rem', marginBottom: 12 }}>
            Select which source to keep or remove when duplicates exist across sources.
          </p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {sources.map((src) => (
              <div key={src} style={{
                display: 'flex', alignItems: 'center', gap: 6,
                background: 'var(--bg-tertiary)', padding: '8px 12px',
                borderRadius: 6, border: '1px solid var(--border)',
              }}>
                <span style={{ fontWeight: 600, fontSize: '0.85rem', marginRight: 8 }}>{src}</span>
                <button
                  className="btn btn-primary btn-sm"
                  onClick={() => selectBySource(src, 'keep')}
                  style={{ fontSize: '0.7rem', padding: '3px 8px' }}
                >
                  Keep These
                </button>
                <button
                  className="btn btn-danger btn-sm"
                  onClick={() => selectBySource(src, 'remove')}
                  style={{ fontSize: '0.7rem', padding: '3px 8px' }}
                >
                  Remove These
                </button>
              </div>
            ))}
          </div>

          {/* Subfolder-level actions */}
          {subfolders.length > 0 && (
            <details style={{ marginTop: 12 }}>
              <summary style={{ cursor: 'pointer', fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                Fine-grained: by subfolder ({subfolders.length} folders)
              </summary>
              <div style={{ marginTop: 8, maxHeight: 200, overflowY: 'auto' }}>
                {subfolders.map((sub) => (
                  <div key={sub} style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '4px 8px', borderBottom: '1px solid var(--border)',
                    fontSize: '0.8rem',
                  }}>
                    <span style={{
                      flex: 1, overflow: 'hidden', textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap', fontFamily: 'monospace', fontSize: '0.75rem',
                    }}>
                      {sub}
                    </span>
                    <button
                      className="btn btn-primary btn-sm"
                      onClick={() => selectBySubfolder(sub, 'keep')}
                      style={{ fontSize: '0.65rem', padding: '2px 6px' }}
                    >
                      Keep
                    </button>
                    <button
                      className="btn btn-danger btn-sm"
                      onClick={() => selectBySubfolder(sub, 'remove')}
                      style={{ fontSize: '0.65rem', padding: '2px 6px' }}
                    >
                      Remove
                    </button>
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      )}

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
                placeholder="E:\duplicates_backup"
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

      {/* Filter */}
      {sources.length > 1 && (
        <div style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
          <select
            value={filterSource}
            onChange={(e) => setFilterSource(e.target.value)}
            style={{ padding: '6px 10px', background: 'var(--bg-tertiary)', border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text-primary)', fontSize: '0.8rem' }}
          >
            <option value="">All Sources</option>
            {sources.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>
      )}

      {/* Duplicate Groups */}
      <div style={{ marginTop: 8 }}>
        {duplicates.groups.map((group) => {
          // Apply source filter
          if (filterSource) {
            const hasSource = group.files.some((f: any) => f.source === filterSource)
            if (!hasSource) return null
          }

          return (
            <div key={group.hash} className="dup-group">
              <div className="dup-group-header" onClick={() => toggleGroup(group.hash)}>
                <div className="dup-group-info">
                  <span className="count">{group.file_count} copies</span>
                  <span className="wasted">Wasting: {group.total_wasted_space_human}</span>
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                    {group.files[0]?.filename}
                  </span>
                  {/* Show which sources this group spans */}
                  {sources.length > 1 && (
                    <span style={{ fontSize: '0.7rem', color: 'var(--accent)' }}>
                      [{[...new Set(group.files.map((f: any) => f.source).filter(Boolean))].join(' + ')}]
                    </span>
                  )}
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
                        {sources.length > 1 && <th>Source</th>}
                        <th>Path</th>
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
                          {sources.length > 1 && (
                            <td>
                              <span style={{
                                fontSize: '0.7rem', padding: '2px 6px',
                                background: 'var(--bg-primary)', borderRadius: 4,
                                color: 'var(--accent)', fontWeight: 500,
                              }}>
                                {(file as any).source}
                              </span>
                              {(file as any).subfolder && (
                                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginTop: 2 }}>
                                  {(file as any).subfolder}
                                </div>
                              )}
                            </td>
                          )}
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
          )
        })}
      </div>
    </div>
  )
}
