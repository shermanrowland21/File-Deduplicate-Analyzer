export interface FileInfo {
  path: string;
  filename: string;
  extension: string;
  size: number;
  size_human: string;
  mime_type: string | null;
  hash: string;
  modified_time: string;
  created_time: string;
}

export interface DuplicateGroup {
  hash: string;
  file_count: number;
  total_wasted_space: number;
  total_wasted_space_human: string;
  files: FileInfo[];
}

export interface DuplicatesResponse {
  scan_id: string;
  total_groups: number;
  total_duplicate_files: number;
  total_wasted_space: number;
  total_wasted_space_human: string;
  groups: DuplicateGroup[];
  status?: string;
  in_progress?: boolean;
}

export interface ScanStatus {
  scan_id: string;
  status: string;
  total_files: number;
  processed_files: number;
  duplicates_found: number;
  directory: string;
}

export interface FileMetadata {
  file_path: string;
  filename: string;
  extension: string;
  size: number;
  mime_type: string | null;
  description: string;
  category: string;
  tags: string[];
  suggested_name: string;
  content_summary: string;
  additional_metadata: Record<string, unknown>;
}

export interface NamingConvention {
  template: string;
  date_format: string;
  separator: string;
  case: string;
  max_length: number;
  replace_spaces_with: string;
}

export interface RenamePreview {
  original_path: string;
  original_name: string;
  new_name: string;
  new_path: string;
}

export interface BedrockModel {
  model_id: string;
  model_name: string;
  provider: string;
  supports_images: boolean;
  supports_video: boolean;
  description: string;
}
