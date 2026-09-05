"""
Drive <-> Local STATE RECONCILIATION (tree-walk, identity-based).

Replaces the fragile "modifiedTime/trashed" delta detection with a real
comparison of current Drive state against the local Organized/ tree. It answers,
per file, one of: ADD, DELETE, MOVE, UPDATE, REEXPORT(native), or SKIP.

Why this design (per user's requirements):
  - Trust ACTUAL current state, not Google's change flags.
  - Walk the folder structure; a folder vanishing in Drive does NOT imply its
    files were deleted — they may have MOVED. Move detection is done over the
    WHOLE tree by content identity (MD5), so a relocated file is a MOVE, never a
    false DELETE.
  - Detect in-place UPDATES via size + full MD5 vs Google's md5Checksum.
  - Native Google Docs have no md5Checksum -> always re-export to Office.
  - One local read computes BOTH the dedup smart-hash (SHA-256 / 256KB x3) and
    the full MD5, persisted to the existing scan cache for reuse in future dedup.

Classification per Drive/local file:
  ADD      : in Drive, no local counterpart anywhere
  DELETE   : local, no Drive counterpart anywhere (and not matched as a move)
  MOVE     : same content (MD5) present both sides but at different relative path
  UPDATE   : same relative path, content differs (size or MD5)
  REEXPORT : native Google doc (always refresh to Office)
  SKIP     : same path, identical content

READ-ONLY in detect; the apply phase performs the file operations.
"""
import os
import csv
import json
import subprocess
import threading
import time
from typing import Optional

from .file_scanner import compute_hashes, LARGE_FILE_THRESHOLD
from . import scan_cache

# Configurable via environment; sensible defaults for local dev.
GAM_PATH = os.environ.get("GAM_PATH", r"C:\GAM7\gam.exe")
ORGANIZED_ROOT = os.environ.get("ORGANIZED_ROOT", r"E:\Google Drive Files\Organized")
DEFAULT_ADMIN_USER = os.environ.get("GAM_ADMIN_USER", "admin@example.com")
STORE_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer")
RECON_DIR = os.path.join(STORE_DIR, "reconcile")

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
_NATIVE_EXT = {"document": "docx", "spreadsheet": "xlsx",
               "presentation": "pptx", "drawing": "png"}

_detect_jobs: dict = {}


# ------------------------------------------------------------------ GAM utils

def gam_available() -> bool:
    return os.path.exists(GAM_PATH)


def _gam_csv(out_path: str, args: list[str], timeout: int = 3600,
             expect_rows: bool = False, retries: int = 1) -> int:
    """
    Run a GAM print command writing CSV to out_path. Returns data-row count.
    If expect_rows is True and the file comes back empty/headerless, retries
    (guards against transient locks/failures that would otherwise look like
    'no data'). Waits for the previous file handle to be free.
    """
    for attempt in range(retries + 1):
        # ensure any stale file is gone and not locked
        if os.path.exists(out_path):
            for _ in range(5):
                try:
                    os.remove(out_path)
                    break
                except OSError:
                    time.sleep(0.5)
        cmd = [GAM_PATH, "redirect", "csv", out_path, "multiprocess"] + args
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
        rows = 0
        if os.path.exists(out_path):
            with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                rows = max(0, sum(1 for _ in f) - 1)
        if rows > 0 or not expect_rows or attempt == retries:
            return rows
        time.sleep(2)  # transient failure — wait and retry
    return 0


def _read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return list(csv.DictReader(f))


def list_all_users() -> list[str]:
    out = os.path.join(RECON_DIR, "_users.csv")
    _gam_csv(out, ["print", "users", "query", "isSuspended=false",
                   "fields", "primaryEmail"], timeout=600)
    return [(r.get("primaryEmail") or "").strip()
            for r in _read_csv(out) if (r.get("primaryEmail") or "").strip()]


def list_shared_drives() -> list[dict]:
    out = os.path.join(RECON_DIR, "_teamdrives.csv")
    _gam_csv(out, ["print", "teamdrives", "fields", "id,name"], timeout=600)
    return [{"id": r["id"].strip(), "name": r["name"].strip()}
            for r in _read_csv(out) if r.get("id") and r.get("name")]


# --------------------------------------------------------------- path helpers

def _sanitize(component: str) -> str:
    bad = '<>:"|?*'
    out = "".join("_" if c in bad else c for c in component)
    return out.rstrip(" .") or "_"


