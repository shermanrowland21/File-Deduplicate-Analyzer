"""
Visual search router - image similarity, natural language visual search, and tag-based search.
Three search modes:
1. Image similarity: upload a reference image, find matching frames
2. Natural language: describe what you're looking for in words
3. Structured tags: filter by objects, materials, colors, scene type
"""
import os
import json
import tempfile
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

from ..services.media.embeddings import (
    embed_image, embed_text, embed_image_bytes,
    search_by_image, search_by_image_bytes, search_by_text, search_by_vector,
    add_to_index, add_batch_to_index, get_index_stats, flush_index, rebuild_index,
)
from ..services.media.object_tagger import tag_frame, tag_frames_batch, search_by_tags
from ..services.media import metadata_store

router = APIRouter()


# --- Image Similarity Search ---

@router.post("/search-by-image")
async def visual_search_by_image(
    image: UploadFile = File(...),
    top_k: int = Form(default=20),
    min_score: float = Form(default=0.3),
):
    """
    Upload a reference image and find visually similar frames across all analyzed media.
    Use case: "Find me all frames that look like this rock"
    """
    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image file")

    results = search_by_image_bytes(image_bytes, top_k=top_k, min_score=min_score)

    if not results:
        return {
            "query_type": "image_similarity",
            "results": [],
            "total": 0,
            "note": "No similar frames found. Ensure media has been analyzed and indexed.",
        }

    return {
        "query_type": "image_similarity",
        "results": results,
        "total": len(results),
    }


@router.get("/search-by-image-path")
async def visual_search_by_image_path(
    image_path: str = Query(...),
    top_k: int = 20,
    min_score: float = 0.3,
):
    """
    Search by a local image path (for using an already-extracted frame as reference).
    """
    if not os.path.exists(image_path):
        raise HTTPException(status_code=404, detail=f"Image not found: {image_path}")

    results = search_by_image(image_path, top_k=top_k, min_score=min_score)

    return {
        "query_type": "image_similarity",
        "reference_image": image_path,
        "results": results,
        "total": len(results),
    }


# --- Natural Language Visual Search ---

class TextSearchRequest(BaseModel):
    query: str
    top_k: int = 20
    min_score: float = 0.2


@router.post("/search-by-text")
async def visual_search_by_text(request: TextSearchRequest):
    """
    Natural language visual search.
    Describe what you're looking for and find matching frames.
    e.g. "red sandstone formation near water", "whiteboard with diagram", "person presenting slides"
    """
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    results = search_by_text(request.query.strip(), top_k=request.top_k, min_score=request.min_score)

    return {
        "query_type": "natural_language",
        "query": request.query,
        "results": results,
        "total": len(results),
    }


@router.get("/search-by-text")
async def visual_search_by_text_get(
    q: str = Query(...),
    top_k: int = 20,
    min_score: float = 0.2,
):
    """GET version of natural language visual search."""
    results = search_by_text(q.strip(), top_k=top_k, min_score=min_score)
    return {
        "query_type": "natural_language",
        "query": q,
        "results": results,
        "total": len(results),
    }


# --- Structured Tag Search ---

class TagSearchRequest(BaseModel):
    objects: Optional[list[str]] = None  # e.g. ["rock", "boulder"]
    materials: Optional[list[str]] = None  # e.g. ["granite", "sandstone"]
    colors: Optional[list[str]] = None  # e.g. ["red", "grey"]
    scene_tags: Optional[list[str]] = None  # e.g. ["outdoor", "mountain"]
    content_type: Optional[str] = None  # e.g. "landscape"
    min_match_score: float = 0.5


@router.post("/search-by-tags")
async def visual_search_by_tags(request: TagSearchRequest):
    """
    Search frames by structured tags.
    Filter by objects, materials, colors, scene type.
    e.g. objects=["rock"] + materials=["granite"] + scene_tags=["outdoor"]
    """
    # Build query dict from non-None fields
    query_tags = {}
    if request.objects:
        query_tags["objects"] = request.objects
    if request.materials:
        query_tags["materials"] = request.materials
    if request.colors:
        query_tags["colors"] = request.colors
    if request.scene_tags:
        query_tags["scene_tags"] = request.scene_tags
    if request.content_type:
        query_tags["content_type"] = request.content_type

    if not query_tags:
        raise HTTPException(status_code=400, detail="At least one filter criterion required")

    # Load all tagged frames from the metadata store
    # For now, load from the vector metadata which includes tags
    from ..services.media.embeddings import _metadata, _ensure_loaded
    _ensure_loaded()

    tagged_frames = [m for m in _metadata if "tags" in m]

    results = search_by_tags(query_tags, tagged_frames, min_match_score=request.min_match_score)

    return {
        "query_type": "structured_tags",
        "query_tags": query_tags,
        "results": results[:50],  # Cap at 50
        "total": len(results),
    }


