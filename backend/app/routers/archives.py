"""
Archive extraction router - extract Google Takeout zips and other archives.
"""
import os
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from ..services.archive_extractor import (
    find_archives, extract_archives, get_extraction_job, cancel_extraction,
)

router = APIRouter()


class ExtractRequest(BaseModel):
    source_dir: str  # directory containing archive files
    output_dir: Optional[str] = None  # where to extract (default: source_dir/extracted)
    archives: Optional[list[str]] = None  # specific archive paths (default: all in source_dir)


@router.get("/find")
async def find_archive_files(directory: str = Query(...)):
    """Find all archive files (zip, tar, gz) in a directory."""
    if not os.path.exists(directory):
        raise HTTPException(status_code=404, detail=f"Directory not found: {directory}")
    archives = find_archives(directory)
    total_size = sum(a["size"] for a in archives)
    return {
        "directory": directory,
        "archives": archives,
        "total_count": len(archives),
        "total_size": total_size,
        "total_size_human": _human_size(total_size),
    }


@router.post("/extract")
async def start_extraction(request: ExtractRequest):
    """
    Start extracting archives. Handles Google Takeout multi-part zips.
    Runs in background — poll /status/{job_id} for progress.
    """
    if not os.path.exists(request.source_dir):
        raise HTTPException(status_code=404, detail=f"Directory not found: {request.source_dir}")

    job_id = extract_archives(
        source_dir=request.source_dir,
        output_dir=request.output_dir,
        archives=request.archives,
    )

    return {"job_id": job_id, "status": "started"}


@router.get("/status/{job_id}")
async def extraction_status(job_id: str):
    """Get extraction progress."""
    job = get_extraction_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/stop/{job_id}")
async def stop_extraction(job_id: str):
    """Stop a running extraction."""
    cancelled = cancel_extraction(job_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Extraction not running or not found")
    return {"cancelled": True}


def _human_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"
