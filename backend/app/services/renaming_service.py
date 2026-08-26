"""
File renaming service with configurable naming conventions.
Applies naming templates using metadata from AI analysis.
"""
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional


def apply_naming_convention(
    file_path: str,
    template: str,
    metadata: dict,
    date_format: str = "%Y-%m-%d",
    separator: str = "_",
    case: str = "lower",
    max_length: int = 255,
    replace_spaces_with: str = "_",
) -> str:
    """
    Apply a naming convention template to generate a new filename.
    
    Available template variables:
    - {date}: File creation/modification date
    - {category}: AI-determined category
    - {description}: Short description from AI
    - {suggested_name}: AI-suggested name
    - {tags}: First 3 tags joined by separator
    - {ext}: Original file extension
    - {original}: Original filename (without extension)
    - {size}: Human readable file size
    - {mime}: MIME type category (image, video, document, etc.)
    - {counter}: Auto-incrementing counter (handled during rename)
    - {hash}: First 8 chars of file hash
    """
    original_name = Path(file_path).stem
    extension = Path(file_path).suffix.lower()
    
    # Get modification date
    try:
        mod_time = os.path.getmtime(file_path)
        file_date = datetime.fromtimestamp(mod_time).strftime(date_format)
    except OSError:
        file_date = datetime.now().strftime(date_format)

    # Build substitution values
    category = metadata.get("category", "unknown")
    description = metadata.get("description", "")
    suggested_name = metadata.get("suggested_name", original_name)
    tags = metadata.get("tags", [])
    mime_type = metadata.get("mime_type", "")
    mime_category = mime_type.split("/")[0] if mime_type and "/" in mime_type else "unknown"
    file_hash = metadata.get("hash", "00000000")[:8]

    # Clean up suggested name
    suggested_name = sanitize_filename(suggested_name, replace_spaces_with)
    
    # Build a short description for filename use
    short_desc = description[:50] if description else ""
    short_desc = sanitize_filename(short_desc, replace_spaces_with)

    # Tags as string
    tags_str = separator.join(tags[:3]) if tags else ""
    tags_str = sanitize_filename(tags_str, replace_spaces_with)

    # Template substitution
    new_name = template
    new_name = new_name.replace("{date}", file_date)
    new_name = new_name.replace("{category}", category)
    new_name = new_name.replace("{description}", short_desc)
    new_name = new_name.replace("{suggested_name}", suggested_name)
    new_name = new_name.replace("{tags}", tags_str)
    new_name = new_name.replace("{ext}", extension.lstrip("."))
    new_name = new_name.replace("{original}", original_name)
    new_name = new_name.replace("{mime}", mime_category)
    new_name = new_name.replace("{hash}", file_hash)
    new_name = new_name.replace("{counter}", "001")  # Default counter

    # Apply case transformation
    if case == "lower":
        new_name = new_name.lower()
    elif case == "upper":
        new_name = new_name.upper()
    elif case == "title":
        new_name = new_name.title()

    # Replace spaces
    new_name = new_name.replace(" ", replace_spaces_with)

    # Clean up multiple consecutive separators
    if separator:
        pattern = re.escape(separator) + "{2,}"
        new_name = re.sub(pattern, separator, new_name)

    # Remove leading/trailing separators
    new_name = new_name.strip(separator + " ._-")

    # Ensure extension is present
    if not new_name.endswith(extension):
        # Check if template already includes .{ext}
        if "{ext}" not in template:
            new_name = new_name + extension

    # Truncate if too long (preserve extension)
    if len(new_name) > max_length:
        name_part = new_name[: max_length - len(extension) - 1]
        new_name = name_part + extension

    return new_name


def sanitize_filename(name: str, replace_spaces_with: str = "_") -> str:
    """Remove or replace characters that are invalid in filenames."""
    # Remove/replace invalid characters
    invalid_chars = r'[<>:"/\\|?*\x00-\x1f]'
    name = re.sub(invalid_chars, "", name)
    # Replace spaces
    name = name.replace(" ", replace_spaces_with)
    # Remove leading/trailing dots and spaces
    name = name.strip(". ")
    return name


def preview_rename(
    file_path: str,
    template: str,
    metadata: dict,
    date_format: str = "%Y-%m-%d",
    separator: str = "_",
    case: str = "lower",
    max_length: int = 255,
    replace_spaces_with: str = "_",
) -> dict:
    """Generate a rename preview without actually renaming."""
    new_name = apply_naming_convention(
        file_path=file_path,
        template=template,
        metadata=metadata,
        date_format=date_format,
        separator=separator,
        case=case,
        max_length=max_length,
        replace_spaces_with=replace_spaces_with,
    )
    
    directory = os.path.dirname(file_path)
    new_path = os.path.join(directory, new_name)

    return {
        "original_path": file_path.replace("\\", "/"),
        "original_name": os.path.basename(file_path),
        "new_name": new_name,
        "new_path": new_path.replace("\\", "/"),
    }


def apply_rename(renames: list[dict]) -> dict:
    """
    Apply file renames.
    Each rename dict should have 'original_path' and 'new_path'.
    """
    results = {
        "success": True,
        "files_renamed": 0,
        "errors": [],
    }

    for rename in renames:
        original = rename["original_path"]
        new_path = rename["new_path"]

        try:
            if not os.path.exists(original):
                results["errors"].append(f"File not found: {original}")
                continue

            # Handle collision - add counter
            final_path = new_path
            counter = 1
            while os.path.exists(final_path) and final_path != original:
                name, ext = os.path.splitext(new_path)
                final_path = f"{name}_{counter:03d}{ext}"
                counter += 1

            # Perform the rename
            shutil.move(original, final_path)
            results["files_renamed"] += 1

        except (OSError, PermissionError) as e:
            results["errors"].append(f"Error renaming {original}: {str(e)}")

    if results["errors"]:
        results["success"] = results["files_renamed"] > 0

    return results