def _local_top_for(kind: str, label: str) -> str:
    """Top-level Organized/ folder for a source."""
    return os.path.join(ORGANIZED_ROOT, _sanitize(label))


import re as _re

# Google Takeout mangles exported filenames in predictable ways:
#   - appends "-at-<ISO timestamp>" for file revisions/versions
#   - appends "-pinned" / "-pin" for pinned revisions
#   - appends "(N)" for de-duplicated names
#   - TRUNCATES long names, sometimes leaving a trailing "-" or partial word
# "-pinned"/"-pin" revision marker.
_PIN_RE = _re.compile(r"-pinned|-pin", _re.I)
# Full or truncated "-at-<ISO timestamp>" suffix. Once we see "-at-<digit>" we
# treat EVERYTHING to the end of the stem as the timestamp tail (covers full
# timestamps, fractional seconds, trailing Z, and Google's mid-timestamp
# truncations). Anchored to end of stem so it never eats real words.
# "-at-<timestamp>" tail, optionally followed by "-pinned"/"-p" and a final
# extension (e.g. "...-at-2025-...Z-pinned.sample"). Capture group 1 preserves
# any trailing real extension so we can re-append it.
_TS_TAIL_RE = _re.compile(r"-at-\d[\d\-t:_.z]*(?:-p[a-z]*)?(\.[a-z0-9]{1,7})?$", _re.I)
_TS_TAIL_TRUNC_RE = _re.compile(r"-at-?$", _re.I)  # bare "-at" / "-at-" leftover
_DUP_RE = _re.compile(r"\(\d+\)")


def _norm_name(name: str) -> str:
    """
    Normalize a filename for cross-side matching: lowercase, strip Google export
    suffixes (full OR truncated -at-timestamp, -pinned, (N)), collapse
    whitespace, drop trailing dangling '-'/'_' left by truncation. Keeps ext.
    """
    low = name.lower()
    # 1) Strip "-at-<timestamp>[-pinned][.ext]" tail, preserving any real
    #    trailing extension captured by the regex.
    m = _TS_TAIL_RE.search(low)
    if m:
        kept_ext = m.group(1) or ""
        s = low[:m.start()] + kept_ext
    else:
        s = _PIN_RE.sub("", low)
        s = _TS_TAIL_TRUNC_RE.sub("", s)
    # 2) Split real extension, strip (N) dedup suffix + whitespace/dangling chars
    stem, ext = os.path.splitext(s)
    # guard against a fake numeric "extension" left by a partial timestamp
    if ext and (ext[1:].isdigit() or len(ext) > 8):
        stem, ext = s, ""
    stem = _DUP_RE.sub("", stem)
    stem = _re.sub(r"\s+", " ", stem).strip().rstrip("-_ ").strip()
    return stem + ext


def _norm_rel(rel: str) -> str:
    """Normalize the basename of a relative path, keeping its folder prefix."""
    folder, base = rel.rsplit("/", 1) if "/" in rel else ("", rel)
    nb = _norm_name(base)
    return (folder.lower() + "/" + nb) if folder else nb


def _has_ts(name: str) -> bool:
    """True if the name carries a Takeout revision timestamp (full or truncated)."""
    stem = os.path.splitext(name.lower())[0]
    return bool(_TS_TAIL_RE.search(stem) or _TS_TAIL_TRUNC_RE.search(stem))


# --------------------------------------------------- Drive tree from filelist

def _fetch_drive_index(kind: str, source_id: str, admin_user: str, label: str) -> dict:
    """
    Pull the full live (non-trashed) file/folder listing for a source and return
    a dict: file_id -> {name, parent, mime, md5, size, is_folder}.
    Folders are included so we can rebuild paths.
    """
    safe = label.replace("@", "_at_").replace("/", "_")
    csv_path = os.path.join(RECON_DIR, f"tree_{safe}.csv")
    fields = "id,name,mimetype,parents,md5Checksum,size"
    if kind == "user":
        args = ["user", source_id, "print", "filelist",
                "query", "trashed = false", "fields", fields]
    else:
        args = ["user", admin_user, "print", "filelist",
                "select", "teamdriveid", source_id,
                "query", "trashed = false", "fields", fields]
    # expect_rows=True so a transient empty result retries instead of being
    # silently treated as "no files in Drive" (which would look like all-deletes)
    _gam_csv(csv_path, args, timeout=5400, expect_rows=True, retries=2)

    nodes = {}
    for row in _read_csv(csv_path):
        fid = (row.get("id") or "").strip()
        if not fid:
            continue
        parent = ""
        for k in ("parents.0.id", "parents.0", "parents"):
            if row.get(k):
                parent = row[k].strip()
                break
        mime = (row.get("mimeType") or "").strip()
        nodes[fid] = {
            "name": row.get("name") or "",
            "parent": parent,
            "mime": mime,
            "md5": (row.get("md5Checksum") or "").strip().lower(),
            "size": int(row["size"]) if (row.get("size") or "").isdigit() else None,
            "is_folder": mime == "application/vnd.google-apps.folder",
        }
    return nodes


