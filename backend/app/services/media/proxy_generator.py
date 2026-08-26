"""
Proxy video generation service.
Creates lightweight H.264 MP4 proxies for in-browser playback and scrubbing.

Proxy specs (optimized for web streaming):
- Codec: H.264 (universally supported in browsers)
- Resolution: 720p (1280x720) or lower if source is smaller
- Bitrate: 2 Mbps (good quality for review, small file)
- Audio: AAC 128kbps mono
- Format: MP4 with faststart (moov atom at beginning for instant playback)

A 4-hour source at full res might be 50GB.
The proxy will be ~3.5GB at 2Mbps, or ~900MB at 500kbps.
For pure scrubbing, 500kbps is sufficient.
"""
import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

from .audio_extractor import get_ffmpeg_path, get_media_info

# Proxy storage
PROXY_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "proxies")

# Track proxy generation jobs
_proxy_jobs: dict = {}


def get_proxy_path(source_path: str, quality: str = "review") -> str:
    """Get the expected proxy path for a source file."""
    source_name = Path(source_path).stem
    return os.path.join(PROXY_DIR, f"{source_name}_proxy_{quality}.mp4")


def proxy_exists(source_path: str, quality: str = "review") -> bool:
    """Check if a proxy already exists for this source."""
    return os.path.exists(get_proxy_path(source_path, quality))


def get_proxy_job_status(job_id: str) -> Optional[dict]:
    """Get status of a proxy generation job."""
    return _proxy_jobs.get(job_id)


def generate_proxy(
    source_path: str,
    quality: str = "review",
    callback: Optional[callable] = None,
) -> str:
    """
    Start proxy generation in background thread.
    Returns a job_id for polling progress.

    Quality presets:
    - "scrub": 480p, 500kbps — tiny, fast to generate, just for scrubbing
    - "review": 720p, 2Mbps — good quality for review and approvals
    - "edit": 1080p, 5Mbps — higher quality proxy for offline editing
    """
    job_id = f"proxy_{Path(source_path).stem}_{quality}"

    # Check if proxy already exists
    proxy_path = get_proxy_path(source_path, quality)
    if os.path.exists(proxy_path):
        _proxy_jobs[job_id] = {
            "status": "completed",
            "source_path": source_path,
            "proxy_path": proxy_path.replace("\\", "/"),
            "progress": 100,
            "quality": quality,
        }
        return job_id

    _proxy_jobs[job_id] = {
        "status": "running",
        "source_path": source_path,
        "proxy_path": proxy_path.replace("\\", "/"),
        "progress": 0,
        "quality": quality,
        "phase": "starting",
    }

    thread = threading.Thread(
        target=_generate_proxy_worker,
        args=(job_id, source_path, proxy_path, quality),
        daemon=True,
    )
    thread.start()

    return job_id


def _generate_proxy_worker(job_id: str, source_path: str, proxy_path: str, quality: str):
    """Background worker for proxy generation."""
    job = _proxy_jobs[job_id]
    ffmpeg = get_ffmpeg_path()

    # Get source info for progress tracking
    info = get_media_info(source_path)
    duration = info.get("duration_seconds", 0)

    # Quality presets
    presets = {
        "scrub": {"resolution": "854x480", "video_bitrate": "500k", "audio_bitrate": "64k", "preset": "ultrafast"},
        "review": {"resolution": "1280x720", "video_bitrate": "2000k", "audio_bitrate": "128k", "preset": "fast"},
        "edit": {"resolution": "1920x1080", "video_bitrate": "5000k", "audio_bitrate": "192k", "preset": "medium"},
    }
    p = presets.get(quality, presets["review"])

    os.makedirs(os.path.dirname(proxy_path), exist_ok=True)

    # Build FFmpeg command for proxy generation
    cmd = [
        ffmpeg, "-y",
        "-i", source_path,
        "-c:v", "libx264",
        "-preset", p["preset"],
        "-b:v", p["video_bitrate"],
        "-maxrate", p["video_bitrate"],
        "-bufsize", str(int(p["video_bitrate"].rstrip("k")) * 2) + "k",
        "-vf", f"scale={p['resolution']}:force_original_aspect_ratio=decrease,pad={p['resolution']}:(ow-iw)/2:(oh-ih)/2",
        "-c:a", "aac",
        "-b:a", p["audio_bitrate"],
        "-ac", "1" if quality == "scrub" else "2",
        "-movflags", "+faststart",  # Critical: enables instant browser playback
        "-progress", "pipe:1",  # Output progress to stdout
        proxy_path,
    ]

    try:
        job["phase"] = "transcoding"
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Parse FFmpeg progress output
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line.startswith("out_time_us="):
                try:
                    time_us = int(line.split("=")[1].strip())
                    current_time = time_us / 1_000_000
                    if duration > 0:
                        job["progress"] = min(99, int((current_time / duration) * 100))
                except (ValueError, IndexError):
                    pass

        if process.returncode == 0 and os.path.exists(proxy_path):
            file_size = os.path.getsize(proxy_path)
            job["status"] = "completed"
            job["progress"] = 100
            job["phase"] = "complete"
            job["file_size"] = file_size
            job["file_size_human"] = _human_size(file_size)
        else:
            stderr = process.stderr.read()
            job["status"] = "error"
            job["error"] = stderr[:500] if stderr else "FFmpeg proxy generation failed"

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def extract_clip_lowres(
    source_path: str,
    start_time: float,
    end_time: float,
    output_path: Optional[str] = None,
    quality: str = "review",
) -> dict:
    """
    Extract a clip and transcode to low-res for sharing/approvals.
    Unlike the lossless extract, this re-encodes to a small shareable file.

    Perfect for: sending to clients, Slack, email, stakeholder review.
    """
    ffmpeg = get_ffmpeg_path()

    presets = {
        "scrub": {"resolution": "854x480", "video_bitrate": "500k", "audio_bitrate": "64k"},
        "review": {"resolution": "1280x720", "video_bitrate": "2000k", "audio_bitrate": "128k"},
    }
    p = presets.get(quality, presets["review"])

    if output_path is None:
        source_name = Path(source_path).stem
        start_str = _seconds_to_filename(start_time)
        end_str = _seconds_to_filename(end_time)
        output_dir = os.path.join(PROXY_DIR, "clips")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"{source_name}_clip_{start_str}_to_{end_str}_lowres.mp4")

    duration = end_time - start_time

    cmd = [
        ffmpeg, "-y",
        "-ss", str(start_time),
        "-i", source_path,
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "fast",
        "-b:v", p["video_bitrate"],
        "-vf", f"scale={p['resolution']}:force_original_aspect_ratio=decrease",
        "-c:a", "aac",
        "-b:a", p["audio_bitrate"],
        "-ac", "2",
        "-movflags", "+faststart",
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode == 0 and os.path.exists(output_path):
            file_size = os.path.getsize(output_path)
            return {
                "success": True,
                "output_path": output_path.replace("\\", "/"),
                "filename": os.path.basename(output_path),
                "file_size": file_size,
                "file_size_human": _human_size(file_size),
                "duration_seconds": duration,
                "quality": quality,
                "resolution": p["resolution"],
                "shareable": True,
            }
        else:
            return {"success": False, "error": result.stderr[:500]}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Transcode timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _seconds_to_filename(seconds: float) -> str:
    """Convert seconds to filename-safe timestamp."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


def _human_size(size_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"
