"""
Shared Drive name mapper.

Google Workspace admin Takeout exports shared drives as folders named by their
internal numeric ID: 'Resource$1 -<int64>'. That numeric ID is NOT derivable from
the Drive API's base64 ID, so we map by CONTENT instead:

  1. GAM lists each shared drive with its real name + the names of its
     top-level folders (direct children of the drive root).
  2. For each Resource$1 folder on disk, read its top-level folder names.
  3. Match the folder-name sets -> Resource$1 folder = real shared-drive name.

This is reliable because a shared drive's set of top-level folders is a strong,
near-unique fingerprint.

Requires GAM7 installed (C:\\GAM7\\gam.exe) and configured with admin access.
"""
import os
import csv
import json
import subprocess
import threading
import time
from io import StringIO
from typing import Optional

GAM_PATH = r"C:\GAM7\gam.exe"
MAP_STORE = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "shared_drive_map.json")

_map_jobs: dict = {}


def _gam(*args, timeout: int = 120) -> str:
    """Run a GAM command, return stdout text."""
    result = subprocess.run(
        [GAM_PATH, *args],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )
    return result.stdout or ""


def gam_available() -> bool:
    return os.path.exists(GAM_PATH)


def list_shared_drives(admin_user: str) -> list[dict]:
    """Return [{id, name}] for all shared drives via GAM."""
    out = _gam("print", "teamdrives", "fields", "id,name", timeout=180)
    drives = []
    reader = csv.DictReader(StringIO(out))
    for row in reader:
        did = (row.get("id") or "").strip()
        name = (row.get("name") or "").strip()
        if did and name:
            drives.append({"id": did, "name": name})
    return drives


def get_top_level_folders(admin_user: str, drive_id: str) -> set:
    """
    Return the set of top-level folder names (direct children of a shared drive root).
    Uses a parents query so we only get depth-1 folders.
    """
    query = f"'{drive_id}' in parents and mimeType = 'application/vnd.google-apps.folder'"
    out = _gam(
        "user", admin_user, "print", "filelist",
        "select", "teamdriveid", drive_id,
        "query", query, "fields", "name",
        timeout=180,
    )
    names = set()
    reader = csv.DictReader(StringIO(out))
    for row in reader:
        n = (row.get("name") or "").strip()
        if n:
            names.add(_normalize(n))
    return names


def _normalize(name: str) -> str:
    """Normalize a folder name for matching (trim, lowercase, collapse spaces)."""
    return " ".join(name.strip().lower().split())


def _disk_top_folders(drive_dir: str) -> set:
    """Top-level folder names inside an extracted Resource's Drive/ folder."""
    names = set()
    try:
        for entry in os.listdir(drive_dir):
            full = os.path.join(drive_dir, entry)
            if os.path.isdir(full) and entry.lower() != "trash":
                names.add(_normalize(entry))
    except OSError:
        pass
    return names


def _find_drive_dir(resource_root: str) -> Optional[str]:
    """Find the Takeout/Drive folder inside a Resource folder (handles takeout-xxx wrapper)."""
    try:
        for entry in os.listdir(resource_root):
            wrapper = os.path.join(resource_root, entry)
            if os.path.isdir(wrapper):
                cand = os.path.join(wrapper, "Takeout", "Drive")
                if os.path.isdir(cand):
                    return cand
        direct = os.path.join(resource_root, "Takeout", "Drive")
        if os.path.isdir(direct):
            return direct
    except OSError:
        pass
    return None


def _match_score(gam_folders: set, disk_folders: set) -> float:
    """Jaccard-style overlap score between two folder-name sets."""
    if not gam_folders or not disk_folders:
        return 0.0
    inter = len(gam_folders & disk_folders)
    union = len(gam_folders | disk_folders)
    return inter / union if union else 0.0


def build_mapping(admin_user: str, extracted_root: str) -> str:
    """
    Build the Resource$1-folder -> shared-drive-name mapping in the background.
    Returns a job_id to poll.
    """
    job_id = f"map_{int(time.time())}"
    _map_jobs[job_id] = {
        "status": "running",
        "admin_user": admin_user,
        "extracted_root": extracted_root,
        "phase": "starting",
        "shared_drives_total": 0,
        "resource_folders_total": 0,
        "matched": 0,
        "unmatched_resources": [],
        "mapping": {},          # resource_folder_name -> shared_drive_name
        "ambiguous": [],
        "current": "",
        "cancelled": False,
        "started_at": time.time(),
    }
    threading.Thread(target=_build_mapping_worker, args=(job_id, admin_user, extracted_root), daemon=True).start()
    return job_id


def get_map_job(job_id: str):
    return _map_jobs.get(job_id)


