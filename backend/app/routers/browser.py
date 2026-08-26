"""
Directory browser router - allows clicking through the filesystem to pick a directory.
"""
import os
import string
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()


@router.get("/drives")
async def list_drives():
    """List available drives (Windows) or root (Linux/Mac)."""
    drives = []
    if os.name == "nt":
        # Windows: check all drive letters
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                try:
                    # Get drive label if possible
                    drives.append({
                        "name": f"{letter}:",
                        "path": drive,
                    })
                except Exception:
                    pass
    else:
        # Unix-like: start from root
        drives.append({"name": "/", "path": "/"})
        # Also add home directory as a shortcut
        home = os.path.expanduser("~")
        drives.append({"name": f"~ ({home})", "path": home})

    return {"drives": drives}


@router.get("/list")
async def list_directory(path: str = Query(..., description="Directory path to list")):
    """List subdirectories in a given path. Only returns directories, not files."""
    try:
        target = Path(path)
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail=f"Path is not a directory: {path}")

        entries = []
        try:
            for item in sorted(target.iterdir(), key=lambda x: x.name.lower()):
                if item.is_dir():
                    # Skip hidden directories and system dirs
                    if item.name.startswith(".") or item.name.startswith("$"):
                        continue
                    try:
                        # Check if we can actually access it
                        has_subdirs = any(
                            sub.is_dir()
                            for sub in item.iterdir()
                            if not sub.name.startswith(".") and not sub.name.startswith("$")
                        )
                    except (PermissionError, OSError):
                        has_subdirs = False

                    entries.append({
                        "name": item.name,
                        "path": str(item).replace("\\", "/"),
                        "has_children": has_subdirs,
                    })
        except PermissionError:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}")

        # Also return some info about file count in this dir
        file_count = 0
        try:
            file_count = sum(1 for f in target.iterdir() if f.is_file())
        except (PermissionError, OSError):
            pass

        return {
            "path": str(target).replace("\\", "/"),
            "parent": str(target.parent).replace("\\", "/") if target.parent != target else None,
            "directories": entries,
            "file_count": file_count,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/quick-access")
async def quick_access():
    """Return common quick-access directories."""
    home = os.path.expanduser("~")
    shortcuts = []

    common_dirs = [
        ("Home", home),
        ("Desktop", os.path.join(home, "Desktop")),
        ("Documents", os.path.join(home, "Documents")),
        ("Downloads", os.path.join(home, "Downloads")),
        ("Pictures", os.path.join(home, "Pictures")),
        ("Videos", os.path.join(home, "Videos")),
        ("Music", os.path.join(home, "Music")),
    ]

    for name, path in common_dirs:
        if os.path.exists(path):
            shortcuts.append({"name": name, "path": path.replace("\\", "/")})

    return {"shortcuts": shortcuts}
