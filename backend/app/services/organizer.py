"""
Organizer: build a clean, human-readable COPY of an extracted Google Workspace
admin export WITHOUT ever modifying the extracted source of truth.

WHY COPY (not move):
    Reorganizing in place is irreversible. If the naming/flattening is wrong we
    cannot roll back. Copying leaves the extracted tree untouched so any mistake
    is fixed by deleting the output and re-running. Disk has ample headroom.

TARGET STRUCTURE (clean, no junk wrapper layers):

    Organized/
        user@example.com/          <- personal account = email address
            <files and folders that were inside My Drive, directly here>
            My Computer/                 <- synced-computer content preserved
        Earth Energy Stones/             <- shared drive = REAL name via GAM
            <its top-level subfolders directly here>
        Marketing/
            ...

SOURCE STRUCTURE we are reading (per prior investigation):

    extracted/<batchid>/
        <email>/takeout-<ts>/Takeout/Drive/My Drive/...      (personal)
        <email>/takeout-<ts>/Takeout/Drive/My Computer/...   (personal, keep)
        Resource$1 -<int64>/takeout-<ts>/Takeout/Drive/<subfolders>  (shared drive)

RULES:
    - Personal account -> top-level folder named exactly the email address.
      The contents of "My Drive" are placed DIRECTLY inside it (no "My Drive"
      layer). "My Computer" is preserved as a subfolder (it's real synced data).
      "Trash" and "Unorganized" are skipped by default.
    - Shared drive (Resource$1) -> top-level folder named the REAL shared-drive
      name from the GAM content-match mapping. Its top-level subfolders go
      directly inside. If a Resource folder is empty or unmapped, it is skipped
      (empty) or kept under its raw id (unmapped) and flagged.
    - Clutter (*-info.json, archive_browser.html, __MACOSX, *.json metadata at
      the Drive root like Workspaces.json) is NOT copied.
    - Original file timestamps are preserved (copy2).
"""
import os
import csv
import json
import shutil
import threading
import time
from io import StringIO
from typing import Optional

from .media.drive_mapper import load_mapping, _find_drive_dir, _normalize

_organize_jobs: dict = {}

# Folders inside Drive/ that are not real user content
_SKIP_DRIVE_CHILDREN = {"trash"}
# Personal-only folders we skip by default (orphaned/shared-with-me clutter)
_SKIP_PERSONAL_EXTRA = {"unorganized"}
# Clutter filenames never copied
_CLUTTER_FILES = {"archive_browser.html", "workspaces.json"}


# Real file extensions that, when followed by ".json", indicate a Google Takeout
# metadata sidecar (e.g. "Report.pdf.json", "clip.mp4.json").
_SIDECAR_INNER_EXTS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt", "rtf",
    "odt", "ods", "odp", "html", "htm",
    "jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "heic", "webp", "svg",
    "psd", "ai", "eps",
    "mp4", "mov", "m4v", "avi", "mkv", "wmv", "webm", "mpg", "mpeg",
    "mp3", "m4a", "wav", "aac", "flac", "ogg", "wma",
    "zip", "rar", "7z", "gz",
    "gdoc", "gsheet", "gslides", "gform", "gdraw", "gsite", "gmap", "gscript",
    "prproj", "aep", "als", "wig",
}


def _is_clutter_file(name: str) -> bool:
    """
    True if the file is Google Takeout metadata clutter, not user data.

    Covers:
      - *-info.json  / *-i.json           (truncated info sidecars)
      - Shared Drive Metadata.json        (drive-level metadata)
      - <name>.<realext>.json             (double-extension sidecar, e.g. foo.pdf.json)
      - archive_browser.html, Workspaces.json
    """
    low = name.lower()
    if low in _CLUTTER_FILES:
        return True
    if low.endswith("-info.json") or low.endswith("-i.json"):
        return True
    if low == "shared drive metadata.json":
        return True
    if low.endswith(".json"):
        stem = low[:-5]  # strip ".json"
        # double-extension sidecar: the remaining stem still ends in a real ext
        inner_ext = stem.rsplit(".", 1)[-1] if "." in stem else ""
        if inner_ext in _SIDECAR_INNER_EXTS:
            return True
    return False


