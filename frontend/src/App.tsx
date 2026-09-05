import { useState } from 'react'
import { ScanPanel } from './components/ScanPanel'
import { DuplicatesPanel } from './components/DuplicatesPanel'
import { AnalysisPanel } from './components/AnalysisPanel'
import { RenamingPanel } from './components/RenamingPanel'
import { MediaPanel } from './components/MediaPanel'
import { VisualSearchPanel } from './components/VisualSearchPanel'
import { ExtractPanel } from './components/ExtractPanel'
import { useScanJob } from './useScanJob'

type View = 'scan' | 'duplicates' | 'extract' | 'media' | 'visual' | 'analysis' | 'renaming'

function App() {
  const [currentView, setCurrentView] = useState<View>('scan')
  // Scan job lives at the App level so it SURVIVES tab switches and keeps
  // polling in the background regardless of which view is shown.
  const scan = useScanJob()
  const duplicates = scan.duplicates

  return (
    <div className="app-container">
      <header className="app-header">
        <h1>File Deduplicate Analyzer</h1>
        <span className="status">
          {scan.progress
            ? `${scan.scanning ? 'Scanning' : 'Last scan'}: ${scan.progress.directory}` +
              (scan.scanning ? ` — ${scan.progress.processed_files.toLocaleString()} hashed, ${scan.progress.duplicates_found.toLocaleString()} dupes` : '')
            : 'No scan active'}
        </span>
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
            />
          )}
          {currentView === 'extract' && <ExtractPanel />}
          {currentView === 'media' && <MediaPanel />}
          {currentView === 'visual' && <VisualSearchPanel />}
          {currentView === 'analysis' && <AnalysisPanel />}
          {currentView === 'renaming' && <RenamingPanel />}
        </main>
      </div>
    </div>
  )
}

export default App
