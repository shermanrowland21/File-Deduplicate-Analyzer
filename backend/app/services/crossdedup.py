"""
MD5 CROSS-INDEX DEDUP across the two E: folders (Organized + Dropbox-Snapshot).

Reuses the full-file MD5 indexes already built (no re-scan). Finds every group
of byte-identical files (same MD5) that appears MORE THAN ONCE — whether the
copies span the two folders (cross-folder) or repeat within one folder
(within-folder) — picks ONE keeper per group by a rule you control, and
quarantines the rest (reversible).

Keeper rule:
  1. prefer_folder: keep the copy in the preferred folder ('organized' or
     'snapshot') when the group spans both.
  2. tiebreak (within the preferred set, or when no preference applies):
     - shortest path (closest to a root, least buried)
     - then cleanest name (fewest ' (N)' / '-at-<timestamp>' extraction markers)
     - then longest name (more descriptive)
  Leave-one invariant: exactly one keeper survives per group; never all removed.

Quarantine, never hard-delete: redundant copies move to a dated
_CrossDedupQuarantine at their OWN folder root, preserving relative path, with a
reversible manifest. undo() restores.
"""
import os
import re
import time
import json
import sqlite3
import threading
from typing import Optional

from . import md5_index as mi

ORGANIZED_ROOT = os.environ.get("ORGANIZED_ROOT", r"E:\Google Drive Files\Organized")
SNAPSHOT_ROOT = os.environ.get("SNAPSHOT_ROOT", r"E:\Dropbox-Snapshot")

_STORE = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "crossdedup")
_MANIFEST_DIR = os.path.join(_STORE, "manifests")

_jobs: dict = {}

_PAREN_RE = re.compile(r" \(\d+\)(\.[^.]+)?$")
_TS_RE = re.compile(r"-at-\d[\d\-t:_.z]*", re.I)


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def _clean_score(name: str) -> int:
    """Lower = cleaner. Penalize extraction markers so the keeper is the tidiest
    copy (no ' (N)', no '-at-timestamp')."""
    s = 0
    if _PAREN_RE.search(name):
        s += 10
    if _TS_RE.search(name):
        s += 5
    return s


def _load_index(db_path: str, tag: str, into: dict):
    if not os.path.exists(db_path):
        return
    c = sqlite3.connect(db_path)
    try:
        for path, md5, size in c.execute("SELECT path, md5, size FROM files WHERE md5<>''"):
            into.setdefault(md5, []).append(
                {"path": path, "size": size or 0, "folder": tag})
    finally:
        c.close()


def _pick_keeper(copies: list, prefer_folder: str) -> dict:
    """Choose the single keeper for a group per the rule."""
    pool = copies
    if prefer_folder in ("organized", "snapshot"):
        preferred = [c for c in copies if c["folder"] == prefer_folder]
        if preferred:
            pool = preferred
    # tiebreak: shortest path, then cleanest name, then longest name
    def key(c):
        name = os.path.basename(c["path"])
        return (len(_norm(c["path"])), _clean_score(name), -len(name))
    return sorted(pool, key=key)[0]


