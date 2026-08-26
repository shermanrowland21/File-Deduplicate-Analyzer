"""
AWS Bedrock integration for file analysis.
Supports text, images, video, and document analysis using available models.
"""
import base64
import json
import os
import mimetypes
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# Supported models with their capabilities
AVAILABLE_MODELS = [
    {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_name": "Claude 3.5 Sonnet v2",
        "provider": "Anthropic",
        "supports_images": True,
        "supports_video": False,
        "description": "Best balance of intelligence and speed. Supports text and image analysis.",
    },
    {
        "model_id": "anthropic.claude-3-5-haiku-20241022-v1:0",
        "model_name": "Claude 3.5 Haiku",
        "provider": "Anthropic",
        "supports_images": True,
        "supports_video": False,
        "description": "Fastest model, good for bulk file analysis. Supports text and images.",
    },
    {
        "model_id": "anthropic.claude-sonnet-4-20250514-v1:0",
        "model_name": "Claude Sonnet 4",
        "provider": "Anthropic",
        "supports_images": True,
        "supports_video": True,
        "description": "Latest Claude model with advanced reasoning. Supports text, images, and video.",
    },
    {
        "model_id": "amazon.titan-text-express-v1",
        "model_name": "Titan Text Express",
        "provider": "Amazon",
        "supports_images": False,
        "supports_video": False,
        "description": "Amazon's text model. Good for text-only file analysis.",
    },
    {
        "model_id": "amazon.nova-pro-v1:0",
        "model_name": "Amazon Nova Pro",
        "provider": "Amazon",
        "supports_images": True,
        "supports_video": True,
        "description": "Amazon's multimodal model. Supports text, images, and video analysis.",
    },
    {
        "model_id": "amazon.nova-lite-v1:0",
        "model_name": "Amazon Nova Lite",
        "provider": "Amazon",
        "supports_images": True,
        "supports_video": True,
        "description": "Lightweight multimodal model. Fast and cost-effective for bulk analysis.",
    },
]

# File type categories
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".toml",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss",
    ".java", ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".rb", ".php",
    ".sh", ".bat", ".ps1", ".sql", ".r", ".swift", ".kt", ".scala",
    ".ini", ".cfg", ".conf", ".env", ".log",
}


def get_bedrock_client():
    """Create a Bedrock runtime client."""
    try:
        session = boto3.Session()
        client = session.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        return client
    except NoCredentialsError:
        raise RuntimeError(
            "AWS credentials not configured. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
            "environment variables or configure ~/.aws/credentials"
        )


def get_available_models() -> list[dict]:
    """Return list of available Bedrock models."""
    return AVAILABLE_MODELS


def classify_file(file_path: str) -> str:
    """Classify a file into a category based on extension."""
    ext = Path(file_path).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    elif ext in VIDEO_EXTENSIONS:
        return "video"
    elif ext in DOCUMENT_EXTENSIONS:
        return "document"
    elif ext in TEXT_EXTENSIONS:
        return "text"
    else:
        return "binary"


def read_file_for_analysis(file_path: str) -> dict:
    """
    Read a file and prepare it for Bedrock analysis.
    Returns a dict with type, content (base64 or text), and mime_type.
    """
    file_type = classify_file(file_path)
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    file_size = os.path.getsize(file_path)

    if file_type == "text":
        # Read text content directly
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(100000)  # Limit to 100KB of text
            return {
                "type": "text",
                "content": content,
                "mime_type": mime_type,
                "file_size": file_size,
            }
        except Exception:
            # Fall back to binary read
            file_type = "binary"

    if file_type == "image":
        # Read and base64 encode image
        with open(file_path, "rb") as f:
            content = base64.standard_b64encode(f.read()).decode("utf-8")
        # Map to supported media types
        media_type = mime_type
        if media_type == "image/svg+xml":
            media_type = "image/png"  # SVG not directly supported, would need conversion
        return {
            "type": "image",
            "content": content,
            "mime_type": media_type,
            "file_size": file_size,
        }

    if file_type == "video":
        # For video, we'll read a portion for analysis
        # Most models have size limits, so we may need to sample
        max_video_size = 25 * 1024 * 1024  # 25MB limit for most models
        with open(file_path, "rb") as f:
            content = base64.standard_b64encode(f.read(max_video_size)).decode("utf-8")
        return {
            "type": "video",
            "content": content,
            "mime_type": mime_type,
            "file_size": file_size,
        }

    if file_type == "document":
        # For PDFs and documents, read as binary
        with open(file_path, "rb") as f:
            content = base64.standard_b64encode(f.read()).decode("utf-8")
        return {
            "type": "document",
            "content": content,
            "mime_type": mime_type,
            "file_size": file_size,
        }

    # Binary/unknown files - read metadata only
    return {
        "type": "binary",
        "content": None,
        "mime_type": mime_type,
        "file_size": file_size,
    }


