"""
Reconstruct router — build the shared-drive Drive blueprint, match local hashed
files to it (recording correct names/paths in a fix-ledger), and inspect state.
Read-only matching; renames/moves are a separate confirmed step (not here yet).
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services import drive_blueprint as bp
from ..services import reconstruct as rc

router = APIRouter()


class BlueprintBuildRequest(BaseModel):
    admin_user: Optional[str] = None
    force: bool = False


@router.post("/blueprint/build")
async def blueprint_build(request: BlueprintBuildRequest):
    """Crawl ALL shared drives via GAM into the blueprint (md5 -> name+path).
    Resumable per-drive. Network-bound (safe to run alongside MD5 hashing)."""
    admin = request.admin_user or bp.DEFAULT_ADMIN_USER
    job_id = bp.build(admin_user=admin, force=request.force)
    return {"job_id": job_id, "status": "started"}


@router.get("/blueprint/status/{job_id}")
async def blueprint_status(job_id: str):
    job = bp.get_build_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/blueprint/stop/{job_id}")
async def blueprint_stop(job_id: str):
    if not bp.cancel_build(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"cancelled": True}


@router.get("/blueprint/stats")
async def blueprint_stats():
    b = bp.open_blueprint()
    try:
        return b.stats()
    finally:
        b.close()


class MatchRequest(BaseModel):
    local_root: str


@router.post("/match")
async def match(request: MatchRequest):
    """Match local hashed files (md5_index for local_root) against the blueprint,
    recording correct name/path in the ledger. Idempotent/resumable, read-only."""
    job_id = rc.match(request.local_root)
    return {"job_id": job_id, "status": "started"}


@router.get("/match/status/{job_id}")
async def match_status(job_id: str):
    job = rc.get_match_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/ledger/stats")
async def ledger_stats():
    return rc.ledger_stats()


@router.get("/folders/plan")
async def folders_plan(local_root: str):
    """DRY RUN — classify local folders vs the Drive blueprint (keep / delete-empty
    / relocate-content-then-delete)."""
    return rc.folder_cleanup_plan(local_root)


@router.get("/folders/analyze")
async def folders_analyze(local_root: str):
    """DRY RUN — MD5-driven folder classification (name-agnostic): real_folder
    (w/ canonical Drive name), redundant, empty, unknown."""
    return rc.folder_analysis(local_root)


@router.get("/folders/merge-plan")
async def folders_merge_plan(local_root: str):
    """DRY RUN — split webinar pairs (truncated folder + its existing canonical
    twin) that should be merged."""
    return rc.merge_plan(local_root)


class FolderMergeRequest(BaseModel):
    local_root: str
    confirm: bool = False


@router.post("/folders/merge")
async def folders_merge(request: FolderMergeRequest):
    """Merge split twin folders into the canonical Drive-named folder (skip
    byte-identical dupes), quarantine emptied shells. Reversible."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.apply_merge(request.local_root, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class FolderMergeUndoRequest(BaseModel):
    manifest_file: str
    confirm: bool = False


@router.post("/folders/merge-undo")
async def folders_merge_undo(request: FolderMergeUndoRequest):
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.undo_merge(request.manifest_file, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.get("/folders/validate")
async def folders_validate(local_folder: str):
    """Compare a local folder's content (by md5) to Google Drive's blueprint —
    matched / missing_locally / extra_locally / verdict."""
    res = rc.validate_against_drive(local_folder)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class FolderFixRequest(BaseModel):
    local_root: str
    do_rename: bool = True
    do_redundant: bool = True
    do_empty: bool = True
    confirm: bool = False


@router.post("/folders/fix")
async def folders_fix(request: FolderFixRequest):
    """Rename real truncated folders to canonical Drive name; quarantine redundant
    + empty folders. Never touches 'unknown'. Reversible."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.apply_folder_fix(request.local_root, do_rename=request.do_rename,
                              do_redundant=request.do_redundant,
                              do_empty=request.do_empty, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class FolderFixUndoRequest(BaseModel):
    manifest_file: str
    confirm: bool = False


@router.post("/folders/fix-undo")
async def folders_fix_undo(request: FolderFixUndoRequest):
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.undo_folder_fix(request.manifest_file, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class FolderApplyRequest(BaseModel):
    local_root: str
    relocate: bool = True
    confirm: bool = False


@router.post("/folders/apply")
async def folders_apply(request: FolderApplyRequest):
    """Relocate content out of not-in-Drive folders to their correct Drive location,
    then quarantine emptied/empty shells. Reversible."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.apply_folder_cleanup(request.local_root, confirm=True,
                                  relocate=request.relocate)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class FolderUndoRequest(BaseModel):
    manifest_file: str
    confirm: bool = False