def _build_groups(prefer_folder: str, cross_only: bool = False,
                  snapshot_only: bool = False) -> dict:
    """Build duplicate groups across/within the two folders.

    snapshot_only=True (STRICT, recommended for "clean Dropbox against Organized
    as master"): the ONLY files ever queued for removal are files under
    Snapshot, and only when a copy of the same content exists in Organized (so
    Organized is the guaranteed keeper). NO Organized file is ever touched — not
    within-Organized dupes, not '-pinned' artifacts, nothing. Snapshot-unique
    files (no Organized twin) are kept. This is the safest master-preserving mode.

    cross_only=True (looser): keep only groups that span BOTH folders, then drop
    every redundant copy in the group (can include redundant Organized copies).
    Kept for completeness; snapshot_only takes precedence when both are set."""
    by_md5: dict = {}
    _load_index(mi.open_index(ORGANIZED_ROOT).db_path, "organized", by_md5)
    _load_index(mi.open_index(SNAPSHOT_ROOT).db_path, "snapshot", by_md5)

    groups = []
    redundant_files = 0
    reclaimable = 0
    cross_groups = cross_files = cross_bytes = 0
    within_groups = within_files = 0
    for md5, copies in by_md5.items():
        if len(copies) < 2:
            continue
        folders = {c["folder"] for c in copies}
        is_cross = len(folders) > 1

        if snapshot_only:
            # Only remove Snapshot files that ALSO exist in Organized.
            organized_copies = [c for c in copies if c["folder"] == "organized"]
            snapshot_copies = [c for c in copies if c["folder"] == "snapshot"]
            if not organized_copies or not snapshot_copies:
                continue   # no Organized keeper, or nothing in Snapshot -> skip
            # keeper is the cleanest Organized copy; Organized is NEVER removed
            keeper = _pick_keeper(organized_copies, "organized")
            redundant = snapshot_copies   # remove ALL snapshot copies (Organized survives)
            gb = sum(c["size"] for c in redundant)
            groups.append({
                "md5": md5, "keeper": keeper, "redundant": redundant,
                "cross_folder": True, "reclaim": gb,
            })
            redundant_files += len(redundant)
            reclaimable += gb
            cross_groups += 1
            cross_files += len(redundant)
            cross_bytes += gb
            continue

        if cross_only and not is_cross:
            continue   # skip within-folder-only groups entirely
        keeper = _pick_keeper(copies, prefer_folder)
        redundant = [c for c in copies if c["path"] != keeper["path"]]
        if not redundant:
            continue
        gb = sum(c["size"] for c in redundant)
        groups.append({
            "md5": md5, "keeper": keeper, "redundant": redundant,
            "cross_folder": is_cross,
            "reclaim": gb,
        })
        redundant_files += len(redundant)
        reclaimable += gb
        if is_cross:
            cross_groups += 1
            cross_files += len(redundant)
            cross_bytes += gb
        else:
            within_groups += 1
            within_files += len(redundant)
    return {
        "groups": groups,
        "summary": {
            "duplicate_groups": len(groups),
            "redundant_files": redundant_files,
            "reclaimable_bytes": reclaimable,
            "cross_folder_groups": cross_groups,
            "cross_folder_redundant_files": cross_files,
            "cross_folder_reclaimable_bytes": cross_bytes,
            "within_folder_groups": within_groups,
            "within_folder_redundant_files": within_files,
            "prefer_folder": prefer_folder,
            "cross_only": cross_only,
            "snapshot_only": snapshot_only,
        },
    }


def preview(prefer_folder: str = "organized", examples: int = 12,
            cross_only: bool = False, snapshot_only: bool = False) -> dict:
    data = _build_groups(prefer_folder, cross_only=cross_only,
                         snapshot_only=snapshot_only)
    s = data["summary"]
    ex = []
    for g in data["groups"][:examples]:
        ex.append({
            "keeper": g["keeper"]["path"],
            "keeper_folder": g["keeper"]["folder"],
            "removes": [r["path"] for r in g["redundant"]][:4],
            "cross_folder": g["cross_folder"],
            "reclaim_mb": round(g["reclaim"] / 1024 / 1024, 1),
        })
    return {"summary": s, "examples": ex}


# --------------------------------------------------------------- paged view
# Cache the last-built groups so the UI can page without rebuilding (~30s) each
# request. Keyed by (prefer_folder, cross_only). Invalidated on purge/undo.
_groups_cache: dict = {}


def _get_cached_groups(prefer_folder: str, cross_only: bool,
                       snapshot_only: bool) -> dict:
    key = (prefer_folder, cross_only, snapshot_only)
    if key not in _groups_cache:
        _groups_cache[key] = _build_groups(prefer_folder, cross_only=cross_only,
                                           snapshot_only=snapshot_only)
    return _groups_cache[key]


def clear_cache():
    _groups_cache.clear()


def groups_page(prefer_folder: str = "organized", cross_only: bool = True,
                snapshot_only: bool = False, offset: int = 0, limit: int = 100,
                folder_filter: str = "") -> dict:
    """Paged view of duplicate groups for the review UI. Returns the summary plus
    a slice of groups, each with the keeper and the exact files queued to be
    quarantined. Cached so paging is instant after the first build."""
    data = _get_cached_groups(prefer_folder, cross_only, snapshot_only)
    groups = data["groups"]
    if folder_filter:
        ff = folder_filter.lower()
        groups = [g for g in groups
                  if ff in g["keeper"]["path"].lower()
                  or any(ff in r["path"].lower() for r in g["redundant"])]
    total = len(groups)
    page = groups[offset:offset + limit]
    out = []
    for g in page:
        out.append({
            "md5": g["md5"],
            "keeper": g["keeper"]["path"],
            "keeper_folder": g["keeper"]["folder"],
            "cross_folder": g["cross_folder"],
            "reclaim_mb": round(g["reclaim"] / 1024 / 1024, 1),
            "removes": [{"path": r["path"], "folder": r["folder"],
                         "size_mb": round(r["size"] / 1024 / 1024, 1)}
                        for r in g["redundant"]],
        })
    return {
        "summary": data["summary"],
        "total_groups": total,
        "offset": offset,
        "limit": limit,
        "filtered": bool(folder_filter),
        "groups": out,
    }


