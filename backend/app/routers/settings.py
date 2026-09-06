"""
Settings router — lets the user choose which Bedrock model each analysis function
uses, without editing code or restarting. All choices are Bedrock-only (in-AWS).
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import settings_store
from ..services.bedrock_client import AVAILABLE_MODELS

router = APIRouter()


@router.get("/models")
async def get_models():
    """Return each analysis function's current model choice + metadata, plus the
    list of available Bedrock models to choose from."""
    return {
        "functions": settings_store.get_all_models(),
        "available_models": AVAILABLE_MODELS,
    }


class SetModelRequest(BaseModel):
    key: str          # e.g. "file_namer_text", "dedup_advisor"
    model_id: str     # a Bedrock model id / inference profile; "" reverts to default


@router.post("/models")
async def set_model(request: SetModelRequest):
    """Set (or clear, if model_id is empty) the model for one function. Takes
    effect immediately — services resolve their model at call time."""
    try:
        value = settings_store.set_model(request.key, request.model_id.strip())
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"key": request.key, "value": value}


@router.post("/models/{key}/reset")
async def reset_model(key: str):
    """Revert one function to its env/built-in default."""
    try:
        value = settings_store.reset_model(key)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"key": key, "value": value}
