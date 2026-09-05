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
};
