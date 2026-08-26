"""
Visual analysis service.
Analyzes extracted video frames using Bedrock (Claude Sonnet 4 / Nova Pro).
Performs OCR, scene description, object detection.

Local: sends frames to Bedrock API.
AWS: Lambda processes frames from S3, results to DynamoDB.
"""
import base64
import json
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError


def get_bedrock_client():
    """Get Bedrock runtime client."""
    return boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


# Best models for visual analysis tasks
VISUAL_MODELS = {
    "ocr": "anthropic.claude-sonnet-4-20250514-v1:0",  # Best at reading text in images
    "scene": "amazon.nova-pro-v1:0",  # Good multimodal scene understanding
    "detailed": "anthropic.claude-sonnet-4-20250514-v1:0",  # Most thorough analysis
    "fast": "amazon.nova-lite-v1:0",  # Quick classification, bulk processing
}


def analyze_frame(
    frame_path: str,
    analysis_type: str = "detailed",
    model_id: Optional[str] = None,
    custom_prompt: Optional[str] = None,
) -> dict:
    """
    Analyze a single video frame / image.

    analysis_type:
    - "detailed": Full description + OCR + objects + scene type
    - "ocr": Focus on extracting text from the image
    - "scene": Scene classification and description
    - "fast": Quick category and basic description

    Returns: {description, ocr_text, objects, scene_type, raw_analysis}
    """
    client = get_bedrock_client()

    if model_id is None:
        model_id = VISUAL_MODELS.get(analysis_type, VISUAL_MODELS["detailed"])

    # Read and encode the frame
    with open(frame_path, "rb") as f:
        image_bytes = f.read()

    # Build the analysis prompt
    if custom_prompt:
        prompt_text = custom_prompt
    elif analysis_type == "ocr":
        prompt_text = (
            "Extract ALL text visible in this image. Include text from signs, screens, "
            "documents, whiteboards, labels, captions — everything readable. "
            "Preserve the layout structure where possible. "
            "Return JSON: {\"ocr_text\": \"...\", \"text_regions\": [{\"text\": \"...\", \"location\": \"...\"}]}"
        )
    elif analysis_type == "scene":
        prompt_text = (
            "Describe this scene concisely. What is happening? Where is this? "
            "Identify key objects and people (without identifying real individuals). "
            "Return JSON: {\"description\": \"...\", \"scene_type\": \"...\", "
            "\"objects\": [...], \"setting\": \"...\"}"
        )
    elif analysis_type == "fast":
        prompt_text = (
            "Classify this image briefly. "
            "Return JSON: {\"category\": \"...\", \"description\": \"one sentence\"}"
        )
    else:  # detailed
        prompt_text = (
            "Analyze this image thoroughly. Provide:\n"
            "1. A detailed description of what you see\n"
            "2. Any text visible (OCR)\n"
            "3. Key objects identified\n"
            "4. The type of scene/setting\n"
            "5. Any notable details (colors, brands, dates, etc.)\n\n"
            "Return JSON: {\"description\": \"...\", \"ocr_text\": \"...\", "
            "\"objects\": [...], \"scene_type\": \"...\", \"details\": {...}}"
        )

    # Determine image format
    ext = os.path.splitext(frame_path)[1].lower()
    format_map = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".gif": "gif", ".webp": "webp"}
    img_format = format_map.get(ext, "jpeg")

    try:
        response = client.converse(
            modelId=model_id,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": img_format,
                            "source": {"bytes": image_bytes},
                        }
                    },
                    {"text": prompt_text},
                ],
            }],
            system=[{"text": "You are a precise visual analyst. Always respond with valid JSON only, no markdown."}],
            inferenceConfig={"maxTokens": 2048, "temperature": 0.1},
        )

        # Extract response
        response_text = ""
        for block in response["output"]["message"]["content"]:
            if "text" in block:
                response_text += block["text"]

        # Parse JSON
        response_text = response_text.strip()
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1] if "\n" in response_text else response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        response_text = response_text.strip()

        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            result = {"description": response_text, "ocr_text": "", "objects": [], "scene_type": "unknown"}

        return {
            "description": result.get("description", ""),
            "ocr_text": result.get("ocr_text", ""),
            "objects": result.get("objects", []),
            "scene_type": result.get("scene_type", result.get("category", "")),
            "details": result.get("details", {}),
            "model_used": model_id,
        }

    except ClientError as e:
        return {
            "description": "",
            "ocr_text": "",
            "objects": [],
            "scene_type": "error",
            "error": str(e),
        }


def analyze_frames_batch(
    frames: list[dict],
    analysis_type: str = "scene",
    model_id: Optional[str] = None,
) -> list[dict]:
    """
    Analyze multiple frames. Each frame dict has {timestamp, frame_path}.
    Returns list of analysis results with timestamps.
    """
    results = []
    for frame in frames:
        analysis = analyze_frame(
            frame_path=frame["frame_path"],
            analysis_type=analysis_type,
            model_id=model_id,
        )
        analysis["timestamp"] = frame["timestamp"]
        analysis["frame_path"] = frame["frame_path"]
        results.append(analysis)

    return results