def _batch_roots(extracted_root: str) -> list[str]:
    """Return the batch-id subfolders (e.g. 20260713T195041Z) or the root itself."""
    try:
        entries = [os.path.join(extracted_root, e) for e in os.listdir(extracted_root)]
        batch_dirs = [
            e for e in entries
            if os.path.isdir(e) and os.path.basename(e)[:8].isdigit()
        ]
        if batch_dirs:
            return batch_dirs
    except OSError:
        pass
    return [extracted_root]


def _account_folders(extracted_root: str) -> list[tuple[str, str]]:
    """
    Return [(entry_name, full_path)] for every account/resource folder that has a
    Drive folder somewhere inside it. Descends the batch-id layer and also the
    accidental nested 'extracted' layer if present.
    """
    result = []
    seen = set()
    scan_roots = list(_batch_roots(extracted_root))
    # Also descend one accidental 'extracted' nesting if it exists
    for sr in list(scan_roots):
        nested = os.path.join(sr, "extracted")
        if os.path.isdir(nested):
            scan_roots.extend(_batch_roots(nested))

    for scan_root in scan_roots:
        try:
            for entry in os.listdir(scan_root):
                full = os.path.join(scan_root, entry)
                if not os.path.isdir(full):
                    continue
                if entry.lower() == "extracted":
                    continue
                drive_dir = _find_drive_dir(full)
                if not drive_dir:
                    continue
                key = entry.lower()
                if key in seen:
                    # Same account appearing under two roots — keep first
                    continue
                seen.add(key)
                result.append((entry, full))
        except OSError:
            pass
    return result


def _copy_tree_filtered(src: str, dst: str, job: dict):
    """
    Recursively COPY src -> dst, skipping clutter files and preserving timestamps.
    Never touches src.

    Merge-safe / idempotent: if a destination file already exists with the SAME
    size, it is treated as already-copied and skipped (so a second pass over
    overlapping data doesn't duplicate). Only genuinely different same-named
    files get a collision-safe rename.
    """
    os.makedirs(dst, exist_ok=True)
    try:
        entries = os.listdir(src)
    except OSError:
        return
    for name in entries:
        if job.get("cancelled"):
            return
        s = os.path.join(src, name)
        if os.path.isdir(s):
            d = os.path.join(dst, name)
            _copy_tree_filtered(s, d, job)
        else:
            if _is_clutter_file(name):
                job["clutter_skipped"] += 1
                continue
            target = os.path.join(dst, name)
            try:
                src_size = os.path.getsize(s)
            except OSError:
                src_size = -1
            if os.path.exists(target):
                try:
                    if os.path.getsize(target) == src_size:
                        job["already_present"] = job.get("already_present", 0) + 1
                        continue  # identical -> already copied by a prior pass
                except OSError:
                    pass
                target = _collision_safe(target, job)
            try:
                shutil.copy2(s, target)
                job["files_copied"] += 1
                job["bytes_copied"] += os.path.getsize(target)
                if job["files_copied"] % 200 == 0:
                    job["current"] = s.replace("\\", "/")
            except OSError as e:
                job.setdefault("copy_errors", []).append(f"{s}: {e}")


def _copy_file_merge_safe(src: str, dst_dir: str, job: dict):
    """Copy a single file into dst_dir, merge-safe (skip identical, rename differing)."""
    name = os.path.basename(src)
    target = os.path.join(dst_dir, name)
    try:
        src_size = os.path.getsize(src)
    except OSError:
        src_size = -1
    if os.path.exists(target):
        try:
            if os.path.getsize(target) == src_size:
                job["already_present"] = job.get("already_present", 0) + 1
                return
        except OSError:
            pass
        target = _collision_safe(target, job)
    os.makedirs(dst_dir, exist_ok=True)
    try:
        shutil.copy2(src, target)
        job["files_copied"] += 1
        job["bytes_copied"] += os.path.getsize(target)
    except OSError as e:
        job.setdefault("copy_errors", []).append(f"{src}: {e}")


