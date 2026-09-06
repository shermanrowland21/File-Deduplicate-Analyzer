"""
FIND & PURGE BY NAME.

Search file AND folder names by keyword(s) under a chosen root, then quarantine
the matches (reversible). Purpose: decommission cleanup — e.g. purge an old
client's material ("Brandon Dawson", "BDawson") before an S3 migration, whether
or not the files are duplicated.

Design:
  - Match on the FULL PATH (case-insensitive substring), so a keyword on a
    folder catches everything beneath it, and a keyword on a filename catches
    loose files anywhere.
  - Optional whole-word matching (word boundaries) to avoid over-matching
    (e.g. "dawson" not matching "dawsonville" unrelated).
  - Results are grouped:
      * matched FOLDERS  -> the whole subtree is one unit (recursive file count
        + bytes). If a folder matches, we do NOT also list its children
        separately (they'd be redundant / double-counted).
      * matched loose FILES -> files whose own name matches but whose parent
        folder did NOT match.
  - Quarantine, never hard-delete: move to a dated _PurgeQuarantine folder at
    the root with a manifest. undo() restores from the manifest.

SAFETY: run this on a LOCAL/SANDBOX copy (e.g. the Dropbox-Snapshot folder), not
a live-syncing folder, or quarantines will propagate to the cloud.

Concepts here are intended to be ported to the CPMS platform later — keep it
clean and self-contained.
"""
import os
import re
import json
import shutil
import threading
import time
from typing import Optional

STORE_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "purge")

_OUR_DIRS = ("_EmptyQuarantine", "_MergeQuarantine", "_BackfillQuarantine",
             "_ReconcileQuarantine", "_PurgeQuarantine", "_DeletedSince_",
             "_PreReexport_")

_search_jobs: dict = {}


# ---------------------------------------------------------------- matching

def _compile_matchers(keywords: list[str], whole_word: bool) -> list:
    matchers = []
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        if whole_word:
            matchers.append(re.compile(r"\b" + re.escape(kw) + r"\b", re.I))
        else:
            low = kw.lower()
            matchers.append(low)  # plain substring, case-insensitive
    return matchers


def _matches(name: str, matchers: list) -> Optional[str]:
    """Return the first keyword that matches `name`, or None."""
    low = name.lower()
    for m in matchers:
        if isinstance(m, str):
            if m in low:
                return m
        else:
            if m.search(name):
                return m.pattern
    return None


# ---------------------------------------------------------------- fs helpers

def _dir_stats(path: str) -> tuple:
    """Recursive (file_count, total_bytes) under a folder, skipping our dirs."""
    count = 0
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIRS)]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return count, total


# ---------------------------------------------------------------- search job

def search(root: str, keywords: list[str], whole_word: bool = False) -> str:
    os.makedirs(STORE_DIR, exist_ok=True)
    job_id = f"purge_{int(time.time())}"
    _search_jobs[job_id] = {
        "status": "running", "phase": "starting", "root": root,
        "keywords": keywords, "whole_word": whole_word,
        "folders_scanned": 0, "matched_folders": 0, "matched_files": 0,
        "total_bytes": 0, "current": "", "started_at": time.time(),
        "cancelled": False, "result": None, "error": None, "source": None,
    }
    threading.Thread(target=_search_worker,
                     args=(job_id, root, keywords, whole_word),
                     daemon=True).start()
    return job_id


