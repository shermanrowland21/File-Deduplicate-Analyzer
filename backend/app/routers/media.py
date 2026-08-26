"""
Media analysis router - pipeline control, search, clip extraction, and metadata access.
"""
import os
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

from ..services.media.pipeline import analyze_media_file, get_job_status
from ..services.media import metadata_store
from ..services.media.scene_detect import detect_scenes, generate_storyboard
from ..services.media.clip_extractor import extract_clip, extract_clips_batch

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


class BatchAnalyzeRequest(BaseModel):
    paths: list[str]  # files or directories
    recursive: bool = True
    file_extensions: Optional[list[str]] = None  # filter, e.g. ["mp4", "avi", "mov", "mkv", "mp3", "wav"]
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

# --- Scene Detection & Storyboard ---

class SceneDetectRequest(BaseModel):
    file_path: str
    method: str = "adaptive"  # adaptive, content, threshold
    threshold: Optional[float] = None
    min_scene_length_sec: float = 2.0
    max_scene_gap_sec: float = 120.0
    generate_thumbnails: bool = True
    thumbnail_width: int = 320


@router.post("/scenes")
async def detect_video_scenes(request: SceneDetectRequest):
    """
    Detect scene boundaries in a video and generate storyboard thumbnails.
    Returns a list of scenes with timestamps and thumbnail paths.
    """
    if not os.path.exists(request.file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {request.file_path}")

    try:
        scenes = detect_scenes(
            video_path=request.file_path,
            method=request.method,
            threshold=request.threshold,
            min_scene_length_sec=request.min_scene_length_sec,
            max_scene_gap_sec=request.max_scene_gap_sec,
        )

        if request.generate_thumbnails:
            scenes = generate_storyboard(
                video_path=request.file_path,
                scenes=scenes,
                thumbnail_width=request.thumbnail_width,
            )

        return {
            "file_path": request.file_path,
            "total_scenes": len(scenes),
            "scenes": scenes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scene detection failed: {str(e)}")


@router.get("/thumbnail")
async def get_thumbnail(path: str = Query(...)):
    """Serve a scene thumbnail image."""
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(path, media_type="image/jpeg")


# --- Clip Extraction ---

class ClipRequest(BaseModel):
    source_path: str
    start_time: float  # seconds
    end_time: float  # seconds
    output_dir: Optional[str] = None
    output_format: Optional[str] = None  # mp4, mov, mkv
    include_audio: bool = True


class BatchClipRequest(BaseModel):
    source_path: str
    clips: list[dict]  # each: {start_time, end_time, name (optional)}
    output_dir: Optional[str] = None


@router.post("/extract-clip")
async def extract_video_clip(request: ClipRequest):
    """
    Extract a clip from a source video. LOSSLESS — no re-encoding.
    Original quality preserved: resolution, codec, color space, bitrate.
    Output is ready for Premiere Pro / DaVinci Resolve / Final Cut.

    Extraction takes seconds regardless of clip length.
    """
    if not os.path.exists(request.source_path):
        raise HTTPException(status_code=404, detail=f"Source file not found: {request.source_path}")

    if request.start_time >= request.end_time:
        raise HTTPException(status_code=400, detail="start_time must be less than end_time")

    result = extract_clip(
        source_path=request.source_path,
        start_time=request.start_time,
        end_time=request.end_time,
        output_dir=request.output_dir,
        output_format=request.output_format,
        include_audio=request.include_audio,
    )

    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Extraction failed"))

    return result


@router.post("/extract-clips-batch")
async def extract_clips_batch_endpoint(request: BatchClipRequest):
    """
    Extract multiple clips from the same source video.
    All clips are lossless stream copies — full resolution, Premiere-ready.
    """
    if not os.path.exists(request.source_path):
        raise HTTPException(status_code=404, detail=f"Source file not found: {request.source_path}")

    results = extract_clips_batch(
        source_path=request.source_path,
        clips=request.clips,
        output_dir=request.output_dir,
    )

    successful = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]

    return {
        "source_path": request.source_path,
        "total_clips": len(request.clips),
        "successful": len(successful),
        "failed": len(failed),
        "clips": results,
    }


@router.get("/download-clip")
async def download_clip(path: str = Query(...)):
    """
    Download an extracted clip file.
    Returns the file with proper content type for direct download.
    """
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Clip file not found")

    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    media_types = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".webm": "video/webm",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Proxy Generation & Playback ---

from ..services.media.proxy_generator import (
    generate_proxy, get_proxy_job_status, proxy_exists, get_proxy_path,
    extract_clip_lowres,
)


class ProxyRequest(BaseModel):
    file_path: str
    quality: str = "review"  # scrub, review, edit


class ClipExportRequest(BaseModel):
    source_path: str
    start_time: float
    end_time: float
    hi_res: bool = True
    lo_res: bool = True
    lo_res_quality: str = "review"


class BatchExportRequest(BaseModel):
    """Export multiple clips from search results."""
    clips: list[dict]  # each: {source_path, start_time, end_time, label (optional)}
    hi_res: bool = True
    lo_res: bool = True
    output_dir: Optional[str] = None


@router.post("/proxy/generate")
async def generate_video_proxy(request: ProxyRequest):
    """
    Generate a low-res proxy for in-browser playback and scrubbing.
    If proxy already exists, returns immediately.
    Otherwise starts background generation and returns job_id for polling.
    """
    if not os.path.exists(request.file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {request.file_path}")

    job_id = generate_proxy(source_path=request.file_path, quality=request.quality)
    status = get_proxy_job_status(job_id)
    return status


@router.get("/proxy/status/{job_id}")
async def proxy_generation_status(job_id: str):
    """Poll proxy generation progress."""
    status = get_proxy_job_status(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Proxy job not found")
    return status


@router.get("/proxy/stream")
async def stream_proxy(file_path: str = Query(...), quality: str = "review"):
    """
    Stream a proxy video for in-browser playback.
    Returns the proxy file if it exists, or 404 if not generated yet.
    """
    proxy_path = get_proxy_path(file_path, quality)
    if not os.path.exists(proxy_path):
        # Check if source has a proxy at any quality
        for q in ["scrub", "review", "edit"]:
            alt = get_proxy_path(file_path, q)
            if os.path.exists(alt):
                proxy_path = alt
                break
        else:
            raise HTTPException(
                status_code=404,
                detail="Proxy not generated yet. Call POST /api/media/proxy/generate first."
            )

    return FileResponse(
        proxy_path,
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",  # Enable seeking in browser
            "Cache-Control": "public, max-age=86400",
        },
    )


@router.post("/export-clip")
async def export_clip(request: ClipExportRequest):
    """
    Export a clip in both hi-res (lossless, for Premiere) and lo-res (for review/sharing).
    """
    if not os.path.exists(request.source_path):
        raise HTTPException(status_code=404, detail=f"Source not found: {request.source_path}")

    results = {"source_path": request.source_path, "start_time": request.start_time, "end_time": request.end_time}

    if request.hi_res:
        hi = extract_clip(
            source_path=request.source_path,
            start_time=request.start_time,
            end_time=request.end_time,
        )
        results["hi_res"] = hi

    if request.lo_res:
        lo = extract_clip_lowres(
            source_path=request.source_path,
            start_time=request.start_time,
            end_time=request.end_time,
            quality=request.lo_res_quality,
        )
        results["lo_res"] = lo

    return results


@router.post("/export-batch")
async def export_batch(request: BatchExportRequest):
    """
    Batch export clips from search results.
    Each clip gets hi-res (Premiere-ready) and/or lo-res (shareable) versions.
    """
    results = []

    for clip in request.clips:
        source = clip.get("source_path", "")
        start = clip.get("start_time", 0)
        end = clip.get("end_time", 0)
        label = clip.get("label", "")

        if not os.path.exists(source):
            results.append({"source_path": source, "error": "File not found", "success": False})
            continue

        clip_result = {"source_path": source, "start_time": start, "end_time": end, "label": label}

        if request.hi_res:
            hi = extract_clip(
                source_path=source,
                start_time=start,
                end_time=end,
                output_dir=request.output_dir,
            )
            clip_result["hi_res"] = hi

        if request.lo_res:
            lo = extract_clip_lowres(
                source_path=source,
                start_time=start,
                end_time=end,
            )
            clip_result["lo_res"] = lo

        clip_result["success"] = True
        results.append(clip_result)

    successful = sum(1 for r in results if r.get("success"))
    return {
        "total_clips": len(request.clips),
        "successful": successful,
        "failed": len(request.clips) - successful,
        "results": results,
    }
