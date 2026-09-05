"""
Google Drive DELTA SYNC (GAM-based).

Goal: after the big Takeout export (baseline 2026-07-13T19:50:41Z), catch up on
everything that changed in Drive since then, so the local Organized/ tree matches
current Drive state before an M365 migration.

Handles:
  - ADDS / MODS : files with modifiedTime > baseline  -> (re)download into Organized/
  - DELETIONS   : files trashed since baseline         -> quarantine the local copy
  - Native Google Docs -> exported as Office (.docx/.xlsx/.pptx) on download

Design notes:
  - Uses GAM7 (already admin-authenticated). GAM invocation that reliably writes
    CSV to a file (avoids PowerShell stream mangling):
        gam redirect csv <file> multiprocess user <email> print filelist query ...
  - Scope: all users (gam print users) + all shared drives (gam print teamdrives).
  - DETECT phase is READ-ONLY: it only lists, never downloads or deletes.
  - APPLY phase downloads adds/mods and quarantine-moves deletions. It NEVER hard
    deletes: a locally-present file that was deleted in Drive is MOVED into
    <ORGANIZED>/_DeletedSince_<date>/... mirroring its relative path.

This module intentionally has no side effects on import.
"""
import os
import csv
import json
import subprocess
import threading
import time
from io import StringIO
from typing import Optional

GAM_PATH = os.environ.get("GAM_PATH", r"C:\GAM7\gam.exe")
ORGANIZED_ROOT = os.environ.get("ORGANIZED_ROOT", r"E:\Google Drive Files\Organized")
DEFAULT_ADMIN_USER = os.environ.get("GAM_ADMIN_USER", "admin@example.com")
# Delta reference point (UTC). Configure to your export time via env.
BASELINE = os.environ.get("DELTA_BASELINE", "1970-01-01T00:00:00")
STORE_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer")
DELTA_DIR = os.path.join(STORE_DIR, "delta")

# Native Google MIME types -> (export mime, output extension)
GOOGLE_EXPORT = {
    "application/vnd.google-apps.document":
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "application/vnd.google-apps.spreadsheet":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "application/vnd.google-apps.presentation":
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
    "application/vnd.google-apps.drawing":
        ("image/png", "png"),
}

_detect_jobs: dict = {}
_apply_jobs: dict = {}


# ---------------------------------------------------------------- GAM helpers

def gam_available() -> bool:
    return os.path.exists(GAM_PATH)


def _gam_csv_to_file(out_path: str, args: list[str], timeout: int = 3600) -> int:
    """
    Run a GAM 'print' command writing CSV directly to out_path via GAM's own
    redirect (reliable on Windows). Returns the data row count (excluding header).
    """
    if os.path.exists(out_path):
        os.remove(out_path)
    cmd = [GAM_PATH, "redirect", "csv", out_path, "multiprocess"] + args
    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                   encoding="utf-8", errors="replace")
    if not os.path.exists(out_path):
        return 0
    with open(out_path, "r", encoding="utf-8", errors="replace") as f:
        return max(0, sum(1 for _ in f) - 1)


def _read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return list(csv.DictReader(f))


def list_all_users() -> list[str]:
    out = os.path.join(DELTA_DIR, "_users.csv")
    _gam_csv_to_file(out, ["print", "users", "query", "isSuspended=false",
                           "fields", "primaryEmail"], timeout=600)
    users = []
    for row in _read_csv(out):
        e = (row.get("primaryEmail") or row.get("email") or "").strip()
        if e:
            users.append(e)
    return users


def list_shared_drives() -> list[dict]:
    out = os.path.join(DELTA_DIR, "_teamdrives.csv")
    _gam_csv_to_file(out, ["print", "teamdrives", "fields", "id,name"], timeout=600)
    drives = []
    for row in _read_csv(out):
        did = (row.get("id") or "").strip()
        name = (row.get("name") or "").strip()
        if did and name:
            drives.append({"id": did, "name": name})
    return drives


