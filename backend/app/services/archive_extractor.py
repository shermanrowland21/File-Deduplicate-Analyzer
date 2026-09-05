"""
Archive extraction service.
Handles Google Takeout multi-part zips, standard zips, tar.gz, 7z.

Google Takeout specifics:
- Often comes as multiple numbered zip files (takeout-xxx-001.zip, 002.zip, etc.)
- Each zip is independent (not split archive), just separate chunks of your data
- JSON metadata sidecar files alongside media files
- Nested folder structure: Takeout/Google Photos/2023/...
"""
import os
import re
import zipfile
import tarfile
import threading
import time
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional


def _find_7zip() -> Optional[str]:
    """Locate 7-Zip executable if installed."""
    candidates = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # Check PATH
    from shutil import which
    found = which("7z")
    return found


SEVENZIP_PATH = _find_7zip()

# Track extraction jobs
_extract_jobs: dict = {}


def get_extraction_job(job_id: str) -> Optional[dict]:
    """Get status of an extraction job."""
    return _extract_jobs.get(job_id)


def find_archives(directory: str, recursive: bool = True) -> list[dict]:
    """
    Find all archive files in a directory (and subdirectories if recursive).
    Returns info about each archive found.
    """
    archives = []
    archive_extensions = {".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".bz2", ".7z", ".rar"}

    try:
        if recursive:
            for root, dirs, files in os.walk(directory):
                for filename in sorted(files):
                    name_lower = filename.lower()
                    is_archive = False
                    for ext in archive_extensions:
                        if name_lower.endswith(ext):
                            is_archive = True
                            break
                    if is_archive:
                        full_path = os.path.join(root, filename)
                        try:
                            size = os.path.getsize(full_path)
                            archives.append({
                                "path": full_path.replace("\\", "/"),
                                "filename": filename,
                                "relative_path": os.path.relpath(full_path, directory).replace("\\", "/"),
                                "size": size,
                                "size_human": _human_size(size),
                                "extension": Path(filename).suffix.lower(),
                            })
                        except OSError:
                            pass
        else:
            for item in sorted(Path(directory).iterdir()):
                if item.is_file():
                    name_lower = item.name.lower()
                    is_archive = False
                    for ext in archive_extensions:
                        if name_lower.endswith(ext):
                            is_archive = True
                            break
                    if is_archive:
                        archives.append({
                            "path": str(item).replace("\\", "/"),
                            "filename": item.name,
                            "relative_path": item.name,
                            "size": item.stat().st_size,
                            "size_human": _human_size(item.stat().st_size),
                            "extension": item.suffix.lower(),
                        })
    except (OSError, PermissionError):
        pass

    return archives


# Background archive discovery with progress
_find_jobs: dict = {}


def find_archives_background(directory: str, recursive: bool = True) -> str:
    """Start finding archives in background. Returns job_id for polling."""
    job_id = f"find_{int(time.time())}_{Path(directory).name[:15]}"
    _find_jobs[job_id] = {
        "status": "running",
        "directory": directory,
        "archives_found": 0,
        "folders_scanned": 0,
        "current_folder": "",
        "archives": [],
        "total_size": 0,
        "total_size_human": "0 B",
        "started_at": time.time(),
    }

    thread = threading.Thread(
        target=_find_archives_worker,
        args=(job_id, directory, recursive),
        daemon=True,
    )
    thread.start()
    return job_id


def get_find_job(job_id: str):
    """Get status of a find archives job."""
    return _find_jobs.get(job_id)


def cancel_find_job(job_id: str) -> bool:
    """Cancel a running find job. Keeps results found so far."""
    if job_id not in _find_jobs:
        return False
    _find_jobs[job_id]["cancelled"] = True
    return True


def _find_archives_worker(job_id: str, directory: str, recursive: bool):
    """Background worker to find archives with progress updates."""
    job = _find_jobs[job_id]
    archive_extensions = {".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".bz2", ".7z", ".rar"}

    try:
        if recursive:
            for root, dirs, files in os.walk(directory):
                if job.get("cancelled"):
                    job["status"] = "completed"
                    job["current_folder"] = ""
                    job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
                    return

                job["folders_scanned"] += 1
                job["current_folder"] = os.path.relpath(root, directory).replace("\\", "/")

                for filename in files:
                    name_lower = filename.lower()
                    is_archive = False
                    for ext in archive_extensions:
                        if name_lower.endswith(ext):
                            is_archive = True
                            break
                    if is_archive:
                        full_path = os.path.join(root, filename)
                        try:
                            size = os.path.getsize(full_path)
                            job["archives"].append({
                                "path": full_path.replace("\\", "/"),
                                "filename": filename,
                                "relative_path": os.path.relpath(full_path, directory).replace("\\", "/"),
                                "size": size,
                                "size_human": _human_size(size),
                                "extension": Path(filename).suffix.lower(),
                            })
                            job["archives_found"] += 1
                            job["total_size"] += size
                            job["total_size_human"] = _human_size(job["total_size"])
                        except OSError:
                            pass
        else:
            for item in Path(directory).iterdir():
                if item.is_file():
                    name_lower = item.name.lower()
                    for ext in archive_extensions:
                        if name_lower.endswith(ext):
                            size = item.stat().st_size
                            job["archives"].append({
                                "path": str(item).replace("\\", "/"),
                                "filename": item.name,
                                "relative_path": item.name,
                                "size": size,
                                "size_human": _human_size(size),
                                "extension": item.suffix.lower(),
                            })
                            job["archives_found"] += 1
                            job["total_size"] += size
                            job["total_size_human"] = _human_size(job["total_size"])
                            break

        job["status"] = "completed"
        job["current_folder"] = ""
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# --- Combined Scan + Extract (parallel pipeline) ---
# Walks the tree and extracts each archive as it's found, using a worker pool.

from concurrent.futures import ThreadPoolExecutor
import json as _json

_scan_extract_jobs: dict = {}

# Persistent extraction ledger — survives restarts, prevents re-extraction
LEDGER_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer")
LEDGER_PATH = os.path.join(LEDGER_DIR, "extraction_ledger.json")
_ledger_lock = threading.Lock()


def _archive_key(archive_path: str) -> str:
    """Unique key for an archive: normalized path + size + mtime."""
    try:
        stat = os.stat(archive_path)
        return f"{os.path.normpath(archive_path).lower()}|{stat.st_size}|{int(stat.st_mtime)}"
    except OSError:
        return os.path.normpath(archive_path).lower()


def _load_ledger() -> dict:
    """Load the extraction ledger from disk."""
    if not os.path.exists(LEDGER_PATH):
        return {}
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            return _json.load(f)
    except (_json.JSONDecodeError, OSError):
        return {}


def _record_extraction(archive_path: str, output_dir: str, file_count: int):
    """Record a completed extraction in the ledger."""
    with _ledger_lock:
        ledger = _load_ledger()
        key = _archive_key(archive_path)
        try:
            size = os.path.getsize(archive_path)
        except OSError:
            size = 0
        ledger[key] = {
            "archive_path": archive_path.replace("\\", "/"),
            "filename": os.path.basename(archive_path),
            "output_dir": output_dir.replace("\\", "/"),
            "file_count": file_count,
            "size": size,
            "extracted_at": datetime.now().isoformat(),
        }
        os.makedirs(LEDGER_DIR, exist_ok=True)
        try:
            with open(LEDGER_PATH, "w", encoding="utf-8") as f:
                _json.dump(ledger, f, indent=2)
        except OSError:
            pass


def is_already_extracted(archive_path: str) -> bool:
    """Check if an archive has already been extracted (same path, size, mtime)."""
    ledger = _load_ledger()
    return _archive_key(archive_path) in ledger


def get_ledger_entries() -> list[dict]:
    """Get all ledger entries for display."""
    ledger = _load_ledger()
    return list(ledger.values())


def get_ledger_stats() -> dict:
    """Get summary stats of the extraction ledger."""
    ledger = _load_ledger()
    entries = list(ledger.values())
    total_files = sum(e.get("file_count", 0) for e in entries)
    total_size = sum(e.get("size", 0) for e in entries)
    return {
        "total_archives_extracted": len(entries),
        "total_files_extracted": total_files,
        "total_size": total_size,
        "total_size_human": _human_size(total_size),
    }


def clear_ledger():
    """Clear the entire extraction ledger (forces re-extraction of everything)."""
    with _ledger_lock:
        if os.path.exists(LEDGER_PATH):
            os.remove(LEDGER_PATH)


# --- Flatten & Reorganize: collapse wrapper folders into clean structure ---
#
# Turns the deep Workspace-export tree into a flat, readable layout:
#
#   Personal accounts:
#     FROM  extracted/<batch>/user@domain.com/takeout-xxx/Takeout/Drive/<contents>
#     TO    <dest>/Personal - user@domain.com/<contents>
#
#   Shared drives (each named folder inside a Resource export becomes top-level):
#     FROM  extracted/<batch>/Resource$1 -xxx/takeout-xxx/Takeout/Drive/<SharedName>/<contents>
#     TO    <dest>/<SharedName>/<contents>
#
_flatten_jobs: dict = {}


def flatten_extracted(extracted_root: str, dest_root: Optional[str] = None) -> str:
    """
    Reorganize an extracted Workspace export into a clean, flat structure.
    Moves (not copies) content so it's fast and space-neutral.
    """
    if dest_root is None:
        dest_root = os.path.join(os.path.dirname(extracted_root.rstrip("/\\")), "Organized")

    job_id = f"flatten_{int(time.time())}"
    _flatten_jobs[job_id] = {
        "status": "running",
        "extracted_root": extracted_root,
        "dest_root": dest_root.replace("\\", "/"),
        "personal_accounts": 0,
        "shared_drives": 0,
        "items_moved": 0,
        "current": "",
        "collisions_renamed": 0,
        "cancelled": False,
        "started_at": time.time(),
    }
    thread = threading.Thread(target=_flatten_worker, args=(job_id, extracted_root, dest_root), daemon=True)
    thread.start()
    return job_id


def get_flatten_job(job_id: str):
    return _flatten_jobs.get(job_id)


def cancel_flatten(job_id: str) -> bool:
    if job_id in _flatten_jobs:
        _flatten_jobs[job_id]["cancelled"] = True
        return True
    return False


def _find_drive_folder(account_root: str) -> Optional[str]:
    """
    Given an account/resource folder, locate the Takeout/Drive folder inside,
    handling the takeout-xxx wrapper layer. Returns the Drive folder path or None.
    """
    try:
        for entry in os.listdir(account_root):
            wrapper = os.path.join(account_root, entry)
            if not os.path.isdir(wrapper):
                continue
            # Common: <account>/takeout-xxx/Takeout/Drive
            candidate = os.path.join(wrapper, "Takeout", "Drive")
            if os.path.isdir(candidate):
                return candidate
            # Sometimes: <account>/Takeout/Drive (no takeout wrapper)
        # Direct: <account>/Takeout/Drive
        direct = os.path.join(account_root, "Takeout", "Drive")
        if os.path.isdir(direct):
            return direct
    except OSError:
        pass
    return None


def _safe_move(src: str, dst: str, job: dict):
    """Move src into dst, renaming on collision to avoid overwrite."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    final = dst
    if os.path.exists(final):
        base = os.path.basename(dst)
        parent = os.path.dirname(dst)
        name, ext = os.path.splitext(base)
        counter = 1
        while os.path.exists(final):
            final = os.path.join(parent, f"{name} ({counter}){ext}")
            counter += 1
        job["collisions_renamed"] += 1
    shutil.move(src, final)
    job["items_moved"] += 1


def _flatten_worker(job_id: str, extracted_root: str, dest_root: str):
    job = _flatten_jobs[job_id]
    try:
        os.makedirs(dest_root, exist_ok=True)

        # The export may have a batch-id layer (e.g. 20260713T195041Z). Descend into it.
        roots_to_scan = [extracted_root]
        try:
            entries = [os.path.join(extracted_root, e) for e in os.listdir(extracted_root)]
            batch_dirs = [e for e in entries if os.path.isdir(e) and os.path.basename(e)[0:8].isdigit()]
            if batch_dirs:
                roots_to_scan = batch_dirs
        except OSError:
            pass

        for scan_root in roots_to_scan:
            if job.get("cancelled"):
                break
            try:
                account_entries = os.listdir(scan_root)
            except OSError:
                continue

            for account in account_entries:
                if job.get("cancelled"):
                    break
                account_path = os.path.join(scan_root, account)
                if not os.path.isdir(account_path):
                    continue

                drive_folder = _find_drive_folder(account_path)
                if not drive_folder:
                    continue  # no Drive content in this account

                is_resource = account.lower().startswith("resource")
                job["current"] = account

                if is_resource:
                    # Each named folder inside Drive/ = its own top-level shared drive
                    try:
                        for shared_name in os.listdir(drive_folder):
                            if job.get("cancelled"):
                                break
                            shared_src = os.path.join(drive_folder, shared_name)
                            if not os.path.isdir(shared_src):
                                continue
                            if shared_name.lower() == "trash":
                                continue  # skip trash
                            dst = os.path.join(dest_root, shared_name)
                            _safe_move(shared_src, dst, job)
                        job["shared_drives"] += 1
                    except OSError:
                        pass
                else:
                    # Personal account: move everything in Drive/ under "Personal - <account>"
                    dst_account = os.path.join(dest_root, f"Personal - {account}")
                    os.makedirs(dst_account, exist_ok=True)
                    try:
                        for item in os.listdir(drive_folder):
                            if job.get("cancelled"):
                                break
                            src = os.path.join(drive_folder, item)
                            if os.path.isdir(src) and item.lower() == "trash":
                                continue
                            _safe_move(src, os.path.join(dst_account, item), job)
                        job["personal_accounts"] += 1
                    except OSError:
                        pass

        job["status"] = "cancelled" if job.get("cancelled") else "completed"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# --- Cleanup: strip Google Takeout clutter from extracted output ---

_cleanup_jobs: dict = {}


def cleanup_extracted(target_dir: str) -> str:
    """
    Remove Google Takeout clutter from an extracted directory:
      - *-info.json  (metadata sidecars — dates already on files)
      - archive_browser.html  (Takeout navigation page)
      - _MACOSX folders (macOS zip junk)
      - Empty folders left behind
    Runs in background. Returns job_id.
    """
    job_id = f"cleanup_{int(time.time())}"
    _cleanup_jobs[job_id] = {
        "status": "running",
        "target_dir": target_dir,
        "json_deleted": 0,
        "html_deleted": 0,
        "macosx_deleted": 0,
        "empty_dirs_removed": 0,
        "bytes_freed": 0,
        "current": "",
        "cancelled": False,
        "started_at": time.time(),
    }
    thread = threading.Thread(target=_cleanup_worker, args=(job_id, target_dir), daemon=True)
    thread.start()
    return job_id


def get_cleanup_job(job_id: str):
    return _cleanup_jobs.get(job_id)


def cancel_cleanup(job_id: str) -> bool:
    if job_id in _cleanup_jobs:
        _cleanup_jobs[job_id]["cancelled"] = True
        return True
    return False


def _cleanup_worker(job_id: str, target_dir: str):
    job = _cleanup_jobs[job_id]
    try:
        # Pass 1: delete clutter files
        for root, dirs, files in os.walk(target_dir):
            if job.get("cancelled"):
                break
            # Remove _MACOSX directories wholesale
            for d in list(dirs):
                if d == "__MACOSX":
                    full = os.path.join(root, d)
                    try:
                        sz = _dir_size(full)
                        shutil.rmtree(full, ignore_errors=True)
                        job["macosx_deleted"] += 1
                        job["bytes_freed"] += sz
                    except OSError:
                        pass
                    dirs.remove(d)

            for f in files:
                if job.get("cancelled"):
                    break
                low = f.lower()
                fp = os.path.join(root, f)
                remove = False
                if low.endswith("-info.json"):
                    remove = True
                    key = "json_deleted"
                elif low == "archive_browser.html":
                    remove = True
                    key = "html_deleted"
                else:
                    continue
                try:
                    sz = os.path.getsize(fp)
                    os.remove(fp)
                    job[key] += 1
                    job["bytes_freed"] += sz
                    if (job["json_deleted"] + job["html_deleted"]) % 500 == 0:
                        job["current"] = root.replace("\\", "/")
                except OSError:
                    pass

        # Pass 2: remove now-empty directories (bottom-up)
        for root, dirs, files in os.walk(target_dir, topdown=False):
            if job.get("cancelled"):
                break
            try:
                if not os.listdir(root):
                    os.rmdir(root)
                    job["empty_dirs_removed"] += 1
            except OSError:
                pass

        job["status"] = "cancelled" if job.get("cancelled") else "completed"
        job["bytes_freed_human"] = _human_size(job["bytes_freed"])
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


# --- Reconcile: figure out what's already extracted on disk ---

_reconcile_jobs: dict = {}


def reconcile_ledger(source_dir: str, output_dir: Optional[str] = None) -> str:
    """
    Scan all archives in source_dir. For each, check if its contents already exist
    on disk in output_dir. If so, record it in the ledger as already extracted.
    This avoids re-extracting archives that were done before the ledger worked.

    Reads only the zip INDEX (file list), not the actual data — fast.
    """
    if output_dir is None:
        output_dir = os.path.join(source_dir, "extracted")

    job_id = f"reconcile_{int(time.time())}"
    _reconcile_jobs[job_id] = {
        "status": "running",
        "source_dir": source_dir,
        "output_dir": output_dir,
        "archives_checked": 0,
        "already_extracted": 0,
        "needs_extraction": 0,
        "current": "",
        "cancelled": False,
        "started_at": time.time(),
    }

    thread = threading.Thread(
        target=_reconcile_worker,
        args=(job_id, source_dir, output_dir),
        daemon=True,
    )
    thread.start()
    return job_id


def get_reconcile_job(job_id: str):
    return _reconcile_jobs.get(job_id)


def cancel_reconcile(job_id: str) -> bool:
    if job_id in _reconcile_jobs:
        _reconcile_jobs[job_id]["cancelled"] = True
        return True
    return False


def _archive_fully_on_disk(archive_path: str, output_dir: str, sample_limit: int = 25) -> tuple:
    """
    Check if a zip's contents are already extracted to output_dir.
    Samples up to `sample_limit` files and verifies they exist with matching size.
    Returns (is_extracted, file_count).
    """
    path_lower = archive_path.lower()
    if not path_lower.endswith(".zip"):
        return (False, 0)  # only handle zips for now

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            infos = [i for i in zf.infolist()
                     if not i.filename.endswith("/")
                     and "__MACOSX" not in i.filename
                     and not i.filename.lower().endswith(".mbox")
                     and "all mail including spam" not in i.filename.lower()]

            if not infos:
                return (True, 0)  # empty/all-skipped zip counts as done

            # Sample evenly across the archive
            total = len(infos)
            step = max(1, total // sample_limit)
            sample = infos[::step]

            for info in sample:
                target = os.path.normpath(os.path.join(output_dir, info.filename))
                if not os.path.exists(target):
                    return (False, total)
                # Size check (extracted file should match uncompressed size)
                try:
                    if os.path.getsize(target) != info.file_size:
                        return (False, total)
                except OSError:
                    return (False, total)

            return (True, total)
    except (zipfile.BadZipFile, OSError):
        return (False, 0)


def _reconcile_worker(job_id: str, source_dir: str, output_dir: str):
    """Walk archives, check disk, record already-extracted ones in ledger."""
    job = _reconcile_jobs[job_id]
    archive_extensions = {".zip"}

    try:
        for root, dirs, files in os.walk(source_dir):
            # Don't descend into the extracted or processed folders
            dirs[:] = [d for d in dirs if d.lower() not in ("extracted", "processed")]

            for filename in files:
                if job.get("cancelled"):
                    break
                if not any(filename.lower().endswith(e) for e in archive_extensions):
                    continue

                archive_path = os.path.join(root, filename)
                job["current"] = filename
                job["archives_checked"] += 1

                # Skip if already in ledger
                if is_already_extracted(archive_path):
                    job["already_extracted"] += 1
                    continue

                is_done, file_count = _archive_fully_on_disk(archive_path, output_dir)
                if is_done:
                    _record_extraction(archive_path, output_dir, file_count)
                    job["already_extracted"] += 1
                else:
                    job["needs_extraction"] += 1

        job["status"] = "cancelled" if job.get("cancelled") else "completed"
        job["current"] = ""
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def move_to_processed(archive_path: str, source_dir: str) -> bool:
    """Move a completed archive to source_dir/processed/, preserving relative path."""
    try:
        processed_dir = os.path.join(source_dir, "processed")
        relative = os.path.relpath(archive_path, source_dir)
        dest = os.path.join(processed_dir, relative)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(archive_path, dest)
        return True
    except (OSError, shutil.Error):
        return False


def move_completed_to_processed(source_dir: str) -> dict:
    """
    Move all archives in the ledger that came from source_dir to a processed folder.
    Call this after extraction is done to clean up.
    """
    ledger = _load_ledger()
    moved = 0
    failed = 0
    already_gone = 0

    source_norm = os.path.normpath(source_dir).lower()

    for key, entry in ledger.items():
        archive_path = entry.get("archive_path", "").replace("/", os.sep)
        # Only move archives from this source dir
        if not os.path.normpath(archive_path).lower().startswith(source_norm):
            continue
        if not os.path.exists(archive_path):
            already_gone += 1
            continue
        if move_to_processed(archive_path, source_dir):
            moved += 1
        else:
            failed += 1

    return {
        "moved": moved,
        "failed": failed,
        "already_gone": already_gone,
        "processed_dir": os.path.join(source_dir, "processed").replace("\\", "/"),
    }


def scan_and_extract(
    source_dir: str,
    output_dir: Optional[str] = None,
    recursive: bool = True,
    max_workers: int = 3,
    move_processed: bool = False,
    only_drive: bool = False,
    delete_after: bool = False,
) -> str:
    """
    Scan for archives AND extract them in parallel.
    As each archive is discovered, it's queued for extraction immediately.
    Multiple archives extract concurrently (max_workers threads).
    """
    job_id = f"scanx_{int(time.time())}_{Path(source_dir).name[:15]}"

    if output_dir is None:
        output_dir = os.path.join(source_dir, "extracted")

    _scan_extract_jobs[job_id] = {
        "status": "running",
        "source_dir": source_dir,
        "output_dir": output_dir.replace("\\", "/"),
        "phase": "scanning",
        "archives_found": 0,
        "archives_extracted": 0,
        "archives_skipped": 0,
        "files_extracted": 0,
        "folders_scanned": 0,
        "current_folder": "",
        "current_file": "",
        "current_file_size": 0,
        "currently_extracting": [],
        "total_size": 0,
        "total_size_human": "0 B",
        "bytes_written": 0,
        "archives_moved": 0,
        "archives_deleted": 0,
        "move_processed": move_processed,
        "only_drive": only_drive,
        "delete_after": delete_after,
        "errors": [],
        "cancelled": False,
        "started_at": time.time(),
        "_last_bytes": 0,
        "_last_bytes_time": time.time(),
    }

    thread = threading.Thread(
        target=_scan_extract_worker,
        args=(job_id, source_dir, output_dir, recursive, max_workers),
        daemon=True,
    )
    thread.start()
    return job_id


def get_scan_extract_job(job_id: str):
    """Get status of a scan+extract job with computed throughput."""
    job = _scan_extract_jobs.get(job_id)
    if job is None:
        return None

    # Compute throughput (MB/s) since last poll
    now = time.time()
    bytes_now = job.get("bytes_written", 0)
    last_bytes = job.get("_last_bytes", 0)
    last_time = job.get("_last_bytes_time", now)
    elapsed = now - last_time

    if elapsed > 0.5:
        rate = (bytes_now - last_bytes) / elapsed
        job["throughput_bps"] = rate
        job["throughput_human"] = f"{_human_size(int(rate))}/s"
        job["_last_bytes"] = bytes_now
        job["_last_bytes_time"] = now

    # Total data written so far
    job["bytes_written_human"] = _human_size(bytes_now)
    job["elapsed_seconds"] = round(now - job["started_at"], 1)

    # Return a clean copy without internal fields
    return {k: v for k, v in job.items() if not k.startswith("_")}


def cancel_scan_extract(job_id: str) -> bool:
    """Cancel a scan+extract job."""
    if job_id not in _scan_extract_jobs:
        return False
    _scan_extract_jobs[job_id]["cancelled"] = True
    return True


def _scan_extract_worker(job_id, source_dir, output_dir, recursive, max_workers):
    """Walk tree, extract each archive as found, in parallel."""
    job = _scan_extract_jobs[job_id]
    archive_extensions = {".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".bz2", ".7z", ".rar"}

    os.makedirs(output_dir, exist_ok=True)

    def extract_one(archive_path):
        """Extract a single archive (runs in worker thread)."""
        name = os.path.basename(archive_path)

        # Compute per-archive destination that PRESERVES the source structure.
        # Each archive extracts into a folder mirroring its location under source_dir,
        # keeping different accounts/exports isolated instead of merged.
        dest = _archive_destination(archive_path, source_dir, output_dir)

        # Skip if already extracted — check ledger OR verify contents on disk.
        # Disk check makes it self-healing even if the ledger is empty/stale.
        already_done = is_already_extracted(archive_path)
        if not already_done and archive_path.lower().endswith(".zip"):
            on_disk, _cnt = _archive_fully_on_disk(archive_path, dest)
            if on_disk:
                already_done = True
                _record_extraction(archive_path, dest, _cnt)  # backfill ledger

        def finalize_archive():
            """After successful extraction: delete the zip (frees space) or move it."""
            if job.get("delete_after"):
                try:
                    os.remove(archive_path)
                    job["archives_deleted"] = job.get("archives_deleted", 0) + 1
                except OSError as e:
                    job["errors"].append(f"delete failed {name}: {e}")
            elif job.get("move_processed"):
                if move_to_processed(archive_path, source_dir):
                    job["archives_moved"] = job.get("archives_moved", 0) + 1

        if already_done:
            job["archives_skipped"] += 1
            finalize_archive()
            return

        job["currently_extracting"].append(name)
        try:
            count = _extract_single(archive_path, dest, job)
            job["archives_extracted"] += 1
            job["files_extracted"] += count
            # Record in persistent ledger BEFORE deleting, so it's tracked
            _record_extraction(archive_path, dest, count)
            # Only delete/move AFTER extraction succeeded and was recorded
            finalize_archive()
        except Exception as e:
            job["errors"].append(f"{name}: {str(e)}")
        finally:
            if name in job["currently_extracting"]:
                job["currently_extracting"].remove(name)

    # --- Auto-throttle: a permit gate that can shrink concurrency if the HDD thrashes ---
    # Start with `max_workers` permits. A monitor thread watches throughput and reduces
    # the allowed concurrency to 1 if it detects thrashing (low MB/s with >1 worker).
    import threading as _th
    gate = _th.Semaphore(max_workers)
    throttle_state = {"allowed": max_workers, "stop": False}

    def _monitor_throughput():
        """Sample throughput every 60s; drop to 1 worker if 2 workers thrash."""
        last_bytes = job.get("bytes_written", 0)
        while not throttle_state["stop"] and not job.get("cancelled"):
            time.sleep(60)
            now_bytes = job.get("bytes_written", 0)
            mb_per_sec = (now_bytes - last_bytes) / 60 / (1024 * 1024)
            last_bytes = now_bytes
            job["monitor_mbps"] = round(mb_per_sec, 1)
            # Thrash signature: running 2+ workers but throughput is poor (<25 MB/s)
            if throttle_state["allowed"] > 1 and mb_per_sec < 25 and now_bytes > 0:
                # Reduce to 1 by consuming a permit permanently
                if gate.acquire(blocking=False):
                    throttle_state["allowed"] = 1
                    job["auto_throttled"] = True
                    job["throttle_reason"] = f"Dropped to 1 worker (was thrashing at {round(mb_per_sec,1)} MB/s)"

    monitor = _th.Thread(target=_monitor_throughput, daemon=True)
    monitor.start()

    # Wrap extract_one so each execution must hold a gate permit
    _raw_extract_one = extract_one

    def extract_one(archive_path):  # noqa: F811
        with gate:
            return _raw_extract_one(archive_path)

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = []

            def walk():
                if recursive:
                    for root, dirs, files in os.walk(source_dir):
                        if job.get("cancelled"):
                            return
                        # Don't crawl our own output folders — huge waste of time
                        dirs[:] = [d for d in dirs if d.lower() not in ("extracted", "processed", "organized")]
                        job["folders_scanned"] += 1
                        job["current_folder"] = os.path.relpath(root, source_dir).replace("\\", "/")
                        for filename in files:
                            name_lower = filename.lower()
                            for ext in archive_extensions:
                                if name_lower.endswith(ext):
                                    full_path = os.path.join(root, filename)
                                    try:
                                        job["total_size"] += os.path.getsize(full_path)
                                        job["total_size_human"] = _human_size(job["total_size"])
                                    except OSError:
                                        pass
                                    job["archives_found"] += 1
                                    # Queue for extraction immediately
                                    futures.append(pool.submit(extract_one, full_path))
                                    break
                else:
                    for item in Path(source_dir).iterdir():
                        if job.get("cancelled"):
                            return
                        if item.is_file():
                            name_lower = item.name.lower()
                            for ext in archive_extensions:
                                if name_lower.endswith(ext):
                                    job["archives_found"] += 1
                                    try:
                                        job["total_size"] += item.stat().st_size
                                        job["total_size_human"] = _human_size(job["total_size"])
                                    except OSError:
                                        pass
                                    futures.append(pool.submit(extract_one, str(item)))
                                    break

            walk()
            job["phase"] = "extracting"
            # Wait for all extractions to finish
            for f in futures:
                if job.get("cancelled"):
                    break
                f.result()

        throttle_state["stop"] = True
        job["status"] = "cancelled" if job.get("cancelled") else "completed"
        job["phase"] = "complete"
        job["current_folder"] = ""
        job["currently_extracting"] = []
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)

    except Exception as e:
        throttle_state["stop"] = True
        job["status"] = "error"
        job["error"] = str(e)
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)


def extract_archives(
    source_dir: str,
    output_dir: Optional[str] = None,
    archives: Optional[list[str]] = None,
) -> str:
    """
    Extract archives from source_dir (or specific archive paths) into output_dir.
    Runs in background thread. Returns job_id for polling.

    If output_dir is None, extracts alongside the archives in a subfolder.
    """
    job_id = f"extract_{int(time.time())}_{Path(source_dir).name[:20]}"

    if output_dir is None:
        output_dir = os.path.join(source_dir, "extracted")

    _extract_jobs[job_id] = {
        "status": "running",
        "source_dir": source_dir,
        "output_dir": output_dir.replace("\\", "/"),
        "phase": "discovering",
        "total_archives": 0,
        "processed_archives": 0,
        "current_archive": "",
        "files_extracted": 0,
        "errors": [],
        "progress": 0,
        "elapsed_seconds": 0,
        "started_at": time.time(),
        "cancelled": False,
    }

    thread = threading.Thread(
        target=_extract_worker,
        args=(job_id, source_dir, output_dir, archives),
        daemon=True,
    )
    thread.start()

    return job_id


def cancel_extraction(job_id: str) -> bool:
    """Cancel a running extraction."""
    if job_id not in _extract_jobs:
        return False
    _extract_jobs[job_id]["cancelled"] = True
    return True


def _extract_worker(
    job_id: str,
    source_dir: str,
    output_dir: str,
    specific_archives: Optional[list[str]],
):
    """Background extraction worker."""
    job = _extract_jobs[job_id]

    try:
        os.makedirs(output_dir, exist_ok=True)

        # Find archives to extract
        if specific_archives:
            archive_paths = specific_archives
        else:
            found = find_archives(source_dir, recursive=True)
            archive_paths = [a["path"] for a in found]

        job["total_archives"] = len(archive_paths)

        if not archive_paths:
            job["status"] = "completed"
            job["phase"] = "complete"
            job["progress"] = 100
            job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
            return

        job["phase"] = "extracting"
        total_files = 0

        for i, archive_path in enumerate(archive_paths):
            if job["cancelled"]:
                job["status"] = "cancelled"
                job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
                return

            job["current_archive"] = os.path.basename(archive_path)
            job["processed_archives"] = i
            job["progress"] = int((i / len(archive_paths)) * 100)

            try:
                extracted = _extract_single(archive_path, output_dir, job)
                total_files += extracted
            except Exception as e:
                job["errors"].append(f"{os.path.basename(archive_path)}: {str(e)}")

        job["processed_archives"] = len(archive_paths)
        job["files_extracted"] = total_files
        job["status"] = "completed"
        job["phase"] = "complete"
        job["progress"] = 100
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)


def _archive_destination(archive_path: str, source_dir: str, output_root: str) -> str:
    """
    Compute the extraction destination for an archive, PRESERVING source structure.

    Mirrors the archive's location relative to source_dir, so different accounts
    and exports stay isolated instead of merging into one flat folder.

    Multi-part Google Takeout zips (base-001.zip, base-002.zip) all map to the
    SAME destination folder so their contents reassemble into one Drive structure.

    Example:
      source: E:/Data Analysis Project
      archive: E:/Data Analysis Project/Google Drive/20260713T195041Z/abuse@x.com/takeout-...001.zip
      dest:   <output_root>/Google Drive/20260713T195041Z/abuse@x.com/takeout-...
    """
    archive_path = os.path.normpath(archive_path)
    source_dir = os.path.normpath(source_dir)

    # Relative folder of the archive within the source tree
    rel_dir = os.path.relpath(os.path.dirname(archive_path), source_dir)
    if rel_dir == ".":
        rel_dir = ""

    # Base archive name without extension
    base = os.path.basename(archive_path)
    base = re.sub(r"\.(zip|tar\.gz|tgz|tar|gz)$", "", base, flags=re.IGNORECASE)

    # Strip multi-part suffixes so all parts share one folder:
    #   takeout-20260713T195045Z-001      -> takeout-20260713T195045Z
    #   takeout-20260713T204653Z-2-001    -> takeout-20260713T204653Z
    #   something.part1 / .z01            -> something
    base = re.sub(r"-\d+(-\d+)?$", "", base)      # trailing -001 or -2-001
    base = re.sub(r"\.part\d+$", "", base, flags=re.IGNORECASE)

    dest = os.path.join(output_root, rel_dir, base) if rel_dir else os.path.join(output_root, base)
    return os.path.normpath(dest)


def _extract_with_7zip(archive_path: str, output_dir: str, job: dict) -> int:
    """
    Extract a zip using 7-Zip CLI. Much faster than Python's zipfile.
    Excludes mailbox files via 7z's -x! switch.
    Parses progress output to update byte/file counts.
    Returns count of files extracted.
    """
    os.makedirs(output_dir, exist_ok=True)

    cmd = [
        SEVENZIP_PATH,
        "x",                      # extract with full paths
        archive_path,
        f"-o{output_dir}",        # output directory
        "-y",                     # yes to all prompts (overwrite)
        "-bb0",                   # NO per-file logging (prevents stdout flood on huge archives)
        "-bsp2",                  # progress to stderr only
        "-bso0",                  # suppress standard output stream
        # Exclude mailbox / mail exports + macOS junk
        "-x!*.mbox",
        r"-x!*All mail Including Spam and Trash*",
        "-x!__MACOSX*",
        "-xr!__MACOSX",
        "-x!*-info.json",         # Takeout metadata sidecars
        "-x!archive_browser.html",
    ]

    # Drive-only mode: include only the Takeout/Drive portion, skip Mail/Calendar/Activity/etc.
    # Exclude "My Activity" explicitly since its subfolder is also named "Drive".
    if job.get("only_drive"):
        cmd += [
            r"-ir!Takeout\Drive\*",
            r"-x!Takeout\My Activity\*",
        ]

    files_extracted = 0
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        # Drain progress lines (percentages) lightly — no accumulation.
        # 7z with -bsp2 emits progress like "42%". Keeps memory flat on huge archives.
        for line in proc.stdout:
            if job.get("cancelled"):
                proc.terminate()
                break
            line = line.strip()
            m = re.match(r"^(\d+)%", line)
            if m:
                job["current_archive_pct"] = int(m.group(1))

        proc.wait()

        # 7z return codes: 0 = ok, 1 = warning (non-fatal, e.g. skipped files)
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"7-Zip exited with code {proc.returncode}")

        # Track bytes by archive size for throughput (approximate but flat memory)
        try:
            asize = os.path.getsize(archive_path)
            job["bytes_written"] = job.get("bytes_written", 0) + asize
        except OSError:
            pass

        # Roughly bump file counter — we don't parse per-file to avoid stdout flood.
        # Use a nominal increment; the archive-level counter is the reliable progress metric.
        job["files_extracted"] = job.get("files_extracted", 0)
        return 0

    except FileNotFoundError:
        raise RuntimeError("7-Zip executable not found")


def _extract_single(archive_path: str, output_dir: str, job: dict) -> int:
    """Extract a single archive file. Returns count of files extracted."""
    path_lower = archive_path.lower()
    files_extracted = 0

    # 4MB buffer — efficient sequential I/O for HDD, low memory footprint.
    # Fewer, larger disk operations = less head thrashing, gentler on the drive.
    BUFFER_SIZE = 4 * 1024 * 1024

    # Skip these — giant mail exports we don't want right now
    SKIP_EXTENSIONS = {".mbox"}
    SKIP_PATTERNS = ["all mail including spam and trash", ".mbox"]

    only_drive = job.get("only_drive", False)

    def should_skip(member_name: str) -> bool:
        low = member_name.lower()
        if any(low.endswith(ext) for ext in SKIP_EXTENSIONS):
            return True
        if any(p in low for p in SKIP_PATTERNS):
            return True
        # Drive-only mode: keep only files under Takeout/Drive/, and NOT My Activity
        if only_drive:
            norm = member_name.replace("\\", "/").lower()
            if "/my activity/" in norm or norm.startswith("takeout/my activity/"):
                return True
            # Must be directly under Takeout/Drive/
            if "takeout/drive/" not in norm:
                return True
        return False

    # --- Fast path: use 7-Zip if available for .zip files ---
    if path_lower.endswith(".zip") and SEVENZIP_PATH:
        try:
            count = _extract_with_7zip(archive_path, output_dir, job)
            return count
        except Exception:
            # Fall through to Python extraction if 7z fails
            pass

    if path_lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                for info in zf.infolist():
                    if job.get("cancelled"):
                        return files_extracted
                    member = info.filename
                    # Skip directories and __MACOSX junk
                    if member.endswith("/") or "__MACOSX" in member:
                        continue
                    # Skip mailbox files — too large, not wanted
                    if should_skip(member):
                        job["mbox_skipped"] = job.get("mbox_skipped", 0) + 1
                        continue
                    try:
                        # Build safe target path (prevent path traversal)
                        target = os.path.join(output_dir, member)
                        target = os.path.normpath(target)
                        if not target.startswith(os.path.normpath(output_dir)):
                            continue  # skip paths that escape output dir
                        os.makedirs(os.path.dirname(target), exist_ok=True)

                        # Show the file currently being written + its size
                        job["current_file"] = os.path.basename(member)
                        job["current_file_size"] = info.file_size

                        # Copy with large buffer, tracking bytes for throughput
                        with zf.open(info) as src, open(target, "wb") as dst:
                            while True:
                                if job.get("cancelled"):
                                    break
                                chunk = src.read(BUFFER_SIZE)
                                if not chunk:
                                    break
                                dst.write(chunk)
                                job["bytes_written"] = job.get("bytes_written", 0) + len(chunk)

                        files_extracted += 1
                        job["files_extracted"] = job.get("files_extracted", 0) + 1
                    except (OSError, zipfile.BadZipFile):
                        pass
        except zipfile.BadZipFile as e:
            raise RuntimeError(f"Bad zip file: {e}")

    elif path_lower.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive_path, "r:gz") as tf:
            members = tf.getmembers()
            for member in members:
                if job.get("cancelled"):
                    return files_extracted
                if member.isfile():
                    if should_skip(member.name):
                        job["mbox_skipped"] = job.get("mbox_skipped", 0) + 1
                        continue
                    try:
                        tf.extract(member, output_dir)
                        files_extracted += 1
                        job["files_extracted"] = job.get("files_extracted", 0) + 1
                    except (OSError, tarfile.TarError):
                        pass

    elif path_lower.endswith(".tar"):
        with tarfile.open(archive_path, "r:") as tf:
            members = tf.getmembers()
            for member in members:
                if job.get("cancelled"):
                    return files_extracted
                if member.isfile():
                    if should_skip(member.name):
                        job["mbox_skipped"] = job.get("mbox_skipped", 0) + 1
                        continue
                    try:
                        tf.extract(member, output_dir)
                        files_extracted += 1
                        job["files_extracted"] = job.get("files_extracted", 0) + 1
                    except (OSError, tarfile.TarError):
                        pass

    elif path_lower.endswith(".gz") and not path_lower.endswith(".tar.gz"):
        import gzip
        out_name = Path(archive_path).stem
        out_path = os.path.join(output_dir, out_name)
        with gzip.open(archive_path, "rb") as gz_in:
            with open(out_path, "wb") as f_out:
                shutil.copyfileobj(gz_in, f_out, BUFFER_SIZE)
        files_extracted = 1

    else:
        raise RuntimeError(f"Unsupported archive format: {Path(archive_path).suffix}")

    return files_extracted


def _human_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"
