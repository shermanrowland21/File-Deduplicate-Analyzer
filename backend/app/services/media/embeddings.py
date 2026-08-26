"""
Multimodal embedding service using Amazon Titan Multimodal Embeddings.
Generates vector embeddings for images and text, stores in FAISS for similarity search.

Supports:
1. Image → vector (for visual similarity search)
2. Text → vector (for natural language visual search)
3. Cross-modal: text query finds similar images and vice versa

Local: FAISS index on disk.
AWS: OpenSearch Serverless with k-NN plugin.
"""
import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import faiss
import boto3
from botocore.exceptions import ClientError

# Vector store location
VECTOR_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "vectors")
INDEX_PATH = os.path.join(VECTOR_DIR, "visual_index.faiss")
METADATA_PATH = os.path.join(VECTOR_DIR, "visual_metadata.json")

# Titan Multimodal Embeddings output dimension
EMBEDDING_DIM = 1024

# Lock for thread-safe index operations
_lock = threading.Lock()

# Cached index and metadata
_index: Optional[faiss.IndexFlatIP] = None
_metadata: list[dict] = []


def _get_bedrock_client():
    """Get Bedrock runtime client."""
    return boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def _load_index():
    """Load or create the FAISS index and metadata."""
    global _index, _metadata

    os.makedirs(VECTOR_DIR, exist_ok=True)

    if os.path.exists(INDEX_PATH):
        _index = faiss.read_index(INDEX_PATH)
    else:
        # Use Inner Product (cosine similarity after normalization)
        _index = faiss.IndexFlatIP(EMBEDDING_DIM)

    if os.path.exists(METADATA_PATH):
        with open(METADATA_PATH, "r", encoding="utf-8") as f:
            _metadata = json.load(f)
    else:
        _metadata = []


def _save_index():
    """Persist index and metadata to disk."""
    global _index, _metadata

    os.makedirs(VECTOR_DIR, exist_ok=True)
    if _index is not None:
        faiss.write_index(_index, INDEX_PATH)
    with open(METADATA_PATH, "w", encoding="utf-8") as f:
        json.dump(_metadata, f)


def _ensure_loaded():
    """Ensure index is loaded."""
    global _index
    if _index is None:
        _load_index()


# --- Embedding Generation ---

def embed_image(image_path: str) -> Optional[np.ndarray]:
    """
    Generate embedding vector for an image using Titan Multimodal Embeddings.
    Returns a normalized 1024-dim vector.
    """
    client = _get_bedrock_client()

    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

        response = client.invoke_model(
            modelId="amazon.titan-embed-image-v1",
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "inputImage": image_b64,
                "embeddingConfig": {"outputEmbeddingLength": EMBEDDING_DIM},
            }),
        )

        result = json.loads(response["body"].read())
        embedding = np.array(result["embedding"], dtype=np.float32)

        # Normalize for cosine similarity
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        return embedding

    except ClientError as e:
        print(f"Embedding error for {image_path}: {e}")
        return None
    except Exception as e:
        print(f"Unexpected embedding error: {e}")
        return None


def embed_text(text: str) -> Optional[np.ndarray]:
    """
    Generate embedding vector for text using Titan Multimodal Embeddings.
    Same vector space as images — enables cross-modal search.
    """
    client = _get_bedrock_client()

    try:
        response = client.invoke_model(
            modelId="amazon.titan-embed-image-v1",
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "inputText": text,
                "embeddingConfig": {"outputEmbeddingLength": EMBEDDING_DIM},
            }),
        )

        result = json.loads(response["body"].read())
        embedding = np.array(result["embedding"], dtype=np.float32)

        # Normalize
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        return embedding

    except ClientError as e:
        print(f"Text embedding error: {e}")
        return None
    except Exception as e:
        print(f"Unexpected text embedding error: {e}")
        return None