def _collision_safe(dst: str, job: dict) -> str:
    if not os.path.exists(dst):
        return dst
    parent = os.path.dirname(dst)
    base = os.path.basename(dst)
    name, ext = os.path.splitext(base)
    i = 1
    while True:
        cand = os.path.join(parent, f"{name} ({i}){ext}")
        if not os.path.exists(cand):
            job["collisions_renamed"] += 1
            return cand
        i += 1


def _resource_display_name(entry: str, mapping: dict, used_names: dict) -> Optional[str]:
    """
    Resolve a Resource$1 folder to its clean shared-drive name.
    Returns None if it should be skipped (no mapping AND we choose to skip),
    otherwise the de-duplicated display name.
    """
    real = mapping.get(entry)
    if not real:
        # Unmapped resource: keep raw id so nothing is lost, but flag it
        real = entry
    if real in used_names:
        used_names[real] += 1
        return f"{real} ({used_names[real]})"
    used_names[real] = 1
    return real


def plan_organization(extracted_root: str) -> dict:
    """
    DRY RUN. Produce the exact structure that organize() would create, without
    copying anything. Lets the user verify names + layout first.
    """
    mapping = load_mapping()
    accounts = _account_folders(extracted_root)
    used_names: dict = {}
    personal = []
    shared = []
    skipped_empty = []
    unmapped = []

    for entry, full in accounts:
        drive_dir = _find_drive_dir(full)
        if not drive_dir:
            continue
        is_resource = entry.lower().startswith("resource")

        if is_resource:
            # Determine top-level subfolders (its content)
            try:
                children = [
                    c for c in os.listdir(drive_dir)
                    if os.path.isdir(os.path.join(drive_dir, c))
                    and c.lower() not in _SKIP_DRIVE_CHILDREN
                ]
            except OSError:
                children = []
            # any files at drive root (rare) count as content too
            has_files = False
            try:
                has_files = any(
                    os.path.isfile(os.path.join(drive_dir, c)) and not _is_clutter_file(c)
                    for c in os.listdir(drive_dir)
                )
            except OSError:
                pass
            if not children and not has_files:
                skipped_empty.append(entry)
                continue
            display = _resource_display_name(entry, mapping, used_names)
            rec = {"resource_id": entry, "name": display, "subfolders": sorted(children)}
            if entry not in mapping:
                unmapped.append(entry)
            shared.append(rec)
        else:
            # Personal: contents of My Drive go directly under the email folder.
            my_drive = os.path.join(drive_dir, "My Drive")
            top_items = []
            if os.path.isdir(my_drive):
                try:
                    top_items = sorted(
                        c for c in os.listdir(my_drive)
                        if c.lower() not in _SKIP_DRIVE_CHILDREN and not _is_clutter_file(c)
                    )
                except OSError:
                    pass
            extras = []
            try:
                for c in os.listdir(drive_dir):
                    cl = c.lower()
                    if cl == "my drive":
                        continue
                    if cl in _SKIP_DRIVE_CHILDREN or cl in _SKIP_PERSONAL_EXTRA:
                        continue
                    if os.path.isdir(os.path.join(drive_dir, c)):
                        extras.append(c)  # e.g. "My Computer"
            except OSError:
                pass
            personal.append({
                "email": entry,
                "my_drive_items": top_items[:50],
                "my_drive_item_count": len(top_items),
                "extra_folders": sorted(extras),
            })

    return {
        "extracted_root": extracted_root,
        "personal_accounts": personal,
        "shared_drives": shared,
        "skipped_empty_resources": skipped_empty,
        "unmapped_resources": unmapped,
        "personal_count": len(personal),
        "shared_count": len(shared),
        "skipped_empty_count": len(skipped_empty),
        "mapping_loaded": bool(mapping),
    }


