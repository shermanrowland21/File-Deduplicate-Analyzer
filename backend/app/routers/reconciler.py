"""
Folder Reconciler router — reconcile the subfolders of one chosen folder under
Organized/ against each other and (optionally) the authoritative Google Drive
structure (via GAM). Merge near-name sibling shells, backfill missing content,
quarantine truly-empty folders. Everything reversible.
"""
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services import folder_reconciler as fr

router = APIRouter()


class ScanRequest(BaseModel):
    parent: str                              # folder whose SUBFOLDERS to reconcile
    validate_drive: bool = False             # cross-check against Google Drive (GAM)
    admin_user: Optional[str] = None


@router.post("/scan")
async def scan(request: ScanRequest):
    """Start a background scan classifying `parent`'s subfolders into
    merge-groups / backfill-candidates / ok."""
    if not os.path.isdir(request.parent):
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.parent}")
    admin = request.admin_user or fr.DEFAULT_ADMIN_USER
    job_id = fr.scan(request.parent, validate_drive=request.validate_drive,
                     admin_user=admin)
    return {"job_id": job_id, "status": "started",
            "validate_drive": request.validate_drive}


@router.get("/status/{job_id}")
async def status(job_id: str):
    job = fr.get_scan_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class MergeRequest(BaseModel):
    parent: str
    canonical_name: str
    member_names: list[str]
    confirm: bool = False


@router.post("/merge")
async def merge(request: MergeRequest):
    """Merge near-name sibling subfolders into one clean canonical folder,
    collision-safe, quarantining emptied shells. Reversible via /undo."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to merge")
    res = fr.merge_group(request.parent, request.canonical_name,
                         request.member_names, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class DeleteRequest(BaseModel):
    paths: list[str]
    confirm: bool = False


@router.post("/delete")
async def delete(request: DeleteRequest):
    """Quarantine empty folders (reversible). Refuses non-empty folders."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to delete")
    return fr.delete_empty(request.paths, confirm=True)


class DetectBackfillRequest(BaseModel):
    parent: str
    folder_names: list[str]
    admin_user: Optional[str] = None


@router.post("/backfill/detect")
async def backfill_detect(request: DetectBackfillRequest):
    """Read-only: report Dropbox/Drive sources that could backfill empty folders."""
    if not os.path.isdir(request.parent):
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.parent}")
    admin = request.admin_user or fr.DEFAULT_ADMIN_USER
    return fr.detect_backfill(request.parent, request.folder_names, admin_user=admin)


class BackfillDropboxRequest(BaseModel):
    parent: str
    folder_name: str
    dropbox_path: str
    confirm: bool = False


@router.post("/backfill/dropbox")
async def backfill_dropbox(request: BackfillDropboxRequest):
    """Copy content from a local Dropbox folder into the empty local folder."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to backfill")
    res = fr.backfill_from_dropbox(request.parent, request.folder_name,
                                   request.dropbox_path, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class BackfillDriveRequest(BaseModel):
    parent: str
    folder_name: str
    admin_user: Optional[str] = None
    confirm: bool = False


@router.post("/backfill/drive")
async def backfill_drive(request: BackfillDriveRequest):
    """Download every file inside the matching Google Drive folder into the empty
    local folder (recreates subfolders, exports native docs to Office). Returns a
    job id — poll /backfill/drive/status/{job}."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to backfill")
    if not os.path.isdir(request.parent):
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.parent}")
    admin = request.admin_user or fr.DEFAULT_ADMIN_USER
    try:
        job_id = fr.backfill_from_drive(request.parent, request.folder_name,
                                        admin_user=admin, confirm=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"job_id": job_id, "status": "started"}


@router.get("/backfill/drive/status/{job_id}")
async def backfill_drive_status(job_id: str):
    job = fr.get_backfill_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class UndoRequest(BaseModel):
    manifest_file: str
    confirm: bool = False


@router.post("/undo")
async def undo(request: UndoRequest):
    """Reverse a previous merge or delete using its manifest."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to undo")
    res = fr.undo(request.manifest_file, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res
