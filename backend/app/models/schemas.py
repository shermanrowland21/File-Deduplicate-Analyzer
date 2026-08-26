"""
Pydantic models for request/response schemas
"""
from pydantic import BaseModel
from typing import Optional
from enum import Enum


class ScanRequest(BaseModel):
    directory: str
    recursive: bool = True
    include_hidden: bool = False
    min_file_size: int = 0  # bytes
    max_file_size: Optional[int] = None  # bytes
    file_extensions: Optional[list[str]] = None  # filter by extension


class ScanStatus(BaseModel):
    scan_id: str
    status: str  # "running", "completed", "error"
    total_files: int
    processed_files: int
    duplicates_found: int
    directory: str


class FileInfo(BaseModel):
    path: str
    filename: str
    extension: str
    size: int
    size_human: str
    mime_type: Optional[str] = None
    hash: str
    modified_time: str
    created_time: str


class DuplicateGroup(BaseModel):
    hash: str
    file_count: int
    total_wasted_space: int
    total_wasted_space_human: str
    files: list[FileInfo]


class DuplicatesResponse(BaseModel):
    scan_id: str
    total_groups: int
    total_duplicate_files: int
    total_wasted_space: int
    total_wasted_space_human: str
    groups: list[DuplicateGroup]


class DeduplicateAction(str, Enum):
    DELETE = "delete"
    MOVE_TO_TRASH = "move_to_trash"
    MOVE_TO_FOLDER = "move_to_folder"


class DeduplicateRequest(BaseModel):
    scan_id: str
    files_to_remove: list[str]  # file paths to remove
    action: DeduplicateAction = DeduplicateAction.MOVE_TO_TRASH
    move_to_folder: Optional[str] = None  # required if action is MOVE_TO_FOLDER


class DeduplicateResult(BaseModel):
    success: bool
    files_processed: int
    files_removed: int
    space_freed: int
    space_freed_human: str
    errors: list[str]


class AnalysisRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    file_path: str
    model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    analysis_prompt: Optional[str] = None  # custom analysis instructions


class FileMetadata(BaseModel):
    file_path: str
    filename: str
    extension: str
    size: int
    mime_type: Optional[str] = None
    description: str
    category: str
    tags: list[str]
    suggested_name: str
    content_summary: str
    additional_metadata: dict = {}


class NamingConvention(BaseModel):
    template: str  # e.g. "{date}_{category}_{description}.{ext}"
    date_format: str = "%Y-%m-%d"
    separator: str = "_"
    case: str = "lower"  # "lower", "upper", "title", "original"
    max_length: int = 255
    replace_spaces_with: str = "_"


class RenameRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    file_path: str
    naming_convention: NamingConvention
    metadata: Optional[FileMetadata] = None
    model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"


class RenamePreview(BaseModel):
    original_path: str
    original_name: str
    new_name: str
    new_path: str


class BulkRenameRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    file_paths: list[str]
    naming_convention: NamingConvention
    model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"


class BulkRenamePreview(BaseModel):
    previews: list[RenamePreview]
    errors: list[str]


class ApplyRenameRequest(BaseModel):
    renames: list[RenamePreview]


class ApplyRenameResult(BaseModel):
    success: bool
    files_renamed: int
    errors: list[str]


class BedrockModel(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_id: str
    model_name: str
    provider: str
    supports_images: bool
    supports_video: bool
    description: str
