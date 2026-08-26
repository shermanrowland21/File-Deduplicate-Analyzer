"""
Clip extraction service.
Extracts segments from source video using FFmpeg stream copy (lossless).

Key principle: NO RE-ENCODING. Uses -c copy for:
- Original resolution preserved
- Original codec preserved (H.264, H.265, ProRes, etc.)
- Original color space and bit depth preserved
- Original bitrate preserved
- Extraction takes seconds, not minutes
- Output drops directly into Premiere Pro / DaVinci Resolve / Final Cut

For Premiere Pro compatibility:
- Maintains original container format (mp4, mov, mkv)
- Preserves all audio tracks
- Preserves timecode metadata
- Clean keyframe-aligned cuts (seeks to nearest keyframe for precision)
"""
import os
import subprocess
from pathlib import Path
from typing import Optional

from .audio_extractor import get_ffmpeg_path, get_media_info

# Default output directory for extracted clips
CLIPS_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "clips")


def extract_clip(
    source_path: str,
    start_time: float,
    end_time: float,
    output_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    keyframe_aligned: bool = True,
    include_audio: bool = True,
    output_format: Optional[str] = None,
) -> dict:
    """
    Extract a clip from a source video. Lossless stream copy — no re-encoding.

    Parameters:
    - source_path: path to source video
    - start_time: start timestamp in seconds
    - end_time: end timestamp in seconds
    - output_path: full output path (optional, auto-generated if not provided)
    - output_dir: output directory (defaults to CLIPS_DIR)
    - keyframe_aligned: if True, seeks to nearest keyframe before start (more accurate)
    - include_audio: include audio tracks
    - output_format: force output format (mp4, mov, mkv). Default: same as source.

    Returns: {output_path, duration, file_size, format, codec}
    """
    ffmpeg = get_ffmpeg_path()

    # Determine output path
    if output_path is None:
        if output_dir is None:
            output_dir = CLIPS_DIR
        os.makedirs(output_dir, exist_ok=True)

        source_name = Path(source_path).stem
        ext = output_format or Path(source_path).suffix.lstrip(".")
        if not ext:
            ext = "mp4"

        # Name clip with timestamp range
        start_str = _seconds_to_filename(start_time)
        end_str = _seconds_to_filename(end_time)
        output_path = os.path.join(
            output_dir,
            f"{source_name}_clip_{start_str}_to_{end_str}.{ext}"
        )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Build FFmpeg command
    # Key: put -ss BEFORE -i for fast seeking (input seeking), then use -to for duration
    duration = end_time - start_time

    cmd = [ffmpeg, "-y"]

    if keyframe_aligned:
        # Input seeking: fast, seeks to nearest keyframe before start_time
        cmd.extend(["-ss", str(start_time)])
        cmd.extend(["-i", source_path])
        cmd.extend(["-t", str(duration)])
    else:
        # Output seeking: slower but frame-accurate
        cmd.extend(["-i", source_path])
        cmd.extend(["-ss", str(start_time)])
        cmd.extend(["-to", str(end_time)])

    # Stream copy — no re-encoding
    cmd.extend(["-c", "copy"])

    if not include_audio:
        cmd.append("-an")

    # Avoid negative timestamps from seeking
    cmd.extend(["-avoid_negative_ts", "make_zero"])

    # Copy all streams (multiple audio tracks, subtitles)
    cmd.extend(["-map", "0"])

    cmd.append(output_path)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            # If stream copy fails (rare, codec mismatch), provide clear error
            return {
                "success": False,
                "error": f"FFmpeg failed: {result.stderr[:500]}",
                "command": " ".join(cmd),
            }

        if not os.path.exists(output_path):
            return {"success": False, "error": "Output file not created"}

        # Get clip info
        clip_info = get_media_info(output_path)
        file_size = os.path.getsize(output_path)

        return {
            "success": True,
            "output_path": output_path.replace("\\", "/"),
            "filename": os.path.basename(output_path),
            "duration_seconds": clip_info.get("duration_seconds", duration),
            "file_size": file_size,
            "file_size_human": _human_size(file_size),
            "format": Path(output_path).suffix.lstrip("."),
            "resolution": f"{clip_info.get('width', '?')}x{clip_info.get('height', '?')}",
            "source_path": source_path.replace("\\", "/"),
            "start_time": start_time,
            "end_time": end_time,
            "lossless": True,
            "premiere_ready": True,
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Clip extraction timed out (>2 minutes)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def extract_clips_batch(
    source_path: str,
    clips: list[dict],
    output_dir: Optional[str] = None,
) -> list[dict]:
    """
    Extract multiple clips from the same source video.
    Each clip dict: {start_time, end_time, name (optional)}

    Returns list of extraction results.
    """
    results = []
    if output_dir is None:
        source_name = Path(source_path).stem
        output_dir = os.path.join(CLIPS_DIR, source_name)

    os.makedirs(output_dir, exist_ok=True)

    for i, clip in enumerate(clips):
        # Allow custom naming
        if "name" in clip and clip["name"]:
            ext = Path(source_path).suffix or ".mp4"
            custom_path = os.path.join(output_dir, f"{clip['name']}{ext}")
        else:
            custom_path = None

        result = extract_clip(
            source_path=source_path,
            start_time=clip["start_time"],
            end_time=clip["end_time"],
            output_path=custom_path,
            output_dir=output_dir,
        )
        result["clip_index"] = i
        results.append(result)

    return results


def extract_scene_as_clip(
    source_path: str,
    scene: dict,
    output_dir: Optional[str] = None,
    padding_seconds: float = 0.5,
) -> dict:
    """
    Extract a detected scene as a clip with optional padding.
    Padding adds a small buffer before/after for smoother cuts.
    """
    start = max(0, scene["start_time"] - padding_seconds)
    end = scene["end_time"] + padding_seconds

    # Get video duration to cap end time
    info = get_media_info(source_path)
    max_duration = info.get("duration_seconds", end)
    end = min(end, max_duration)

    return extract_clip(
        source_path=source_path,
        start_time=start,
        end_time=end,
        output_dir=output_dir,
    )


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