def _build_rel_path(fid: str, nodes: dict, kind: str, label: str,
                    _seen=None) -> str:
    """
    Build the file's path relative to the source's Organized/ top folder.
    For users we strip a leading 'My Drive'. Shared-drive root is not in nodes,
    so recursion stops there.
    """
    _seen = _seen or set()
    if fid in _seen or fid not in nodes:
        return ""
    _seen.add(fid)
    name = nodes[fid]["name"]
    parent = nodes[fid]["parent"]
    if parent and parent in nodes:
        up = _build_rel_path(parent, nodes, kind, label, _seen)
        return (up + "/" + name) if up else name
    # reached a root not in nodes
    if kind == "user" and name.lower() == "my drive":
        return ""  # drop the My Drive wrapper
    return name


def _drive_files_with_paths(nodes: dict, kind: str, label: str) -> dict:
    """
    Return {rel_path: {id,name,mime,md5,size,is_native}} for every FILE (not
    folder) in the source, path relative to the Organized/ top folder.
    """
    out = {}
    for fid, n in nodes.items():
        if n["is_folder"]:
            continue
        # Shortcuts are pointers, not content — GAM cannot download them and they
        # would be false 'adds'. Their real target is captured under its owner.
        if n["mime"] == "application/vnd.google-apps.shortcut":
            continue
        rel = _build_rel_path(fid, nodes, kind, label)
        # strip leading 'My Drive/' if it survived
        if kind == "user" and rel.lower().startswith("my drive/"):
            rel = rel[len("my drive/"):]
        rel = rel.strip("/")
        if not rel:
            rel = _sanitize(n["name"])
        mime = n["mime"]
        native = mime in GOOGLE_EXPORT
        # native docs get an Office extension appended on export
        if native:
            ext = GOOGLE_EXPORT[mime][1]
            if not rel.lower().endswith("." + ext):
                rel = rel + "." + ext
        out[rel] = {"id": fid, "name": n["name"], "mime": mime,
                    "md5": n["md5"], "size": n["size"], "is_native": native}
    return out


# ------------------------------------------------------ local tree + hashing

def _walk_local(top: str) -> dict:
    """Return {rel_path: abs_path} for every file under a source's local top folder."""
    out = {}
    if not os.path.isdir(top):
        return out
    for root, dirs, files in os.walk(top):
        # skip our own quarantine/backup dirs anywhere
        dirs[:] = [d for d in dirs if not d.startswith("_DeletedSince_")
                   and not d.startswith("_PreReexport_")]
        for fn in files:
            ap = os.path.join(root, fn)
            rel = os.path.relpath(ap, top).replace("\\", "/")
            out[rel] = ap
    return out


def _local_md5(abs_path: str, size: int, cache: dict, mtime: float) -> Optional[str]:
    """Get local file MD5, using/refreshing the shared scan cache (one read gives
    both smart-hash + md5, both persisted)."""
    entry = scan_cache.get_cached_entry(cache, abs_path, size, mtime)
    if entry and entry.get("md5"):
        return entry["md5"]
    res = compute_hashes(abs_path, size, want_md5=True)
    scan_cache.put_cached_entry(cache, abs_path, size, mtime,
                                res.get("smart"), res.get("md5"))
    return res.get("md5")


# --------------------------------------------------------------- reconcile