# ---------------------------------------------------------------- detect phase

def detect(scope: str = "all", admin_user: str = DEFAULT_ADMIN_USER) -> str:
    """
    Start a READ-ONLY detect job.
      scope = "all" | "users" | "drives"
    Returns job_id.
    """
    os.makedirs(DELTA_DIR, exist_ok=True)
    job_id = f"delta_detect_{int(time.time())}"
    _detect_jobs[job_id] = {
        "status": "running", "phase": "starting", "scope": scope,
        "baseline": BASELINE, "admin_user": admin_user,
        "users_total": 0, "users_done": 0,
        "drives_total": 0, "drives_done": 0,
        "adds_mods": 0, "deletions": 0, "native_docs": 0,
        "current": "", "cancelled": False, "started_at": time.time(),
        "report_file": os.path.join(DELTA_DIR, f"{job_id}.json"),
        "errors": [],
    }
    threading.Thread(target=_detect_worker, args=(job_id, scope, admin_user),
                     daemon=True).start()
    return job_id


def get_detect_job(job_id: str):
    return _detect_jobs.get(job_id)


def cancel_detect(job_id: str) -> bool:
    if job_id in _detect_jobs:
        _detect_jobs[job_id]["cancelled"] = True
        return True
    return False


def _source_changes(job: dict, source_kind: str, source_id: str, label: str) -> dict:
    """
    For one user or shared drive, list adds/mods and deletions since baseline.
    Returns {"adds_mods": [...], "deletions": [...]}.
    source_kind = "user" | "drive"
    """
    safe = label.replace("@", "_at_").replace("/", "_")
    fields = "id,name,mimetype,modifiedtime,trashed"

    if source_kind == "user":
        base_args = ["user", source_id, "print", "filelist"]
    else:
        base_args = ["user", job["admin_user"], "print", "filelist",
                     "select", "teamdriveid", source_id]

    # adds & mods (not trashed, modified after baseline)
    am_csv = os.path.join(DELTA_DIR, f"am_{safe}.csv")
    _gam_csv_to_file(am_csv, base_args + [
        "query", f"modifiedTime > '{BASELINE}' and trashed = false",
        "fields", fields], timeout=3600)
    adds_mods = _read_csv(am_csv)

    # deletions (trashed after baseline)
    del_csv = os.path.join(DELTA_DIR, f"del_{safe}.csv")
    _gam_csv_to_file(del_csv, base_args + [
        "query", f"modifiedTime > '{BASELINE}' and trashed = true",
        "fields", fields], timeout=3600)
    deletions = _read_csv(del_csv)

    return {"adds_mods": adds_mods, "deletions": deletions}


