"""
Bulk FILE re-naming advisor router — crawl a directory, propose descriptive
filenames from the ACTUAL content (images via vision+OCR, docs via text +
embedded-image OCR), confidence-ranked, then apply reversibly.

NOTE (local scope): advanced time-based media intelligence (video/audio
transcription, learning knowledge base) lives in the CPMS platform, not here.
Locally, video/audio files are flagged needs_media_analysis rather than processed.
"""
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services import file_namer

router = APIRouter()


class AnalyzeRequest(BaseModel):
    directory: str
    recursive: bool = True
    only_illnamed: bool = True
    max_files: int = 300
    text_model: Optional[str] = None
    vision_model: Optional[str] = None
    # optional per-file-type naming conventions; when present, the AI's
    # understanding is formatted INTO the matching type's convention.
    conventions: Optional[dict] = None


@router.post("/analyze")
async def analyze(request: AnalyzeRequest):
    """Start a background job that proposes descriptive names for files in a
    directory by reading their real content. Confidence-ranked; never guesses."""
    if not os.path.isdir(request.directory):
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.directory}")
    job_id = file_namer.analyze_dir(
        request.directory,
        recursive=request.recursive,
        only_illnamed=request.only_illnamed,
        max_files=request.max_files,
        text_model=request.text_model,      # None → resolved from Settings
        vision_model=request.vision_model,  # None → resolved from Settings
        conventions=request.conventions,    # None → AI descriptive name only
    )
    return {"job_id": job_id, "status": "started",
            "rename_threshold": file_namer.risk_threshold("rename")}


@router.get("/status/{job_id}")
async def status(job_id: str):
    job = file_namer.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/stop/{job_id}")
async def stop(job_id: str):
    if not file_namer.cancel_job(job_id):
        raise HTTPException(status_code=400, detail="Job not found")
    return {"cancelled": True}


class ProposeOneRequest(BaseModel):
    path: str
    text_model: Optional[str] = None
    vision_model: Optional[str] = None


@router.post("/propose")
async def propose_one(request: ProposeOneRequest):
    """Propose a name for a single file (reads its actual content)."""
    if not os.path.isfile(request.path):
        raise HTTPException(status_code=404, detail=f"File not found: {request.path}")
    return file_namer.propose_for_file(
        request.path,
        text_model=request.text_model,      # None → resolved from Settings
        vision_model=request.vision_model,  # None → resolved from Settings
    )


class RenameItem(BaseModel):
    path: str
    proposed_name: str


class ApplyRequest(BaseModel):
    items: list[RenameItem]
    confirm: bool = False


@router.post("/apply")
async def apply(request: ApplyRequest):
    """Apply approved file renames. Collision-safe + reversible (manifest).
    Requires confirm=true. Never overwrites or deletes."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to apply renames")
    items = [i.model_dump() for i in request.items]
    return file_namer.apply_renames(items, confirm=True)


class UndoRequest(BaseModel):
    manifest: str
    confirm: bool = False


@router.post("/undo")
async def undo(request: UndoRequest):
    """Reverse a previous apply using its manifest path."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to undo")
    return file_namer.undo_renames(request.manifest, confirm=True)