def detect(scope: str = "all", admin_user: str = DEFAULT_ADMIN_USER,
           hash_local: bool = True) -> str:
    os.makedirs(RECON_DIR, exist_ok=True)
    job_id = f"reconcile_{int(time.time())}"
    _detect_jobs[job_id] = {
        "status": "running", "phase": "starting", "scope": scope,
        "admin_user": admin_user, "hash_local": hash_local,
        "users_total": 0, "users_done": 0,
        "drives_total": 0, "drives_done": 0,
        "adds": 0, "deletes": 0, "moves": 0, "updates": 0,
        "reexports": 0, "skips": 0, "version_artifacts": 0,
        "current": "", "cancelled": False, "started_at": time.time(),
        "report_file": os.path.join(RECON_DIR, f"{job_id}.json"),
        "errors": [],
    }
    threading.Thread(target=_detect_worker,
                     args=(job_id, scope, admin_user, hash_local),
                     daemon=True).start()
    return job_id


def get_detect_job(job_id: str):
    return _detect_jobs.get(job_id)


def cancel_detect(job_id: str) -> bool:
    if job_id in _detect_jobs:
        _detect_jobs[job_id]["cancelled"] = True
        return True
    return False


class EmptyDriveIndexError(Exception):
    """Raised when Drive returns no files for a source that has local files —
    a safety guard so we never mass-quarantine on a failed/empty Drive fetch."""


