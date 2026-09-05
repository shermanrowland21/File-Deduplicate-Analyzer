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


def compute_md5(file_path: str, chunk_size: int = 1048576) -> Optional[str]:
    """
    Full-file MD5 hex digest. Needed to compare against Google Drive's
    md5Checksum field (Drive only exposes MD5, not SHA-256).
    """
    md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                md5.update(chunk)
        return md5.hexdigest()
    except (OSError, PermissionError):
        return None


def compute_hashes(file_path: str, file_size: int,
                   want_md5: bool = True) -> dict:
    """
    Single-read hasher: computes the dedup smart-hash (full SHA-256 for <50MB,
    256KB x3 fingerprint for >=50MB) AND the full-file MD5 in ONE pass over the
    file's bytes. Reading is the expensive part; running two hashers over the
    same bytes is nearly free.

    Returns {"smart": <sha256-or-fingerprint>, "md5": <full-md5>, "size": <int>}.
    Any hash that couldn't be computed is None.

    Note: for files >=50MB the "smart" hash is the 256KB x3 fingerprint (matching
    the existing dedup engine), while "md5" is always the FULL-file MD5 so it can
    be compared to Google's whole-file checksum.
    """
    sha256 = hashlib.sha256()
    md5 = hashlib.md5() if want_md5 else None
    is_large = file_size >= LARGE_FILE_THRESHOLD

    try:
        if is_large:
            # For the smart fingerprint we only sample 3 regions, but MD5 needs
            # the whole file. Read the whole file once, feed MD5 every chunk and
            # feed the fingerprint only the sampled ranges.
            sha256.update(str(file_size).encode())
            mid_start = max(0, file_size // 2 - SAMPLE_SIZE // 2)
            last_start = max(0, file_size - SAMPLE_SIZE)
            pos = 0
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(1048576)
                    if not chunk:
                        break
                    if md5:
                        md5.update(chunk)
                    # feed fingerprint the bytes that fall in sampled windows
                    cstart, cend = pos, pos + len(chunk)
                    for ws in (0, mid_start, last_start):
                        we = ws + SAMPLE_SIZE
                        s = max(cstart, ws)
                        e = min(cend, we)
                        if s < e:
                            sha256.update(chunk[s - cstart:e - cstart])
                    pos = cend
            smart = sha256.hexdigest()
        else:
            # Small file: full SHA-256 and full MD5 in the same pass.
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(1048576)
                    if not chunk:
                        break
                    sha256.update(chunk)
                    if md5:
                        md5.update(chunk)
            smart = sha256.hexdigest()
        return {"smart": smart,
                "md5": md5.hexdigest() if md5 else None,
                "size": file_size}
    except (OSError, PermissionError):
        return {"smart": None, "md5": None, "size": file_size}


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
    """Save hash cache to disk atomically (temp file + replace) so a crash
    during the write can never corrupt/truncate the existing cache."""
    cache_path = _get_cache_path(directory)
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp_path = cache_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"directory": directory, "entries": entries}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, cache_path)  # atomic on Windows + POSIX
    except OSError:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
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
        since_last_flush = 0
        FLUSH_EVERY = 500  # incrementally persist the hash cache every N files

        def _flush_cache(directory):
            """Merge new entries over the existing on-disk cache and save, so a
            crash never discards previously computed hashes (crash-resumable)."""
            merged = dict(caches[directory])          # previously cached hashes
            merged.update(new_caches[directory])       # plus anything hashed this run
            _save_cache(directory, merged)

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
                    # Still save cache for what we've processed (merge, don't lose prior)
                    _flush_cache(directory)
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
                    since_last_flush += 1

                    if processed % 100 == 0:
                        scan["duplicates_found"] = sum(
                            len(files) - 1
                            for files in scan["files"].values()
                            if len(files) > 1
                        )

                    # Incrementally persist the hash cache so a crash/kill mid-scan
                    # doesn't throw away hashing work — next run resumes from here.
                    if since_last_flush >= FLUSH_EVERY:
                        _flush_cache(directory)
                        since_last_flush = 0

                except (OSError, PermissionError):
                    continue

            # Flush at the end of each source directory too
            _flush_cache(directory)

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

        # Save caches for each directory (merge over prior so nothing is lost)
        for directory in directories:
            _flush_cache(directory)

        # Persist the completed scan results to disk so the Duplicates page and
        # the dedup resolver survive a backend restart (no re-scan needed).
        try:
            persist_scan(scan_id)
        except Exception:
            pass

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


