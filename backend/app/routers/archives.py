"""
Archive extraction router - extract Google Takeout zips and other archives.
"""
import os
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from ..services.archive_extractor import (
    find_archives, find_archives_background, get_find_job, cancel_find_job,
    extract_archives, get_extraction_job, cancel_extraction,
    scan_and_extract, get_scan_extract_job, cancel_scan_extract,
    get_ledger_stats, get_ledger_entries, clear_ledger,
)

router = APIRouter()


class ExtractRequest(BaseModel):
    source_dir: str
    output_dir: Optional[str] = None
    archives: Optional[list[str]] = None


@router.post("/find")
async def start_find_archives(directory: str = Query(...), recursive: bool = True):
    """Start searching for archives in background. Returns job_id to poll."""
    if not os.path.exists(directory):
        raise HTTPException(status_code=404, detail=f"Directory not found: {directory}")
    job_id = find_archives_background(directory, recursive=recursive)
    return {"job_id": job_id, "status": "started"}


@router.get("/find")
async def find_archive_files(directory: str = Query(...), recursive: bool = True):
    """Start searching for archives in background. Returns job_id to poll."""
    if not os.path.exists(directory):
        raise HTTPException(status_code=404, detail=f"Directory not found: {directory}")
    job_id = find_archives_background(directory, recursive=recursive)
    return {"job_id": job_id, "status": "started"}


@router.get("/find-status/{job_id}")
async def find_archives_status(job_id: str):
    """Poll archive discovery progress."""
    job = get_find_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/find-stop/{job_id}")
