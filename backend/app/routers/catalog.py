"""
Unified filesystem catalog router. Register roots, refresh incrementally
(metadata-only), and search file/folder names instantly from the catalog
instead of re-walking disk.
"""
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services import catalog as cat

router = APIRouter()


class RegisterRequest(BaseModel):
    root: str
    label: Optional[str] = None


@router.post("/register")
async def register(request: RegisterRequest):
    if not os.path.isdir(request.root):
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.root}")
    try:
        return cat.register_root(request.root, request.label)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class RefreshRequest(BaseModel):
    root: Optional[str] = None   # None = refresh all registered roots


@router.post("/refresh")
async def refresh(request: RefreshRequest):
    """Start an incremental, metadata-only refresh (no file reads)."""
    if request.root and not os.path.isdir(request.root):
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.root}")
    job_id = cat.refresh(request.root)
    return {"job_id": job_id, "status": "started", "root": request.root or "ALL"}


@router.get("/refresh-status/{job_id}")
async def refresh_status(job_id: str):
    job = cat.get_refresh_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/refresh-stop/{job_id}")
async def refresh_stop(job_id: str):
    if not cat.cancel_refresh(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"cancelled": True}


@router.get("/roots")
async def roots():
    return {"roots": cat.list_roots()}


@router.get("/stats")
async def stats():
    return cat.stats()


class SearchRequest(BaseModel):
    keywords: list[str]
    whole_word: bool = False
    roots: Optional[list[str]] = None


@router.post("/search")
async def search(request: SearchRequest):
    """Instant name search across the catalog (file AND folder names)."""
    kws = [k for k in (request.keywords or []) if k and k.strip()]
    if not kws:
        raise HTTPException(status_code=400, detail="At least one keyword required")
    res = cat.search(kws, whole_word=request.whole_word, roots=request.roots)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res
