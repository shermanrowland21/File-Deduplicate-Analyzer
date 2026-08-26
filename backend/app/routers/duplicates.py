"""
Duplicates router - handles retrieving and managing duplicate file groups.
"""
from fastapi import APIRouter, HTTPException
from ..models.schemas import DuplicatesResponse, DeduplicateRequest, DeduplicateResult
from ..services.file_scanner import get_duplicates
from ..services.deduplicator import deduplicate_files

router = APIRouter()


@router.get("/{scan_id}", response_model=DuplicatesResponse)
async def get_duplicate_groups(scan_id: str):
    """Get all duplicate file groups from a completed scan."""
    result = get_duplicates(scan_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Scan not found or not yet completed",
        )
    return result


@router.post("/deduplicate", response_model=DeduplicateResult)
async def deduplicate(request: DeduplicateRequest):
    """Remove duplicate files based on the specified action."""
    result = deduplicate_files(
        files_to_remove=request.files_to_remove,
        action=request.action.value,
        move_to_folder=request.move_to_folder,
    )
    return result
