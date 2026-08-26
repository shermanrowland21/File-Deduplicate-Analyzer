"""
Topic and keyword extraction service.
Processes transcript chunks through Claude Haiku to extract:
- Topics discussed in each segment
- Keywords and entities
- Semantic summaries

Local: Bedrock API calls.
AWS: Lambda + Bedrock, results to DynamoDB/OpenSearch.
"""
import json
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError


# Use Haiku for speed on bulk topic extraction
DEFAULT_MODEL = "anthropic.claude-3-5-haiku-20241022-v1:0"
CHUNK_SIZE_SECONDS = 60  # Process transcript in 60-second chunks


def get_bedrock_client():
    """Get Bedrock runtime client."""
    return boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def chunk_transcript(segments: list[dict], chunk_duration: int = CHUNK_SIZE_SECONDS) -> list[dict]:
    """
    Group transcript segments into time-based chunks for topic extraction.
    Each chunk covers ~60 seconds of content.
    """
    if not segments:
        return []

    chunks = []
    current_chunk = {
        "start_time": segments[0]["start_time"],
        "end_time": segments[0]["end_time"],
        "text": "",
        "segments": [],
    }

    for seg in segments:
        # If this segment would push the chunk past the duration limit, start a new chunk
        if seg["start_time"] - current_chunk["start_time"] > chunk_duration and current_chunk["text"]:
            chunks.append(current_chunk)
            current_chunk = {
                "start_time": seg["start_time"],
                "end_time": seg["end_time"],
                "text": "",
                "segments": [],
            }

        current_chunk["end_time"] = seg["end_time"]
        if current_chunk["text"]:
            current_chunk["text"] += " "
        current_chunk["text"] += seg["text"]
        current_chunk["segments"].append(seg)

    # Don't forget the last chunk
    if current_chunk["text"]:
        chunks.append(current_chunk)

    return chunks


def extract_topics_from_chunk(
    chunk_text: str,
    context: str = "",
    model_id: str = DEFAULT_MODEL,
) -> dict:
    """
    Extract topics, keywords, and entities from a transcript chunk.
    Returns: {topics: [...], keywords: [...], entities: [...], summary: "..."}
    """
    client = get_bedrock_client()

    prompt = f"""Analyze this transcript segment and extract structured metadata.

Transcript:
\"\"\"{chunk_text}\"\"\"

{f"Context: {context}" if context else ""}

Return JSON with:
- topics: array of main topics discussed (2-5 topics, each a short phrase)
- keywords: array of important keywords/terms (5-15 keywords)
- entities: array of named entities mentioned (people, companies, places, products)
- summary: one sentence summary of what's discussed
- sentiment: overall tone (neutral, positive, negative, technical, casual)

Return ONLY valid JSON."""

    try:
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": "You extract structured metadata from transcripts. Always respond with valid JSON only."}],
            inferenceConfig={"maxTokens": 1024, "temperature": 0.1},
        )

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

        result = json.loads(response_text.strip())
        return result

    except (ClientError, json.JSONDecodeError) as e:
        return {
            "topics": [],
            "keywords": [],
            "entities": [],
            "summary": "",
            "error": str(e),
        }


def extract_topics_from_transcript(
    segments: list[dict],
    chunk_duration: int = CHUNK_SIZE_SECONDS,
    model_id: str = DEFAULT_MODEL,
) -> dict:
    """
    Process a full transcript — chunk it and extract topics from each chunk.
    Returns aggregated topics, keywords, and per-chunk details.
    """
    chunks = chunk_transcript(segments, chunk_duration)

    all_topics = []
    all_keywords = []
    chunk_results = []

    for chunk in chunks:
        if not chunk["text"].strip():
            continue

        result = extract_topics_from_chunk(
            chunk_text=chunk["text"],
            model_id=model_id,
        )

        # Add time context to each topic/keyword
        for topic in result.get("topics", []):
            all_topics.append({
                "topic": topic,
                "start_time": chunk["start_time"],
                "end_time": chunk["end_time"],
                "confidence": 1.0,
                "source": "transcript",
            })

        for keyword in result.get("keywords", []):
            all_keywords.append({
                "keyword": keyword,
                "start_time": chunk["start_time"],
                "end_time": chunk["end_time"],
                "frequency": 1,
            })

        chunk_results.append({
            "start_time": chunk["start_time"],
            "end_time": chunk["end_time"],
            "topics": result.get("topics", []),
            "keywords": result.get("keywords", []),
            "entities": result.get("entities", []),
            "summary": result.get("summary", ""),
        })

    # Deduplicate and count keyword frequency
    keyword_freq = {}
    for kw in all_keywords:
        key = kw["keyword"].lower()
        if key not in keyword_freq:
            keyword_freq[key] = {
                "keyword": kw["keyword"],
                "start_time": kw["start_time"],
                "end_time": kw["end_time"],
                "frequency": 0,
            }
        keyword_freq[key]["frequency"] += 1
        keyword_freq[key]["end_time"] = max(keyword_freq[key]["end_time"], kw["end_time"])

    return {
        "topics": all_topics,
        "keywords": list(keyword_freq.values()),
        "chunks": chunk_results,
        "total_chunks": len(chunks),
    }


def generate_file_summary(
    transcript_text: str,
    visual_descriptions: list[str] = None,
    model_id: str = "anthropic.claude-3-5-haiku-20241022-v1:0",
) -> dict:
    """
    Generate an overall summary of a media file from its transcript and visual analysis.
    """
    client = get_bedrock_client()

    visual_context = ""
    if visual_descriptions:
        visual_context = "\n\nVisual context from key frames:\n" + "\n".join(
            f"- {d}" for d in visual_descriptions[:10]
        )

    # Truncate transcript if too long
    max_chars = 50000
    truncated = transcript_text[:max_chars]
    if len(transcript_text) > max_chars:
        truncated += "\n[... transcript truncated ...]"

    prompt = f"""Analyze this media file content and provide a comprehensive summary.

Transcript:
\"\"\"{truncated}\"\"\"{visual_context}

Return JSON with:
- title: a descriptive title for this content (what you'd name this file)
- summary: 2-3 paragraph summary of the full content
- main_topics: array of the 5-10 main topics/subjects covered
- key_moments: array of {{time_description, what_happens}} for notable moments
- category: type of content (meeting, lecture, interview, tutorial, presentation, etc.)
- suggested_filename: a descriptive filename for this media

Return ONLY valid JSON."""

    try:
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": "You summarize media content. Always respond with valid JSON only."}],
            inferenceConfig={"maxTokens": 4096, "temperature": 0.2},
        )

        response_text = ""
        for block in response["output"]["message"]["content"]:
            if "text" in block:
                response_text += block["text"]

        response_text = response_text.strip()
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1] if "\n" in response_text else response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

        return json.loads(response_text.strip())

    except (ClientError, json.JSONDecodeError) as e:
        return {"error": str(e)}