def get_active_scan() -> Optional[dict]:
    """Return the status of the most recent RUNNING scan, if any. Lets the
    frontend re-attach and resume polling after a tab switch / page reload."""
    running = [(sid, s) for sid, s in _scans.items() if s.get("status") == "running"]
    if not running:
        return None
    # most recently started
    running.sort(key=lambda kv: kv[1].get("started_at", 0), reverse=True)
    return get_scan_status(running[0][0])


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
    """Get duplicate groups from a scan. Returns PARTIAL results while the scan
    is still running (grouping is maintained live), with an 'in_progress' flag,
    so the UI can show duplicates as they're found instead of only at the end."""
    if scan_id not in _scans:
        return None

    scan = _scans[scan_id]
    in_progress = scan["status"] not in ("completed", "cancelled")

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
        "status": scan["status"],
        "in_progress": in_progress,
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


# ------------------------------------------------------------ persistence
# Completed scans are saved to disk so the Duplicates page + dedup resolver
# survive a backend restart without a full re-scan.

_SCANS_STORE = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "scans")


def persist_scan(scan_id: str) -> Optional[str]:
    """Write a completed scan's grouped results + metadata to disk."""
    scan = _scans.get(scan_id)
    if not scan:
        return None
    os.makedirs(_SCANS_STORE, exist_ok=True)
    payload = {
        "scan_id": scan_id,
        "status": scan.get("status"),
        "directory": scan.get("directory"),
        "directories": scan.get("directories"),
        "started_at": scan.get("started_at"),
        "elapsed_seconds": scan.get("elapsed_seconds"),
        "processed_files": scan.get("processed_files"),
        "duplicates_found": scan.get("duplicates_found"),
        "saved_at": time.time(),
        # only keep hash-groups that actually have duplicates (saves space)
        "files": {h: fl for h, fl in scan.get("files", {}).items() if len(fl) > 1},
    }
    path = os.path.join(_SCANS_STORE, f"{scan_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    # update 'latest' pointer
    with open(os.path.join(_SCANS_STORE, "latest.json"), "w", encoding="utf-8") as f:
        json.dump({"scan_id": scan_id, "saved_at": payload["saved_at"]}, f)
    return path


def list_persisted_scans() -> list:
    """List saved scans (metadata only), newest first."""
    if not os.path.isdir(_SCANS_STORE):
        return []
    out = []
    for fn in os.listdir(_SCANS_STORE):
        if not fn.endswith(".json") or fn == "latest.json":
            continue
        try:
            with open(os.path.join(_SCANS_STORE, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
            groups = sum(1 for fl in d.get("files", {}).values() if len(fl) > 1)
            out.append({
                "scan_id": d.get("scan_id"),
                "directories": d.get("directories") or d.get("directory"),
                "saved_at": d.get("saved_at"),
                "duplicate_groups": groups,
                "duplicates_found": d.get("duplicates_found"),
            })
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda x: x.get("saved_at") or 0, reverse=True)
    return out


def load_persisted_scan(scan_id: str) -> bool:
    """Load a persisted scan back into memory so get_duplicates works on it."""
    if scan_id in _scans and _scans[scan_id].get("status") == "completed":
        return True
    path = os.path.join(_SCANS_STORE, f"{scan_id}.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    _scans[scan_id] = {
        "status": "completed",
        "files": d.get("files", {}),
        "all_files": [f for fl in d.get("files", {}).values() for f in fl],
        "directory": d.get("directory"),
        "directories": d.get("directories"),
        "started_at": d.get("started_at", 0),
        "elapsed_seconds": d.get("elapsed_seconds", 0),
        "processed_files": d.get("processed_files", 0),
        "duplicates_found": d.get("duplicates_found", 0),
        "phase": "complete",
    }
    return True


def latest_scan_id() -> Optional[str]:
    """Return the scan_id of the most recently persisted scan, if any."""
    p = os.path.join(_SCANS_STORE, "latest.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f).get("scan_id")
        except (OSError, json.JSONDecodeError):
            pass
    scans = list_persisted_scans()
    return scans[0]["scan_id"] if scans else None
