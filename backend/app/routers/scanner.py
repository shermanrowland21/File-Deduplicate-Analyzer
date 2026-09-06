"""
Scanner router - handles directory scanning for duplicate detection.
Scan runs in background; frontend polls /status/{scan_id} for progress.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..models.schemas import ScanRequest
from ..services.file_scanner import scan_directory, get_scan_status, get_cache_info, clear_cache, cancel_scan, get_active_scan

router = APIRouter()


@router.get("/active")
async def active_scan():
    """Return the currently running scan (if any) so the UI can re-attach after
    a tab switch or reload. Returns {active: false} when nothing is running."""
    status = get_active_scan()
    if status is None:
        return {"active": False}
    return {"active": True, **status}


@router.post("/from-cache")
async def build_from_cache(request: ScanRequest):
    """Build a dedup-ready SQLite scan DIRECTLY from existing hash caches for the
    given directories — no filesystem walk, no re-hashing. Instantly makes an
    already-hashed tree (e.g. Dropbox) available for deduplication while a live
    scan of other directories runs. Returns the new scan_id."""
    from ..services.file_scanner import build_store_from_cache
    directories = request.directories or ([request.directory] if request.directory else [])
    if not directories:
        raise HTTPException(status_code=400, detail="Provide 'directory' or 'directories'")
    scan_id = build_store_from_cache(directories)
    if scan_id is None:
        raise HTTPException(status_code=404, detail="No hash cache found for those directories")
    return {"scan_id": scan_id, "status": "completed", "source": "hash_cache"}


class MergeRequest(BaseModel):
    scan_ids: list[str]


@router.post("/merge")
async def merge_scans_endpoint(request: MergeRequest):
    """Merge several finished scans into ONE unified scan for CROSS-SOURCE dedup
    (e.g. Dropbox scan + Google Drive scan -> find files living in both). No
    re-scan / re-hash. Returns the new merged scan_id; use it with the Resolver
    or /api/duplicates/{scan_id}."""
    from ..services.file_scanner import merge_scans
    if not request.scan_ids or len(request.scan_ids) < 1:
        raise HTTPException(status_code=400, detail="Provide scan_ids to merge")
    result = merge_scans(request.scan_ids)
    if result is None:
        raise HTTPException(status_code=404, detail="None of the given scans have a store to merge")
    return result


@router.get("/persisted")
async def persisted_scans():
    """List completed/checkpointed scans saved to disk, newest first, so the UI
    can offer 'resume last scan' without re-walking the filesystem."""
    from ..services.file_scanner import list_persisted_scans, latest_scan_id
    return {"latest": latest_scan_id(), "scans": list_persisted_scans()}


@router.post("/resume/{scan_id}")
async def resume_scan(scan_id: str):
    """Load a previously persisted scan's results back into memory WITHOUT any
    filesystem walk or re-hashing, and return its status. The duplicates are then
    available via /api/duplicates/{scan_id}. This is the 'close & reopen, go
    straight to duplicates' path."""
    from ..services.file_scanner import load_persisted_scan, get_scan_status
    if not load_persisted_scan(scan_id):
        raise HTTPException(status_code=404, detail="No persisted scan with that id")
    status = get_scan_status(scan_id)
    if status is None:
        raise HTTPException(status_code=500, detail="Failed to load persisted scan")
    return {"resumed": True, **status}


@router.post("/scan")
async def start_scan(request: ScanRequest):
    """Start scanning one or more directories for duplicate files. Returns immediately with scan_id."""
    # Support both single directory and multiple directories
    directories = []
    if request.directories:
        directories = request.directories
    elif request.directory:
        directories = [request.directory]
    else:
        raise HTTPException(status_code=400, detail="Provide 'directory' or 'directories'")

    scan_id = scan_directory(
        directories=directories,
        recursive=request.recursive,
        include_hidden=request.include_hidden,
        min_file_size=request.min_file_size,
        max_file_size=request.max_file_size,
        file_extensions=request.file_extensions,
    )

    status = get_scan_status(scan_id)
    if status is None:
        raise HTTPException(status_code=500, detail="Failed to start scan")

    return status


@router.get("/status/{scan_id}")
async def get_status(scan_id: str):
    """Get the current progress of a scan (poll this endpoint)."""
    status = get_scan_status(scan_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    return status


@router.get("/cache-info")
async def cache_info(directory: str):
    """Get info about cached scan data for a directory."""
    info = get_cache_info(directory)
    if info is None:
        return {"has_cache": False}
    return {"has_cache": True, **info}


@router.delete("/cache")
async def delete_cache(directory: str):
    """Clear the scan cache for a directory (forces full rescan next time)."""
    cleared = clear_cache(directory)
    return {"cleared": cleared}


@router.post("/stop/{scan_id}")
async def stop_scan(scan_id: str):
    """Stop a running scan. Results collected so far are preserved."""
    cancelled = cancel_scan(scan_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Scan not running or not found")
    return {"cancelled": True, "scan_id": scan_id}
