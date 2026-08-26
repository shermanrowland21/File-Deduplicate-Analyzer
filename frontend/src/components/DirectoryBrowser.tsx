import { useState, useEffect } from 'react'

interface DirectoryEntry {
  name: string
  path: string
  has_children: boolean
}

interface DirectoryBrowserProps {
  onSelect: (path: string) => void
  onClose: () => void
}

export function DirectoryBrowser({ onSelect, onClose }: DirectoryBrowserProps) {
  const [currentPath, setCurrentPath] = useState<string | null>(null)
  const [parentPath, setParentPath] = useState<string | null>(null)
  const [directories, setDirectories] = useState<DirectoryEntry[]>([])
  const [drives, setDrives] = useState<{ name: string; path: string }[]>([])
  const [shortcuts, setShortcuts] = useState<{ name: string; path: string }[]>([])
  const [fileCount, setFileCount] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    loadDrives()
    loadShortcuts()
  }, [])

  const loadDrives = async () => {
    try {
      const res = await fetch('/api/browser/drives')
      if (!res.ok) {
        setError(`Backend not reachable (HTTP ${res.status}). Make sure the backend is running on port 8000.`)
        return
      }
      const data = await res.json()
      setDrives(data.drives || [])
      // Auto-navigate to first drive
      if (data.drives && data.drives.length > 0) {
        navigateTo(data.drives[0].path)
      }
    } catch (err: any) {
      setError(`Cannot connect to backend: ${err.message || 'Network error'}. Is the backend running?`)
    }
  }

  const loadShortcuts = async () => {
    try {
      const res = await fetch('/api/browser/quick-access')
      if (res.ok) {
        const data = await res.json()
        setShortcuts(data.shortcuts || [])
      }
    } catch {
      // Non-critical
    }
  }

  const navigateTo = async (path: string) => {
    setLoading(true)
    setError('')
    try {
      const res = await fetch(`/api/browser/list?path=${encodeURIComponent(path)}`)
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Failed to load directory')
      }
      const data = await res.json()
      setCurrentPath(data.path)
      setParentPath(data.parent)
      setDirectories(data.directories || [])
      setFileCount(data.file_count || 0)
    } catch (err: any) {
      setError(err.message || 'Failed to browse directory')
    } finally {
      setLoading(false)
    }
  }

  const handleSelect = () => {
    if (currentPath) {
      onSelect(currentPath)
    }
  }

  return (
    <div className="dir-browser-overlay" onClick={onClose}>
      <div className="dir-browser" onClick={(e) => e.stopPropagation()}>
        <div className="dir-browser-header">
          <h3>Browse to Directory</h3>
          <button className="btn btn-secondary btn-sm" onClick={onClose}>✕</button>
        </div>

        {/* Quick Access */}
        <div className="dir-browser-shortcuts">
          {shortcuts.map((s) => (
            <button
              key={s.path}
              className="shortcut-btn"
              onClick={() => navigateTo(s.path)}
              title={s.path}
            >
              {s.name}
            </button>
          ))}
          {drives.length > 1 && drives.map((d) => (
            <button
              key={d.path}
              className="shortcut-btn shortcut-drive"
              onClick={() => navigateTo(d.path)}
            >
              {d.name}
            </button>
          ))}
        </div>

        {/* Current Path */}
        <div className="dir-browser-path">
          <span className="path-label">Path:</span>
          <span className="path-value">{currentPath || '...'}</span>
          {fileCount > 0 && (
            <span className="file-count">{fileCount} files here</span>
          )}
        </div>

        {error && <div className="alert alert-error" style={{ margin: '8px 16px' }}>{error}</div>}

        {/* Directory Listing */}
        <div className="dir-browser-list">
          {/* Up button */}
          {parentPath && parentPath !== currentPath && (
            <div
              className="dir-entry dir-entry-up"
              onClick={() => navigateTo(parentPath)}
            >
              <span className="dir-icon">⬆</span>
              <span className="dir-name">..</span>
            </div>
          )}

          {loading ? (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)' }}>
              <span className="spinner" /> Loading...
            </div>
          ) : directories.length === 0 ? (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)' }}>
              No subdirectories
            </div>
          ) : (
            directories.map((dir) => (
              <div
                key={dir.path}
                className="dir-entry"
                onClick={() => navigateTo(dir.path)}
              >
                <span className="dir-icon">{dir.has_children ? '📂' : '📁'}</span>
                <span className="dir-name">{dir.name}</span>
                {dir.has_children && <span className="dir-arrow">›</span>}
              </div>
            ))
          )}
        </div>

        {/* Footer with select button */}
        <div className="dir-browser-footer">
          <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
          <button
            className="btn btn-primary"
            onClick={handleSelect}
            disabled={!currentPath}
          >
            Select This Folder
          </button>
        </div>
      </div>
    </div>
  )
}
