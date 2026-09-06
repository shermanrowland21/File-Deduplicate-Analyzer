"""
Folder Re-Naming Advisor router — analyze a parent directory's subfolders,
propose corrected names from evidence inside each folder (cheap Bedrock model,
stays on AWS), and apply approved renames reversibly.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services import folder_namer

router = APIRouter()


class AnalyzeRequest(BaseModel):
    parent: str                              # the folder whose SUBFOLDERS to fix
    model_id: Optional[str] = None           # defaults to cheap DeepSeek-on-Bedrock
    only_suspected: bool = True              # limit to truncated-looking names
    max_folders: int = 500


@router.post("/analyze")
async def analyze(request: AnalyzeRequest):
    """Start a background job proposing corrected names for the subfolders of
    `parent`. Confidence-ranked; reads real file evidence, never guesses."""
    import os
    if not os.path.isdir(request.parent):
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.parent}")
    model = request.model_id or folder_namer._default_model()
    job_id = folder_namer.analyze_parent(
        request.parent, model_id=model,
        only_suspected=request.only_suspected, max_folders=request.max_folders)
    return {"job_id": job_id, "status": "started", "model": model}


@router.get("/status/{job_id}")
async def status(job_id: str):
    job = folder_namer.get_analyze_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/stop/{job_id}")
async def stop(job_id: str):
    if not folder_namer.cancel_analyze(job_id):
        raise HTTPException(status_code=400, detail="Job not found")
    return {"cancelled": True}


class ProposeOneRequest(BaseModel):
    folder: str
    model_id: Optional[str] = None


@router.post("/propose")
async def propose_one(request: ProposeOneRequest):
    """Propose a name for a single folder (reads its evidence)."""
    import os
    if not os.path.isdir(request.folder):
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.folder}")
    return folder_namer.propose_for_folder(
        request.folder, model_id=request.model_id or folder_namer._default_model())


class RenameItem(BaseModel):
    folder: str
    proposed_name: str


class ApplyRequest(BaseModel):
    items: list[RenameItem]
    confirm: bool = False


@router.post("/apply")
async def apply(request: ApplyRequest):
    """Apply approved folder renames. Collision-safe + reversible (manifest).
    Requires confirm=true. Never overwrites or deletes."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to apply renames")
    items = [i.model_dump() for i in request.items]
    return folder_namer.apply_renames(items, confirm=True)


class UndoRequest(BaseModel):
    manifest: str
    confirm: bool = False


@router.post("/undo")
async def undo(request: UndoRequest):
    """Reverse a previous apply using its manifest path."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to undo")
    return folder_namer.undo_renames(request.manifest, confirm=True)
