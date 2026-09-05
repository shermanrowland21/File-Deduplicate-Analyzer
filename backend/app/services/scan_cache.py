"""
Scan cache - persists file hash results to disk so subsequent scans
only need to hash new or modified files.

Cache key: normalized directory path
Cache entries keyed by: file path -> {hash, size, mtime}

If a file's size and mtime haven't changed, we reuse the cached hash.
"""
import json
import os
import hashlib
from pathlib import Path
from typing import Optional

# Store caches in user's home directory
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_cache")


def _get_cache_path(directory: str) -> str:
    """Get the cache file path for a given directory."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    # Create a safe filename from the directory path
    dir_hash = hashlib.md5(os.path.normpath(directory).lower().encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{dir_hash}.json")


def load_cache(directory: str) -> dict:
    """
    Load cached scan data for a directory.
    Returns dict of: filepath -> {hash, size, mtime}
    """
    cache_path = _get_cache_path(directory)
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("entries", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(directory: str, entries: dict):
    """
    Save scan cache for a directory.
    entries: dict of filepath -> {hash, size, mtime}
    """
    cache_path = _get_cache_path(directory)
    os.makedirs(CACHE_DIR, exist_ok=True)
    data = {
        "directory": directory,
        "entries": entries,
    }
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass  # Non-critical if cache write fails


def get_cached_hash(cache: dict, file_path: str, size: int, mtime: float) -> Optional[str]:
    """
    Check if we have a valid cached hash for this file.
    Valid = same size and modification time.
    """
    normalized = file_path.replace("\\", "/")
    entry = cache.get(normalized)
    if entry is None:
        return None
    # If size and mtime match, the file hasn't changed
    if entry.get("size") == size and entry.get("mtime") == mtime:
        return entry.get("hash")
    return None


def get_cached_entry(cache: dict, file_path: str, size: int, mtime: float) -> Optional[dict]:
    """
    Return the full cached entry ({hash, md5, size, mtime}) if size+mtime match,
    else None. Backward compatible: entries created before md5 existed simply
    have no 'md5' key.
    """
    normalized = file_path.replace("\\", "/")
    entry = cache.get(normalized)
    if entry is None:
        return None
    if entry.get("size") == size and entry.get("mtime") == mtime:
        return entry
    return None


def put_cached_entry(cache: dict, file_path: str, size: int, mtime: float,
                     smart_hash_val: Optional[str], md5_val: Optional[str]) -> None:
    """
    Store/refresh a cache entry with both the dedup smart-hash and the full MD5.
    Mutates `cache` in place. `hash` stays the smart-hash for dedup compatibility.
    """
    normalized = file_path.replace("\\", "/")
    entry = cache.get(normalized) or {}
    entry["size"] = size
    entry["mtime"] = mtime
    if smart_hash_val is not None:
        entry["hash"] = smart_hash_val
    if md5_val is not None:
        entry["md5"] = md5_val
    cache[normalized] = entry


def build_cache_entries(all_files: list) -> dict:
    """
    Build cache entries dict from a list of scanned file info dicts.
    """
    entries = {}
    for file_info in all_files:
        path = file_info["path"].replace("\\", "/")
        entries[path] = {
            "hash": file_info["hash"],
            "size": file_info["size"],
            "mtime": file_info.get("mtime_raw", 0),
        }
    return entries


def get_cache_stats(directory: str) -> Optional[dict]:
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
            "last_updated": stat.st_mtime,
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


def clear_all_caches() -> int:
    """Clear all cached scan data. Returns number of caches cleared."""
    if not os.path.exists(CACHE_DIR):
        return 0
    count = 0
    for f in os.listdir(CACHE_DIR):
        if f.endswith(".json"):
            os.remove(os.path.join(CACHE_DIR, f))
            count += 1
    return count