def embed_image_bytes(image_bytes: bytes) -> Optional[np.ndarray]:
    """Generate embedding from raw image bytes (for uploaded reference images)."""
    client = _get_bedrock_client()

    try:
        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

        response = client.invoke_model(
            modelId="amazon.titan-embed-image-v1",
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "inputImage": image_b64,
                "embeddingConfig": {"outputEmbeddingLength": EMBEDDING_DIM},
            }),
        )

        result = json.loads(response["body"].read())
        embedding = np.array(result["embedding"], dtype=np.float32)

        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        return embedding

    except (ClientError, Exception) as e:
        print(f"Image bytes embedding error: {e}")
        return None


# --- Index Operations ---

def add_to_index(
    embedding: np.ndarray,
    metadata: dict,
):
    """
    Add an embedding to the FAISS index with metadata.

    metadata should include:
    - file_path: source video/image path
    - frame_path: path to the thumbnail/frame image
    - timestamp: time in video (for video frames)
    - description: text description of the frame
    - objects: list of detected objects
    - tags: structured tags
    """
    with _lock:
        _ensure_loaded()

        # Add to FAISS
        vector = embedding.reshape(1, -1).astype(np.float32)
        _index.add(vector)

        # Store metadata at matching index position
        _metadata.append(metadata)

        # Persist periodically (every 50 additions)
        if len(_metadata) % 50 == 0:
            _save_index()


def add_batch_to_index(embeddings: list[np.ndarray], metadata_list: list[dict]):
    """Add multiple embeddings at once (more efficient)."""
    with _lock:
        _ensure_loaded()

        vectors = np.array(embeddings, dtype=np.float32)
        _index.add(vectors)
        _metadata.extend(metadata_list)
        _save_index()


def search_by_vector(
    query_vector: np.ndarray,
    top_k: int = 20,
    min_score: float = 0.3,
) -> list[dict]:
    """
    Search the index for similar vectors.
    Returns top_k results with similarity scores.
    """
    with _lock:
        _ensure_loaded()

        if _index is None or _index.ntotal == 0:
            return []

        query = query_vector.reshape(1, -1).astype(np.float32)
        scores, indices = _index.search(query, min(top_k, _index.ntotal))

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(_metadata):
                continue
            if score < min_score:
                continue
            result = {**_metadata[idx], "similarity_score": float(score)}
            results.append(result)

        return results


def search_by_image(image_path: str, top_k: int = 20, min_score: float = 0.3) -> list[dict]:
    """Search for visually similar frames/images given an image file."""
    embedding = embed_image(image_path)
    if embedding is None:
        return []
    return search_by_vector(embedding, top_k, min_score)


def search_by_image_bytes(image_bytes: bytes, top_k: int = 20, min_score: float = 0.3) -> list[dict]:
    """Search for visually similar frames given uploaded image bytes."""
    embedding = embed_image_bytes(image_bytes)
    if embedding is None:
        return []
    return search_by_vector(embedding, top_k, min_score)


def search_by_text(query: str, top_k: int = 20, min_score: float = 0.2) -> list[dict]:
    """
    Natural language visual search.
    Text query is embedded in the same space as images — finds visually matching frames.
    e.g. "red sandstone formation near water" → frames containing that.
    """
    embedding = embed_text(query)
    if embedding is None:
        return []
    return search_by_vector(embedding, top_k, min_score)


# --- Index Management ---

def get_index_stats() -> dict:
    """Get stats about the current vector index."""
    _ensure_loaded()
    return {
        "total_vectors": _index.ntotal if _index else 0,
        "dimension": EMBEDDING_DIM,
        "index_path": INDEX_PATH,
        "metadata_entries": len(_metadata),
    }


def rebuild_index():
    """Clear and rebuild the index (call after re-analyzing files)."""
    global _index, _metadata
    with _lock:
        _index = faiss.IndexFlatIP(EMBEDDING_DIM)
        _metadata = []
        _save_index()


def flush_index():
    """Force save the current index state to disk."""
    with _lock:
        _save_index()
