"""
Media analysis pipeline orchestrator.
Chains: extract audio → transcribe → extract frames → analyze visuals → extract topics → store metadata.

Local: sequential Python calls.
AWS: Step Functions state machine with parallel branches.
"""
import os
import threading
import time
from pathlib import Path
from typing import Optional

from .audio_extractor import extract_audio, extract_keyframes, get_media_info
from .transcription import transcribe_audio
from .visual_analyzer import analyze_frames_batch, analyze_frame
from .topic_extractor import extract_topics_from_transcript, generate_file_summary
from . import metadata_store

# Still-image types the pipeline can analyze directly (vision + OCR), in
# addition to time-based media (video/audio).
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic"}

# Track active pipeline jobs
_jobs: dict = {}


def get_job_status(job_id: str) -> Optional[dict]:
    """Get the current status of a pipeline job."""
    return _jobs.get(job_id)


def _format_time(seconds: float) -> str:
    """Format seconds to HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def analyze_media_file(
    file_path: str,
    options: dict = None,
) -> str:
    """
    Start the full analysis pipeline for a media file.
    Returns a job_id for tracking progress.

    Options:
    - transcribe: bool (default True) — extract audio and transcribe
    - visual: bool (default True) — extract and analyze keyframes
    - topics: bool (default True) — extract topics from transcript
    - frame_interval: int (default 30) — seconds between keyframe extractions
    - max_frames: int (default 60) — max keyframes to extract
    - visual_model: str — model for frame analysis
    - topic_model: str — model for topic extraction
    """
    if options is None:
        options = {}

    job_id = f"job_{int(time.time())}_{os.path.basename(file_path)[:20]}"
    _jobs[job_id] = {
        "status": "running",
        "file_path": file_path,
        "phase": "initializing",
        "progress": 0,
        "steps_completed": [],
        "steps_total": [],
        "error": None,
        "result": None,
        "started_at": time.time(),
    }

    thread = threading.Thread(
        target=_run_pipeline,
        args=(job_id, file_path, options),
        daemon=True,
    )
    thread.start()

    return job_id


def _run_pipeline(job_id: str, file_path: str, options: dict):
    """Execute the full analysis pipeline."""
    job = _jobs[job_id]

    do_transcribe = options.get("transcribe", True)
    do_visual = options.get("visual", True)
    do_topics = options.get("topics", True)
    frame_interval = options.get("frame_interval", 30)
    max_frames = options.get("max_frames", 60)
    visual_model = options.get("visual_model")
    topic_model = options.get("topic_model", "anthropic.claude-3-5-haiku-20241022-v1:0")

    # Still images take a dedicated branch: no audio/frames — just a direct
    # vision analysis (description + OCR + objects), stored the same way so
    # search + the File Renamer can use it exactly like video/audio analysis.
    if Path(file_path).suffix.lower() in IMAGE_EXTS:
        _run_image_pipeline(job, file_path, visual_model, topic_model)
        return

    try:
        # Step 1: Get media info
        job["phase"] = "media_info"
        job["progress"] = 5
        info = get_media_info(file_path)

        if "error" in info:
            job["status"] = "error"
            job["error"] = f"Cannot read media file: {info['error']}"
            return

        # Register file in metadata store
        file_id = metadata_store.upsert_media_file(
            file_path=file_path.replace("\\", "/"),
            filename=os.path.basename(file_path),
            extension=Path(file_path).suffix.lower(),
            mime_type=_guess_mime(file_path),
            file_size=info.get("file_size", os.path.getsize(file_path)),
            duration_seconds=info.get("duration_seconds", 0),
            width=info.get("width", 0),
            height=info.get("height", 0),
            metadata=info,
        )
        metadata_store.set_analysis_status(file_id, "processing")
        job["file_id"] = file_id
        job["media_info"] = info
        job["steps_completed"].append("media_info")
        job["progress"] = 10

        transcript_segments = []
        visual_results = []

        # Step 2: Audio extraction and transcription
        if do_transcribe and info.get("duration_seconds", 0) > 0:
            # Check if there's an audio stream
            has_audio = any(s.get("type") == "audio" for s in info.get("streams", []))

            if has_audio:
                job["phase"] = "extracting_audio"
                job["progress"] = 15
                audio_path = extract_audio(file_path)
                job["steps_completed"].append("audio_extraction")
                job["progress"] = 25

                job["phase"] = "transcribing"
                job["progress"] = 30
                transcript_result = transcribe_audio(audio_path)
                transcript_segments = transcript_result.get("segments", [])

                if transcript_segments:
                    metadata_store.add_transcript_segments(file_id, transcript_segments)
                    job["steps_completed"].append("transcription")
                    job["transcript_segments"] = len(transcript_segments)
                elif transcript_result.get("note"):
                    job["transcription_note"] = transcript_result["note"]

                job["progress"] = 50

                # Clean up temp audio
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

        # Step 3: Visual analysis (can run in parallel with transcription on AWS)
        if do_visual and info.get("duration_seconds", 0) > 0:
            has_video = any(s.get("type") == "video" for s in info.get("streams", []))

            if has_video:
                job["phase"] = "extracting_frames"
                job["progress"] = 55
                frames = extract_keyframes(
                    file_path,
                    interval_seconds=frame_interval,
                    max_frames=max_frames,
                )
                job["steps_completed"].append("frame_extraction")
                job["frames_extracted"] = len(frames)
                job["progress"] = 65

                if frames:
                    job["phase"] = "analyzing_frames"
                    job["progress"] = 70
                    visual_results = analyze_frames_batch(
                        frames,
                        analysis_type="detailed",
                        model_id=visual_model,
                    )

                    # Store visual segments
                    visual_segments = []
                    for vr in visual_results:
                        visual_segments.append({
                            "timestamp": vr["timestamp"],
                            "frame_path": vr.get("frame_path", ""),
                            "description": vr.get("description", ""),
                            "ocr_text": vr.get("ocr_text", ""),
                            "objects": vr.get("objects", []),
                            "scene_type": vr.get("scene_type", ""),
                        })
                    metadata_store.add_visual_segments(file_id, visual_segments)
                    job["steps_completed"].append("visual_analysis")
                    job["progress"] = 80

        # Step 4: Topic extraction
        if do_topics and transcript_segments:
            job["phase"] = "extracting_topics"
            job["progress"] = 85
            topic_result = extract_topics_from_transcript(
                transcript_segments,
                model_id=topic_model,
            )

            if topic_result.get("topics"):
                metadata_store.add_topics(file_id, topic_result["topics"])
            if topic_result.get("keywords"):
                metadata_store.add_keywords(file_id, topic_result["keywords"])

            job["steps_completed"].append("topic_extraction")
            job["topics_found"] = len(topic_result.get("topics", []))
            job["keywords_found"] = len(topic_result.get("keywords", []))
            job["progress"] = 90

        # Step 5: Generate overall summary
        if transcript_segments or visual_results:
            job["phase"] = "generating_summary"
            job["progress"] = 95

            full_text = " ".join(s["text"] for s in transcript_segments)
            visual_descriptions = [v.get("description", "") for v in visual_results if v.get("description")]

            summary = generate_file_summary(
                transcript_text=full_text,
                visual_descriptions=visual_descriptions,
                model_id=topic_model,
            )

            if not summary.get("error"):
                # Update the file metadata with the summary
                metadata_store.upsert_media_file(
                    file_path=file_path.replace("\\", "/"),
                    filename=os.path.basename(file_path),
                    extension=Path(file_path).suffix.lower(),
                    mime_type=_guess_mime(file_path),
                    file_size=info.get("file_size", 0),
                    duration_seconds=info.get("duration_seconds", 0),
                    width=info.get("width", 0),
                    height=info.get("height", 0),
                    metadata={**info, "summary": summary},
                )
                job["summary"] = summary
                job["steps_completed"].append("summary")

        # Done
        metadata_store.set_analysis_status(file_id, "completed")
        job["status"] = "completed"
        job["phase"] = "complete"
        job["progress"] = 100
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
        if "file_id" in job:
            metadata_store.set_analysis_status(job["file_id"], "error")


def _run_image_pipeline(job: dict, file_path: str, visual_model, topic_model):
    """Analyze a STILL IMAGE: one vision pass (description + OCR + objects),
    stored as a visual segment + summary — same store shape as time-based media
    so search and the File Renamer treat images and videos uniformly."""
    try:
        norm_path = file_path.replace("\\", "/")
        try:
            fsize = os.path.getsize(file_path)
        except OSError:
            fsize = 0
        # dimensions (best-effort via Pillow)
        w = h = 0
        try:
            from PIL import Image
            with Image.open(file_path) as im:
                w, h = im.size
        except Exception:
            pass

        file_id = metadata_store.upsert_media_file(
            file_path=norm_path,
            filename=os.path.basename(file_path),
            extension=Path(file_path).suffix.lower(),
            mime_type=_guess_mime(file_path),
            file_size=fsize,
            duration_seconds=0,
            width=w, height=h,
            metadata={"media_kind": "image", "width": w, "height": h},
        )
        metadata_store.set_analysis_status(file_id, "processing")
        job["file_id"] = file_id
        job["phase"] = "analyzing_image"
        job["progress"] = 40

        # Vision pass: detailed description + OCR + objects.
        analysis = analyze_frame(file_path, analysis_type="detailed",
                                 model_id=visual_model)
        description = analysis.get("description", "")
        ocr_text = analysis.get("ocr_text", "")

        metadata_store.add_visual_segments(file_id, [{
            "timestamp": 0,
            "frame_path": norm_path,
            "description": description,
            "ocr_text": ocr_text,
            "objects": analysis.get("objects", []),
            "scene_type": analysis.get("scene_type", ""),
        }])
        job["steps_completed"] = ["image_analysis"]
        job["progress"] = 80

        # Summary from the description + any OCR text, so images are searchable
        # and nameable just like videos.
        summary = generate_file_summary(
            transcript_text=ocr_text,
            visual_descriptions=[description] if description else [],
            model_id=topic_model,
        )
        if not summary.get("error"):
            metadata_store.upsert_media_file(
                file_path=norm_path,
                filename=os.path.basename(file_path),
                extension=Path(file_path).suffix.lower(),
                mime_type=_guess_mime(file_path),
                file_size=fsize, duration_seconds=0, width=w, height=h,
                metadata={"media_kind": "image", "width": w, "height": h,
                          "summary": summary},
            )
            job["summary"] = summary
            job["steps_completed"].append("summary")

        metadata_store.set_analysis_status(file_id, "completed")
        job["status"] = "completed"
        job["phase"] = "complete"
        job["progress"] = 100
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
        if "file_id" in job:
            metadata_store.set_analysis_status(job["file_id"], "error")


def _guess_mime(file_path: str) -> str:
    """Guess MIME type from extension."""
    import mimetypes
    mime, _ = mimetypes.guess_type(file_path)
    return mime or "application/octet-stream"