def _try_catalog_search(root: str, keywords: list[str], whole_word: bool):
    """If `root` (or a parent of it) is in the unified catalog, answer instantly
    from there. Returns a result dict or None to fall back to a live walk."""
    try:
        from . import catalog as cat
    except Exception:
        return None
    try:
        roots = cat.list_roots()
    except Exception:
        return None
    ap = os.path.abspath(root).replace("\\", "/").rstrip("/")
    # find a cataloged root that equals or contains `root`
    covering = None
    for r in roots:
        rp = os.path.abspath(r["root"]).replace("\\", "/").rstrip("/")
        if r.get("last_scan_at") and (ap == rp or ap.startswith(rp + "/")):
            covering = r["root"]
            break
    if not covering:
        return None
    res = cat.search(keywords, whole_word=whole_word, roots=[covering])
    if res.get("error"):
        return None
    # scope catalog results down to the requested subfolder if root != covering
    if ap != os.path.abspath(covering).replace("\\", "/").rstrip("/"):
        pre = ap + "/"
        res["matched_folders"] = [f for f in res["matched_folders"]
                                  if f["path"].replace("\\", "/").startswith(pre)
                                  or f["path"].replace("\\", "/").rstrip("/") == ap]
        res["matched_files"] = [f for f in res["matched_files"]
                                if f["path"].replace("\\", "/").startswith(pre)]
        res["counts"] = {
            "folders": len(res["matched_folders"]),
            "files": len(res["matched_files"]),
            "total_items": len(res["matched_folders"]) + len(res["matched_files"]),
            "total_bytes": sum(f["bytes"] for f in res["matched_folders"])
                           + sum(f["bytes"] for f in res["matched_files"]),
            "total_file_count": sum(f.get("files", 0) for f in res["matched_folders"])
                                + len(res["matched_files"]),
        }
    return res


def get_search_job(job_id: str):
    return _search_jobs.get(job_id)


def cancel_search(job_id: str) -> bool:
    if job_id in _search_jobs:
        _search_jobs[job_id]["cancelled"] = True
        return True
    return False


def _search_worker(job_id, root, keywords, whole_word):
    job = _search_jobs[job_id]
    try:
        if not os.path.isdir(root):
            job["status"] = "error"; job["error"] = f"Folder not found: {root}"
            return
        matchers = _compile_matchers(keywords, whole_word)
        if not matchers:
            job["status"] = "error"; job["error"] = "No keywords provided"
            return

        # FAST PATH: if this root is in the unified catalog, answer from SQL
        # (milliseconds) instead of re-walking the disk.
        cat_res = _try_catalog_search(root, keywords, whole_word)
        if cat_res is not None:
            job["source"] = "catalog"
            job["matched_folders"] = cat_res["counts"]["folders"]
            job["matched_files"] = cat_res["counts"]["files"]
            job["total_bytes"] = cat_res["counts"]["total_bytes"]
            job["result"] = cat_res
            job["phase"] = "complete"; job["status"] = "completed"
            job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
            return

        job["source"] = "live_walk"
        job["phase"] = "searching"
        matched_folders = []       # {path, keyword, files, bytes}
        matched_files = []         # {path, keyword, bytes}
        # Track matched-folder prefixes so we don't double-list their children.
        matched_folder_prefixes: list[str] = []

        for dirpath, dirs, files in os.walk(root):
            if job.get("cancelled"):
                job["status"] = "cancelled"; return
            # never descend into our own quarantine dirs
            dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIRS)]
            job["folders_scanned"] += 1
            job["current"] = dirpath

            norm = dirpath.replace("\\", "/")
            # If this dir is already inside a matched folder, skip listing its
            # own matches (the whole subtree is already captured).
            inside_matched = any(norm.startswith(p) for p in matched_folder_prefixes)

            # check each immediate subfolder name for a match
            for d in list(dirs):
                sub = os.path.join(dirpath, d)
                subnorm = sub.replace("\\", "/")
                if any(subnorm.startswith(p) for p in matched_folder_prefixes):
                    continue
                kw = _matches(d, matchers)
                if kw:
                    fc, fb = _dir_stats(sub)
                    matched_folders.append({
                        "path": sub, "keyword": kw, "files": fc, "bytes": fb,
                    })
                    matched_folder_prefixes.append(subnorm + "/")
                    matched_folder_prefixes.append(subnorm)
                    job["matched_folders"] += 1
                    job["total_bytes"] += fb

            if inside_matched:
                continue  # its files are covered by the parent match

            # check loose files in this dir (whose parent didn't match)
            for f in files:
                kw = _matches(f, matchers)
                if kw:
                    fp = os.path.join(dirpath, f)
                    try:
                        b = os.path.getsize(fp)
                    except OSError:
                        b = 0
                    matched_files.append({"path": fp, "keyword": kw, "bytes": b})
                    job["matched_files"] += 1
                    job["total_bytes"] += b

        result = {
            "root": root,
            "matched_folders": sorted(matched_folders, key=lambda x: -x["bytes"]),
            "matched_files": sorted(matched_files, key=lambda x: -x["bytes"]),
            "counts": {
                "folders": len(matched_folders),
                "files": len(matched_files),
                "total_items": len(matched_folders) + len(matched_files),
                "total_bytes": job["total_bytes"],
                "total_file_count": sum(x["files"] for x in matched_folders)
                                    + len(matched_files),
            },
        }
        job["result"] = result
        job["phase"] = "complete"; job["status"] = "completed"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)


