"""
Scanner router - handles directory scanning for duplicate detection.
Scan runs in background; frontend polls /status/{scan_id} for progress.
"""
from fastapi import APIRouter, HTTPException
from ..models.schemas import ScanRequest
from ..services.file_scanner import scan_directory, get_scan_status, get_cache_info, clear_cache, cancel_scan

router = APIRouter()


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
