"""
Dedup resolver router (Phase A): directory-level, structure-aware bulk
resolution of duplicates, plus access to persisted scans.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services import dedup_resolver
from ..services.file_scanner import (
    list_persisted_scans, load_persisted_scan, latest_scan_id,
)

router = APIRouter()


@router.get("/scans")
async def scans():
    """List persisted (completed) scans, newest first."""
    return {"latest": latest_scan_id(), "scans": list_persisted_scans()}


class AnalyzeRequest(BaseModel):
    scan_id: Optional[str] = None          # defaults to latest persisted scan
    source_of_truth: list[str] = []        # directories/prefixes to KEEP
    within_source: bool = False            # 'clean inside one folder' mode


@router.post("/analyze")
async def analyze(request: AnalyzeRequest):
    """
    Classify a scan's duplicate groups and compute a SAFE removal plan.
    Read-only — changes nothing on disk.
    """
    scan_id = request.scan_id or latest_scan_id()
    if not scan_id:
        raise HTTPException(status_code=404, detail="No scan available")
    load_persisted_scan(scan_id)  # restore from disk if not in memory
    result = dedup_resolver.analyze(scan_id, request.source_of_truth,
                                    within_source=request.within_source)
    if result is None:
        raise HTTPException(status_code=404, detail="Scan not found or has no results")
    return result


class ReviewRequest(BaseModel):
    scan_id: Optional[str] = None
    source_of_truth: list[str] = []        # preferred/keep folders
    within_source: bool = False
    tiebreak: str = "biggest"              # newest|oldest|longest_name|shortest_path|biggest|smallest
    limit: int = 300


@router.post("/review")
async def review(request: ReviewRequest):
    """Card-based review payload for the outcome-first Resolver UI: summary
    (reclaimable/auto-selected/protected) + per-group cards (keeper + copies)."""
    scan_id = request.scan_id or latest_scan_id()
    if not scan_id:
        raise HTTPException(status_code=404, detail="No scan available")
    load_persisted_scan(scan_id)
    result = dedup_resolver.build_review(
        scan_id, request.source_of_truth,
        within_source=request.within_source, limit=request.limit,
        tiebreak=request.tiebreak)
    if result is None:
        raise HTTPException(status_code=404, detail="Scan not found or has no results")
    return result


class ExecuteRequest(BaseModel):
    scan_id: Optional[str] = None
    source_of_truth: list[str] = []
    within_source: bool = False
    tiebreak: str = "biggest"
    only_paths: Optional[list[str]] = None  # apply a reviewed subset (optional)
    confirm: bool = False


@router.post("/execute")
async def execute(request: ExecuteRequest):
    """
    Execute the safe removal plan: move planned duplicate copies to a dated
    quarantine (reversible). Requires confirm=true. `only_paths` applies just the
    user-reviewed subset; omit it to run the whole safe plan.
    """
    if not request.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to execute")
    scan_id = request.scan_id or latest_scan_id()
    if not scan_id:
        raise HTTPException(status_code=404, detail="No scan available")
    load_persisted_scan(scan_id)
    return dedup_resolver.execute(
        scan_id, request.source_of_truth,
        within_source=request.within_source, only_paths=request.only_paths,
        tiebreak=request.tiebreak)


# --- Phase B: LLM content-advisor (reads actual files, never guesses) ---

class AdviseStartRequest(BaseModel):
    scan_id: Optional[str] = None
    model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    max_groups: int = 200          # cap: prioritized by wasted space
    min_size: int = 0              # only advise on groups with files >= this


@router.post("/advise")
async def advise(request: AdviseStartRequest):
    """Start a background LLM advisory job that READS actual file content and
    classifies duplicate groups (biggest-wasted-space first, capped)."""
    from ..services import dedup_advisor
    scan_id = request.scan_id or latest_scan_id()
    if not scan_id:
        raise HTTPException(status_code=404, detail="No scan available")
    load_persisted_scan(scan_id)
    job_id = dedup_advisor.advise_scan(
        scan_id, model_id=request.model_id,
        max_groups=request.max_groups, min_size=request.min_size)
    return {"job_id": job_id, "status": "started"}


@router.get("/advise-status/{job_id}")
async def advise_status(job_id: str):
    from ..services import dedup_advisor
    job = dedup_advisor.get_advisor_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Advisor job not found")
    return job


@router.post("/advise-stop/{job_id}")
async def advise_stop(job_id: str):
    from ..services import dedup_advisor
    if not dedup_advisor.cancel_advisor(job_id):
        raise HTTPException(status_code=400, detail="Job not found")
    return {"cancelled": True}


class AdviseOneRequest(BaseModel):
    scan_id: Optional[str] = None
    group_hash: str
    model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


@router.post("/advise-group")
async def advise_group(request: AdviseOneRequest):
    """Advise on a single duplicate group (reads its actual content)."""
    from ..services import dedup_advisor
    from ..services.file_scanner import get_duplicates
    scan_id = request.scan_id or latest_scan_id()
    if not scan_id:
        raise HTTPException(status_code=404, detail="No scan available")
    load_persisted_scan(scan_id)
    dup = get_duplicates(scan_id)
    if dup is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    grp = next((g for g in dup.get("groups", []) if g.get("hash") == request.group_hash), None)
    if grp is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return dedup_advisor.advise_group(grp, model_id=request.model_id)
