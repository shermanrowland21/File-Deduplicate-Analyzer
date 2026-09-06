"""
Renaming router - handles file renaming with naming conventions.
"""
import os
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from ..models.schemas import (
    RenameRequest,
    RenamePreview,
    BulkRenameRequest,
    BulkRenamePreview,
    ApplyRenameRequest,
    ApplyRenameResult,
)
from ..services.bedrock_client import analyze_file
from ..services.renaming_service import preview_rename, apply_rename, classify_file_type, FILE_TYPE_GROUPS

router = APIRouter()


@router.get("/type-groups")
async def type_groups():
    """List the file-type groups (image/video/document/…) so the UI can offer a
    naming convention per type."""
    return {"groups": list(FILE_TYPE_GROUPS.keys()) + ["default"],
            "extensions": {g: sorted(e) for g, e in FILE_TYPE_GROUPS.items()}}


@router.get("/list-files")
async def list_files(
    path: str = Query(..., description="Folder whose files to list"),
    recursive: bool = Query(False, description="Include files in subfolders"),
    include_hidden: bool = Query(False),
):
    """List FILES (not folders) in a directory, so the Smart Rename UI can load a
    whole folder's files without hand-typing paths."""
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail=f"Folder not found: {path}")
    files = []
    try:
        if recursive:
            for root, dirs, names in os.walk(path):
                if not include_hidden:
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                for n in names:
                    if not include_hidden and n.startswith("."):
                        continue
                    fp = os.path.join(root, n)
                    files.append({"path": fp.replace("\\", "/"), "name": n})
                    if len(files) >= 5000:
                        break
                if len(files) >= 5000:
                    break
        else:
            for n in sorted(os.listdir(path)):
                fp = os.path.join(path, n)
                if os.path.isfile(fp):
                    if not include_hidden and n.startswith("."):
                        continue
                    files.append({"path": fp.replace("\\", "/"), "name": n})
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"folder": path.replace("\\", "/"), "count": len(files),
            "truncated": len(files) >= 5000, "files": files}


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


class TypedBulkRenameRequest(BaseModel):
    model_config = {"protected_namespaces": ()}
    file_paths: list[str]
    # per-type conventions keyed by group name (image/video/document/…) + "default"
    conventions: dict  # {group: {template, date_format, separator, case, max_length, replace_spaces_with}}
    model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"


@router.post("/preview-bulk-typed", response_model=BulkRenamePreview)
async def preview_bulk_typed(request: TypedBulkRenameRequest):
    """Preview renames applying a DIFFERENT naming convention per file type
    (image vs video vs Word doc vs Excel …). Each file is classified by extension
    and named with its group's convention, falling back to 'default'."""
    previews = []
    errors = []
    conv = request.conventions or {}
    default = conv.get("default") or {"template": "{date}_{suggested_name}.{ext}"}

    for file_path in request.file_paths:
        try:
            group = classify_file_type(file_path)
            c = conv.get(group) or default
            metadata = analyze_file(file_path=file_path, model_id=request.model_id)
            result = preview_rename(
                file_path=file_path,
                template=c.get("template", default.get("template")),
                metadata=metadata,
                date_format=c.get("date_format", "%Y-%m-%d"),
                separator=c.get("separator", "_"),
                case=c.get("case", "lower"),
                max_length=c.get("max_length", 255),
                replace_spaces_with=c.get("replace_spaces_with", "_"),
            )
            # annotate which type-group convention was used (for UI grouping)
            r = result.model_dump() if hasattr(result, "model_dump") else dict(result)
            r["type_group"] = group
            previews.append(r)
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
