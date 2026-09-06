import { useState, useEffect } from 'react'
import { track } from './telemetry'
import { ScanPanel } from './components/ScanPanel'
import { DuplicatesPanel } from './components/DuplicatesPanel'
import { AnalysisPanel } from './components/AnalysisPanel'
import { SmartRenamePanel } from './components/SmartRenamePanel'
import { MediaPanel } from './components/MediaPanel'
import { VisualSearchPanel } from './components/VisualSearchPanel'
import { ExtractPanel } from './components/ExtractPanel'
import { ResolverPanel } from './components/ResolverPanel'
import { FolderRenamerPanel } from './components/FolderRenamerPanel'
import { FolderReconcilerPanel } from './components/FolderReconcilerPanel'
import { PurgeByNamePanel } from './components/PurgeByNamePanel'
import { SettingsPanel } from './components/SettingsPanel'
import { useScanJob } from './useScanJob'

type View = 'scan' | 'duplicates' | 'resolver' | 'extract' | 'media' | 'visual' | 'analysis' | 'renaming' | 'folders' | 'reconciler' | 'purge' | 'settings'

function App() {
  const [currentView, setCurrentView] = useState<View>('scan')

  // Theme: 'dark' (default) or 'light'. Persisted to localStorage and applied
  // to <html data-theme> so all the CSS variables re-skin the whole app.
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    const saved = localStorage.getItem('theme')
    return saved === 'light' ? 'light' : 'dark'
  })
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('theme', theme)
  }, [theme])
  const toggleTheme = () => setTheme(t => (t === 'dark' ? 'light' : 'dark'))

  // Scan job lives at the App level so it SURVIVES tab switches and keeps
  // polling in the background regardless of which view is shown.
  const scan = useScanJob()
  const duplicates = scan.duplicates

  // Telemetry: log every view change with a snapshot of scan state, so the
  // server log shows exactly what the UI is doing on each tab switch.
  useEffect(() => {
    track('view_change', {
      view: currentView,
      scanning: scan.scanning,
      scan_id: scan.progress?.scan_id ?? null,
      processed: scan.progress?.processed_files ?? null,
      duplicates_found: scan.progress?.duplicates_found ?? null,
      duplicate_groups: duplicates?.total_groups ?? null,
    })
  }, [currentView])

  // Telemetry: log scan status transitions (start/running/complete/blank).
  useEffect(() => {
    track('scan_state', {
      scanning: scan.scanning,
      status: scan.progress?.status ?? null,
      scan_id: scan.progress?.scan_id ?? null,
      processed: scan.progress?.processed_files ?? null,
      duplicates_found: scan.progress?.duplicates_found ?? null,
      dup_groups: duplicates?.total_groups ?? null,
      error: scan.error || null,
    })
  }, [scan.scanning, scan.progress?.status, duplicates?.total_groups])

  return (
    <div className="app-container">
      <header className="app-header">
        <h1>File Deduplicate Analyzer</h1>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <span className="status">
            {scan.progress
              ? `${scan.scanning ? 'Scanning' : 'Last scan'}: ${scan.progress.directory}` +
                (scan.scanning ? ` — ${scan.progress.processed_files.toLocaleString()} hashed, ${scan.progress.duplicates_found.toLocaleString()} dupes` : '')
              : 'No scan active'}
          </span>
          <button className="theme-toggle" onClick={toggleTheme}
            title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}>
            {theme === 'dark' ? '☀ Light' : '🌙 Dark'}
          </button>
        </div>
      </header>

      <div className="app-content">
        <nav className="sidebar">
          <ul className="sidebar-nav">
            <li
              className={currentView === 'scan' ? 'active' : ''}
              onClick={() => setCurrentView('scan')}
            >
              Scan Directory
              {scan.scanning && (
                <span className="spinner" style={{ marginLeft: 8, width: 12, height: 12, verticalAlign: 'middle' }} title="Scan running in background" />
              )}
            </li>
            <li
              className={currentView === 'duplicates' ? 'active' : ''}
              onClick={() => setCurrentView('duplicates')}
            >
              Duplicates
              {duplicates && duplicates.total_groups > 0 && (
                <span style={{ marginLeft: 8, fontSize: '0.75rem', color: 'var(--warning)' }}>
                  ({duplicates.total_groups}{duplicates.in_progress ? '…' : ''})
                </span>
              )}
            </li>
            <li
              className={currentView === 'resolver' ? 'active' : ''}
              onClick={() => setCurrentView('resolver')}
            >
              Resolver
            </li>
            <li
              className={currentView === 'folders' ? 'active' : ''}
              onClick={() => setCurrentView('folders')}
            >
              Folder Renamer
            </li>
            <li
              className={currentView === 'reconciler' ? 'active' : ''}
              onClick={() => setCurrentView('reconciler')}
            >
              Folder Reconciler
            </li>
            <li
              className={currentView === 'purge' ? 'active' : ''}
              onClick={() => setCurrentView('purge')}
            >
              Find &amp; Purge
            </li>
            <li
              className={currentView === 'extract' ? 'active' : ''}
              onClick={() => setCurrentView('extract')}
            >
              Extract Archives
            </li>
            <li
              className={currentView === 'media' ? 'active' : ''}
              onClick={() => setCurrentView('media')}
            >
              Media Intelligence
            </li>
            <li
              className={currentView === 'visual' ? 'active' : ''}
              onClick={() => setCurrentView('visual')}
            >
              Visual Search
            </li>
            <li
              className={currentView === 'analysis' ? 'active' : ''}
              onClick={() => setCurrentView('analysis')}
            >
              File Analysis
            </li>
            <li
              className={currentView === 'renaming' ? 'active' : ''}
              onClick={() => setCurrentView('renaming')}
            >
              Smart Rename
            </li>
            <li
              className={currentView === 'settings' ? 'active' : ''}
              onClick={() => setCurrentView('settings')}
            >
              Settings
            </li>
          </ul>
        </nav>

        <main className="main-panel">
          {/* Keep ScanPanel MOUNTED across tab switches (hidden, not unmounted)
              so its state/polling context persists. The scan job itself lives
              in the App-level hook, but staying mounted avoids any flicker. */}
          <div style={{ display: currentView === 'scan' ? 'block' : 'none' }}>
            <ScanPanel scan={scan} />
          </div>
          {currentView === 'duplicates' && (
            <DuplicatesPanel
              duplicates={duplicates}
              scanId={scan.progress?.scan_id || null}
              scanning={scan.scanning}
            />
          )}
          {currentView === 'resolver' && <ResolverPanel />}
          {currentView === 'folders' && <FolderRenamerPanel />}
          {currentView === 'reconciler' && <FolderReconcilerPanel />}
          {currentView === 'purge' && <PurgeByNamePanel />}
          {currentView === 'settings' && <SettingsPanel />}
          {currentView === 'extract' && <ExtractPanel />}
          {currentView === 'media' && <MediaPanel />}
          {currentView === 'visual' && <VisualSearchPanel />}
          {currentView === 'analysis' && <AnalysisPanel />}
          {currentView === 'renaming' && <SmartRenamePanel />}
        </main>
      </div>
    </div>
  )
}

export default App