def _reconcile_source(job: dict, kind: str, sid: str, label: str,
                      admin_user: str, hash_local: bool) -> dict:
    """Classify every file for one source. Returns the source result dict."""
    nodes = _fetch_drive_index(kind, sid, admin_user, label)
    drive_files = _drive_files_with_paths(nodes, kind, label)   # rel -> meta
    top = _local_top_for(kind, label)
    local_files = _walk_local(top)                              # rel -> abs

    # SAFETY GUARD: a Drive fetch that returns zero files while the local folder
    # has files almost always means the GAM call failed (lock/timeout/network),
    # NOT that every file was deleted. Never mass-quarantine on that. Bail out.
    if not drive_files and local_files:
        raise EmptyDriveIndexError(
            f"Drive returned 0 files for {kind} '{label}' but {len(local_files)} "
            f"local files exist — refusing to classify as deletes (likely a "
            f"failed Drive fetch). Skipped for safety.")

    # LAZY HASHING: we do NOT hash all local files up front. Most files match by
    # path+size and are skipped without ever being read. We only hash the small
    # set of "orphans" (files present on one side but not path-matched on the
    # other) to distinguish MOVE from ADD/DELETE.
    cache = scan_cache.load_cache(top) if hash_local else {}

    # local size index (cheap: os.stat only, no reads)
    local_size = {}   # rel -> {abs, size, mtime}
    for rel, ap in local_files.items():
        try:
            st = os.stat(ap)
        except OSError:
            continue
        local_size[rel] = {"abs": ap, "size": st.st_size, "mtime": st.st_mtime}

    adds, deletes, moves, updates, reexports, skips = [], [], [], [], [], []
    version_artifacts = []   # local Takeout revision copies (-at-<ts>) w/ a current version
    matched_local = set()
    drive_orphans = []   # Drive files with no local path-match (candidate add/move-target)

    # normalized-name index of ALL drive files (for rename/truncation matching)
    drive_by_norm = {}
    for rel, m in drive_files.items():
        drive_by_norm.setdefault(_norm_rel(rel), []).append((rel, m))

    # ---- PASS 1: path-based classification (no hashing) ----
    for rel, m in drive_files.items():
        if m["is_native"]:
            reexports.append({"rel": rel, "id": m["id"], "mime": m["mime"],
                              "name": m["name"]})
            if rel in local_size:
                matched_local.add(rel)
            continue

        lm = local_size.get(rel)
        if lm is not None:
            matched_local.add(rel)
            if m["size"] is not None and lm["size"] != m["size"]:
                updates.append({"rel": rel, "id": m["id"], "mime": m["mime"],
                                "reason": "size"})
            else:
                skips.append(rel)
        else:
            drive_orphans.append((rel, m))

    local_orphans = [(rel, meta) for rel, meta in local_size.items()
                     if rel not in matched_local and rel not in drive_files]

    # ---- PASS 2a: separate VERSION ARTIFACTS ----
    # A local file carrying a "-at-<timestamp>" revision suffix whose CURRENT
    # (clean-named) version still exists — in Drive or locally — is a Takeout
    # export revision copy, not a real user file. Bucket it separately so it is
    # never confused with a genuine deletion.
    # Precompute (once) the set of normalized names that have a CLEAN (no-ts)
    # local version — used to decide if a "-at-<ts>" file is a version artifact.
    clean_local_norms = set()
    for r in local_size:
        b = r.rsplit("/", 1)[-1]
        if not _has_ts(b):
            clean_local_norms.add(_norm_rel(r))

    remaining_local = []
    for rel, meta in local_orphans:
        base = rel.rsplit("/", 1)[-1]
        if _has_ts(base):
            nrel = _norm_rel(rel)
            # current version exists in Drive OR as a clean-named local file
            if nrel in drive_by_norm or nrel in clean_local_norms:
                version_artifacts.append({"rel": rel, "abs": meta["abs"]})
                continue
        remaining_local.append((rel, meta))
    local_orphans = remaining_local

    # ---- PASS 2b: match drive orphans to local orphans ----
    # Tier 1: normalized-name match (no hashing) — catches renames/untruncation.
    # Tier 2: md5 hash fallback (size-gated) for anything still unmatched.
    local_by_norm = {}
    for rel, meta in local_orphans:
        local_by_norm.setdefault(_norm_rel(rel), []).append((rel, meta))

    used_local_orphan = set()
    unmatched_drive = []

    for rel, m in drive_orphans:
        nrel = _norm_rel(rel)
        cand = None
        for cand_rel, cand_meta in local_by_norm.get(nrel, []):
            if cand_rel not in used_local_orphan:
                cand = cand_rel
                break
        if cand is not None:
            moves.append({"from": cand, "to": rel, "id": m["id"]})
            used_local_orphan.add(cand)
            matched_local.add(cand)
        else:
            unmatched_drive.append((rel, m))

    # ---- Tier 1.5: PREFIX-TRUNCATION match (no hashing) ----
    # Google truncates long filenames, so the LOCAL copy's normalized name is
    # often a prefix of the full Drive name (same folder). Match a still-
    # unmatched Drive orphan to a local orphan in the SAME folder whose
    # normalized stem is a prefix of the Drive file's normalized stem.
    #
    # PERFORMANCE: to avoid O(drive_orphans x local_orphans) blowup on large
    # folders, we BUCKET local orphans by (folder, ext, first-K-chars-of-stem).
    # A valid truncated prefix is >= MIN_PREFIX chars, so the first K chars must
    # match — we only compare within the tiny same-bucket set.
    MIN_PREFIX = 10
    BUCKET_K = 10  # == MIN_PREFIX; first K chars are shared by any prefix match

    def _split_norm(nrel):
        folder, base = nrel.rsplit("/", 1) if "/" in nrel else ("", nrel)
        stem, ext = os.path.splitext(base)
        return folder, stem, ext

    # Precompute normalized (folder, stem, ext) for local orphans ONCE and bucket.
    local_bucket = {}   # (folder, ext, stem[:K]) -> [(rel, meta, stem)]
    for r, mt in local_orphans:
        if r in used_local_orphan:
            continue
        nf, ns, ne = _split_norm(_norm_rel(r))
        if len(ns) < MIN_PREFIX:
            continue
        key = (nf, ne, ns[:BUCKET_K])
        local_bucket.setdefault(key, []).append((r, mt, ns))

    after_prefix = []
    for rel, m in unmatched_drive:
        dnf, dns, dne = _split_norm(_norm_rel(rel))
        chosen = None
        if len(dns) >= BUCKET_K:
            bucket = local_bucket.get((dnf, dne, dns[:BUCKET_K]), [])
            cands = [(lr, lmt) for (lr, lmt, lns) in bucket
                     if lr not in used_local_orphan and dns.startswith(lns)]
            if len(cands) == 1:
                chosen = cands[0][0]
            elif len(cands) > 1 and m["size"] is not None:
                sized = [c for c in cands if c[1]["size"] == m["size"]]
                if len(sized) == 1:
                    chosen = sized[0][0]
        if chosen is not None:
            moves.append({"from": chosen, "to": rel, "id": m["id"],
                          "via": "prefix"})
            used_local_orphan.add(chosen)
            matched_local.add(chosen)
        else:
            after_prefix.append((rel, m))
    unmatched_drive = after_prefix

    # Tier 2: hash fallback for still-unmatched orphans (small set)
    hashed = 0
    if hash_local and unmatched_drive:
        # index remaining local orphans by md5 (only those not yet used)
        remaining_local_orphans = [(r, mt) for r, mt in local_orphans
                                   if r not in used_local_orphan]
        # size-gate: only hash local orphans whose size matches some drive orphan
        drive_sizes = {m["size"] for _, m in unmatched_drive if m["size"] is not None}
        local_orphan_md5 = {}
        for r, mt in remaining_local_orphans:
            if mt["size"] in drive_sizes:
                md5 = _local_md5(mt["abs"], mt["size"], cache, mt["mtime"])
                hashed += 1
                if md5:
                    local_orphan_md5.setdefault(md5, []).append((r, mt))
        if hashed:
            scan_cache.save_cache(top, cache)
        still_unmatched = []
        for rel, m in unmatched_drive:
            moved_from = None
            if m["md5"] and m["md5"] in local_orphan_md5:
                for cand_rel, _cm in local_orphan_md5[m["md5"]]:
                    if cand_rel not in used_local_orphan:
                        moved_from = cand_rel
                        break
            if moved_from:
                moves.append({"from": moved_from, "to": rel, "id": m["id"]})
                used_local_orphan.add(moved_from)
                matched_local.add(moved_from)
            else:
                still_unmatched.append((rel, m))
        unmatched_drive = still_unmatched

    # remaining unmatched drive orphans = genuine ADDS
    for rel, m in unmatched_drive:
        adds.append({"rel": rel, "id": m["id"], "mime": m["mime"],
                     "name": m["name"], "is_native": m["is_native"]})

    # remaining local orphans not claimed by a move = genuine DELETE
    for rel, meta in local_orphans:
        if rel in used_local_orphan:
            continue
        deletes.append({"rel": rel, "abs": meta["abs"]})

    return {
        "kind": kind, "label": label, "id": sid,
        "drive_file_count": len(drive_files),
        "local_file_count": len(local_files),
        "hashed_orphans": hashed,
        "adds": adds, "deletes": deletes, "moves": moves,
        "updates": updates, "reexports": reexports,
        "version_artifacts": version_artifacts,
        "skip_count": len(skips),
    }


