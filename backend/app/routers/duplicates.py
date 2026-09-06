"""
Duplicates router - handles retrieving and managing duplicate file groups.
"""
from fastapi import APIRouter, HTTPException, Query
from ..models.schemas import DuplicatesResponse, DeduplicateRequest, DeduplicateResult
from ..services.file_scanner import get_duplicates, load_persisted_scan
from ..services.deduplicator import deduplicate_files

router = APIRouter()


@router.get("/{scan_id}", response_model=DuplicatesResponse)
async def get_duplicate_groups(
    scan_id: str,
    limit: int = Query(500, description="Max duplicate groups to return (biggest-waste first). Keeps huge scans responsive."),
):
    """Get duplicate file groups from a completed scan. Loads a persisted scan
    from disk if it isn't in memory, and caps the number of groups returned
    (largest wasted space first) so scans with tens of thousands of groups stay
    responsive."""
    # ensure the scan is loaded (persisted store-backed scans load on demand)
    load_persisted_scan(scan_id)
    result = get_duplicates(scan_id, limit=limit)
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