async def stop_find_archives(job_id: str):
    """Stop scanning for archives. Keeps what's been found so far."""
    cancelled = cancel_find_job(job_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Job not found or already done")
    return {"stopped": True}


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


# --- Combined Scan + Extract (parallel) ---

class ScanExtractRequest(BaseModel):
    source_dir: str
    output_dir: Optional[str] = None
    recursive: bool = True
    max_workers: int = 3
    move_processed: bool = False
    only_drive: bool = False
    delete_after: bool = False


@router.post("/scan-extract")
async def start_scan_extract(request: ScanExtractRequest):
    """
    Scan and extract in parallel. Extracts each archive as it's found.
    Best for large datasets — no waiting for full scan before extraction starts.
    """
    if not os.path.exists(request.source_dir):
        raise HTTPException(status_code=404, detail=f"Directory not found: {request.source_dir}")

    job_id = scan_and_extract(
        source_dir=request.source_dir,
        output_dir=request.output_dir,
        recursive=request.recursive,
        max_workers=request.max_workers,
        move_processed=request.move_processed,
        only_drive=request.only_drive,
        delete_after=request.delete_after,
    )
    return {"job_id": job_id, "status": "started"}


class FlattenRequest(BaseModel):
    extracted_root: str
    dest_root: Optional[str] = None


@router.post("/flatten")
async def start_flatten(request: FlattenRequest):
    """
    Reorganize extracted Workspace export into clean flat structure:
    personal accounts and each shared drive become top-level folders.
    """
    from ..services.archive_extractor import flatten_extracted
    if not os.path.exists(request.extracted_root):
        raise HTTPException(status_code=404, detail=f"Directory not found: {request.extracted_root}")
    job_id = flatten_extracted(request.extracted_root, request.dest_root)
    return {"job_id": job_id, "status": "started"}


@router.get("/flatten-status/{job_id}")
async def flatten_status(job_id: str):
    from ..services.archive_extractor import get_flatten_job
    job = get_flatten_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class CleanupRequest(BaseModel):
    target_dir: str


@router.post("/cleanup")
async def start_cleanup(request: CleanupRequest):
    """Strip Takeout clutter (info.json, archive_browser.html, _MACOSX, empty folders)."""
    from ..services.archive_extractor import cleanup_extracted
    if not os.path.exists(request.target_dir):
        raise HTTPException(status_code=404, detail=f"Directory not found: {request.target_dir}")
    job_id = cleanup_extracted(request.target_dir)
    return {"job_id": job_id, "status": "started"}


@router.get("/cleanup-status/{job_id}")
async def cleanup_status(job_id: str):
    from ..services.archive_extractor import get_cleanup_job
    job = get_cleanup_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class ReconcileRequest(BaseModel):
    source_dir: str
    output_dir: Optional[str] = None


@router.post("/reconcile")
async def start_reconcile(request: ReconcileRequest):
    """
    Detect which archives are already extracted on disk and record them in the ledger.
    Avoids re-extracting archives done before the ledger was tracking.
    """
    from ..services.archive_extractor import reconcile_ledger
    if not os.path.exists(request.source_dir):
        raise HTTPException(status_code=404, detail=f"Directory not found: {request.source_dir}")
    job_id = reconcile_ledger(request.source_dir, request.output_dir)
    return {"job_id": job_id, "status": "started"}


@router.get("/reconcile-status/{job_id}")
async def reconcile_status(job_id: str):
    from ..services.archive_extractor import get_reconcile_job
    job = get_reconcile_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class MoveProcessedRequest(BaseModel):
    source_dir: str


@router.post("/move-processed")
async def move_processed_archives(request: MoveProcessedRequest):
    """
    Move all already-extracted archives (from the ledger) into a 'processed' folder.
    Use this to clean up after extraction completes.
    """
    from ..services.archive_extractor import move_completed_to_processed
    result = move_completed_to_processed(request.source_dir)
    return result


# --- Shared-drive name mapping (GAM content-match) ---

class BuildMapRequest(BaseModel):
    admin_user: str
    extracted_root: str


@router.post("/build-map")
async def build_shared_drive_map(request: BuildMapRequest):
    """
    Build the Resource$1-folder -> real shared-drive-name mapping using GAM
    content-matching (compares each shared drive's top-level folder set against
    the folders found on disk). Runs in background. Returns job_id.
    """
    from ..services.media.drive_mapper import build_mapping, gam_available, GAM_PATH
    if not gam_available():
        raise HTTPException(status_code=400, detail=f"GAM not found at {GAM_PATH}")
    if not os.path.exists(request.extracted_root):
        raise HTTPException(status_code=404, detail=f"Directory not found: {request.extracted_root}")
    job_id = build_mapping(request.admin_user, request.extracted_root)
    return {"job_id": job_id, "status": "started"}


@router.get("/build-map-status/{job_id}")
async def build_map_status(job_id: str):
    from ..services.media.drive_mapper import get_map_job
    job = get_map_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # Do not serialize the set objects; job dict already holds JSON-safe values
    return job


@router.get("/shared-drive-map")
async def get_shared_drive_map():
    """Return the saved Resource -> shared-drive-name mapping."""
    from ..services.media.drive_mapper import load_mapping, MAP_STORE
    return {"mapping": load_mapping(), "map_file": MAP_STORE}


class ManualNamesRequest(BaseModel):
    overrides: dict  # {"Resource$1 -NNNN": "Real Drive Name"}


@router.post("/shared-drive-map/manual")
async def set_manual_shared_drive_names(request: ManualNamesRequest):
    """
    Manually name shared drives that couldn't be auto content-matched
    (e.g. the live drive was reorganized since the export snapshot).
    """
    from ..services.media.drive_mapper import set_manual_names
    merged = set_manual_names(request.overrides)
    return {"mapping": merged}


# --- Organize: COPY into clean structure (preserves source) ---

class OrganizePreviewRequest(BaseModel):
    extracted_root: str


@router.post("/organize-preview")
async def organize_preview(request: OrganizePreviewRequest):
    """
    DRY RUN. Show the clean structure + names that organize would create,
    without copying anything. Verify before committing.
    """
    from ..services.organizer import plan_organization
    if not os.path.exists(request.extracted_root):
        raise HTTPException(status_code=404, detail=f"Directory not found: {request.extracted_root}")
    return plan_organization(request.extracted_root)


class OrganizeRequest(BaseModel):
    extracted_root: str
    dest_root: Optional[str] = None
    include_my_computer: bool = True


@router.post("/organize")
async def start_organize(request: OrganizeRequest):
    """
    COPY the extracted export into a clean, named structure. Never modifies the
    extracted source. Personal accounts named by email (My Drive contents
    directly inside); shared drives named via GAM mapping.
    """
    from ..services.organizer import organize
    if not os.path.exists(request.extracted_root):
        raise HTTPException(status_code=404, detail=f"Directory not found: {request.extracted_root}")
    job_id = organize(request.extracted_root, request.dest_root, request.include_my_computer)
    return {"job_id": job_id, "status": "started"}


@router.get("/organize-status/{job_id}")
async def organize_status(job_id: str):
    from ..services.organizer import get_organize_job
    job = get_organize_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/organize-stop/{job_id}")
async def stop_organize(job_id: str):
    from ..services.organizer import cancel_organize
    if not cancel_organize(job_id):
        raise HTTPException(status_code=400, detail="Job not found")
    return {"cancelled": True}


# --- Drive <-> Local State Reconciliation (tree-walk, identity-based) ---

class ReconcileDetectRequest(BaseModel):
    scope: str = "all"           # all | users | drives
    admin_user: str = "admin@example.com"
    hash_local: bool = True      # compute local MD5s for update/move detection


@router.post("/reconcile-detect")
async def reconcile_detect(request: ReconcileDetectRequest):
    """
    READ-ONLY tree reconciliation: compares current Drive state to local
    Organized/ tree and classifies every file as add/delete/move/update/reexport.
    """
    from ..services import drive_reconcile
    if not drive_reconcile.gam_available():
        raise HTTPException(status_code=400, detail="GAM not found")
    job_id = drive_reconcile.detect(scope=request.scope,
                                    admin_user=request.admin_user,
                                    hash_local=request.hash_local)
    return {"job_id": job_id, "status": "started"}


@router.get("/reconcile-detect-status/{job_id}")
async def reconcile_detect_status(job_id: str):
    from ..services import drive_reconcile
    job = drive_reconcile.get_detect_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/reconcile-detect-stop/{job_id}")
async def reconcile_detect_stop(job_id: str):
    from ..services import drive_reconcile
    if not drive_reconcile.cancel_detect(job_id):
        raise HTTPException(status_code=400, detail="Job not found")
    return {"cancelled": True}


class ReconcileApplyRequest(BaseModel):
    report_file: str
    admin_user: str = "admin@example.com"
    do_adds: bool = True
    do_updates: bool = True
    do_moves: bool = True
    do_deletes: bool = True
    do_reexports: bool = True


@router.post("/reconcile-apply")
async def reconcile_apply(request: ReconcileApplyRequest):
    """
    Apply a reconciliation report: download adds, relocate moves, re-download
    updates (old quarantined), quarantine deletes, re-export native docs.
    """
    from ..services import drive_reconcile
    if not os.path.exists(request.report_file):
        raise HTTPException(status_code=404, detail=f"Report not found: {request.report_file}")
    job_id = drive_reconcile.apply_reconcile(
        request.report_file, admin_user=request.admin_user,
        do_adds=request.do_adds, do_updates=request.do_updates,
        do_moves=request.do_moves, do_deletes=request.do_deletes,
        do_reexports=request.do_reexports)
    return {"job_id": job_id, "status": "started"}


@router.get("/reconcile-apply-status/{job_id}")
async def reconcile_apply_status(job_id: str):
    from ..services import drive_reconcile
    job = drive_reconcile.get_apply_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/reconcile-apply-stop/{job_id}")
async def reconcile_apply_stop(job_id: str):
    from ..services import drive_reconcile
    if not drive_reconcile.cancel_apply(job_id):
        raise HTTPException(status_code=400, detail="Job not found")
    return {"cancelled": True}


# --- Google Drive Delta Sync (legacy modifiedTime-based; superseded by reconcile) ---

class DeltaDetectRequest(BaseModel):
    scope: str = "all"           # all | users | drives
    admin_user: str = "admin@example.com"


@router.post("/delta-detect")
async def delta_detect(request: DeltaDetectRequest):
    """
    READ-ONLY: detect Drive changes since the export baseline (adds/mods +
    deletions) across users and/or shared drives. Produces a delta report.
    """
    from ..services import drive_delta
    if not drive_delta.gam_available():
        raise HTTPException(status_code=400, detail=f"GAM not found at {drive_delta.GAM_PATH}")
    job_id = drive_delta.detect(scope=request.scope, admin_user=request.admin_user)
    return {"job_id": job_id, "status": "started", "baseline": drive_delta.BASELINE}


@router.get("/delta-detect-status/{job_id}")
async def delta_detect_status(job_id: str):
    from ..services import drive_delta
    job = drive_delta.get_detect_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/delta-detect-stop/{job_id}")
async def delta_detect_stop(job_id: str):
    from ..services import drive_delta
    if not drive_delta.cancel_detect(job_id):
        raise HTTPException(status_code=400, detail="Job not found")
    return {"cancelled": True}


class DeltaApplyRequest(BaseModel):
    report_file: str
    admin_user: str = "admin@example.com"
    do_downloads: bool = True
    do_deletions: bool = True


@router.post("/delta-apply")
async def delta_apply(request: DeltaApplyRequest):
    """
    Apply a detect report: download adds/mods into Organized/ (native docs
    converted to Office), quarantine-move deletions. Never hard-deletes.
    """
    from ..services import drive_delta
    if not os.path.exists(request.report_file):
        raise HTTPException(status_code=404, detail=f"Report not found: {request.report_file}")
    job_id = drive_delta.apply_delta(
        request.report_file, admin_user=request.admin_user,
        do_downloads=request.do_downloads, do_deletions=request.do_deletions)
    return {"job_id": job_id, "status": "started"}


@router.get("/delta-apply-status/{job_id}")
async def delta_apply_status(job_id: str):
    from ..services import drive_delta
    job = drive_delta.get_apply_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/delta-apply-stop/{job_id}")
async def delta_apply_stop(job_id: str):
    from ..services import drive_delta
    if not drive_delta.cancel_apply(job_id):
        raise HTTPException(status_code=400, detail="Job not found")
    return {"cancelled": True}


class ReexportRequest(BaseModel):
    scope: str = "all"
    admin_user: str = "admin@example.com"


@router.post("/delta-reexport")
async def delta_reexport(request: ReexportRequest):
    """
    Re-export existing Google-origin Office files in Organized/ from Drive in
    proper Office format (fixes flattened Takeout conversions). Backs up originals.
    """
    from ..services import drive_delta
    if not drive_delta.gam_available():
        raise HTTPException(status_code=400, detail="GAM not found")
    job_id = drive_delta.reexport_native(admin_user=request.admin_user, scope=request.scope)
    return {"job_id": job_id, "status": "started"}


@router.get("/delta-reexport-status/{job_id}")
async def delta_reexport_status(job_id: str):
    from ..services import drive_delta
    job = drive_delta.get_reexport_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/delta-reexport-stop/{job_id}")
async def delta_reexport_stop(job_id: str):
    from ..services import drive_delta
    if not drive_delta.cancel_reexport(job_id):
        raise HTTPException(status_code=400, detail="Job not found")
    return {"cancelled": True}


@router.get("/scan-extract-status/{job_id}")
async def scan_extract_status(job_id: str):
    """Poll scan+extract progress."""
    job = get_scan_extract_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/scan-extract-stop/{job_id}")
async def stop_scan_extract(job_id: str):
    """Stop a scan+extract job."""
    cancelled = cancel_scan_extract(job_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Job not found")
    return {"cancelled": True}


# --- Extraction Ledger ---

@router.get("/ledger")
async def get_ledger():
    """Get the extraction ledger - what's been extracted across all sessions."""
    return {
        "stats": get_ledger_stats(),
        "entries": get_ledger_entries(),
    }


@router.delete("/ledger")
async def reset_ledger():
    """Clear the ledger - forces re-extraction of everything."""
    clear_ledger()
    return {"cleared": True}


class RecordExtractionRequest(BaseModel):
    archive_paths: list[str]
    output_dir: str


@router.post("/ledger/record")
async def record_extractions(request: RecordExtractionRequest):
    """
    Manually record archives as already-extracted.
    Useful for archives extracted before the ledger existed.
    """
    from ..services.archive_extractor import _record_extraction
    recorded = 0
    for path in request.archive_paths:
        if os.path.exists(path):
            _record_extraction(path, request.output_dir, 0)
            recorded += 1
    return {"recorded": recorded}


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

