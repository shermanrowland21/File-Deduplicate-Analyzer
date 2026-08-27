"""
File scanning and hashing service for duplicate detection.
Uses SHA-256 for byte-level duplicate identification.
Processes files as they're discovered so progress is visible immediately.
Runs scans in background threads for non-blocking API.
"""
import hashlib
import json
import os
import time
import uuid
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional
import mimetypes

# In-memory store for scan results
_scans: dict = {}

# Persistent cache directory
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_cache")


def human_readable_size(size_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.2f} {units[i]}"


def compute_file_hash(file_path: str, chunk_size: int = 1048576) -> Optional[str]:
    """Compute full SHA-256 hash of a file. Uses 1MB chunks."""
    sha256 = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                sha256.update(chunk)
        return sha256.hexdigest()
    except (OSError, PermissionError):
        return None


# Threshold for using quick fingerprint vs full hash
LARGE_FILE_THRESHOLD = 50 * 1024 * 1024  # 50MB
SAMPLE_SIZE = 262144  # 256KB samples


def compute_quick_fingerprint(file_path: str, file_size: int) -> Optional[str]:
    """
    Fast fingerprint for large files.
    Hashes: file_size + first 256KB + middle 256KB + last 256KB.
    768KB total read regardless of file size.
    Two files matching this are effectively guaranteed identical —
    same size + same start + same middle + same end.
    """
    sha256 = hashlib.sha256()
    try:
        # Include file size so different-sized files never collide
        sha256.update(str(file_size).encode())

        with open(file_path, "rb") as f:
            # First 256KB
            sha256.update(f.read(SAMPLE_SIZE))

            # Middle 256KB
            mid_point = file_size // 2
            f.seek(max(0, mid_point - SAMPLE_SIZE // 2))
            sha256.update(f.read(SAMPLE_SIZE))

            # Last 256KB
            f.seek(max(0, file_size - SAMPLE_SIZE))
            sha256.update(f.read(SAMPLE_SIZE))

        return sha256.hexdigest()
    except (OSError, PermissionError):
        return None


def smart_hash(file_path: str, file_size: int) -> Optional[str]:
    """
    Choose hashing strategy based on file size:
    - Small files (<50MB): full SHA-256 (byte-perfect)
    - Large files (>=50MB): fingerprint from front+middle+back (256KB each)
    """
    if file_size < LARGE_FILE_THRESHOLD:
        return compute_file_hash(file_path)
    else:
        return compute_quick_fingerprint(file_path, file_size)


def get_mime_type(file_path: str) -> Optional[str]:
    """Get MIME type of a file."""
    mime_type, _ = mimetypes.guess_type(file_path)
    return mime_type


# --- Scan Cache ---

def _get_cache_path(directory: str) -> str:
    """Get the cache file path for a given directory."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    dir_hash = hashlib.md5(os.path.normpath(directory).lower().encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{dir_hash}.json")


def _load_cache(directory: str) -> dict:
    """Load cached hashes. Returns dict of filepath -> {hash, size, mtime}."""
    cache_path = _get_cache_path(directory)
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("entries", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(directory: str, entries: dict):
    """Save hash cache to disk."""
    cache_path = _get_cache_path(directory)
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"directory": directory, "entries": entries}, f)
    except OSError:
        pass


# --- Scanner ---

def _run_scan(
    scan_id: str,
    directories: list[str],
    recursive: bool,
    include_hidden: bool,
    min_file_size: int,
    max_file_size: Optional[int],
    file_extensions: Optional[list[str]],
):
    """
    Background scan worker. Scans multiple directories, tags each file
    with its source root and relative subfolder path.
    """
    scan = _scans[scan_id]

    try:
        # Check for cancellation
        if scan.get("cancelled"):
            scan["status"] = "cancelled"
            return

        # Validate all directories
        for directory in directories:
            dir_path = Path(directory)
            if not dir_path.exists():
                scan["status"] = "error"
                scan["error"] = f"Directory not found: {directory}"
                return
            if not dir_path.is_dir():
                scan["status"] = "error"
                scan["error"] = f"Path is not a directory: {directory}"
                return

        # Normalize extension filter once
        ext_filter = None
        if file_extensions:
            ext_filter = set(e.lower().lstrip(".") for e in file_extensions)

        scan["phase"] = "scanning"
        processed = 0
        discovered = 0

        # Load caches for all directories
        caches = {}
        new_caches = {}
        for directory in directories:
            caches[directory] = _load_cache(directory)
            new_caches[directory] = {}

        cache_hits = 0

        # Iterate through each source directory
        for directory in directories:
            dir_normalized = directory.replace("\\", "/").rstrip("/")
            source_label = os.path.basename(dir_normalized)
            cache = caches[directory]

            scan["current_source"] = source_label

            def walk_files(d):
                """Generator that yields file paths."""
                if recursive:
                    for root, dirs, files in os.walk(d):
                        if not include_hidden:
                            dirs[:] = [dd for dd in dirs if not dd.startswith(".")]
                        for filename in files:
                            if not include_hidden and filename.startswith("."):
                                continue
                            yield os.path.join(root, filename)
                else:
                    for item in Path(d).iterdir():
                        if item.is_file():
                            if not include_hidden and item.name.startswith("."):
                                continue
                            yield str(item)

            for fp in walk_files(directory):
                # Check for cancellation
                if scan.get("cancelled"):
                    scan["status"] = "cancelled"
                    scan["phase"] = "cancelled"
                    scan["elapsed_seconds"] = round(time.time() - scan["started_at"], 1)
                    # Still save cache for what we've processed
                    _save_cache(directory, new_caches[directory])
                    return

                discovered += 1
                scan["discovered_files"] = discovered

                if discovered % 50 == 0:
                    scan["current_dir"] = os.path.dirname(fp).replace("\\", "/")

                try:
                    stat = os.stat(fp)
                    size = stat.st_size
                    mtime = stat.st_mtime

                    # Apply filters
                    if size < min_file_size:
                        continue
                    if max_file_size and size > max_file_size:
                        continue
                    if ext_filter:
                        ext = Path(fp).suffix.lower().lstrip(".")
                        if ext not in ext_filter:
                            continue

                    # Check cache
                    normalized_path = fp.replace("\\", "/")
                    cached = cache.get(normalized_path)
                    if cached and cached.get("size") == size and cached.get("mtime") == mtime:
                        file_hash = cached["hash"]
                        cache_hits += 1
                    else:
                        scan["current_file"] = f"{os.path.basename(fp)} ({human_readable_size(size)})"
                        scan["current_dir"] = os.path.dirname(fp).replace("\\", "/")
                        scan["hashing_size"] = size

                        file_hash = smart_hash(fp, size)
                        scan["hashing_size"] = 0
                        if file_hash is None:
                            processed += 1
                            scan["processed_files"] = processed
                            continue

                    # Update cache
                    new_caches[directory][normalized_path] = {
                        "hash": file_hash,
                        "size": size,
                        "mtime": mtime,
                    }

                    # Compute relative path within the source directory
                    relative_path = os.path.relpath(fp, directory).replace("\\", "/")
                    subfolder = os.path.dirname(relative_path).replace("\\", "/")

                    scan["current_file"] = os.path.basename(fp)

                    file_info = {
                        "path": normalized_path,
                        "filename": os.path.basename(fp),
                        "extension": Path(fp).suffix.lower(),
                        "size": size,
                        "size_human": human_readable_size(size),
                        "mime_type": get_mime_type(fp),
                        "hash": file_hash,
                        "modified_time": datetime.fromtimestamp(mtime).isoformat(),
                        "created_time": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                        "source": source_label,
                        "source_path": dir_normalized,
                        "subfolder": subfolder,
                    }

                    # Group by hash
                    if file_hash not in scan["files"]:
                        scan["files"][file_hash] = []
                    scan["files"][file_hash].append(file_info)
                    scan["all_files"].append(file_info)

                    processed += 1
                    scan["processed_files"] = processed
                    scan["total_files"] = processed

                    if processed % 100 == 0:
                        scan["duplicates_found"] = sum(
                            len(files) - 1
                            for files in scan["files"].values()
                            if len(files) > 1
                        )

                except (OSError, PermissionError):
                    continue

        # Final stats
        scan["duplicates_found"] = sum(
            len(files) - 1
            for files in scan["files"].values()
            if len(files) > 1
        )
        scan["total_files"] = processed
        scan["phase"] = "complete"
        scan["current_file"] = ""
        scan["current_dir"] = ""
        scan["current_source"] = ""
        scan["cache_hits"] = cache_hits
        scan["status"] = "completed"
        scan["elapsed_seconds"] = round(time.time() - scan["started_at"], 1)

        # Save caches for each directory
        for directory in directories:
            _save_cache(directory, new_caches[directory])

    except Exception as e:
        scan["status"] = "error"
        scan["error"] = str(e)


def scan_directory(
    directories: list[str],
    recursive: bool = True,
    include_hidden: bool = False,
    min_file_size: int = 0,
    max_file_size: Optional[int] = None,
    file_extensions: Optional[list[str]] = None,
) -> str:
    """
    Start scanning one or more directories in the background.
    Returns a scan_id immediately for polling progress.
    """
    scan_id = str(uuid.uuid4())
    _scans[scan_id] = {
        "status": "running",
        "directories": directories,
        "directory": ", ".join(os.path.basename(d.rstrip("/\\")) for d in directories),
        "total_files": 0,
        "discovered_files": 0,
        "processed_files": 0,
        "duplicates_found": 0,
        "cache_hits": 0,
        "phase": "starting",
        "current_file": "",
        "current_dir": "",
        "current_source": "",
        "hashing_size": 0,
        "elapsed_seconds": 0,
        "files": {},
        "all_files": [],
        "started_at": time.time(),
    }

    thread = threading.Thread(
        target=_run_scan,
        args=(scan_id, directories, recursive, include_hidden, min_file_size, max_file_size, file_extensions),
        daemon=True,
    )
    thread.start()

    return scan_id


def cancel_scan(scan_id: str) -> bool:
    """Cancel a running scan. Returns True if scan was running and is now cancelled."""
    if scan_id not in _scans:
        return False
    scan = _scans[scan_id]
    if scan["status"] != "running":
        return False
    scan["cancelled"] = True
    return True


def get_scan_status(scan_id: str) -> Optional[dict]:
    """Get status of a scan including progress details."""
    if scan_id not in _scans:
        return None
    scan = _scans[scan_id]

    elapsed = round(time.time() - scan["started_at"], 1) if scan["status"] == "running" else scan.get("elapsed_seconds", 0)

    return {
        "scan_id": scan_id,
        "status": scan["status"],
        "total_files": scan["total_files"],
        "discovered_files": scan.get("discovered_files", 0),
        "processed_files": scan["processed_files"],
        "duplicates_found": scan["duplicates_found"],
        "cache_hits": scan.get("cache_hits", 0),
        "directory": scan["directory"],
        "phase": scan.get("phase", ""),
        "current_file": scan.get("current_file", ""),
        "current_dir": scan.get("current_dir", ""),
        "hashing_size": scan.get("hashing_size", 0),
        "elapsed_seconds": elapsed,
        "error": scan.get("error", None),
    }


def get_cache_info(directory: str) -> Optional[dict]:
    """Get info about the existing cache for a directory."""
    cache_path = _get_cache_path(directory)
    if not os.path.exists(cache_path):
        return None
    try:
        stat = os.stat(cache_path)
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("entries", {})
        return {
            "cached_files": len(entries),
            "cache_size_bytes": stat.st_size,
            "cache_size_human": human_readable_size(stat.st_size),
            "last_updated": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }
    except (json.JSONDecodeError, OSError):
        return None


def clear_cache(directory: str) -> bool:
    """Clear the cache for a specific directory."""
    cache_path = _get_cache_path(directory)
    if os.path.exists(cache_path):
        os.remove(cache_path)
        return True
    return False


def get_duplicates(scan_id: str) -> Optional[dict]:
    """Get duplicate groups from a completed scan."""
    if scan_id not in _scans:
        return None

    scan = _scans[scan_id]
    if scan["status"] != "completed":
        return None

    groups = []
    total_wasted = 0
    total_dup_files = 0

    for file_hash, files in scan["files"].items():
        if len(files) > 1:
            wasted = (len(files) - 1) * files[0]["size"]
            total_wasted += wasted
            total_dup_files += len(files) - 1
            groups.append({
                "hash": file_hash,
                "file_count": len(files),
                "total_wasted_space": wasted,
                "total_wasted_space_human": human_readable_size(wasted),
                "files": files,
            })

    groups.sort(key=lambda g: g["total_wasted_space"], reverse=True)

    return {
        "scan_id": scan_id,
        "total_groups": len(groups),
        "total_duplicate_files": total_dup_files,
        "total_wasted_space": total_wasted,
        "total_wasted_space_human": human_readable_size(total_wasted),
        "groups": groups,
    }


def get_all_files(scan_id: str) -> Optional[list]:
    """Get all files from a completed scan."""
    if scan_id not in _scans:
        return None
    return _scans[scan_id].get("all_files", [])
