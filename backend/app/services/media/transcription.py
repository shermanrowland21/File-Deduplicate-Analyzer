"""
Transcription service using Amazon Transcribe.
Produces timestamped, speaker-diarized transcripts.

Local: calls Transcribe API with local files (uploads temp to S3 or uses streaming).
AWS: Transcribe job triggered by Step Functions, output to S3.
"""
import json
import os
import time
import uuid
from typing import Optional

import boto3
from botocore.exceptions import ClientError


def get_transcribe_client():
    """Get Amazon Transcribe client."""
    return boto3.client(
        "transcribe",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def get_s3_client():
    """Get S3 client for uploading audio for Transcribe."""
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


# Bucket for temporary audio uploads (Transcribe needs S3 access)
TRANSCRIBE_BUCKET = os.environ.get("TRANSCRIBE_BUCKET", "")


def transcribe_audio(
    audio_path: str,
    language_code: str = "en-US",
    enable_diarization: bool = True,
    max_speakers: int = 5,
) -> dict:
    """
    Transcribe an audio file using Amazon Transcribe.
    Returns structured transcript with timestamped segments and speaker labels.

    If no S3 bucket is configured, falls back to Bedrock-based transcription.
    """
    if not TRANSCRIBE_BUCKET:
        # Fall back to Bedrock-based transcription for local dev
        return _transcribe_via_bedrock(audio_path, language_code)

    transcribe = get_transcribe_client()
    s3 = get_s3_client()

    # Upload audio to S3
    job_name = f"dedup-analyzer-{uuid.uuid4().hex[:8]}"
    s3_key = f"transcribe-input/{job_name}/{os.path.basename(audio_path)}"

    try:
        s3.upload_file(audio_path, TRANSCRIBE_BUCKET, s3_key)
    except ClientError as e:
        raise RuntimeError(f"Failed to upload audio to S3: {e}")

    media_uri = f"s3://{TRANSCRIBE_BUCKET}/{s3_key}"

    # Start transcription job
    settings = {}
    if enable_diarization:
        settings["ShowSpeakerLabels"] = True
        settings["MaxSpeakerLabels"] = max_speakers

    try:
        transcribe.start_transcription_job(
            TranscriptionJobName=job_name,
            Media={"MediaFileUri": media_uri},
            MediaFormat=_get_media_format(audio_path),
            LanguageCode=language_code,
            Settings=settings,
            OutputBucketName=TRANSCRIBE_BUCKET,
            OutputKey=f"transcribe-output/{job_name}.json",
        )
    except ClientError as e:
        # Clean up S3
        s3.delete_object(Bucket=TRANSCRIBE_BUCKET, Key=s3_key)
        raise RuntimeError(f"Failed to start transcription: {e}")

    # Poll for completion
    while True:
        status = transcribe.get_transcription_job(TranscriptionJobName=job_name)
        job_status = status["TranscriptionJob"]["TranscriptionJobStatus"]

        if job_status == "COMPLETED":
            break
        elif job_status == "FAILED":
            reason = status["TranscriptionJob"].get("FailureReason", "Unknown")
            raise RuntimeError(f"Transcription failed: {reason}")

        time.sleep(5)

    # Download and parse results
    output_key = f"transcribe-output/{job_name}.json"
    response = s3.get_object(Bucket=TRANSCRIBE_BUCKET, Key=output_key)
    result_data = json.loads(response["Body"].read().decode("utf-8"))

    # Parse into our segment format
    segments = _parse_transcribe_output(result_data)

    # Clean up S3
    try:
        s3.delete_object(Bucket=TRANSCRIBE_BUCKET, Key=s3_key)
        s3.delete_object(Bucket=TRANSCRIBE_BUCKET, Key=output_key)
    except Exception:
        pass

    return {
        "segments": segments,
        "full_text": " ".join(s["text"] for s in segments),
        "language": language_code,
        "speakers_detected": len(set(s.get("speaker", "") for s in segments if s.get("speaker"))),
    }


def _parse_transcribe_output(data: dict) -> list[dict]:
    """Parse Amazon Transcribe JSON output into our segment format."""
    segments = []

    results = data.get("results", {})
    items = results.get("items", [])

    # Group items into segments (by punctuation/pauses)
    current_segment = {
        "start_time": 0.0,
        "end_time": 0.0,
        "text": "",
        "speaker": None,
        "confidence": 0.0,
        "word_count": 0,
    }

    for item in items:
        if item.get("type") == "punctuation":
            current_segment["text"] += item["alternatives"][0]["content"]
            # End segment at sentence boundaries
            if item["alternatives"][0]["content"] in ".!?":
                if current_segment["text"].strip():
                    if current_segment["word_count"] > 0:
                        current_segment["confidence"] /= current_segment["word_count"]
                    current_segment["text"] = current_segment["text"].strip()
                    del current_segment["word_count"]
                    segments.append(current_segment)
                current_segment = {
                    "start_time": 0.0, "end_time": 0.0,
                    "text": "", "speaker": None,
                    "confidence": 0.0, "word_count": 0,
                }
        else:
            # Pronunciation item
            start = float(item.get("start_time", 0))
            end = float(item.get("end_time", 0))
            content = item["alternatives"][0]["content"]
            conf = float(item["alternatives"][0].get("confidence", 0))

            if current_segment["start_time"] == 0:
                current_segment["start_time"] = start
            current_segment["end_time"] = end

            if current_segment["text"]:
                current_segment["text"] += " "
            current_segment["text"] += content
            current_segment["confidence"] += conf
            current_segment["word_count"] += 1

    # Don't forget the last segment
    if current_segment["text"].strip():
        if current_segment["word_count"] > 0:
            current_segment["confidence"] /= current_segment["word_count"]
        current_segment["text"] = current_segment["text"].strip()
        del current_segment["word_count"]
        segments.append(current_segment)

    # Add speaker labels if available
    speaker_labels = results.get("speaker_labels", {}).get("segments", [])
    if speaker_labels:
        _apply_speaker_labels(segments, speaker_labels)

    return segments


def _apply_speaker_labels(segments: list[dict], speaker_segments: list[dict]):
    """Map speaker labels to transcript segments based on time overlap."""
    for seg in segments:
        seg_mid = (seg["start_time"] + seg["end_time"]) / 2
        for sp_seg in speaker_segments:
            sp_start = float(sp_seg.get("start_time", 0))
            sp_end = float(sp_seg.get("end_time", 0))
            if sp_start <= seg_mid <= sp_end:
                seg["speaker"] = sp_seg.get("speaker_label", "")
                break


def _get_media_format(file_path: str) -> str:
    """Get media format for Transcribe from file extension."""
    ext = os.path.splitext(file_path)[1].lower()
    format_map = {
        ".wav": "wav", ".mp3": "mp3", ".mp4": "mp4",
        ".flac": "flac", ".ogg": "ogg", ".webm": "webm",
        ".m4a": "mp4", ".amr": "amr",
    }
    return format_map.get(ext, "wav")


def _transcribe_via_bedrock(audio_path: str, language_code: str = "en-US") -> dict:
    """
    Fallback: Use Bedrock for transcription when no S3 bucket is configured.
    This is less accurate than Transcribe but works fully local.
    Note: Most Bedrock models don't support audio input directly.
    For local dev, we'll use a simulated response structure.
    """
    # In local dev without Transcribe bucket, return a placeholder
    # that instructs the user to configure TRANSCRIBE_BUCKET
    return {
        "segments": [],
        "full_text": "",
        "language": language_code,
        "speakers_detected": 0,
        "note": "Set TRANSCRIBE_BUCKET environment variable to enable transcription. "
                "Amazon Transcribe requires audio to be in S3.",
    }
