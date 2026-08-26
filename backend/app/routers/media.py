"""
Media analysis router - pipeline control, search, and metadata access.
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from ..services.media.pipeline import analyze_media_file, get_job_status
from ..services.media import metadata_store

router = APIRouter()


class AnalyzeMediaRequest(BaseModel):
    file_path: str
    transcribe: bool = True
    visual: bool = True
    topics: bool = True
    frame_interval: int = 30
    max_frames: int = 60
    visual_model: Optional[str] = None
    topic_model: str = "anthropic.claude-3-5-haiku-20241022-v1:0"


class SearchRequest(BaseModel):
    query: str
    limit: int = 50


@router.post("/analyze")
async def start_analysis(request: AnalyzeMediaRequest):
    """
    Start the full media analysis pipeline for a file.
    Returns a job_id for polling progress.
    """
    import os
    if not os.path.exists(request.file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {request.file_path}")

    job_id = analyze_media_file(
        file_path=request.file_path,
        options={
            "transcribe": request.transcribe,
            "visual": request.visual,
            "topics": request.topics,
            "frame_interval": request.frame_interval,
            "max_frames": request.max_frames,
            "visual_model": request.visual_model,
            "topic_model": request.topic_model,
        },
    )

    return {"job_id": job_id, "status": "started"}


@router.get("/job/{job_id}")
async def get_analysis_job(job_id: str):
    """Get the current status/progress of an analysis pipeline job."""
    status = get_job_status(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return status


@router.post("/search")
async def search_media(request: SearchRequest):
    """
    Search across all analyzed media — transcripts, topics, keywords, visual descriptions.
    Returns matching segments with file paths and timestamps.
    """
    results = metadata_store.search_all(request.query, request.limit)
    return results


@router.get("/search")
async def search_media_get(q: str = Query(...), limit: int = 50):
    """GET version of search for convenience."""
    results = metadata_store.search_all(q, limit)
    return results


@router.get("/files")
async def list_analyzed_files(limit: int = 100):
    """List all files that have been analyzed."""
    files = metadata_store.get_analyzed_files(limit)
    return {"files": files}


@router.get("/file-analysis")
async def get_file_analysis(file_path: str = Query(...)):
    """Get the complete stratified analysis for a specific file."""
    result = metadata_store.get_file_analysis(file_path)
    if result is None:
        raise HTTPException(status_code=404, detail="No analysis found for this file")
    return result


@router.get("/transcript")
async def get_transcript(file_path: str = Query(...)):
    """Get just the transcript for a file."""
    file_record = metadata_store.get_media_file(file_path)
    if file_record is None:
        raise HTTPException(status_code=404, detail="File not found in analysis database")

    segments = metadata_store.get_transcript(file_record["id"])
    return {
        "file_path": file_path,
        "segments": segments,
        "total_segments": len(segments),
    }
