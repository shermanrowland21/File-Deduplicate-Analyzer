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
async def preview(prefer_folder: str = "organized"):
    """DRY RUN — identical-MD5 duplicate groups across/within the two folders,
    with keeper chosen by prefer_folder + tiebreak. Shows GB reclaimable."""
    if prefer_folder not in ("organized", "snapshot", "none"):
        raise HTTPException(status_code=400, detail="prefer_folder must be organized|snapshot|none")
    return cd.preview(prefer_folder)


class PurgeRequest(BaseModel):
    prefer_folder: str = "organized"
    confirm: bool = False


@router.post("/purge")
async def purge(request: PurgeRequest):
    """Quarantine redundant duplicate copies (keep one per group). Reversible."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    try:
        job_id = cd.purge(request.prefer_folder, confirm=True)
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