def _build_mapping_worker(job_id: str, admin_user: str, extracted_root: str):
    job = _map_jobs[job_id]
    try:
        if not gam_available():
            job["status"] = "error"
            job["error"] = f"GAM not found at {GAM_PATH}"
            return

        # 1. List shared drives
        job["phase"] = "listing_shared_drives"
        drives = list_shared_drives(admin_user)
        job["shared_drives_total"] = len(drives)

        # 2. Fetch top-level folders for each drive (fingerprint)
        job["phase"] = "fingerprinting_drives"
        drive_fingerprints = []  # [{id, name, folders:set}]
        for d in drives:
            if job.get("cancelled"):
                job["status"] = "cancelled"
                return
            job["current"] = f"reading {d['name']}"
            folders = get_top_level_folders(admin_user, d["id"])
            drive_fingerprints.append({"id": d["id"], "name": d["name"], "folders": folders})

        # 3. Find Resource folders on disk (descend batch layer if present)
        job["phase"] = "scanning_disk"
        scan_roots = [extracted_root]
        try:
            for e in os.listdir(extracted_root):
                p = os.path.join(extracted_root, e)
                if os.path.isdir(p) and e[:8].isdigit():
                    scan_roots = [p]
                    break
        except OSError:
            pass

        resource_dirs = []  # [(folder_name, drive_dir, disk_folders:set)]
        for scan_root in scan_roots:
            try:
                for entry in os.listdir(scan_root):
                    if not entry.lower().startswith("resource"):
                        continue
                    rpath = os.path.join(scan_root, entry)
                    if not os.path.isdir(rpath):
                        continue
                    drive_dir = _find_drive_dir(rpath)
                    if drive_dir:
                        resource_dirs.append((entry, drive_dir, _disk_top_folders(drive_dir)))
            except OSError:
                pass
        job["resource_folders_total"] = len(resource_dirs)

        # 4. Match each Resource folder to best-scoring shared drive
        job["phase"] = "matching"
        used_names = {}  # name -> count (handle duplicate names)
        for res_name, drive_dir, disk_folders in resource_dirs:
            if job.get("cancelled"):
                job["status"] = "cancelled"
                return
            best = None
            best_score = 0.0
            second = 0.0
            for fp in drive_fingerprints:
                score = _match_score(fp["folders"], disk_folders)
                if score > best_score:
                    second = best_score
                    best_score = score
                    best = fp
                elif score > second:
                    second = score

            if best and best_score >= 0.5:
                name = best["name"].strip()
                # De-dup identical drive names
                if name in used_names:
                    used_names[name] += 1
                    stored = f"{name} ({used_names[name]})"
                else:
                    used_names[name] = 1
                    stored = name
                job["mapping"][res_name] = stored
                job["matched"] += 1
                if best_score - second < 0.15 and second > 0.3:
                    job["ambiguous"].append({"resource": res_name, "name": stored, "score": round(best_score, 2), "runnerup": round(second, 2)})
            else:
                # Empty drive (no folders) or no confident match — keep raw, flag it
                job["unmatched_resources"].append({"resource": res_name, "best_score": round(best_score, 2), "empty": len(disk_folders) == 0})

        # Persist the mapping
        os.makedirs(os.path.dirname(MAP_STORE), exist_ok=True)
        with open(MAP_STORE, "w", encoding="utf-8") as f:
            json.dump({
                "admin_user": admin_user,
                "extracted_root": extracted_root,
                "mapping": job["mapping"],
                "unmatched": job["unmatched_resources"],
                "ambiguous": job["ambiguous"],
                "built_at": time.time(),
            }, f, indent=2)

        job["phase"] = "complete"
        job["status"] = "completed"
        job["map_file"] = MAP_STORE
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)

    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "GAM command timed out"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def load_mapping() -> dict:
    """Load the saved resource->name mapping (auto + manual overrides merged)."""
    if not os.path.exists(MAP_STORE):
        return {}
    try:
        with open(MAP_STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(data.get("mapping", {}))
        # Manual overrides win over auto matches
        merged.update(data.get("manual", {}))
        return merged
    except (json.JSONDecodeError, OSError):
        return {}


def set_manual_names(overrides: dict) -> dict:
    """
    Apply manual Resource$1 -> name overrides for drives that couldn't be
    content-matched (e.g. the live drive was reorganized since the export).
    Persists into the map file under a 'manual' section so auto-rebuilds keep them.
    Returns the merged mapping.
    """
    data = {}
    if os.path.exists(MAP_STORE):
        try:
            with open(MAP_STORE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    manual = dict(data.get("manual", {}))
    for k, v in overrides.items():
        v = (v or "").strip()
        if v:
            manual[k] = v
        else:
            manual.pop(k, None)
    data["manual"] = manual
    data.setdefault("mapping", data.get("mapping", {}))
    os.makedirs(os.path.dirname(MAP_STORE), exist_ok=True)
    with open(MAP_STORE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return load_mapping()