def _detect_worker(job_id, scope, admin_user, hash_local):
    job = _detect_jobs[job_id]
    report = {"generated_at": time.time(), "organized_root": ORGANIZED_ROOT,
              "sources": []}
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

        job["phase"] = "reconciling"
        for kind, sid, label in sources:
            if job.get("cancelled"):
                job["status"] = "cancelled"
                break
            job["current"] = f"{kind}: {label}"
            try:
                res = _reconcile_source(job, kind, sid, label, admin_user, hash_local)
            except subprocess.TimeoutExpired:
                job["errors"].append(f"timeout: {label}")
                continue
            except EmptyDriveIndexError as e:
                job["errors"].append(f"SAFETY-SKIP {label}: {e}")
                continue
            except Exception as e:
                job["errors"].append(f"{label}: {e}")
                continue
            job["adds"] += len(res["adds"])
            job["deletes"] += len(res["deletes"])
            job["moves"] += len(res["moves"])
            job["updates"] += len(res["updates"])
            job["reexports"] += len(res["reexports"])
            job["version_artifacts"] += len(res.get("version_artifacts", []))
            job["skips"] += res["skip_count"]
            report["sources"].append(res)
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


# ================================================================== APPLY

_apply_jobs: dict = {}


def _safe_local_path(top: str, rel: str) -> str:
    """
    Build a Windows-safe absolute path from a Drive-relative path. The rel uses
    '/' as the separator, but a Drive FILE or FOLDER name can itself contain '/'
    or other reserved chars. We sanitize EACH component so embedded slashes don't
    create phantom directories (the cause of WinError 3 crashes).
    """
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    safe_parts = [_sanitize(p) for p in parts]
    return os.path.join(top, *safe_parts) if safe_parts else top


def _gam_download(user_or_admin: str, file_id: str, mime: str,
                  target_dir: str, timeout: int = 1800) -> bool:
    """Download one Drive file into target_dir; native docs exported to Office."""
    os.makedirs(target_dir, exist_ok=True)
    args = [GAM_PATH, "user", user_or_admin, "get", "drivefile", file_id]
    if mime in GOOGLE_EXPORT:
        args += ["format", GOOGLE_EXPORT[mime][1]]
    args += ["targetfolder", target_dir]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return r.returncode == 0


