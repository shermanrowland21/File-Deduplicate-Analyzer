"""
Scene detection and storyboard generation using PySceneDetect.
Replaces fixed-interval frame extraction with intelligent scene boundary detection.

Detectors used:
- AdaptiveDetector: best for most content, handles camera motion well (two-pass)
- ContentDetector: faster single-pass, good for cuts
- ThresholdDetector: catches fade-to-black transitions

Generates:
- Scene list with start/end timestamps
- Thumbnail for each scene (representative frame)
- Storyboard montage image for visual navigation
"""
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from scenedetect import open_video, SceneManager
from scenedetect.detectors import AdaptiveDetector, ContentDetector, ThresholdDetector
from scenedetect.scene_manager import save_images

from .audio_extractor import get_ffmpeg_path, get_media_info


# Output directory for storyboards and thumbnails
STORYBOARD_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "storyboards")


def detect_scenes(
    video_path: str,
    method: str = "adaptive",
    threshold: Optional[float] = None,
    min_scene_length_sec: float = 2.0,
    max_scene_gap_sec: float = 120.0,
) -> list[dict]:
    """
    Detect scene boundaries in a video using PySceneDetect.

    method:
    - "adaptive": AdaptiveDetector (two-pass, handles camera motion, best quality)
    - "content": ContentDetector (single-pass, faster)
    - "threshold": ThresholdDetector (catches fades/blacks)

    min_scene_length_sec: minimum scene duration to avoid over-segmentation
    max_scene_gap_sec: if no scene change detected for this long, force a boundary
                       (ensures we never go too long without a thumbnail)

    Returns list of scene dicts: {start_time, end_time, duration, scene_index}
    """
    video = open_video(video_path)
    scene_manager = SceneManager()

    # Configure detector
    if method == "adaptive":
        detector = AdaptiveDetector(
            adaptive_threshold=threshold or 3.0,
            min_scene_len=int(min_scene_length_sec * video.frame_rate),
        )
    elif method == "content":
        detector = ContentDetector(
            threshold=threshold or 27.0,
            min_scene_len=int(min_scene_length_sec * video.frame_rate),
        )
    elif method == "threshold":
        detector = ThresholdDetector(
            threshold=threshold or 12.0,
            min_scene_len=int(min_scene_length_sec * video.frame_rate),
        )
    else:
        detector = AdaptiveDetector(
            min_scene_len=int(min_scene_length_sec * video.frame_rate),
        )

    scene_manager.add_detector(detector)
    scene_manager.detect_scenes(video)
    scene_list = scene_manager.get_scene_list()

    # Convert to our format
    scenes = []
    for i, (start, end) in enumerate(scene_list):
        scenes.append({
            "scene_index": i,
            "start_time": start.get_seconds(),
            "end_time": end.get_seconds(),
            "duration": (end - start).get_seconds(),
            "start_timecode": str(start),
            "end_timecode": str(end),
        })

    # If max_scene_gap_sec is set, inject forced boundaries for long scenes
    if max_scene_gap_sec and scenes:
        expanded = []
        for scene in scenes:
            if scene["duration"] > max_scene_gap_sec:
                # Split this long scene into sub-scenes
                t = scene["start_time"]
                while t < scene["end_time"]:
                    end_t = min(t + max_scene_gap_sec, scene["end_time"])
                    expanded.append({
                        "scene_index": len(expanded),
                        "start_time": t,
                        "end_time": end_t,
                        "duration": end_t - t,
                        "start_timecode": _format_timecode(t),
                        "end_timecode": _format_timecode(end_t),
                        "forced_split": True,
                    })
                    t = end_t
            else:
                scene["scene_index"] = len(expanded)
                expanded.append(scene)
        scenes = expanded

    return scenes


def generate_storyboard(
    video_path: str,
    scenes: list[dict],
    output_dir: Optional[str] = None,
    thumbnail_width: int = 320,
) -> list[dict]:
    """
    Generate thumbnail images for each scene boundary.
    Extracts a representative frame from the start of each scene.

    Returns scenes with added 'thumbnail_path' field.
    """
    ffmpeg = get_ffmpeg_path()

    if output_dir is None:
        video_hash = Path(video_path).stem[:30]
        output_dir = os.path.join(STORYBOARD_DIR, video_hash)

    os.makedirs(output_dir, exist_ok=True)

    for scene in scenes:
        # Extract frame at 1 second into the scene (avoids transition frames)
        timestamp = scene["start_time"] + min(1.0, scene["duration"] * 0.1)
        output_path = os.path.join(
            output_dir,
            f"scene_{scene['scene_index']:04d}_{int(scene['start_time'])}s.jpg"
        )

        cmd = [
            ffmpeg, "-y",
            "-ss", str(timestamp),
            "-i", video_path,
            "-vframes", "1",
            "-vf", f"scale={thumbnail_width}:-1",
            "-q:v", "2",
            output_path,
        ]

        try:
            subprocess.run(cmd, capture_output=True, timeout=15)
            if os.path.exists(output_path):
                scene["thumbnail_path"] = output_path.replace("\\", "/")
            else:
                scene["thumbnail_path"] = None
        except (subprocess.TimeoutExpired, OSError):
            scene["thumbnail_path"] = None

    return scenes


def _format_timecode(seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"
