"""
Cross-folder MD5 dedup router. Dedupe identical files across Organized +
Dropbox-Snapshot using the existing MD5 indexes. Preview (dry-run) -> purge
(reversible) -> undo.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services import crossdedup as cd

router = APIRouter()


@router.get("/preview")
async def preview(prefer_folder: str = "organized", cross_only: bool = False,
                  snapshot_only: bool = True):
    """DRY RUN — identical-MD5 duplicate groups. snapshot_only=true (default)
    removes ONLY Snapshot files that also exist in Organized; Organized is never
    touched. Shows GB reclaimable."""
    if prefer_folder not in ("organized", "snapshot", "none"):
        raise HTTPException(status_code=400, detail="prefer_folder must be organized|snapshot|none")
    return cd.preview(prefer_folder, cross_only=cross_only, snapshot_only=snapshot_only)


@router.get("/groups")
async def groups(prefer_folder: str = "organized", cross_only: bool = False,
                 snapshot_only: bool = True, offset: int = 0, limit: int = 100,
                 folder_filter: str = ""):
    """Paged review list: each group's keeper + the exact files queued for
    quarantine. snapshot_only=true (default) never queues an Organized file."""
    if prefer_folder not in ("organized", "snapshot", "none"):
        raise HTTPException(status_code=400, detail="prefer_folder must be organized|snapshot|none")
    return cd.groups_page(prefer_folder, cross_only=cross_only,
                          snapshot_only=snapshot_only,
                          offset=offset, limit=limit, folder_filter=folder_filter)


class PurgeRequest(BaseModel):
    prefer_folder: str = "organized"
    cross_only: bool = False
    snapshot_only: bool = True
    confirm: bool = False


@router.post("/purge")
async def purge(request: PurgeRequest):
    """Quarantine redundant duplicate copies. Reversible. snapshot_only=true
    (default) removes only Snapshot files with an Organized twin; Organized is
    never touched."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    try:
        job_id = cd.purge(request.prefer_folder, confirm=True,
                          cross_only=request.cross_only,
                          snapshot_only=request.snapshot_only)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"job_id": job_id, "status": "started"}


@router.get("/status/{job_id}")
async def status(job_id: str):
    job = cd.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class UndoRequest(BaseModel):
    manifest_file: str
    confirm: bool = False


@router.post("/undo")
async def undo(request: UndoRequest):
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = cd.undo(request.manifest_file, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res