def _gam_batch_download(user_for: str, items: list, top: str, job: dict,
                        counter_key: str, timeout: int = 2700) -> None:
    """
    Download many Drive files in ONE GAM process using GAM's CSV batch mode:
        gam csv <input.csv> gam user "~owner" get drivefile "~id"
                [format "~format"] targetfolder "~folder"
    This avoids spawning a separate GAM process per file (the fatal bottleneck).

    items: list of dicts with keys: id, mime, target_dir
    counter_key: job counter to increment ("added" or "reexported").
    Progress is tracked by counting files actually written into target dirs.
    """
    if not items:
        return
    # Build the batch CSV. GAM substitutes ~col tokens per row.
    batch_csv = os.path.join(RECON_DIR, f"dl_{counter_key}_{int(time.time()*1000)}.csv")
    # native docs need a per-row format column; non-native leave blank
    rows = []
    for it in items:
        try:
            os.makedirs(it["target_dir"], exist_ok=True)
        except OSError as e:
            job["errors"].append(f"mkdir {it['target_dir']}: {e}")
            job["add_failed"] += 1
            continue
        fmt = GOOGLE_EXPORT[it["mime"]][1] if it["mime"] in GOOGLE_EXPORT else ""
        rows.append((user_for, it["id"], fmt, it["target_dir"]))
    with open(batch_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["owner", "id", "fmt", "folder"])
        w.writerows(rows)

    # Two GAM sub-commands: one for native (needs format), one for binary.
    # Simplest robust approach: split into native vs binary batches so the
    # 'format' argument is only present when needed.
    native = [r for r in rows if r[2]]
    binary = [r for r in rows if not r[2]]

    def _folder_counts(subset):
        counts = {}
        for _o, _i, _f, folder in subset:
            if folder not in counts:
                try:
                    counts[folder] = sum(1 for _ in os.scandir(folder))
                except OSError:
                    counts[folder] = 0
        return counts

    def run_batch(subset, with_format):
        if not subset:
            return
        cpath = os.path.join(RECON_DIR,
                             f"b_{counter_key}_{'n' if with_format else 'x'}_{int(time.time()*1000)}.csv")
        with open(cpath, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["owner", "id", "fmt", "folder"])
            w.writerows(subset)
        gam_sub = ["gam", "user", "~owner", "get", "drivefile", "~id"]
        if with_format:
            gam_sub += ["format", "~fmt"]
        gam_sub += ["targetfolder", "~folder"]
        # 'gam csv <file> gam <template>' — GAM runs the template per row within a
        # single process (huge speedup vs per-file subprocess spawns).
        cmd = [GAM_PATH, "csv", cpath] + gam_sub
        before = _folder_counts(subset)
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
        after = _folder_counts(subset)
        # success = net new files across all target folders (delta)
        got = sum(max(0, after.get(fd, 0) - before.get(fd, 0)) for fd in before)
        got = min(got, len(subset))
        job[counter_key] += got
        job["add_failed"] += max(0, len(subset) - got)

    run_batch(binary, with_format=False)
    if job.get("cancelled"):
        return
    run_batch(native, with_format=True)


def apply_reconcile(report_file: str, admin_user: str = DEFAULT_ADMIN_USER,
                    do_adds: bool = True, do_updates: bool = True,
                    do_moves: bool = True, do_deletes: bool = True,
                    do_reexports: bool = True) -> str:
    job_id = f"reconcile_apply_{int(time.time())}"
    _apply_jobs[job_id] = {
        "status": "running", "phase": "starting", "report_file": report_file,
        "admin_user": admin_user,
        "added": 0, "updated": 0, "moved": 0, "quarantined": 0,
        "reexported": 0, "add_failed": 0, "skipped_existing": 0, "errors": [],
        "quarantine_root": os.path.join(ORGANIZED_ROOT, f"_DeletedSince_{time.strftime('%Y%m%d')}"),
        "current": "", "cancelled": False, "started_at": time.time(),
        "flags": {"adds": do_adds, "updates": do_updates, "moves": do_moves,
                  "deletes": do_deletes, "reexports": do_reexports},
    }
    threading.Thread(target=_apply_worker, args=(job_id,), daemon=True).start()
    return job_id


def get_apply_job(job_id: str):
    return _apply_jobs.get(job_id)


def cancel_apply(job_id: str) -> bool:
    if job_id in _apply_jobs:
        _apply_jobs[job_id]["cancelled"] = True
        return True
    return False


