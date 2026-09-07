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
import { CrossDedupPanel } from './components/CrossDedupPanel'
import { PinnedCleanupPanel } from './components/PinnedCleanupPanel'
import { DedupAdvisorPanel } from './components/DedupAdvisorPanel'
import { PanelIntro } from './components/PanelIntro'
import { SettingsPanel } from './components/SettingsPanel'
import { useScanJob } from './useScanJob'

type View = 'scan' | 'duplicates' | 'resolver' | 'extract' | 'media' | 'visual' | 'analysis' | 'renaming' | 'folders' | 'reconciler' | 'purge' | 'crossdedup' | 'pinned' | 'advisor' | 'settings'

// Plain-language "what this screen does" text shown at the top of every tab, so
// there's never a wall of options without an explanation.
type Safety = 'safe' | 'reversible' | 'moves' | 'readonly'
const PANEL_INTRO: Record<View, { title: string; what: string; useWhen?: string; safety: Safety; startHere?: boolean }> = {
  crossdedup: {
    title: 'Cross-Folder Dedup',
    what: 'Finds files that exist in BOTH the Google Drive "Organized" folder (your master) and the Dropbox-Snapshot copy, and removes the redundant Snapshot copy. Organized is never touched. Files unique to Snapshot are kept.',
    useWhen: 'you want to clean the Dropbox-Snapshot of anything already safe in Organized. This is the main dedup step. Purge the "System junk" at the top first.',
    safety: 'reversible', startHere: true,
  },
  pinned: {
    title: 'Pinned Artifact Cleanup',
    what: 'Google Takeout baked version tags into filenames like "name-at-2024...-pinned.pdf". This removes that clutter inside Organized — quarantining redundant copies and renaming the ones worth keeping. Keeper choice is folder-aware and you can guide it by voice.',
    useWhen: 'you want to clear the ugly "-pinned" filenames out of the Organized tree.',
    safety: 'reversible',
  },
  advisor: {
    title: 'Dedup / Routing Advisor',
    what: 'A voice/chat assistant where you state cleanup principles ("terminated employees\' files are duplicates", "media goes to CPMS", "China folders go to China SharePoint") and it turns them into rules, showing how every file would be deduped and routed.',
    useWhen: 'you want to set the big-picture rules and see where everything is headed (CPMS / SharePoint) before acting.',
    safety: 'readonly',
  },
  scan: {
    title: 'Scan Directory',
    what: 'Walks a folder and hashes files to find duplicates within that one location. This is the original single-folder scanner.',
    useWhen: 'you want a quick duplicate scan of one specific folder (not the big cross-folder job).',
    safety: 'readonly',
  },
  duplicates: {
    title: 'Duplicates',
    what: 'Shows duplicate groups found by the Scan Directory tool, with options to remove copies.',
    useWhen: 'you have run a scan and want to review/act on what it found.',
    safety: 'reversible',
  },
  resolver: {
    title: 'Resolver',
    what: 'A card-based review of duplicate groups with keeper rules and bulk selection — an alternate, more manual way to work through duplicates.',
    useWhen: 'you want to hand-review duplicate groups one by one. For the bulk Dropbox cleanup, use Cross-Folder Dedup instead.',
    safety: 'reversible',
  },
  folders: {
    title: 'Folder Renamer',
    what: 'AI-assisted renaming of folders to cleaner, more descriptive names.',
    useWhen: 'you want to tidy up folder names. Not part of the core dedup flow.',
    safety: 'moves',
  },
  reconciler: {
    title: 'Folder Reconciler',
    what: 'Compares your local folders against the authoritative Google Drive structure (via GAM) and merges split/empty folders created by the Takeout export.',
    useWhen: 'you are repairing the mangled Organized folder structure against what Google Drive actually has.',
    safety: 'reversible',
  },
  purge: {
    title: 'Find & Purge by Name',
    what: 'Search file AND folder names for keywords (e.g. a former client) and quarantine everything that matches, whether duplicated or not.',
    useWhen: 'you want to remove a specific client\'s or topic\'s material by name before a migration.',
    safety: 'reversible',
  },
  extract: {
    title: 'Extract Archives',
    what: 'Point at a folder of Google Takeout .zip files and it extracts them all (handles multi-part exports). Optionally deletes each zip after extraction to save space.',
    useWhen: 'you have raw Takeout zips to unpack. You have likely already done this step.',
    safety: 'moves',
  },
  media: {
    title: 'Media Intelligence',
    what: 'Runs AI analysis on video/audio/images — transcripts, keyframes, topics, descriptions — and stores it for search. This is the groundwork for the CPMS media system.',
    useWhen: 'you want to analyze media content. Heavy AI processing; not needed for basic dedup.',
    safety: 'readonly',
  },
  visual: {
    title: 'Visual Search',
    what: 'Search your analyzed media by image similarity or description using AI embeddings.',
    useWhen: 'you have run Media Intelligence and want to find media by what it looks like or contains.',
    safety: 'readonly',
  },
  analysis: {
    title: 'File Analysis',
    what: 'Point at a single file and get an AI description, category, tags, and a suggested name.',
    useWhen: 'you want to understand or label one specific file.',
    safety: 'readonly',
  },
  renaming: {
    title: 'Smart Rename',
    what: 'AI-driven bulk renaming of files using content analysis and a naming template you define.',
    useWhen: 'you want to systematically rename a batch of poorly-named files.',
    safety: 'moves',
  },
  settings: {
    title: 'Settings',
    what: 'Choose which AWS Bedrock AI model each function uses, and switch light/dark theme. All models stay inside your AWS account.',
    useWhen: 'you want to change the AI model for a feature or adjust the theme.',
    safety: 'safe',
  },
}

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
              className={currentView === 'crossdedup' ? 'active' : ''}
              onClick={() => setCurrentView('crossdedup')}
            >
              Cross-Folder Dedup
            </li>
            <li
              className={currentView === 'pinned' ? 'active' : ''}
              onClick={() => setCurrentView('pinned')}
            >
              Pinned Cleanup
            </li>
            <li
              className={currentView === 'advisor' ? 'active' : ''}
              onClick={() => setCurrentView('advisor')}
            >
              Dedup Advisor
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
          {/* Plain-language explainer for whatever tab is open. */}
          {PANEL_INTRO[currentView] && <PanelIntro {...PANEL_INTRO[currentView]} />}

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
          {currentView === 'crossdedup' && <CrossDedupPanel />}
          {currentView === 'pinned' && <PinnedCleanupPanel />}
          {currentView === 'advisor' && <DedupAdvisorPanel />}
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