def _detect_worker(job_id: str, scope: str, admin_user: str):
    job = _detect_jobs[job_id]
    report = {"baseline": BASELINE, "generated_at": time.time(),
              "sources": []}  # each: {kind,label,id,adds_mods:[],deletions:[]}
    try:
        sources = []
        if scope in ("all", "users"):
            job["phase"] = "listing_users"
            users = list_all_users()
            job["users_total"] = len(users)
            sources += [("user", u, u) for u in users]
        if scope in ("all", "drives"):
            job["phase"] = "listing_drives"
            drives = list_shared_drives()
            job["drives_total"] = len(drives)
            sources += [("drive", d["id"], d["name"]) for d in drives]

        job["phase"] = "scanning"
        for kind, sid, label in sources:
            if job.get("cancelled"):
                job["status"] = "cancelled"
                break
            job["current"] = f"{kind}: {label}"
            try:
                res = _source_changes(job, kind, sid, label)
            except subprocess.TimeoutExpired:
                job["errors"].append(f"timeout scanning {label}")
                res = {"adds_mods": [], "deletions": []}
            except Exception as e:
                job["errors"].append(f"{label}: {e}")
                res = {"adds_mods": [], "deletions": []}

            native = sum(1 for r in res["adds_mods"]
                         if (r.get("mimeType") or "") in GOOGLE_EXPORT)
            job["adds_mods"] += len(res["adds_mods"])
            job["deletions"] += len(res["deletions"])
            job["native_docs"] += native
            report["sources"].append({
                "kind": kind, "label": label, "id": sid,
                "adds_mods": res["adds_mods"], "deletions": res["deletions"],
                "native_docs": native,
            })
            if kind == "user":
                job["users_done"] += 1
            else:
                job["drives_done"] += 1

        with open(job["report_file"], "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        if not job.get("cancelled"):
            job["phase"] = "complete"
            job["status"] = "completed"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# ---------------------------------------------------------------- path mapping

def _load_shared_drive_map() -> dict:
    """id/label agnostic: we resolve shared-drive names from the mapper store."""
    try:
        from .media.drive_mapper import load_mapping
        return load_mapping()  # Resource$1 folder -> real name (not used directly here)
    except Exception:
        return {}


def _sanitize(component: str) -> str:
    """Make a Drive path component safe for Windows filesystem."""
    bad = '<>:"|?*'
    out = "".join("_" if c in bad else c for c in component)
    return out.rstrip(" .") or "_"


def _target_dir_for(kind: str, label: str, drive_relpath: str) -> str:
    """
    Map a Drive file's folder path to its Organized/ destination directory.
      user  -> Organized/<email>/<relpath-under-My Drive>
      drive -> Organized/<shared drive name>/<relpath-under-drive-root>
    drive_relpath is GAM's paths.0 (folder chain, file name is the LAST element and
    is stripped by the caller). For user files GAM's path starts with "My Drive/..".
    """
    rel = drive_relpath.replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p]

    if kind == "user":
        # strip a leading "My Drive" wrapper if present (our layout drops it)
        if parts and parts[0].lower() == "my drive":
            parts = parts[1:]
        top = _sanitize(label)  # the email
    else:
        # shared drive: GAM path usually starts with the drive's own name
        if parts and _sanitize(parts[0]).lower() == _sanitize(label).lower():
            parts = parts[1:]
        top = _sanitize(label)  # real shared-drive name

    safe_parts = [_sanitize(p) for p in parts]
    return os.path.join(ORGANIZED_ROOT, top, *safe_parts)


# ---------------------------------------------------------------- apply phase

def _gam_download(source_kind: str, source_id_or_user: str, admin_user: str,
                  file_id: str, mime: str, target_dir: str, timeout: int = 900) -> bool:
    """
    Download one Drive file into target_dir, converting native Google docs.
    source_id_or_user: the user email (user scope) — shared-drive files are also
    fetched via the admin user who has access.
    """
    os.makedirs(target_dir, exist_ok=True)
    user = source_id_or_user if source_kind == "user" else admin_user
    args = [GAM_PATH, "user", user, "get", "drivefile", file_id]
    if mime in GOOGLE_EXPORT:
        _, ext = GOOGLE_EXPORT[mime]
        args += ["format", ext]
    args += ["targetfolder", target_dir]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return r.returncode == 0


def apply_delta(report_file: str, admin_user: str = DEFAULT_ADMIN_USER,
                do_downloads: bool = True, do_deletions: bool = True) -> str:
    """
    Apply a previously-generated detect report.
      - downloads adds/mods into Organized/ (with conversion)
      - quarantine-moves deletions into Organized/_DeletedSince_<date>/
    Returns job_id.
    """
    job_id = f"delta_apply_{int(time.time())}"
    _apply_jobs[job_id] = {
        "status": "running", "phase": "starting",
        "report_file": report_file, "admin_user": admin_user,
        "downloaded": 0, "converted": 0, "quarantined": 0,
        "download_failed": 0, "delete_missing_local": 0,
        "adds_mods_total": 0, "deletions_total": 0,
        "current": "", "cancelled": False, "started_at": time.time(),
        "errors": [],
    }
    threading.Thread(target=_apply_worker,
                     args=(job_id, report_file, admin_user, do_downloads, do_deletions),
                     daemon=True).start()
    return job_id


