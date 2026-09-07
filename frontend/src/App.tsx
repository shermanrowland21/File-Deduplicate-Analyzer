import { useState, useEffect } from 'react'
import { track } from './telemetry'
import { AnalysisPanel } from './components/AnalysisPanel'
import { SmartRenamePanel } from './components/SmartRenamePanel'
import { MediaPanel } from './components/MediaPanel'
import { VisualSearchPanel } from './components/VisualSearchPanel'
import { FolderRenamerPanel } from './components/FolderRenamerPanel'
import { PurgeByNamePanel } from './components/PurgeByNamePanel'
import { CrossDedupPanel } from './components/CrossDedupPanel'
import { PinnedCleanupPanel } from './components/PinnedCleanupPanel'
import { DedupAdvisorPanel } from './components/DedupAdvisorPanel'
import { PanelIntro } from './components/PanelIntro'
import { SettingsPanel } from './components/SettingsPanel'

type View = 'media' | 'visual' | 'analysis' | 'renaming' | 'folders' | 'purge' | 'crossdedup' | 'pinned' | 'advisor' | 'settings'

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
  folders: {
    title: 'Folder Renamer',
    what: 'Reads the files INSIDE a folder to figure out what a truncated/garbled folder really is, then proposes a clean descriptive name with a confidence score and evidence. Renames only after you approve — reversible.',
    useWhen: 'you want to fix mangled folder names using their actual content.',
    safety: 'moves',
  },
  purge: {
    title: 'Find & Purge by Name',
    what: 'Search file AND folder names for keywords (e.g. a former client) and quarantine everything that matches, whether duplicated or not.',
    useWhen: 'you want to remove a specific client\'s or topic\'s material by name before a migration.',
    safety: 'reversible',
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
  const [currentView, setCurrentView] = useState<View>('crossdedup')

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

  // Telemetry: log each tab change.
  useEffect(() => {
    track('view_change', { view: currentView })
  }, [currentView])

  const NAV: { view: View; label: string }[] = [
    { view: 'crossdedup', label: 'Cross-Folder Dedup' },
    { view: 'pinned', label: 'Pinned Cleanup' },
    { view: 'advisor', label: 'Dedup Advisor' },
    { view: 'folders', label: 'Folder Renamer' },
    { view: 'purge', label: 'Find & Purge' },
    { view: 'media', label: 'Media Intelligence' },
    { view: 'visual', label: 'Visual Search' },
    { view: 'analysis', label: 'File Analysis' },
    { view: 'renaming', label: 'Smart Rename' },
    { view: 'settings', label: 'Settings' },
  ]

  return (
    <div className="app-container">
      <header className="app-header">
        <h1>File Deduplicate Analyzer</h1>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <button className="theme-toggle" onClick={toggleTheme}
            title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}>
            {theme === 'dark' ? '☀ Light' : '🌙 Dark'}
          </button>
        </div>
      </header>

      <div className="app-content">
        <nav className="sidebar">
          <ul className="sidebar-nav">
            {NAV.map(n => (
              <li
                key={n.view}
                className={currentView === n.view ? 'active' : ''}
                onClick={() => setCurrentView(n.view)}
              >
                {n.label}
              </li>
            ))}
          </ul>
        </nav>

        <main className="main-panel">
          {/* Plain-language explainer for whatever tab is open. */}
          {PANEL_INTRO[currentView] && <PanelIntro {...PANEL_INTRO[currentView]} />}

          {currentView === 'crossdedup' && <CrossDedupPanel />}
          {currentView === 'pinned' && <PinnedCleanupPanel />}
          {currentView === 'advisor' && <DedupAdvisorPanel />}
          {currentView === 'folders' && <FolderRenamerPanel />}
          {currentView === 'purge' && <PurgeByNamePanel />}
          {currentView === 'media' && <MediaPanel />}
          {currentView === 'visual' && <VisualSearchPanel />}
          {currentView === 'analysis' && <AnalysisPanel />}
          {currentView === 'renaming' && <SmartRenamePanel />}
          {currentView === 'settings' && <SettingsPanel />}
        </main>
      </div>
    </div>
  )
}

export default App