def organize(extracted_root: str, dest_root: Optional[str] = None,
             include_my_computer: bool = True) -> str:
    """
    COPY the extracted export into a clean structure at dest_root.
    NEVER modifies extracted_root. Returns a job_id to poll.
    """
    if dest_root is None:
        dest_root = os.path.join(
            os.path.dirname(extracted_root.rstrip("/\\")), "Organized"
        )
    job_id = f"organize_{int(time.time())}"
    _organize_jobs[job_id] = {
        "status": "running",
        "phase": "starting",
        "extracted_root": extracted_root,
        "dest_root": dest_root.replace("\\", "/"),
        "include_my_computer": include_my_computer,
        "personal_done": 0,
        "shared_done": 0,
        "files_copied": 0,
        "bytes_copied": 0,
        "clutter_skipped": 0,
        "collisions_renamed": 0,
        "already_present": 0,
        "skipped_empty": [],
        "unmapped": [],
        "current": "",
        "cancelled": False,
        "started_at": time.time(),
    }
    threading.Thread(
        target=_organize_worker,
        args=(job_id, extracted_root, dest_root, include_my_computer),
        daemon=True,
    ).start()
    return job_id


def get_organize_job(job_id: str):
    return _organize_jobs.get(job_id)


def cancel_organize(job_id: str) -> bool:
    if job_id in _organize_jobs:
        _organize_jobs[job_id]["cancelled"] = True
        return True
    return False


def _organize_worker(job_id: str, extracted_root: str, dest_root: str,
                     include_my_computer: bool):
    job = _organize_jobs[job_id]
    try:
        os.makedirs(dest_root, exist_ok=True)
        mapping = load_mapping()
        accounts = _account_folders(extracted_root)
        used_names: dict = {}
        job["phase"] = "copying"

        for entry, full in accounts:
            if job.get("cancelled"):
                break
            drive_dir = _find_drive_dir(full)
            if not drive_dir:
                continue
            is_resource = entry.lower().startswith("resource")
            job["current"] = entry

            if is_resource:
                # Gather content children (skip trash)
                try:
                    children = [
                        c for c in os.listdir(drive_dir)
                        if c.lower() not in _SKIP_DRIVE_CHILDREN
                    ]
                except OSError:
                    children = []
                real_children = [
                    c for c in children
                    if os.path.isdir(os.path.join(drive_dir, c))
                    or (os.path.isfile(os.path.join(drive_dir, c)) and not _is_clutter_file(c))
                ]
                if not real_children:
                    job["skipped_empty"].append(entry)
                    continue
                display = _resource_display_name(entry, mapping, used_names)
                if entry not in mapping:
                    job["unmapped"].append(entry)
                dest = os.path.join(dest_root, display)
                for c in real_children:
                    if job.get("cancelled"):
                        break
                    src = os.path.join(drive_dir, c)
                    if os.path.isdir(src):
                        _copy_tree_filtered(src, os.path.join(dest, c), job)
                    elif not _is_clutter_file(c):
                        _copy_file_merge_safe(src, dest, job)
                job["shared_done"] += 1
            else:
                # Personal account named by email; My Drive contents directly inside
                dest = os.path.join(dest_root, entry)
                os.makedirs(dest, exist_ok=True)
                my_drive = os.path.join(drive_dir, "My Drive")
                if os.path.isdir(my_drive):
                    try:
                        for c in os.listdir(my_drive):
                            if job.get("cancelled"):
                                break
                            if c.lower() in _SKIP_DRIVE_CHILDREN:
                                continue
                            src = os.path.join(my_drive, c)
                            if os.path.isdir(src):
                                _copy_tree_filtered(src, os.path.join(dest, c), job)
                            elif not _is_clutter_file(c):
                                _copy_file_merge_safe(src, dest, job)
                    except OSError:
                        pass
                # Preserve My Computer (and other real non-MyDrive folders)
                try:
                    for c in os.listdir(drive_dir):
                        if job.get("cancelled"):
                            break
                        cl = c.lower()
                        if cl == "my drive" or cl in _SKIP_DRIVE_CHILDREN or cl in _SKIP_PERSONAL_EXTRA:
                            continue
                        src = os.path.join(drive_dir, c)
                        if os.path.isdir(src):
                            if cl.startswith("my computer") and not include_my_computer:
                                continue
                            _copy_tree_filtered(src, os.path.join(dest, c), job)
                except OSError:
                    pass
                job["personal_done"] += 1

        job["phase"] = "complete"
        job["status"] = "cancelled" if job.get("cancelled") else "completed"
        job["bytes_copied_human"] = _human_size(job["bytes_copied"])
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


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