def get_apply_job(job_id: str):
    return _apply_jobs.get(job_id)


def cancel_apply(job_id: str) -> bool:
    if job_id in _apply_jobs:
        _apply_jobs[job_id]["cancelled"] = True
        return True
    return False


def _fetch_paths(source_kind: str, source_id: str, admin_user: str, label: str) -> dict:
    """
    id -> full folder+file path for a source, reconstructed from the parent chain.

    We use `print filelist` (fast, reliable for both users and shared drives) to
    fetch EVERY item's id, name, mimeType and parents, then build each file's
    path by walking parents up to the root. This avoids the slow/finicky
    `print filepath` command and works identically for users and shared drives.
    """
    safe = label.replace("@", "_at_").replace("/", "_")
    fl_csv = os.path.join(DELTA_DIR, f"tree_{safe}.csv")
    if source_kind == "user":
        args = ["user", source_id, "print", "filelist",
                "fields", "id,name,mimetype,parents"]
    else:
        args = ["user", admin_user, "print", "filelist",
                "select", "teamdriveid", source_id,
                "fields", "id,name,mimetype,parents"]
    _gam_csv_to_file(fl_csv, args, timeout=3600)

    # Build id -> (name, parent_id) index. GAM emits parents as parents.0.id etc.
    nodes = {}
    for row in _read_csv(fl_csv):
        fid = (row.get("id") or "").strip()
        if not fid:
            continue
        name = row.get("name") or ""
        # first parent id (files in a single folder have one parent)
        parent = ""
        for k in ("parents.0.id", "parents.0", "parents"):
            if row.get(k):
                parent = row.get(k).strip()
                break
        nodes[fid] = (name, parent)

    def build_path(fid, _seen=None):
        _seen = _seen or set()
        if fid in _seen or fid not in nodes:
            return ""
        _seen.add(fid)
        name, parent = nodes[fid]
        if parent and parent in nodes:
            up = build_path(parent, _seen)
            return (up + "/" + name) if up else name
        return name  # reached a root (My Drive / shared-drive root not in nodes)

    return {fid: build_path(fid) for fid in nodes}


