"""
Renaming router - handles file renaming with naming conventions.
"""
from fastapi import APIRouter, HTTPException
from ..models.schemas import (
    RenameRequest,
    RenamePreview,
    BulkRenameRequest,
    BulkRenamePreview,
    ApplyRenameRequest,
    ApplyRenameResult,
)
from ..services.bedrock_client import analyze_file
from ..services.renaming_service import preview_rename, apply_rename

router = APIRouter()


@router.post("/preview", response_model=RenamePreview)
async def preview_single_rename(request: RenameRequest):
    """Preview a rename for a single file using AI analysis and naming convention."""
    try:
        # If metadata is provided, use it; otherwise analyze the file
        if request.metadata:
            metadata = request.metadata.model_dump()
        else:
            metadata = analyze_file(
                file_path=request.file_path,
                model_id=request.model_id,
            )

        result = preview_rename(
            file_path=request.file_path,
            template=request.naming_convention.template,
            metadata=metadata,
            date_format=request.naming_convention.date_format,
            separator=request.naming_convention.separator,
            case=request.naming_convention.case,
            max_length=request.naming_convention.max_length,
            replace_spaces_with=request.naming_convention.replace_spaces_with,
        )
        return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {request.file_path}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview failed: {str(e)}")


@router.post("/preview-bulk", response_model=BulkRenamePreview)
async def preview_bulk_rename(request: BulkRenameRequest):
    """Preview renames for multiple files."""
    previews = []
    errors = []

    for file_path in request.file_paths:
        try:
            metadata = analyze_file(
                file_path=file_path,
                model_id=request.model_id,
            )
            result = preview_rename(
                file_path=file_path,
                template=request.naming_convention.template,
                metadata=metadata,
                date_format=request.naming_convention.date_format,
                separator=request.naming_convention.separator,
                case=request.naming_convention.case,
                max_length=request.naming_convention.max_length,
                replace_spaces_with=request.naming_convention.replace_spaces_with,
            )
            previews.append(result)
        except Exception as e:
            errors.append(f"Error processing {file_path}: {str(e)}")

    return {"previews": previews, "errors": errors}


@router.post("/apply", response_model=ApplyRenameResult)
async def apply_renames(request: ApplyRenameRequest):
    """Apply file renames."""
    try:
        renames = [r.model_dump() for r in request.renames]
        result = apply_rename(renames)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rename failed: {str(e)}")