# ----------------------------------------------------------------- apply

def _root_for(folder_tag: str) -> str:
    return ORGANIZED_ROOT if folder_tag == "organized" else SNAPSHOT_ROOT


def _collision_safe(dst: str) -> str:
    if not os.path.exists(dst):
        return dst
    stem, ext = os.path.splitext(dst)
    i = 1
    while os.path.exists(f"{stem}__{i}{ext}"):
        i += 1
    return f"{stem}__{i}{ext}"


def purge(prefer_folder: str = "organized", confirm: bool = False,
          cross_only: bool = False, snapshot_only: bool = False) -> str:
    """Start a background job that quarantines redundant copies. Returns job id.
    snapshot_only=True removes ONLY Snapshot files that also exist in Organized
    (Organized is never touched). cross_only=True limits to cross-folder groups."""
    if not confirm:
        raise ValueError("confirm=true required")
    job_id = f"crossdedup_{int(time.time())}"
    _jobs[job_id] = {"status": "running", "phase": "grouping",
                     "prefer_folder": prefer_folder, "cross_only": cross_only,
                     "snapshot_only": snapshot_only, "quarantined": 0,
                     "reclaimed_bytes": 0, "errors": 0, "started_at": time.time(),
                     "manifest_file": None}
    threading.Thread(target=_purge_worker,
                     args=(job_id, prefer_folder, cross_only, snapshot_only),
                     daemon=True).start()
    return job_id


def get_job(job_id: str):
    return _jobs.get(job_id)


def _purge_worker(job_id: str, prefer_folder: str, cross_only: bool = False,
                  snapshot_only: bool = False):
    job = _jobs[job_id]
    try:
        os.makedirs(_MANIFEST_DIR, exist_ok=True)
        data = _build_groups(prefer_folder, cross_only=cross_only,
                             snapshot_only=snapshot_only)
        job["phase"] = "quarantining"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        q_roots = {
            "organized": os.path.join(ORGANIZED_ROOT, f"_CrossDedupQuarantine_{stamp}"),
            "snapshot": os.path.join(SNAPSHOT_ROOT, f"_CrossDedupQuarantine_{stamp}"),
        }
        mpath = os.path.join(_MANIFEST_DIR, f"crossdedup_{int(time.time()*1000)}.json")
        manifest = {"action": "crossdedup", "at": time.time(),
                    "prefer_folder": prefer_folder, "cross_only": cross_only,
                    "snapshot_only": snapshot_only, "entries": []}
        job["manifest_file"] = mpath

        def flush():
            try:
                tmp = mpath + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(manifest, f)
                os.replace(tmp, mpath)
            except OSError:
                pass
        flush()

        since = 0
        for g in data["groups"]:
            keeper_path = g["keeper"]["path"]
            # safety: only remove redundant if the keeper still exists on disk
            if not os.path.isfile(keeper_path):
                continue
            for r in g["redundant"]:
                src = r["path"]
                if not os.path.isfile(src):
                    continue
                root = _root_for(r["folder"])
                q_root = q_roots[r["folder"]]
                try:
                    rel = os.path.relpath(os.path.abspath(src), os.path.abspath(root))
                    dest = _collision_safe(os.path.join(q_root, rel))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    os.rename(src, dest)
                    manifest["entries"].append({"from": os.path.abspath(src), "to": dest})
                    job["quarantined"] += 1
                    job["reclaimed_bytes"] += r["size"]
                    since += 1
                    if since >= 50:
                        flush(); since = 0
                except OSError:
                    job["errors"] += 1
        flush()
        clear_cache()   # files moved — stale group cache no longer valid
        job["status"] = "completed"; job["phase"] = "complete"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)


def undo(manifest_file: str, confirm: bool = False) -> dict:
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.exists(manifest_file):
        return {"error": "manifest not found"}
    with open(manifest_file, encoding="utf-8") as f:
        man = json.load(f)
    restored = 0
    errors = []
    for e in man.get("entries", []):
        cur, orig = e["to"], e["from"]
        try:
            if not os.path.isfile(cur):
                errors.append(f"missing: {cur}"); continue
            if os.path.exists(orig):
                errors.append(f"orig exists: {orig}"); continue
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            os.rename(cur, orig); restored += 1
        except OSError as ex:
            errors.append(f"{cur}: {ex}")
    clear_cache()   # files restored — stale group cache no longer valid
    return {"restored": restored, "errors": errors}