# --- Combined Search (all three modes at once) ---

class CombinedSearchRequest(BaseModel):
    text_query: Optional[str] = None
    tag_filters: Optional[TagSearchRequest] = None
    top_k: int = 30


@router.post("/search-combined")
async def visual_search_combined(request: CombinedSearchRequest):
    """
    Combined search using both text similarity and tag filters.
    First gets text-based results, then filters by tags.
    """
    results = []

    if request.text_query:
        text_results = search_by_text(request.text_query.strip(), top_k=request.top_k * 2)
        results = text_results

    # Apply tag filters if provided
    if request.tag_filters and results:
        query_tags = {}
        if request.tag_filters.objects:
            query_tags["objects"] = request.tag_filters.objects
        if request.tag_filters.materials:
            query_tags["materials"] = request.tag_filters.materials
        if request.tag_filters.colors:
            query_tags["colors"] = request.tag_filters.colors
        if request.tag_filters.scene_tags:
            query_tags["scene_tags"] = request.tag_filters.scene_tags

        if query_tags:
            # Filter text results by tags
            filtered = search_by_tags(query_tags, results, min_match_score=0.3)
            results = filtered

    return {
        "query_type": "combined",
        "text_query": request.text_query,
        "results": results[:request.top_k],
        "total": len(results),
    }


# --- Index Management ---

@router.get("/index-stats")
async def get_visual_index_stats():
    """Get statistics about the visual search index."""
    return get_index_stats()


@router.post("/index-frame")
async def index_single_frame(
    frame_path: str = Form(...),
    file_path: str = Form(default=""),
    timestamp: float = Form(default=0.0),
    description: str = Form(default=""),
    generate_tags: bool = Form(default=True),
):
    """
    Index a single frame: generate embedding + tags, add to vector store.
    Call this during analysis pipeline for each extracted keyframe.
    """
    if not os.path.exists(frame_path):
        raise HTTPException(status_code=404, detail=f"Frame not found: {frame_path}")

    # Generate embedding
    embedding = embed_image(frame_path)
    if embedding is None:
        raise HTTPException(status_code=500, detail="Failed to generate embedding")

    # Generate structured tags
    tags = {}
    if generate_tags:
        tags = tag_frame(frame_path)

    # Build metadata
    meta = {
        "frame_path": frame_path.replace("\\", "/"),
        "file_path": file_path.replace("\\", "/"),
        "filename": os.path.basename(file_path) if file_path else os.path.basename(frame_path),
        "timestamp": timestamp,
        "description": description,
        "tags": tags,
    }

    # Add to index
    add_to_index(embedding, meta)

    return {"success": True, "metadata": meta}


@router.post("/index-frames-batch")
async def index_frames_batch(frames: list[dict]):
    """
    Batch index multiple frames.
    Each frame: {frame_path, file_path, timestamp, description}
    Generates embeddings and tags for all, adds to vector store.
    """
    import numpy as np

    embeddings = []
    metadata_list = []
    errors = []

    for frame in frames:
        frame_path = frame.get("frame_path", "")
        if not os.path.exists(frame_path):
            errors.append(f"Not found: {frame_path}")
            continue

        embedding = embed_image(frame_path)
        if embedding is None:
            errors.append(f"Embedding failed: {frame_path}")
            continue

        tags = tag_frame(frame_path)

        meta = {
            "frame_path": frame_path.replace("\\", "/"),
            "file_path": frame.get("file_path", "").replace("\\", "/"),
            "filename": os.path.basename(frame.get("file_path", frame_path)),
            "timestamp": frame.get("timestamp", 0.0),
            "description": frame.get("description", ""),
            "tags": tags,
        }

        embeddings.append(embedding)
        metadata_list.append(meta)

    if embeddings:
        add_batch_to_index(embeddings, metadata_list)

    return {
        "indexed": len(embeddings),
        "errors": errors,
        "total_in_index": get_index_stats()["total_vectors"],
    }


@router.post("/rebuild-index")
async def rebuild_visual_index():
    """Clear and rebuild the visual search index."""
    rebuild_index()
    return {"success": True, "message": "Index cleared. Re-analyze media to rebuild."}


@router.get("/frame-image")
async def get_frame_image(path: str = Query(...)):
    """Serve a frame image for display in search results."""
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Frame not found")
    return FileResponse(path, media_type="image/jpeg")