def _apply_worker(job_id, report_file, admin_user, do_downloads, do_deletions):
    job = _apply_jobs[job_id]
    try:
        with open(report_file, "r", encoding="utf-8") as f:
            report = json.load(f)

        quarantine_root = os.path.join(
            ORGANIZED_ROOT, f"_DeletedSince_{time.strftime('%Y%m%d')}")

        for src in report.get("sources", []):
            job["adds_mods_total"] += len(src.get("adds_mods", []))
            job["deletions_total"] += len(src.get("deletions", []))

        job["phase"] = "applying"
        for src in report.get("sources", []):
            if job.get("cancelled"):
                break
            kind, label, sid = src["kind"], src["label"], src["id"]
            job["current"] = f"{kind}: {label}"

            # Build id->path map once per source (covers adds/mods + deletions)
            try:
                id_paths = _fetch_paths(kind, sid, admin_user, label)
            except Exception as e:
                job["errors"].append(f"paths {label}: {e}")
                id_paths = {}

            # ---- ADDS / MODS: download ----
            if do_downloads:
                for row in src.get("adds_mods", []):
                    if job.get("cancelled"):
                        break
                    fid = (row.get("id") or "").strip()
                    mime = row.get("mimeType") or ""
                    if mime == "application/vnd.google-apps.folder" or not fid:
                        continue
                    full_path = id_paths.get(fid, row.get("name", ""))
                    folder_only = os.path.dirname(full_path.replace("\\", "/"))
                    target_dir = _target_dir_for(kind, label, folder_only)
                    try:
                        ok = _gam_download(kind, label, admin_user, fid, mime, target_dir)
                        if ok:
                            job["downloaded"] += 1
                            if mime in GOOGLE_EXPORT:
                                job["converted"] += 1
                        else:
                            job["download_failed"] += 1
                    except subprocess.TimeoutExpired:
                        job["download_failed"] += 1
                        job["errors"].append(f"download timeout {fid}")
                    except Exception as e:
                        job["download_failed"] += 1
                        job["errors"].append(f"download {fid}: {e}")

            # ---- DELETIONS: quarantine local copy ----
            if do_deletions:
                for row in src.get("deletions", []):
                    if job.get("cancelled"):
                        break
                    fid = (row.get("id") or "").strip()
                    full_path = id_paths.get(fid, row.get("name", ""))
                    folder_only = os.path.dirname(full_path.replace("\\", "/"))
                    fname = os.path.basename(full_path.replace("\\", "/")) or row.get("name", "")
                    local_dir = _target_dir_for(kind, label, folder_only)
                    local_file = os.path.join(local_dir, _sanitize(fname))
                    if os.path.exists(local_file):
                        # mirror relative path under quarantine
                        rel = os.path.relpath(local_file, ORGANIZED_ROOT)
                        q_dest = os.path.join(quarantine_root, rel)
                        os.makedirs(os.path.dirname(q_dest), exist_ok=True)
                        try:
                            import shutil
                            shutil.move(local_file, q_dest)
                            job["quarantined"] += 1
                        except OSError as e:
                            job["errors"].append(f"quarantine {local_file}: {e}")
                    else:
                        job["delete_missing_local"] += 1

        if not job.get("cancelled"):
            job["phase"] = "complete"
            job["status"] = "completed"
        else:
            job["status"] = "cancelled"
        job["quarantine_root"] = quarantine_root
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# ---------------------------------------------------------------- native re-export pass

_reexport_jobs: dict = {}


def _is_google_export_docx(path: str) -> bool:
    """
    A .docx/.xlsx/.pptx produced by Google export lacks docProps/app.xml+core.xml
    (real Office files always include them). Used to find flattened files.
    """
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            return not ("docProps/app.xml" in names or "docProps/core.xml" in names)
    except Exception:
        return False


def reexport_native(admin_user: str = DEFAULT_ADMIN_USER,
                    scope: str = "all") -> str:
    """
    Re-export existing Google-origin Office files in Organized/ by re-downloading
    them from Drive in proper Office format. Originals are backed up to
    Organized/_PreReexport_<date>/ before replacement.
    """
    os.makedirs(DELTA_DIR, exist_ok=True)
    job_id = f"delta_reexport_{int(time.time())}"
    _reexport_jobs[job_id] = {
        "status": "running", "phase": "starting", "scope": scope,
        "admin_user": admin_user,
        "candidates": 0, "reexported": 0, "matched": 0, "unmatched": 0,
        "backed_up": 0, "current": "", "cancelled": False,
        "started_at": time.time(), "errors": [],
    }
    threading.Thread(target=_reexport_worker, args=(job_id, admin_user, scope),
                     daemon=True).start()
    return job_id


def get_reexport_job(job_id: str):
    return _reexport_jobs.get(job_id)


def cancel_reexport(job_id: str) -> bool:
    if job_id in _reexport_jobs:
        _reexport_jobs[job_id]["cancelled"] = True
        return True
    return False