def analyze_file(
    file_path: str,
    model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0",
    custom_prompt: Optional[str] = None,
) -> dict:
    """
    Analyze a file using AWS Bedrock and extract metadata.
    Returns structured metadata about the file content.
    """
    client = get_bedrock_client()
    file_data = read_file_for_analysis(file_path)
    filename = os.path.basename(file_path)
    extension = Path(file_path).suffix.lower()

    # Build the analysis prompt
    base_prompt = custom_prompt or ""
    system_prompt = """You are a file analysis assistant. Analyze the provided file and return a JSON response with the following fields:
- description: A concise 1-2 sentence description of what this file contains
- category: A single category word (e.g., "photo", "document", "spreadsheet", "code", "video", "audio", "presentation", "archive", "configuration", "data")
- tags: An array of 3-7 relevant tags describing the content
- suggested_name: A descriptive filename (without extension) that accurately represents the content
- content_summary: A brief paragraph summarizing the key content
- additional_metadata: An object with any relevant extra metadata (e.g., for images: subject, location, colors; for documents: topic, author type; for code: language, purpose)

Respond ONLY with valid JSON, no markdown formatting or explanation."""

    if base_prompt:
        system_prompt += f"\n\nAdditional instructions: {base_prompt}"

    # Build messages based on file type and model capabilities
    messages = []
    model_info = next((m for m in AVAILABLE_MODELS if m["model_id"] == model_id), None)

    if file_data["type"] == "text":
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "text": f"Analyze this file (filename: {filename}, type: {file_data['mime_type']}, size: {file_data['file_size']} bytes):\n\n```\n{file_data['content'][:50000]}\n```"
                    }
                ],
            }
        ]
    elif file_data["type"] == "image" and model_info and model_info["supports_images"]:
        media_type = file_data["mime_type"]
        # Ensure valid media type for Bedrock
        valid_image_types = ["image/jpeg", "image/png", "image/gif", "image/webp"]
        if media_type not in valid_image_types:
            media_type = "image/png"
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": media_type.split("/")[1],
                            "source": {
                                "bytes": base64.standard_b64decode(file_data["content"])
                            },
                        }
                    },
                    {
                        "text": f"Analyze this image file (filename: {filename}, size: {file_data['file_size']} bytes). Describe what you see and provide metadata."
                    },
                ],
            }
        ]
    elif file_data["type"] == "video" and model_info and model_info["supports_video"]:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "video": {
                            "format": extension.lstrip("."),
                            "source": {
                                "bytes": base64.standard_b64decode(file_data["content"])
                            },
                        }
                    },
                    {
                        "text": f"Analyze this video file (filename: {filename}, size: {file_data['file_size']} bytes). Describe the content and provide metadata."
                    },
                ],
            }
        ]
    elif file_data["type"] == "document":
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "document": {
                            "format": extension.lstrip("."),
                            "name": filename,
                            "source": {
                                "bytes": base64.standard_b64decode(file_data["content"])
                            },
                        }
                    },
                    {
                        "text": f"Analyze this document (filename: {filename}, type: {file_data['mime_type']}, size: {file_data['file_size']} bytes)."
                    },
                ],
            }
        ]
    else:
        # For binary files or unsupported types, analyze based on metadata only
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "text": f"Analyze this file based on its metadata only (filename: {filename}, type: {file_data['mime_type']}, size: {file_data['file_size']} bytes, extension: {extension}). Provide your best analysis based on the filename and type."
                    }
                ],
            }
        ]

    try:
        # Use the Converse API for consistent interface across models
        response = client.converse(
            modelId=model_id,
            messages=messages,
            system=[{"text": system_prompt}],
            inferenceConfig={
                "maxTokens": 2048,
                "temperature": 0.1,
            },
        )

        # Extract response text
        response_text = ""
        for block in response["output"]["message"]["content"]:
            if "text" in block:
                response_text += block["text"]

        # Parse JSON response
        # Clean up any markdown formatting the model might add
        response_text = response_text.strip()
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        response_text = response_text.strip()

        metadata = json.loads(response_text)

        return {
            "file_path": file_path.replace("\\", "/"),
            "filename": filename,
            "extension": extension,
            "size": file_data["file_size"],
            "mime_type": file_data["mime_type"],
            "description": metadata.get("description", ""),
            "category": metadata.get("category", "unknown"),
            "tags": metadata.get("tags", []),
            "suggested_name": metadata.get("suggested_name", ""),
            "content_summary": metadata.get("content_summary", ""),
            "additional_metadata": metadata.get("additional_metadata", {}),
        }

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_msg = e.response["Error"]["Message"]
        raise RuntimeError(f"Bedrock API error ({error_code}): {error_msg}")
    except json.JSONDecodeError:
        # If we can't parse JSON, return raw response in description
        return {
            "file_path": file_path.replace("\\", "/"),
            "filename": filename,
            "extension": extension,
            "size": file_data["file_size"],
            "mime_type": file_data["mime_type"],
            "description": response_text[:500] if response_text else "Analysis failed",
            "category": "unknown",
            "tags": [],
            "suggested_name": Path(filename).stem,
            "content_summary": response_text[:1000] if response_text else "",
            "additional_metadata": {},
        }
