import { useState } from 'react'
import { ScanPanel } from './components/ScanPanel'
import { DuplicatesPanel } from './components/DuplicatesPanel'
import { AnalysisPanel } from './components/AnalysisPanel'
import { RenamingPanel } from './components/RenamingPanel'
import { MediaPanel } from './components/MediaPanel'
import { DuplicatesResponse, ScanStatus } from './types'

type View = 'scan' | 'duplicates' | 'media' | 'analysis' | 'renaming'

function App() {
  const [currentView, setCurrentView] = useState<View>('scan')
  const [scanResult, setScanResult] = useState<ScanStatus | null>(null)
  const [duplicates, setDuplicates] = useState<DuplicatesResponse | null>(null)

  return (
    <div className="app-container">
      <header className="app-header">
        <h1>File Deduplicate Analyzer</h1>
        <span className="status">
          {scanResult ? `Last scan: ${scanResult.directory}` : 'No scan active'}
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
            </li>
            <li
              className={currentView === 'duplicates' ? 'active' : ''}
              onClick={() => setCurrentView('duplicates')}
            >
              Duplicates
              {duplicates && duplicates.total_groups > 0 && (
                <span style={{ marginLeft: 8, fontSize: '0.75rem', color: 'var(--warning)' }}>
                  ({duplicates.total_groups})
                </span>
              )}
            </li>
            <li
              className={currentView === 'media' ? 'active' : ''}
              onClick={() => setCurrentView('media')}
            >
              Media Intelligence
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
          {currentView === 'scan' && (
            <ScanPanel
              onScanComplete={(status, dups) => {
                setScanResult(status)
                setDuplicates(dups)
                if (dups && dups.total_groups > 0) {
                  setCurrentView('duplicates')
                }
              }}
            />
          )}
          {currentView === 'duplicates' && (
            <DuplicatesPanel
              duplicates={duplicates}
              scanId={scanResult?.scan_id || null}
            />
          )}
          {currentView === 'media' && <MediaPanel />}
          {currentView === 'analysis' && <AnalysisPanel />}
          {currentView === 'renaming' && <RenamingPanel />}
        </main>
      </div>
    </div>
  )
}

export default App
