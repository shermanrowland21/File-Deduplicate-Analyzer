import { API_BASE } from './config'

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

export const api = {
  // Scanner
  async scanDirectory(directory: string, options?: {
    recursive?: boolean;
    include_hidden?: boolean;
    min_file_size?: number;
    max_file_size?: number;
    file_extensions?: string[];
  }) {
    const response = await fetch(`${API_BASE}/scanner/scan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ directory, ...options }),
    });
    return handleResponse<any>(response);
  },

  async getScanStatus(scanId: string) {
    const response = await fetch(`${API_BASE}/scanner/status/${scanId}`);
    return handleResponse<any>(response);
  },

  // Duplicates
  async getDuplicates(scanId: string) {
    const response = await fetch(`${API_BASE}/duplicates/${scanId}`);
    return handleResponse<any>(response);
  },

  async deduplicate(scanId: string, filesToRemove: string[], action: string, moveToFolder?: string) {
    const response = await fetch(`${API_BASE}/duplicates/deduplicate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scan_id: scanId,
        files_to_remove: filesToRemove,
        action,
        move_to_folder: moveToFolder,
      }),
    });
    return handleResponse<any>(response);
  },

  // Analysis
  async analyzeFile(filePath: string, modelId: string, analysisPrompt?: string) {
    const response = await fetch(`${API_BASE}/analysis/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file_path: filePath,
        model_id: modelId,
        analysis_prompt: analysisPrompt,
      }),
    });
    return handleResponse<any>(response);
  },

  // Renaming
  async previewRename(filePath: string, namingConvention: any, modelId: string, metadata?: any) {
    const response = await fetch(`${API_BASE}/renaming/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file_path: filePath,
        naming_convention: namingConvention,
        model_id: modelId,
        metadata,
      }),
    });
    return handleResponse<any>(response);
  },

  async previewBulkRename(filePaths: string[], namingConvention: any, modelId: string) {
    const response = await fetch(`${API_BASE}/renaming/preview-bulk`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file_paths: filePaths,
        naming_convention: namingConvention,
        model_id: modelId,
      }),
    });
    return handleResponse<any>(response);
  },

  async previewBulkTyped(filePaths: string[], conventions: Record<string, any>, modelId: string) {
    const response = await fetch(`${API_BASE}/renaming/preview-bulk-typed`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_paths: filePaths, conventions, model_id: modelId }),
    });
    return handleResponse<any>(response);
  },

  async applyRenames(renames: any[]) {
    const response = await fetch(`${API_BASE}/renaming/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ renames }),
    });
    return handleResponse<any>(response);
  },

  // Models
  async getModels() {
    const response = await fetch(`${API_BASE}/models/`);
    return handleResponse<any>(response);
  },

  // Health
  async healthCheck() {
    const response = await fetch(`${API_BASE}/health`);
    return handleResponse<any>(response);
  },

  // Dedup Resolver (Phase A)
  async listScans() {
    const response = await fetch(`${API_BASE}/resolver/scans`);
    return handleResponse<any>(response);
  },

  async resolverReview(scanId: string | null, opts: { source_of_truth?: string[]; within_source?: boolean; tiebreak?: string; limit?: number }) {
    const response = await fetch(`${API_BASE}/resolver/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scan_id: scanId, limit: 300, ...opts }),
    });
    return handleResponse<any>(response);
  },

  async resolverExecute(scanId: string | null, opts: { source_of_truth?: string[]; within_source?: boolean; tiebreak?: string; only_paths?: string[] }) {
    const response = await fetch(`${API_BASE}/resolver/execute`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scan_id: scanId, confirm: true, ...opts }),
    });
    return handleResponse<any>(response);
  },

  // LLM advisor (Phase B) — reads actual file content, never guesses
  async adviseStart(scanId: string | null, opts?: { model_id?: string; max_groups?: number; min_size?: number }) {
    const response = await fetch(`${API_BASE}/resolver/advise`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scan_id: scanId, ...opts }),
    });
    return handleResponse<any>(response);
  },

  async adviseStatus(jobId: string) {
    const response = await fetch(`${API_BASE}/resolver/advise-status/${jobId}`);
    return handleResponse<any>(response);
  },

  async adviseStop(jobId: string) {
    const response = await fetch(`${API_BASE}/resolver/advise-stop/${jobId}`, { method: 'POST' });
    return handleResponse<any>(response);
  },

  // Settings — per-function AI model choices
  async getSettings() {
    const response = await fetch(`${API_BASE}/settings/models`);
    return handleResponse<any>(response);
  },

  async setSettingModel(key: string, modelId: string) {
    const response = await fetch(`${API_BASE}/settings/models`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, model_id: modelId }),
    });
    return handleResponse<any>(response);
  },

  async resetSettingModel(key: string) {
    const response = await fetch(`${API_BASE}/settings/models/${key}/reset`, { method: 'POST' });
    return handleResponse<any>(response);
  },

  // Unified Smart Rename (reads real content; images vision+OCR, docs text+OCR;
  // optionally formats the AI's understanding into per-file-type naming conventions)
  async fileNamerAnalyze(directory: string, opts?: { recursive?: boolean; only_illnamed?: boolean; max_files?: number; conventions?: Record<string, any> }) {
    const response = await fetch(`${API_BASE}/filenamer/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ directory, ...opts }),
    });
    return handleResponse<any>(response);
  },

  async fileNamerStatus(jobId: string) {
    const response = await fetch(`${API_BASE}/filenamer/status/${jobId}`);
    return handleResponse<any>(response);
  },

  async fileNamerStop(jobId: string) {
    const response = await fetch(`${API_BASE}/filenamer/stop/${jobId}`, { method: 'POST' });
    return handleResponse<any>(response);
  },

  async fileNamerApply(items: { path: string; proposed_name: string }[]) {
    const response = await fetch(`${API_BASE}/filenamer/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  async fileNamerUndo(manifest: string) {
    const response = await fetch(`${API_BASE}/filenamer/undo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  // List files in a folder (for Smart Rename "load a whole folder")
  async listFilesInFolder(path: string, recursive = false) {
    const response = await fetch(`${API_BASE}/renaming/list-files?path=${encodeURIComponent(path)}&recursive=${recursive}`);
    return handleResponse<any>(response);
  },

  // Resume a previously persisted scan without re-walking the filesystem
  async listPersistedScans() {
    const response = await fetch(`${API_BASE}/scanner/persisted`);
    return handleResponse<any>(response);
  },

  async getDuplicatesLimited(scanId: string, limit = 500) {
    const response = await fetch(`${API_BASE}/duplicates/${scanId}?limit=${limit}`);
    return handleResponse<any>(response);
  },

  async resumeScan(scanId: string) {
    const response = await fetch(`${API_BASE}/scanner/resume/${scanId}`, { method: 'POST' });
    return handleResponse<any>(response);
  },

  // Folder Re-Naming Advisor (reads files inside truncated folders; stays on AWS Bedrock)
  async folderNamerAnalyze(parent: string, opts?: { model_id?: string; only_suspected?: boolean; max_folders?: number }) {
    const response = await fetch(`${API_BASE}/foldernamer/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent, ...opts }),
    });
    return handleResponse<any>(response);
  },

  async folderNamerStatus(jobId: string) {
    const response = await fetch(`${API_BASE}/foldernamer/status/${jobId}`);
    return handleResponse<any>(response);
  },

  async folderNamerStop(jobId: string) {
    const response = await fetch(`${API_BASE}/foldernamer/stop/${jobId}`, { method: 'POST' });
    return handleResponse<any>(response);
  },

  async folderNamerApply(items: { folder: string; proposed_name: string }[]) {
    const response = await fetch(`${API_BASE}/foldernamer/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  async folderNamerUndo(manifest: string) {
    const response = await fetch(`${API_BASE}/foldernamer/undo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  // Folder Reconciler — merge near-name sibling shells, backfill missing content,
  // quarantine empty folders. Optional Google Drive (GAM) validation for
  // authoritative names. Everything reversible (quarantine + manifest).
  async reconcilerScan(parent: string, opts?: { validate_drive?: boolean; admin_user?: string }) {
    const response = await fetch(`${API_BASE}/reconciler/scan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent, ...opts }),
    });
    return handleResponse<any>(response);
  },

  async reconcilerStatus(jobId: string) {
    const response = await fetch(`${API_BASE}/reconciler/status/${jobId}`);
    return handleResponse<any>(response);
  },

  async reconcilerMerge(parent: string, canonical_name: string, member_names: string[]) {
    const response = await fetch(`${API_BASE}/reconciler/merge`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent, canonical_name, member_names, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  async reconcilerDelete(paths: string[]) {
    const response = await fetch(`${API_BASE}/reconciler/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  async reconcilerBackfillDetect(parent: string, folder_names: string[], admin_user?: string) {
    const response = await fetch(`${API_BASE}/reconciler/backfill/detect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent, folder_names, admin_user }),
    });
    return handleResponse<any>(response);
  },

  async reconcilerBackfillDropbox(parent: string, folder_name: string, dropbox_path: string) {
    const response = await fetch(`${API_BASE}/reconciler/backfill/dropbox`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent, folder_name, dropbox_path, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  async reconcilerBackfillDrive(parent: string, folder_name: string, admin_user?: string) {
    const response = await fetch(`${API_BASE}/reconciler/backfill/drive`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent, folder_name, admin_user, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  async reconcilerBackfillDriveStatus(jobId: string) {
    const response = await fetch(`${API_BASE}/reconciler/backfill/drive/status/${jobId}`);
    return handleResponse<any>(response);
  },

  async reconcilerUndo(manifest_file: string) {
    const response = await fetch(`${API_BASE}/reconciler/undo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest_file, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  // Find & Purge by name — search file/folder names by keyword, quarantine
  // matches (reversible). For decommission cleanup (purge old clients).
  async purgeSearch(root: string, keywords: string[], whole_word = false) {
    const response = await fetch(`${API_BASE}/purge/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root, keywords, whole_word }),
    });
    return handleResponse<any>(response);
  },

  async purgeStatus(jobId: string) {
    const response = await fetch(`${API_BASE}/purge/status/${jobId}`);
    return handleResponse<any>(response);
  },

  async purgeQuarantine(root: string, paths: string[]) {
    const response = await fetch(`${API_BASE}/purge/quarantine`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root, paths, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  async purgeUndo(manifest_file: string) {
    const response = await fetch(`${API_BASE}/purge/undo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest_file, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  // Unified filesystem catalog — crawl once, search/purge query the DB instead
  // of re-walking disk. Incremental metadata-only refresh.
  async catalogRoots() {
    const response = await fetch(`${API_BASE}/catalog/roots`);
    return handleResponse<any>(response);
  },

  async catalogRegister(root: string, label?: string) {
    const response = await fetch(`${API_BASE}/catalog/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root, label }),
    });
    return handleResponse<any>(response);
  },

  async catalogRefresh(root?: string) {
    const response = await fetch(`${API_BASE}/catalog/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root: root ?? null }),
    });
    return handleResponse<any>(response);
  },

  async catalogRefreshStatus(jobId: string) {
    const response = await fetch(`${API_BASE}/catalog/refresh-status/${jobId}`);
    return handleResponse<any>(response);
  },

  async catalogStats() {
    const response = await fetch(`${API_BASE}/catalog/stats`);
    return handleResponse<any>(response);
  },

  // Dedup / Routing Advisor (Step 3a)
  async advisorConfig() {
    const response = await fetch(`${API_BASE}/advisor/config`);
    return handleResponse<any>(response);
  },

  async advisorUpdateConfig(patch: Record<string, any>) {
    const response = await fetch(`${API_BASE}/advisor/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    return handleResponse<any>(response);
  },

  async advisorClassify(examples = 15) {
    const response = await fetch(`${API_BASE}/advisor/classify?examples=${examples}`);
    return handleResponse<any>(response);
  },

  async advisorChat(message: string, history: { role: string; content: string }[]) {
    const response = await fetch(`${API_BASE}/advisor/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, history }),
    });
    return handleResponse<any>(response);
  },

  // Cross-folder dedup (Organized vs Dropbox-Snapshot).
  // snapshotOnly=true (default): remove ONLY Snapshot files that also exist in
  // Organized; Organized is never touched.
  async crossdedupGroups(opts: { preferFolder?: string; snapshotOnly?: boolean; offset?: number; limit?: number; folderFilter?: string } = {}) {
    const p = new URLSearchParams({
      prefer_folder: opts.preferFolder ?? 'organized',
      snapshot_only: String(opts.snapshotOnly ?? true),
      offset: String(opts.offset ?? 0),
      limit: String(opts.limit ?? 100),
      folder_filter: opts.folderFilter ?? '',
    });
    const response = await fetch(`${API_BASE}/crossdedup/groups?${p.toString()}`);
    return handleResponse<any>(response);
  },

  async crossdedupPurge(preferFolder: string, snapshotOnly: boolean) {
    const response = await fetch(`${API_BASE}/crossdedup/purge`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prefer_folder: preferFolder, snapshot_only: snapshotOnly, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  async crossdedupStatus(jobId: string) {
    const response = await fetch(`${API_BASE}/crossdedup/status/${jobId}`);
    return handleResponse<any>(response);
  },

  async crossdedupCancel(jobId: string) {
    const response = await fetch(`${API_BASE}/crossdedup/cancel/${jobId}`, { method: 'POST' });
    return handleResponse<any>(response);
  },

  // Keeper guidance (voice-driven rule + AI ambiguity resolution)
  async crossdedupGetRule() {
    const response = await fetch(`${API_BASE}/crossdedup/rule`);
    return handleResponse<any>(response);
  },

  async crossdedupSetRule(patch: Record<string, any>) {
    const response = await fetch(`${API_BASE}/crossdedup/rule`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    return handleResponse<any>(response);
  },

  async crossdedupGuidanceChat(message: string, history: { role: string; content: string }[]) {
    const response = await fetch(`${API_BASE}/crossdedup/guidance/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, history }),
    });
    return handleResponse<any>(response);
  },

  async crossdedupResolveAmbiguous(opts: { guidance?: string; preferFolder?: string; snapshotOnly?: boolean; maxGroups?: number } = {}) {
    const response = await fetch(`${API_BASE}/crossdedup/resolve-ambiguous`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        guidance: opts.guidance ?? '',
        prefer_folder: opts.preferFolder ?? 'organized',
        snapshot_only: opts.snapshotOnly ?? true,
        max_groups: opts.maxGroups ?? 200,
      }),
    });
    return handleResponse<any>(response);
  },

  async crossdedupClearOverrides() {
    const response = await fetch(`${API_BASE}/crossdedup/clear-ai-overrides`, { method: 'POST' });
    return handleResponse<any>(response);
  },

  // System-junk purge (._ sidecars, .DS_Store, Thumbs.db, temp)
  async junkPreview() {
    const response = await fetch(`${API_BASE}/crossdedup/junk/preview`);
    return handleResponse<any>(response);
  },
  async junkPurge() {
    const response = await fetch(`${API_BASE}/crossdedup/junk/purge`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    });
    return handleResponse<any>(response);
  },
  async junkStatus(jobId: string) {
    const response = await fetch(`${API_BASE}/crossdedup/junk/status/${jobId}`);
    return handleResponse<any>(response);
  },
  async junkCancel(jobId: string) {
    const response = await fetch(`${API_BASE}/crossdedup/junk/cancel/${jobId}`, { method: 'POST' });
    return handleResponse<any>(response);
  },
  async junkUndo(manifestFile: string) {
    const response = await fetch(`${API_BASE}/crossdedup/junk/undo`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest_file: manifestFile, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  async crossdedupUndo(manifestFile: string) {
    const response = await fetch(`${API_BASE}/crossdedup/undo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest_file: manifestFile, confirm: true }),
    });
    return handleResponse<any>(response);
  },

  // -pinned Takeout artifact cleanup (within Organized)
  async pinnedPreview(examples = 12) {
    const response = await fetch(`${API_BASE}/reconstruct/pinned/preview?examples=${examples}`);
    return handleResponse<any>(response);
  },

  async pinnedQuarantine() {
    const response = await fetch(`${API_BASE}/reconstruct/pinned/quarantine`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    });
    return handleResponse<any>(response);   // { job_id, status }
  },

  async pinnedQuarantineStatus(jobId: string) {
    const response = await fetch(`${API_BASE}/reconstruct/pinned/quarantine/status/${jobId}`);
    return handleResponse<any>(response);
  },

  async pinnedQuarantineCancel(jobId: string) {
    const response = await fetch(`${API_BASE}/reconstruct/pinned/quarantine/cancel/${jobId}`, { method: 'POST' });
    return handleResponse<any>(response);
  },

  async pinnedRename() {
    const response = await fetch(`${API_BASE}/reconstruct/pinned/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    });
    return handleResponse<any>(response);
  },

  async pinnedUndo(manifestFile: string) {
    const response = await fetch(`${API_BASE}/reconstruct/pinned/undo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest_file: manifestFile, confirm: true }),
    });
    return handleResponse<any>(response);
  },
};
