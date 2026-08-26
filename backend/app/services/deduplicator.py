"""
Deduplication service - handles file removal, moving to trash, or relocating duplicates.
"""
import os
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional

from .file_scanner import human_readable_size


def deduplicate_files(
    files_to_remove: list[str],
    action: str = "move_to_trash",
    move_to_folder: Optional[str] = None,
) -> dict:
    """
    Remove duplicate files based on the specified action.
    
    Actions:
    - delete: Permanently delete files
    - move_to_trash: Move files to a _deduplicated_trash folder
    - move_to_folder: Move files to a specified folder
    """
    results = {
        "success": True,
        "files_processed": len(files_to_remove),
        "files_removed": 0,
        "space_freed": 0,
        "errors": [],
    }

    # Create trash/destination folder if needed
    if action == "move_to_trash":
        trash_folder = os.path.join(
            os.path.expanduser("~"),
            ".file_dedup_trash",
            datetime.now().strftime("%Y%m%d_%H%M%S"),
        )
        os.makedirs(trash_folder, exist_ok=True)
    elif action == "move_to_folder":
        if not move_to_folder:
            results["success"] = False
            results["errors"].append("move_to_folder path is required for move_to_folder action")
            return results
        os.makedirs(move_to_folder, exist_ok=True)

    for file_path in files_to_remove:
        try:
            if not os.path.exists(file_path):
                results["errors"].append(f"File not found: {file_path}")
                continue

            file_size = os.path.getsize(file_path)

            if action == "delete":
                os.remove(file_path)
            elif action == "move_to_trash":
                # Preserve relative structure in trash
                dest_name = os.path.basename(file_path)
                dest_path = os.path.join(trash_folder, dest_name)
                # Handle name collisions
                counter = 1
                while os.path.exists(dest_path):
                    name, ext = os.path.splitext(dest_name)
                    dest_path = os.path.join(trash_folder, f"{name}_{counter}{ext}")
                    counter += 1
                shutil.move(file_path, dest_path)
            elif action == "move_to_folder":
                dest_name = os.path.basename(file_path)
                dest_path = os.path.join(move_to_folder, dest_name)
                counter = 1
                while os.path.exists(dest_path):
                    name, ext = os.path.splitext(dest_name)
                    dest_path = os.path.join(move_to_folder, f"{name}_{counter}{ext}")
                    counter += 1
                shutil.move(file_path, dest_path)

            results["files_removed"] += 1
            results["space_freed"] += file_size

        except (OSError, PermissionError) as e:
            results["errors"].append(f"Error processing {file_path}: {str(e)}")

    results["space_freed_human"] = human_readable_size(results["space_freed"])
    if results["errors"]:
        results["success"] = len(results["errors"]) < len(files_to_remove)

    return results
