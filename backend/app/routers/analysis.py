"""
Analysis router - handles AI-powered file analysis via AWS Bedrock.
"""
from fastapi import APIRouter, HTTPException
from ..models.schemas import AnalysisRequest, FileMetadata
from ..services.bedrock_client import analyze_file

router = APIRouter()


@router.post("/analyze", response_model=FileMetadata)
async def analyze_single_file(request: AnalysisRequest):
    """Analyze a single file using AWS Bedrock and return metadata."""
    try:
        result = analyze_file(
            file_path=request.file_path,
            model_id=request.model_id,
            custom_prompt=request.analysis_prompt,
        )
        return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {request.file_path}")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")


@router.post("/analyze-batch")
async def analyze_batch(file_paths: list[str], model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"):
    """Analyze multiple files and return metadata for each."""
    results = []
    errors = []

    for file_path in file_paths:
        try:
            result = analyze_file(file_path=file_path, model_id=model_id)
            results.append(result)
        except Exception as e:
            errors.append({"file_path": file_path, "error": str(e)})

    return {"results": results, "errors": errors}