@router.post("/folders/undo")
async def folders_undo(request: FolderUndoRequest):
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.undo_folder_cleanup(request.manifest_file, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.get("/parendupes/find")
async def parendupes_find(local_root: str):
    """DRY RUN — ' (N)' duplicate files that have a same-md5 clean twin (safe to remove)."""
    return rc.find_paren_dupes(local_root)


class ParenPurgeRequest(BaseModel):
    local_root: str
    only_paths: Optional[list[str]] = None
    confirm: bool = False


@router.post("/parendupes/purge")
async def parendupes_purge(request: ParenPurgeRequest):
    """Quarantine the ' (N)' duplicates (keep the clean twin). Reversible."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.purge_paren_dupes(request.local_root, only_paths=request.only_paths, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class PurgeUndoRequest(BaseModel):
    manifest_file: str
    confirm: bool = False


@router.post("/parendupes/undo")
async def parendupes_undo(request: PurgeUndoRequest):
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.undo_purge(request.manifest_file, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.get("/rename/preview")
async def rename_preview(limit: int = 1000):
    """DRY RUN — proposed in-place filename corrections (Drive-authoritative
    name, same folder). No changes made."""
    return rc.rename_preview(limit=limit)


class RenameApplyRequest(BaseModel):
    only_paths: Optional[list[str]] = None
    confirm: bool = False


@router.post("/rename/apply")
async def rename_apply(request: RenameApplyRequest):
    """Rename matched files IN PLACE to their Drive-authoritative name.
    Same folder only, collision-safe, reversible. Requires confirm=true."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.apply_renames(only_paths=request.only_paths, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class RenameUndoRequest(BaseModel):
    manifest_file: str
    confirm: bool = False


@router.post("/rename/undo")
async def rename_undo(request: RenameUndoRequest):
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.undo_renames(request.manifest_file, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.get("/ledger/list")
async def ledger_list(status: str, limit: int = 500):
    return {"status": status, "items": rc.ledger_list(status, limit)}


# ---------------------------------------------------------------- -pinned cleanup

@router.get("/pinned/preview")
async def pinned_preview(examples: int = 12):
    """DRY RUN — count -pinned Takeout artifacts: how many are redundant (have a
    clean identical twin → quarantine) vs unique (→ rename). No changes."""
    return rc.pinned_preview(examples=examples)


class PinnedApplyRequest(BaseModel):
    confirm: bool = False


@router.post("/pinned/quarantine")
async def pinned_quarantine(request: PinnedApplyRequest):
    """Quarantine every -pinned artifact that has a byte-identical clean twin.
    MD5-verified at apply time. Reversible."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.pinned_quarantine(confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.post("/pinned/rename")
async def pinned_rename(request: PinnedApplyRequest):
    """Rename the unique (no-twin) -pinned files, stripping the -at-<ts>-pinned
    suffix back to the clean name. Reversible."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.pinned_rename(confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class PinnedUndoRequest(BaseModel):
    manifest_file: str
    confirm: bool = False


@router.post("/pinned/undo")
async def pinned_undo(request: PinnedUndoRequest):
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = rc.pinned_undo(request.manifest_file, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res
