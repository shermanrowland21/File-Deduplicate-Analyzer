"""
Structured object tagging service.
During frame analysis, generates structured labels and tags using LLM vision.

Produces per-frame:
- Objects: [{name, confidence, category}]
- Materials: [stone, wood, metal, ...]
- Colors: [dominant colors]
- Scene tags: [outdoor, indoor, aerial, underwater, ...]
- Content type: [presentation, meeting, landscape, product, document, ...]
- Custom tags based on user-defined taxonomy

These structured tags enable precise filtered search:
"Find all frames with granite rocks in outdoor settings"
→ objects contains 'rock' AND materials contains 'granite' AND scene_tags contains 'outdoor'
"""
import json
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError


# Default model for tagging (fast + good at structured output)
TAG_MODEL = "anthropic.claude-3-5-haiku-20241022-v1:0"
# Higher quality model for detailed analysis
DETAIL_MODEL = "anthropic.claude-sonnet-4-20250514-v1:0"


def _get_bedrock_client():
    return boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def tag_frame(
    frame_path: str,
    model_id: str = TAG_MODEL,
    custom_taxonomy: Optional[list[str]] = None,
) -> dict:
    """
    Generate structured object tags for a frame/image.

    Returns:
    {
        "objects": [{"name": "rock", "category": "geological", "attributes": ["large", "grey"]}],
        "materials": ["granite", "moss"],
        "colors": ["grey", "green", "brown"],
        "scene_tags": ["outdoor", "nature", "forest"],
        "content_type": "landscape",
        "environment": "outdoor",
        "lighting": "natural daylight",
        "text_visible": false,
        "people_count": 0,
        "custom_tags": []
    }
    """
    client = _get_bedrock_client()

    custom_instruction = ""
    if custom_taxonomy:
        custom_instruction = (
            f"\n\nAlso classify against these custom categories, "
            f"marking which apply: {json.dumps(custom_taxonomy)}"
        )

    prompt = f"""Analyze this image and produce structured metadata tags.

Return ONLY valid JSON with these fields:
- objects: array of objects detected, each with {{name, category, attributes[]}}
  Categories: person, animal, vehicle, furniture, tool, food, geological, botanical, 
  architectural, electronic, document, clothing, art, container, equipment
- materials: array of visible materials (stone, wood, metal, glass, fabric, plastic, 
  water, sand, concrete, brick, paper, leather, ceramic, etc.)
- colors: array of dominant colors (up to 5)
- scene_tags: array of scene descriptors (outdoor, indoor, aerial, underwater, 
  close-up, wide-shot, night, day, urban, rural, forest, desert, ocean, mountain, etc.)
- content_type: single value (landscape, portrait, product, document, presentation, 
  meeting, tutorial, interview, performance, sports, nature, architecture, food, art)
- environment: single value (indoor, outdoor, studio, underwater, aerial)
- lighting: description of lighting (natural daylight, artificial, low-light, 
  backlit, overexposed, dramatic, soft, harsh)
- text_visible: boolean - is there readable text in the image?
- people_count: integer count of people visible
- custom_tags: array of any additional distinctive features worth noting{custom_instruction}

Be precise and specific. "Rock" is less useful than "granite boulder" or "sandstone cliff"."""

    try:
        with open(frame_path, "rb") as f:
            image_bytes = f.read()

        ext = os.path.splitext(frame_path)[1].lower()
        format_map = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".gif": "gif", ".webp": "webp"}
        img_format = format_map.get(ext, "jpeg")

        response = client.converse(
            modelId=model_id,
            messages=[{
                "role": "user",
                "content": [
                    {"image": {"format": img_format, "source": {"bytes": image_bytes}}},
                    {"text": prompt},
                ],
            }],
            system=[{"text": "You are a precise image tagger. Return only valid JSON."}],
            inferenceConfig={"maxTokens": 1024, "temperature": 0.1},
        )

        response_text = ""
        for block in response["output"]["message"]["content"]:
            if "text" in block:
                response_text += block["text"]

        # Clean JSON
        response_text = response_text.strip()
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1] if "\n" in response_text else response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

        tags = json.loads(response_text.strip())
        return tags

    except (ClientError, json.JSONDecodeError, Exception) as e:
        return {
            "objects": [],
            "materials": [],
            "colors": [],
            "scene_tags": [],
            "content_type": "unknown",
            "environment": "unknown",
            "lighting": "unknown",
            "text_visible": False,
            "people_count": 0,
            "custom_tags": [],
            "error": str(e),
        }


def tag_frames_batch(
    frames: list[dict],
    model_id: str = TAG_MODEL,
    custom_taxonomy: Optional[list[str]] = None,
) -> list[dict]:
    """
    Tag multiple frames. Each frame dict: {frame_path, timestamp, ...}
    Returns frames with added 'tags' field.
    """
    results = []
    for frame in frames:
        tags = tag_frame(
            frame_path=frame["frame_path"],
            model_id=model_id,
            custom_taxonomy=custom_taxonomy,
        )
        result = {**frame, "tags": tags}
        results.append(result)
    return results


def search_by_tags(
    query_tags: dict,
    all_frame_tags: list[dict],
    min_match_score: float = 0.5,
) -> list[dict]:
    """
    Search frames by structured tag criteria.

    query_tags example:
    {
        "objects": ["rock", "boulder"],
        "materials": ["granite"],
        "scene_tags": ["outdoor"],
    }

    Scores each frame by how many criteria match.
    """
    results = []

    for frame in all_frame_tags:
        tags = frame.get("tags", {})
        score = 0.0
        total_criteria = 0

        # Check objects
        if "objects" in query_tags:
            total_criteria += 1
            frame_objects = [o.get("name", "").lower() for o in tags.get("objects", [])]
            # Also check attributes
            frame_attrs = []
            for o in tags.get("objects", []):
                frame_attrs.extend([a.lower() for a in o.get("attributes", [])])
            query_objs = [q.lower() for q in query_tags["objects"]]
            matches = sum(1 for q in query_objs if any(q in obj for obj in frame_objects + frame_attrs))
            if matches > 0:
                score += matches / len(query_objs)

        # Check materials
        if "materials" in query_tags:
            total_criteria += 1
            frame_mats = [m.lower() for m in tags.get("materials", [])]
            query_mats = [q.lower() for q in query_tags["materials"]]
            matches = sum(1 for q in query_mats if any(q in m for m in frame_mats))
            if matches > 0:
                score += matches / len(query_mats)

        # Check scene_tags
        if "scene_tags" in query_tags:
            total_criteria += 1
            frame_scene = [s.lower() for s in tags.get("scene_tags", [])]
            query_scene = [q.lower() for q in query_tags["scene_tags"]]
            matches = sum(1 for q in query_scene if any(q in s for s in frame_scene))
            if matches > 0:
                score += matches / len(query_scene)

        # Check content_type
        if "content_type" in query_tags:
            total_criteria += 1
            if query_tags["content_type"].lower() in tags.get("content_type", "").lower():
                score += 1.0

        # Check colors
        if "colors" in query_tags:
            total_criteria += 1
            frame_colors = [c.lower() for c in tags.get("colors", [])]
            query_colors = [q.lower() for q in query_tags["colors"]]
            matches = sum(1 for q in query_colors if any(q in c for c in frame_colors))
            if matches > 0:
                score += matches / len(query_colors)

        # Normalize score
        if total_criteria > 0:
            normalized_score = score / total_criteria
            if normalized_score >= min_match_score:
                results.append({**frame, "match_score": normalized_score})

    # Sort by score descending
    results.sort(key=lambda x: x["match_score"], reverse=True)
    return results