def _reexport_worker(job_id, admin_user, scope):
    """
    Strategy: build a Drive index of native docs (id, name, mimeType) per source,
    then walk the matching Organized/ top-level folder; for each flattened Office
    file whose base name matches a native doc, re-export it.
    Name-based matching within the correct top-level folder is sufficient here.
    """
    import shutil
    job = _reexport_jobs[job_id]
    try:
        backup_root = os.path.join(
            ORGANIZED_ROOT, f"_PreReexport_{time.strftime('%Y%m%d')}")

        # Build Drive index: {top_folder_label: {basename_lower: (user, id, mime)}}
        job["phase"] = "indexing_drive"
        index: dict = {}

        sources = []
        if scope in ("all", "users"):
            for u in list_all_users():
                sources.append(("user", u, u))
        if scope in ("all", "drives"):
            for d in list_shared_drives():
                sources.append(("drive", d["id"], d["name"]))

        native_mimes = "','".join(GOOGLE_EXPORT.keys())
        for kind, sid, label in sources:
            if job.get("cancelled"):
                job["status"] = "cancelled"
                return
            job["current"] = f"index {kind}: {label}"
            safe = label.replace("@", "_at_").replace("/", "_")
            idx_csv = os.path.join(DELTA_DIR, f"idx_{safe}.csv")
            if kind == "user":
                args = ["user", sid, "print", "filelist",
                        "query", f"mimeType in ('{native_mimes}') and trashed = false",
                        "fields", "id,name,mimetype"]
            else:
                args = ["user", admin_user, "print", "filelist",
                        "select", "teamdriveid", sid,
                        "query", f"mimeType in ('{native_mimes}') and trashed = false",
                        "fields", "id,name,mimetype"]
            try:
                _gam_csv_to_file(idx_csv, args, timeout=3600)
            except Exception as e:
                job["errors"].append(f"index {label}: {e}")
                continue
            bucket = index.setdefault(_sanitize(label).lower(), {})
            for row in _read_csv(idx_csv):
                nm = (row.get("name") or "").strip().lower()
                fid = (row.get("id") or "").strip()
                mime = (row.get("mimeType") or "").strip()
                user_for = sid if kind == "user" else admin_user
                if nm and fid:
                    bucket[nm] = (user_for, fid, mime)

        # Walk Organized/ top-level folders, find flattened Office files
        job["phase"] = "reexporting"
        for top in os.listdir(ORGANIZED_ROOT):
            if job.get("cancelled"):
                break
            if top.startswith("_"):  # skip quarantine/backup folders
                continue
            top_path = os.path.join(ORGANIZED_ROOT, top)
            if not os.path.isdir(top_path):
                continue
            bucket = index.get(_sanitize(top).lower(), {})
            if not bucket:
                continue
            job["current"] = f"reexport: {top}"
            for root, dirs, files in os.walk(top_path):
                for fn in files:
                    if job.get("cancelled"):
                        break
                    low = fn.lower()
                    if not (low.endswith(".docx") or low.endswith(".xlsx")
                            or low.endswith(".pptx")):
                        continue
                    full = os.path.join(root, fn)
                    if not _is_google_export_docx(full):
                        continue  # already a real Office file
                    job["candidates"] += 1
                    base = os.path.splitext(fn)[0].lower()
                    hit = bucket.get(base)
                    if not hit:
                        job["unmatched"] += 1
                        continue
                    job["matched"] += 1
                    user_for, fid, mime = hit
                    # backup original
                    rel = os.path.relpath(full, ORGANIZED_ROOT)
                    bkp = os.path.join(backup_root, rel)
                    try:
                        os.makedirs(os.path.dirname(bkp), exist_ok=True)
                        shutil.move(full, bkp)
                        job["backed_up"] += 1
                    except OSError as e:
                        job["errors"].append(f"backup {full}: {e}")
                        continue
                    # re-export fresh copy into the same folder
                    try:
                        ok = _gam_download("user", user_for, admin_user, fid, mime, root)
                        if ok:
                            job["reexported"] += 1
                        else:
                            # restore backup on failure
                            shutil.move(bkp, full)
                            job["errors"].append(f"reexport failed, restored {full}")
                    except Exception as e:
                        try:
                            shutil.move(bkp, full)
                        except OSError:
                            pass
                        job["errors"].append(f"reexport {fid}: {e}")

        if not job.get("cancelled"):
            job["phase"] = "complete"
            job["status"] = "completed"
        job["backup_root"] = backup_root
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)

