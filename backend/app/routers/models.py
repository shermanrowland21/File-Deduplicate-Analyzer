"""
Models router - lists available Bedrock models.
"""
from fastapi import APIRouter
from ..models.schemas import BedrockModel
from ..services.bedrock_client import get_available_models

router = APIRouter()


@router.get("/", response_model=list[BedrockModel])
async def list_models():
    """List all available Bedrock models for file analysis."""
    return get_available_models()
