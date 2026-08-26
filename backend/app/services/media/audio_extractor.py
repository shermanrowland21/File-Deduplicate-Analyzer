"""
Audio extraction service using FFmpeg.
Extracts audio tracks from video files for transcription.
Also extracts media metadata (duration, resolution, codecs).

Local: runs FFmpeg directly.
AWS: Lambda with FFmpeg layer, triggered by S3 upload.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def get_ffmpeg_path() -> str:
    """Find FFmpeg executable."""
    # Check common locations
    for cmd in ["ffmpeg", "ffmpeg.exe"]:
        try:
            result = subprocess.run(
                [cmd, "-version"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return cmd
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    # Check common Windows install paths
    common_paths = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expanduser(r"~\ffmpeg\bin\ffmpeg.exe"),
    ]
    for p in common_paths:
        if os.path.exists(p):
            return p

    raise RuntimeError(
        "FFmpeg not found. Install from https://ffmpeg.org/download.html "
        "and ensure it's in your PATH."
    )


def get_media_info(file_path: str) -> dict:
    """
    Extract media metadata using FFprobe.
    Returns: duration, video/audio codec, resolution, bitrate, etc.
    """
    ffprobe = get_ffmpeg_path().replace("ffmpeg", "ffprobe")
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                file_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {"error": result.stderr}

        data = json.loads(result.stdout)
        info = {
            "duration_seconds": float(data.get("format", {}).get("duration", 0)),
            "format_name": data.get("format", {}).get("format_long_name", ""),
            "file_size": int(data.get("format", {}).get("size", 0)),
            "bit_rate": int(data.get("format", {}).get("bit_rate", 0)),
            "streams": [],
        }

        for stream in data.get("streams", []):
            stream_info = {
                "type": stream.get("codec_type"),
                "codec": stream.get("codec_long_name", stream.get("codec_name", "")),
            }
            if stream["codec_type"] == "video":
                stream_info["width"] = stream.get("width", 0)
                stream_info["height"] = stream.get("height", 0)
                stream_info["fps"] = eval(stream.get("r_frame_rate", "0/1")) if "/" in stream.get("r_frame_rate", "") else 0
                info["width"] = stream_info["width"]
                info["height"] = stream_info["height"]
            elif stream["codec_type"] == "audio":
                stream_info["sample_rate"] = int(stream.get("sample_rate", 0))
                stream_info["channels"] = stream.get("channels", 0)
            info["streams"].append(stream_info)

        return info

    except subprocess.TimeoutExpired:
        return {"error": "FFprobe timed out"}
    except (json.JSONDecodeError, Exception) as e:
        return {"error": str(e)}


def extract_audio(
    video_path: str,
    output_path: Optional[str] = None,
    format: str = "wav",
    sample_rate: int = 16000,
    mono: bool = True,
) -> str:
    """
    Extract audio track from a video file.
    Outputs 16kHz mono WAV by default (optimal for transcription).

    Returns path to the extracted audio file.
    """
    ffmpeg = get_ffmpeg_path()

    if output_path is None:
        # Create output next to the source file
        base = Path(video_path).stem
        output_dir = os.path.join(tempfile.gettempdir(), "file_dedup_audio")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"{base}.{format}")

    cmd = [
        ffmpeg, "-y",  # overwrite
        "-i", video_path,
        "-vn",  # no video
        "-ar", str(sample_rate),
    ]

    if mono:
        cmd.extend(["-ac", "1"])

    if format == "wav":
        cmd.extend(["-f", "wav"])
    elif format == "mp3":
        cmd.extend(["-codec:a", "libmp3lame", "-q:a", "2"])
    elif format == "flac":
        cmd.extend(["-codec:a", "flac"])

    cmd.append(output_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg audio extraction failed: {result.stderr[:500]}")
        return output_path
    except subprocess.TimeoutExpired:
        raise RuntimeError("Audio extraction timed out (>5 minutes)")


def extract_keyframes(
    video_path: str,
    output_dir: Optional[str] = None,
    interval_seconds: int = 30,
    max_frames: int = 100,
) -> list[dict]:
    """
    Extract keyframes from a video at regular intervals.
    Returns list of {timestamp, frame_path} for each extracted frame.

    For a 2hr video at 30s intervals = 240 frames (capped at max_frames).
    """
    ffmpeg = get_ffmpeg_path()

    if output_dir is None:
        base = Path(video_path).stem
        output_dir = os.path.join(tempfile.gettempdir(), "file_dedup_frames", base)

    os.makedirs(output_dir, exist_ok=True)

    # Get video duration first
    info = get_media_info(video_path)
    duration = info.get("duration_seconds", 0)
    if duration == 0:
        return []

    # Calculate timestamps to extract
    timestamps = []
    t = 0.0
    while t < duration and len(timestamps) < max_frames:
        timestamps.append(t)
        t += interval_seconds

    frames = []
    for i, ts in enumerate(timestamps):
        output_path = os.path.join(output_dir, f"frame_{i:04d}_{int(ts)}s.jpg")

        cmd = [
            ffmpeg, "-y",
            "-ss", str(ts),
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",  # high quality JPEG
            output_path,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and os.path.exists(output_path):
                frames.append({
                    "timestamp": ts,
                    "frame_path": output_path.replace("\\", "/"),
                    "frame_index": i,
                })
        except subprocess.TimeoutExpired:
            continue

    return frames
