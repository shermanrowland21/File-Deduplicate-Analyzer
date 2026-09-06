"""
Full-MD5 index router. Builds/queries a whole-file MD5 index for large files
(the videos the dedup scanner only fingerprinted). Foundation for the
reconstruct-to-Drive folder reconciler.
"""
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services import md5_index as mi

router = APIRouter()


class BuildRequest(BaseModel):
    root: str
    min_size_mb: int = 50


@router.post("/build")
async def build(request: BuildRequest):
    """Start a resumable background job computing full MD5 for every file
    >= min_size_mb under root, storing them in an indexed SQLite DB."""
    if not os.path.isdir(request.root):
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.root}")
    try:
        job_id = mi.build_index(request.root, min_size=request.min_size_mb * 1024 * 1024)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"job_id": job_id, "status": "started", "root": request.root,
            "min_size_mb": request.min_size_mb}


@router.get("/status/{job_id}")
async def status(job_id: str):
    job = mi.get_build_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/jobs")
async def jobs():
    return {"jobs": mi.list_jobs()}


@router.post("/stop/{job_id}")
async def stop(job_id: str):
    if not mi.cancel_build(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"cancelled": True}


@router.get("/lookup")
async def lookup(md5: str, root: str):
    """Find local paths whose content MD5 matches (within the index for root)."""
    idx = mi.open_index(root)
    try:
        return {"md5": md5, "matches": idx.find_by_md5(md5)}
    finally:
        idx.close()


@router.get("/stats")
async def stats(root: str):
    idx = mi.open_index(root)
    try:
        return idx.stats()
    finally:
        idx.close()
