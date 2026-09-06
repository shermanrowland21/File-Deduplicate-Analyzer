"""
Find & Purge by name router. Search file/folder names by keyword(s), then
quarantine the matches (reversible). For decommission cleanup (purge old
clients before S3 migration).
"""
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services import name_purge as np

router = APIRouter()


class SearchRequest(BaseModel):
    root: str
    keywords: list[str]
    whole_word: bool = False


@router.post("/search")
async def search(request: SearchRequest):
    """Start a background search for files/folders whose name matches any keyword."""
    if not os.path.isdir(request.root):
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.root}")
    kws = [k for k in (request.keywords or []) if k and k.strip()]
    if not kws:
        raise HTTPException(status_code=400, detail="At least one keyword required")
    job_id = np.search(request.root, kws, whole_word=request.whole_word)
    return {"job_id": job_id, "status": "started"}


@router.get("/status/{job_id}")
async def status(job_id: str):
    job = np.get_search_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/stop/{job_id}")
async def stop(job_id: str):
    if not np.cancel_search(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"cancelled": True}


class QuarantineRequest(BaseModel):
    root: str
    paths: list[str]
    confirm: bool = False


@router.post("/quarantine")
async def quarantine(request: QuarantineRequest):
    """Move selected matched files/folders to a dated _PurgeQuarantine at root.
    Reversible via /undo. Never hard-deletes."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to quarantine")
    res = np.quarantine(request.paths, request.root, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class UndoRequest(BaseModel):
    manifest_file: str
    confirm: bool = False


@router.post("/undo")
async def undo(request: UndoRequest):
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to undo")
    res = np.undo(request.manifest_file, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res