def _quarantine(abs_path: str, quarantine_root: str, job: dict) -> bool:
    """Move a local file into the dated quarantine, mirroring its relative path."""
    import shutil
    if not os.path.exists(abs_path):
        return False
    rel = os.path.relpath(abs_path, ORGANIZED_ROOT)
    dest = os.path.join(quarantine_root, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # collision-safe
    if os.path.exists(dest):
        base, ext = os.path.splitext(dest)
        i = 1
        while os.path.exists(f"{base} ({i}){ext}"):
            i += 1
        dest = f"{base} ({i}){ext}"
    try:
        shutil.move(abs_path, dest)
        return True
    except OSError as e:
        job["errors"].append(f"quarantine {abs_path}: {e}")
        return False


def _apply_one_source(src, kind, label, sid, user_for, top, fl, qroot, job):
    """Apply moves/updates/adds/reexports/deletes for one source. Uses
    _safe_local_path so Drive names containing '/' or reserved chars never
    create phantom directories."""
    import shutil

    # MOVES
    if fl["moves"]:
        for mv in src.get("moves", []):
            if job.get("cancelled"):
                break
            src_abs = _safe_local_path(top, mv["from"])
            dst_abs = _safe_local_path(top, mv["to"])
            if os.path.exists(src_abs):
                try:
                    os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
                    if not os.path.exists(dst_abs):
                        shutil.move(src_abs, dst_abs)
                        job["moved"] += 1
                except OSError as e:
                    job["errors"].append(f"move {src_abs}: {e}")

    # UPDATES
    if fl["updates"]:
        for up in src.get("updates", []):
            if job.get("cancelled"):
                break
            old_abs = _safe_local_path(top, up["rel"])
            tgt_dir = os.path.dirname(old_abs)
            _quarantine(old_abs, qroot, job)
            if _gam_download(user_for, up["id"], up["mime"], tgt_dir):
                job["updated"] += 1
            else:
                job["add_failed"] += 1

    # ADDS — batched, SKIP files already present (safe resume; avoids
    # collision-rename duplicates when re-running after an interruption)
    if fl["adds"] and not job.get("cancelled"):
        add_items = []
        for ad in src.get("adds", []):
            dest_abs = _safe_local_path(top, ad["rel"])
            if os.path.exists(dest_abs):
                job["skipped_existing"] = job.get("skipped_existing", 0) + 1
                continue
            add_items.append({"id": ad["id"], "mime": ad["mime"],
                              "target_dir": os.path.dirname(dest_abs)})
        for i in range(0, len(add_items), 500):
            if job.get("cancelled"):
                break
            _gam_batch_download(user_for, add_items[i:i+500], top, job, "added")

    # REEXPORTS — batched
    if fl["reexports"] and not job.get("cancelled"):
        rx_items = []
        for rx in src.get("reexports", []):
            ext = GOOGLE_EXPORT.get(rx["mime"], (None, "docx"))[1]
            rel = rx["rel"]
            local_rel = rel if rel.lower().endswith("." + ext) else rel + "." + ext
            old_abs = _safe_local_path(top, local_rel)
            if os.path.exists(old_abs):
                _quarantine(old_abs, qroot, job)
            rx_items.append({"id": rx["id"], "mime": rx["mime"],
                             "target_dir": os.path.dirname(old_abs)})
        for i in range(0, len(rx_items), 500):
            if job.get("cancelled"):
                break
            _gam_batch_download(user_for, rx_items[i:i+500], top, job, "reexported")

    # DELETES — quarantine
    if fl["deletes"]:
        for dl in src.get("deletes", []):
            if job.get("cancelled"):
                break
            if _quarantine(dl["abs"], qroot, job):
                job["quarantined"] += 1


def _apply_worker(job_id):
    import shutil
    job = _apply_jobs[job_id]
    fl = job["flags"]
    qroot = job["quarantine_root"]
    try:
        with open(job["report_file"], "r", encoding="utf-8") as f:
            report = json.load(f)

        for src in report.get("sources", []):
            if job.get("cancelled"):
                break
            kind, label, sid = src["kind"], src["label"], src["id"]
            user_for = sid if kind == "user" else job["admin_user"]
            top = _local_top_for(kind, label)
            job["current"] = f"{kind}: {label}"
            # A single bad path in one source must never abort the whole job.
            try:
                _apply_one_source(src, kind, label, sid, user_for, top,
                                  fl, qroot, job)
            except Exception as e:
                job["errors"].append(f"source {label}: {e}")
                continue

        if not job.get("cancelled"):
            job["phase"] = "complete"
            job["status"] = "completed"
        else:
            job["status"] = "cancelled"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)