# ---------------------------------------------------------------- quarantine

def _quarantine_name() -> str:
    return f"_PurgeQuarantine_{time.strftime('%Y%m%d_%H%M%S')}"


def _collision_safe(dst: str) -> str:
    if not os.path.exists(dst):
        return dst
    parent = os.path.dirname(dst)
    base = os.path.basename(dst)
    name, ext = os.path.splitext(base)
    i = 1
    while True:
        cand = os.path.join(parent, f"{name} ({i}){ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


def quarantine(paths: list[str], root: str, confirm: bool = False) -> dict:
    """
    Move matched files/folders into a dated _PurgeQuarantine at `root`,
    preserving their relative sub-path inside quarantine so undo can restore
    exact locations. Reversible via manifest. Never hard-deletes.
    """
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.isdir(root):
        return {"error": f"root not found: {root}"}

    q_root = os.path.join(root, _quarantine_name())
    os.makedirs(q_root, exist_ok=True)
    entries = []
    errors = []
    moved = 0
    moved_bytes = 0

    root_norm = os.path.abspath(root)
    for p in paths:
        if not os.path.exists(p):
            errors.append(f"not found: {p}")
            continue
        ap = os.path.abspath(p)
        # safety: only quarantine things under the root
        if not ap.lower().startswith(root_norm.lower()):
            errors.append(f"outside root, skipped: {p}")
            continue
        rel = os.path.relpath(ap, root_norm)
        dest = os.path.join(q_root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        dest = _collision_safe(dest)
        try:
            # size before move (for reporting)
            if os.path.isdir(ap):
                _, b = _dir_stats(ap)
            else:
                b = os.path.getsize(ap)
            shutil.move(ap, dest)
            entries.append({"original": ap, "quarantined_to": dest,
                            "is_dir": os.path.isdir(dest)})
            moved += 1
            moved_bytes += b
        except OSError as e:
            errors.append(f"{p}: {e}")

    manifest = {
        "action": "purge", "root": root, "quarantine_root": q_root,
        "entries": entries, "at": time.time(),
    }
    mpath = _write_manifest("purge", manifest)
    return {
        "quarantined": moved, "quarantined_bytes": moved_bytes,
        "errors": errors, "manifest_file": mpath, "quarantine_root": q_root,
    }


def undo(manifest_file: str, confirm: bool = False) -> dict:
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.exists(manifest_file):
        return {"error": f"manifest not found: {manifest_file}"}
    with open(manifest_file, "r", encoding="utf-8") as f:
        man = json.load(f)
    restored = 0
    errors = []
    for e in man.get("entries", []):
        orig = e["original"]
        quar = e["quarantined_to"]
        try:
            if os.path.exists(orig):
                errors.append(f"exists, skipped: {orig}")
                continue
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            shutil.move(quar, orig)
            restored += 1
        except OSError as ex:
            errors.append(f"{quar}: {ex}")
    return {"action": "purge_undo", "restored": restored, "errors": errors}


def _write_manifest(kind: str, data: dict) -> str:
    os.makedirs(STORE_DIR, exist_ok=True)
    path = os.path.join(STORE_DIR, f"{kind}_{int(time.time()*1000)}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path
