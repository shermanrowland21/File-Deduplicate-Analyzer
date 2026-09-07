"""
RECONSTRUCT — fix the mangled Takeout extraction using the Drive blueprint.

Joins the LOCAL full-MD5 index (md5_index) against the AUTHORITATIVE Drive
blueprint (drive_blueprint, shared drives only) on md5Checksum, and records —
in a SQLite fix-ledger — each local file's correct name and correct path per
Google Drive. State is tracked so this is fully idempotent + resumable:

  - Run it NOW on whatever the MD5 index has already hashed.
  - Run it again later; it processes only newly-hashed, still-pending files and
    skips everything already resolved.

Statuses (ledger):
  pending   : hashed locally, not yet matched
  matched   : md5 found in the Drive blueprint -> correct_name/correct_rel_path set
  no_match  : md5 not in any shared drive (extraction artifact, or My-Drive-only,
              or genuinely not on a shared drive) -> left alone
  renamed / moved : the fix was applied (later confirmed step)
  error     : something went wrong

READ-ONLY here: the matcher PROPOSES the correct name/path. Applying renames/
moves is a separate, confirmed step (not in this pass).

Portability: self-contained; the Ledger class isolates persistence for CPMS.
"""
import os
import time
import sqlite3
import threading
from typing import Optional

from . import md5_index as mi
from . import drive_blueprint as bp

_DB_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "reconstruct")
_LEDGER_PATH = os.path.join(_DB_DIR, "ledger.db")

_match_jobs: dict = {}


# ============================================================ ledger

class Ledger:
    def __init__(self, db_path: str = _LEDGER_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path             TEXT PRIMARY KEY,
                md5              TEXT,
                status           TEXT,
                drive_file_id    TEXT,
                correct_name     TEXT,
                correct_rel_path TEXT,
                drive_name       TEXT,
                note             TEXT,
                updated_at       REAL
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_led_status ON files(status)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_led_md5 ON files(md5)")
        self._conn.commit()

    def already_resolved(self, path: str) -> bool:
        r = self._conn.execute(
            "SELECT status FROM files WHERE path=?", (path,)).fetchone()
        return bool(r and r[0] in ("matched", "no_match", "renamed", "moved"))

    def mark_many(self, rows: list[tuple]):
        """rows: (path, md5, status, drive_file_id, correct_name,
                  correct_rel_path, drive_name, note)"""
        if not rows:
            return
        now = time.time()
        self._conn.executemany(
            "INSERT OR REPLACE INTO files "
            "(path, md5, status, drive_file_id, correct_name, correct_rel_path, "
            " drive_name, note, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], now) for r in rows])
        self._conn.commit()

    def stats(self) -> dict:
        cur = self._conn.execute(
            "SELECT status, COUNT(*) FROM files GROUP BY status")
        by = {row[0]: row[1] for row in cur.fetchall()}
        total = sum(by.values())
        return {"total": total, "by_status": by, "db_path": self.db_path}

    def list_by_status(self, status: str, limit: int = 500) -> list[dict]:
        self._conn.row_factory = sqlite3.Row
        rows = self._conn.execute(
            "SELECT * FROM files WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (status, limit)).fetchall()
        self._conn.row_factory = None
        return [dict(r) for r in rows]

    def close(self):
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


def open_ledger() -> Ledger:
    return Ledger()


# ============================================================ matcher

def match(local_root: str) -> str:
    """Start a background matcher pass over the LOCAL md5 index for `local_root`,
    joining each hashed file's md5 to the Drive blueprint. Idempotent/resumable:
    skips files already resolved in the ledger."""
    job_id = f"match_{int(time.time())}"
    _match_jobs[job_id] = {
        "status": "running", "phase": "starting", "local_root": local_root,
        "processed": 0, "matched": 0, "no_match": 0, "skipped_done": 0,
        "started_at": time.time(), "cancelled": False, "error": None,
    }
    threading.Thread(target=_match_worker, args=(job_id, local_root),
                     daemon=True).start()
    return job_id


def get_match_job(job_id: str):
    return _match_jobs.get(job_id)


def cancel_match(job_id: str) -> bool:
    if job_id in _match_jobs:
        _match_jobs[job_id]["cancelled"] = True
        return True
    return False


def _match_worker(job_id: str, local_root: str):
    job = _match_jobs[job_id]
    idx = mi.open_index(local_root)     # local full-MD5 index for this root
    blue = bp.open_blueprint()          # authoritative Drive blueprint
    led = open_ledger()
    try:
        # pull all hashed local files for this root (path, md5)
        cur = idx._conn.execute("SELECT path, md5 FROM files")
        batch = []
        for path, md5 in cur:
            if job.get("cancelled"):
                job["status"] = "cancelled"; break
            job["processed"] += 1
            # RESUMABLE: skip files already resolved
            try:
                if led.already_resolved(path):
                    job["skipped_done"] += 1
                    continue
            except Exception:
                pass
            matches = blue.find_by_md5(md5) if md5 else []
            if matches:
                m = matches[0]   # first authoritative match by content
                batch.append((path, md5, "matched", m["file_id"], m["name"],
                              m["rel_path"], m["drive_name"],
                              f"matched in shared drive '{m['drive_name']}'"
                              + (f" (+{len(matches)-1} more)" if len(matches) > 1 else "")))
                job["matched"] += 1
            else:
                batch.append((path, md5, "no_match", None, None, None, None,
                              "md5 not found in any shared drive"))
                job["no_match"] += 1
            if len(batch) >= 500:
                led.mark_many(batch); batch.clear()
        led.mark_many(batch)
        if not job.get("cancelled"):
            job["phase"] = "complete"; job["status"] = "completed"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)
    finally:
        idx.close(); blue.close(); led.close()


# ============================================================ Phase 1: rename in place

import shutil
import json as _json

_MANIFEST_DIR = os.path.join(_DB_DIR, "rename_manifests")


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


def _proposed_renames(limit: Optional[int] = None) -> list[dict]:
    """From ledger status=matched, the files whose LOCAL basename differs from
    the Drive-authoritative correct_name. In-place only (same directory)."""
    led = open_ledger()
    try:
        led._conn.row_factory = sqlite3.Row
        q = ("SELECT path, correct_name, drive_name FROM files "
             "WHERE status='matched' AND correct_name IS NOT NULL")
        rows = led._conn.execute(q).fetchall()
        led._conn.row_factory = None
    finally:
        led.close()
    out = []
    for r in rows:
        path = r["path"]
        cur_name = os.path.basename(path)
        correct = _safe_correct_name(cur_name, r["correct_name"])
        if not correct or cur_name == correct:
            continue   # nothing to fix (or name already correct)
        out.append({
            "path": path,
            "dir": os.path.dirname(path),
            "current_name": cur_name,
            "correct_name": correct,
            "drive_name": r["drive_name"],
        })
        if limit and len(out) >= limit:
            break
    return out


# Windows-reserved characters that can't be in a filename; sanitize defensively.
_BAD_CHARS = '<>:"/\\|?*'


def _safe_correct_name(current: str, drive_name: str) -> Optional[str]:
    """Return the safe target filename to use for an in-place rename.

    Guards:
      - PRESERVE the local extension if the Drive-authoritative name has none
        (Google often stores names without extensions; we must not strip an
        extension off a real file, e.g. drop '.mp4').
      - If the Drive name has a DIFFERENT extension than the local file, keep
        the LOCAL extension (content is the local file; its real type is the
        local ext). We only fix the stem.
      - Sanitize characters illegal on Windows.
      - Never return an empty/dot name.
    """
    if not drive_name:
        return None
    drive_name = "".join(("_" if c in _BAD_CHARS else c) for c in drive_name).strip()
    if not drive_name or drive_name in (".", ".."):
        return None
    loc_stem, loc_ext = os.path.splitext(current)
    drv_stem, drv_ext = os.path.splitext(drive_name)
    if not drv_ext:
        # Drive name has no extension -> keep local extension
        return drv_stem + loc_ext if loc_ext else drive_name
    if loc_ext and drv_ext.lower() != loc_ext.lower():
        # extensions differ -> trust the LOCAL file's actual extension
        return drv_stem + loc_ext
    return drive_name


def rename_preview(limit: int = 1000) -> dict:
    """DRY RUN — proposed in-place filename corrections (no changes)."""
    items = _proposed_renames(limit=limit)
    # total count without limit (cheap second pass count)
    total = len(_proposed_renames(limit=None))
    return {"total_proposed": total, "showing": len(items), "items": items}


def apply_renames(only_paths: Optional[list[str]] = None,
                  confirm: bool = False) -> dict:
    """Rename matched files IN PLACE to their Drive-authoritative name. Same
    directory only — NEVER moves across folders (that's Phase 2). Collision-safe,
    never overwrites, reversible via manifest, ledger updated to status=renamed."""
    if not confirm:
        return {"error": "confirm=true required"}
    os.makedirs(_MANIFEST_DIR, exist_ok=True)
    proposals = _proposed_renames(limit=None)
    if only_paths:
        wanted = set(only_paths)
        proposals = [p for p in proposals if p["path"] in wanted]

    led = open_ledger()
    entries = []
    renamed = 0
    skipped = []
    errors = []
    try:
        ledger_updates = []
        for p in proposals:
            src = p["path"]
            if not os.path.isfile(src):
                skipped.append({"path": src, "reason": "not found / not a file"})
                continue
            dst = os.path.join(p["dir"], p["correct_name"])
            if os.path.abspath(src) == os.path.abspath(dst):
                continue
            final_dst = _collision_safe(dst)
            try:
                os.rename(src, final_dst)
                entries.append({"from": src, "to": final_dst})
                ledger_updates.append((final_dst, "", "renamed", None,
                                       p["correct_name"], None, p["drive_name"],
                                       f"renamed in place from '{p['current_name']}'"))
                # also mark the old path row as superseded (status renamed)
                renamed += 1
            except OSError as e:
                errors.append(f"{src}: {e}")

        # update ledger: new path rows as renamed; old path rows too
        if ledger_updates:
            led.mark_many(ledger_updates)
        # mark original paths as renamed so re-runs don't re-propose them
        if entries:
            now = time.time()
            led._conn.executemany(
                "UPDATE files SET status='renamed', note='superseded by rename', "
                "updated_at=? WHERE path=?",
                [(now, e["from"]) for e in entries])
            led._conn.commit()
    finally:
        led.close()

    manifest = {"action": "reconstruct_rename", "at": time.time(),
                "entries": entries}
    mpath = os.path.join(_MANIFEST_DIR, f"rename_{int(time.time()*1000)}.json")
    with open(mpath, "w", encoding="utf-8") as f:
        _json.dump(manifest, f, indent=2, ensure_ascii=False)

    return {"renamed": renamed, "skipped": skipped, "errors": errors,
            "manifest_file": mpath}


def undo_renames(manifest_file: str, confirm: bool = False) -> dict:
    """Reverse an in-place rename batch using its manifest."""
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.exists(manifest_file):
        return {"error": f"manifest not found: {manifest_file}"}
    with open(manifest_file, "r", encoding="utf-8") as f:
        man = _json.load(f)
    restored = 0
    errors = []
    for e in man.get("entries", []):
        cur = e["to"]
        orig = e["from"]
        try:
            if not os.path.isfile(cur):
                errors.append(f"missing: {cur}")
                continue
            if os.path.exists(orig):
                errors.append(f"original exists, skipped: {orig}")
                continue
            os.rename(cur, orig)
            restored += 1
        except OSError as ex:
            errors.append(f"{cur}: {ex}")
    return {"restored": restored, "errors": errors}


# ============================================================ (N)-dup purge

import re as _re

# Matches a filename ending in " (N)" before the extension, e.g. "video (1).mp4",
# "photo (12).png", or "notes (3)" (no ext). Group 1 = clean stem, group 2 = ext.
_PAREN_RE = _re.compile(r"^(.*?) \((\d+)\)(\.[^.]+)?$")

_PURGE_MANIFEST_DIR = os.path.join(_DB_DIR, "purge_manifests")


def _clean_twin_name(name: str) -> Optional[str]:
    """If `name` is a ' (N)' duplicate, return the clean base name it derives
    from (e.g. 'video (1).mp4' -> 'video.mp4'). Else None."""
    m = _PAREN_RE.match(name)
    if not m:
        return None
    stem, _num, ext = m.group(1), m.group(2), m.group(3) or ""
    return stem + ext


def find_paren_dupes(local_root: str) -> dict:
    """DRY RUN. Find ' (N)' files that are byte-identical (same md5) to a
    clean-named twin. Those ' (N)' files are safe to remove (the clean twin is
    kept). Returns list + counts. Never deletes here."""
    idx = mi.open_index(local_root)
    try:
        rows = idx._conn.execute("SELECT path, md5, size FROM files WHERE md5<>''").fetchall()
    finally:
        idx.close()
    from collections import defaultdict
    by_md5 = defaultdict(list)
    size_of = {}
    for path, md5, size in rows:
        by_md5[md5].append(path)
        size_of[path] = size or 0

    victims = []
    total_bytes = 0
    for md5, paths in by_md5.items():
        if len(paths) < 2:
            continue
        names = {p: os.path.basename(p) for p in paths}
        # clean (non-(N)) names present in this identical-content group
        clean_names = {names[p] for p in paths if not _PAREN_RE.match(names[p])}
        for p in paths:
            n = names[p]
            if not _PAREN_RE.match(n):
                continue
            twin = _clean_twin_name(n)
            # only delete the (N) copy if a clean twin with the SAME content exists
            if twin and twin in clean_names:
                victims.append({"path": p, "name": n, "twin": twin,
                                "size": size_of.get(p, 0), "md5": md5})
                total_bytes += size_of.get(p, 0)
    victims.sort(key=lambda v: -v["size"])
    return {"local_root": local_root, "count": len(victims),
            "total_bytes": total_bytes, "victims": victims}


def purge_paren_dupes(local_root: str, only_paths: Optional[list[str]] = None,
                      confirm: bool = False) -> dict:
    """Quarantine (safe-trash) the ' (N)' duplicate files that have a same-md5
    clean twin. Reversible via manifest. Never hard-deletes; never removes a
    file that lacks a clean identical twin."""
    if not confirm:
        return {"error": "confirm=true required"}
    plan = find_paren_dupes(local_root)
    victims = plan["victims"]
    if only_paths:
        wanted = set(only_paths)
        victims = [v for v in victims if v["path"] in wanted]

    # quarantine root at the drive root of local_root
    stamp = time.strftime("%Y%m%d_%H%M%S")
    q_root = os.path.join(local_root, f"_ParenDupeQuarantine_{stamp}")
    os.makedirs(_PURGE_MANIFEST_DIR, exist_ok=True)

    entries = []
    moved = 0
    moved_bytes = 0
    errors = []
    root_abs = os.path.abspath(local_root)
    for v in victims:
        src = v["path"]
        if not os.path.isfile(src):
            continue
        ap = os.path.abspath(src)
        if not ap.lower().startswith(root_abs.lower()):
            continue
        rel = os.path.relpath(ap, root_abs)
        dest = os.path.join(q_root, rel)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # collision-safe inside quarantine
            d = dest
            i = 1
            while os.path.exists(d):
                stem, ext = os.path.splitext(dest)
                d = f"{stem}__{i}{ext}"; i += 1
            os.rename(ap, d)
            entries.append({"from": ap, "to": d})
            moved += 1
            moved_bytes += v["size"]
        except OSError as e:
            errors.append(f"{src}: {e}")

    manifest = {"action": "paren_dupe_purge", "at": time.time(),
                "local_root": local_root, "quarantine_root": q_root,
                "entries": entries}
    mpath = os.path.join(_PURGE_MANIFEST_DIR, f"purge_{int(time.time()*1000)}.json")
    with open(mpath, "w", encoding="utf-8") as f:
        _json.dump(manifest, f, indent=2, ensure_ascii=False)

    return {"quarantined": moved, "reclaimed_bytes": moved_bytes,
            "errors": errors, "manifest_file": mpath, "quarantine_root": q_root}


def undo_purge(manifest_file: str, confirm: bool = False) -> dict:
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.exists(manifest_file):
        return {"error": f"manifest not found: {manifest_file}"}
    with open(manifest_file, "r", encoding="utf-8") as f:
        man = _json.load(f)
    restored = 0
    errors = []
    for e in man.get("entries", []):
        cur, orig = e["to"], e["from"]
        try:
            if not os.path.isfile(cur):
                errors.append(f"missing: {cur}"); continue
            if os.path.exists(orig):
                errors.append(f"original exists: {orig}"); continue
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            os.rename(cur, orig)
            restored += 1
        except OSError as ex:
            errors.append(f"{cur}: {ex}")
    return {"restored": restored, "errors": errors}


# ============================================================ folder cleanup

_ORGANIZED_ROOT = os.environ.get("ORGANIZED_ROOT", r"E:\Google Drive Files\Organized")
_FOLDER_MANIFEST_DIR = os.path.join(_DB_DIR, "folder_manifests")
_OUR_DIR_PREFIXES = ("_ParenDupeQuarantine", "_EmptyQuarantine", "_MergeQuarantine",
                     "_BackfillQuarantine", "_ReconcileQuarantine", "_PurgeQuarantine",
                     "_FolderQuarantine", "_DeletedSince_", "_PreReexport_")


def _norm(p: str) -> str:
    return p.replace("\\", "/").rstrip("/")


def _drive_folder_pathset() -> set:
    """Set of normalized 'DriveName/rel_path' for every FOLDER in the blueprint
    (lowercased) so we can test whether a local folder exists in Drive."""
    b = bp.open_blueprint()
    out = set()
    try:
        for r in b._conn.execute(
                "SELECT drive_name, rel_path FROM files WHERE is_folder=1"):
            dn, rel = r[0], (r[1] or "")
            out.add(_norm(f"{dn}/{rel}").lower())
    finally:
        b.close()
    return out


def _local_to_drive_key(local_dir: str) -> Optional[str]:
    """Map an absolute local folder under Organized/<DriveName>/<rel> to the
    normalized 'DriveName/rel' key used in the blueprint. None if not under
    Organized."""
    ap = _norm(os.path.abspath(local_dir))
    root = _norm(os.path.abspath(_ORGANIZED_ROOT))
    if not ap.lower().startswith(root.lower() + "/"):
        return None
    rel = ap[len(root) + 1:]           # DriveName/rest...
    return rel.lower()


def _count_files(path: str) -> int:
    n = 0
    for _r, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIR_PREFIXES)]
        n += len(files)
    return n


def folder_cleanup_plan(local_root: str) -> dict:
    """DRY RUN. Walk folders under local_root; classify each vs the Drive
    blueprint (shared drives):
      in_drive              : folder exists in Drive -> keep
      empty_not_in_drive    : not in Drive AND no files -> delete (quarantine)
      content_not_in_drive  : not in Drive BUT has files -> files should relocate
                              to their correct Drive folder (by md5), then delete
                              the emptied shell
    """
    drive_folders = _drive_folder_pathset()
    # ledger: path -> correct destination folder (from its Drive match)
    led = open_ledger()
    try:
        led._conn.row_factory = sqlite3.Row
        # map local file path -> (drive_name, correct_rel_path dir)
        pass
    finally:
        led._conn.row_factory = None
        led.close()

    in_drive, empty_not, content_not = [], [], []
    for dirpath, dirs, files in os.walk(local_root):
        dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIR_PREFIXES)]
        key = _local_to_drive_key(dirpath)
        if key is None:
            continue
        if key in drive_folders:
            in_drive.append(dirpath)
            continue
        # folder not in Drive
        fc = _count_files(dirpath)
        if fc == 0:
            # only flag leaf-empty (no non-quarantine subfolders either)
            real_subs = [d for d in dirs]
            if not real_subs:
                empty_not.append({"path": dirpath})
        else:
            content_not.append({"path": dirpath, "files": fc})

    return {
        "local_root": local_root,
        "counts": {
            "in_drive": len(in_drive),
            "empty_not_in_drive": len(empty_not),
            "content_not_in_drive": len(content_not),
        },
        "empty_not_in_drive": sorted(empty_not, key=lambda x: x["path"]),
        "content_not_in_drive": sorted(content_not, key=lambda x: -x["files"]),
    }


def _ledger_dest_for(led, path: str):
    """Return (drive_name, correct_rel_path) for a local file from the ledger,
    or (None, None) if not matched."""
    r = led._conn.execute(
        "SELECT drive_name, correct_rel_path FROM files "
        "WHERE path=? AND status='matched'", (path,)).fetchone()
    if r and r[1]:
        return r[0], r[1]
    return None, None


def _collision_safe_path(dst: str) -> str:
    if not os.path.exists(dst):
        return dst
    stem, ext = os.path.splitext(dst)
    i = 1
    while True:
        cand = f"{stem} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def apply_folder_cleanup(local_root: str, confirm: bool = False,
                         relocate: bool = True) -> dict:
    """Apply the folder cleanup plan. Reversible via manifest.

    empty_not_in_drive   -> quarantine the empty folder.
    content_not_in_drive -> if relocate: move each file to its correct Drive
                            location (Organized/<drive_name>/<correct_rel_path>),
                            collision-safe; then, if the shell is empty, quarantine
                            it. A file with no ledger match is LEFT in place (its
                            folder is then NOT deleted, for safety).
    """
    if not confirm:
        return {"error": "confirm=true required"}
    plan = folder_cleanup_plan(local_root)
    os.makedirs(_FOLDER_MANIFEST_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    q_root = os.path.join(local_root, f"_FolderQuarantine_{stamp}")

    led = open_ledger()
    moved = []          # {from, to}
    quarantined = []    # {from, to}
    errors = []
    unmatched_left = 0
    try:
        # 1) relocate content from not-in-drive folders that have files
        if relocate:
            for item in plan["content_not_in_drive"]:
                folder = item["path"]
                for r, dirs, files in os.walk(folder):
                    dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIR_PREFIXES)]
                    for fn in files:
                        src = os.path.join(r, fn)
                        dn, rel = _ledger_dest_for(led, src)
                        if not dn or not rel:
                            unmatched_left += 1
                            continue
                        # correct absolute local destination from Drive path
                        dest = os.path.join(_ORGANIZED_ROOT,
                                            *([dn] + rel.replace("\\", "/").split("/")))
                        if _norm(os.path.abspath(dest)) == _norm(os.path.abspath(src)):
                            continue
                        try:
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            dest = _collision_safe_path(dest)
                            os.rename(src, dest)
                            moved.append({"from": src, "to": dest})
                        except OSError as e:
                            errors.append(f"move {src}: {e}")

        # 2) quarantine empties (plan's empties + shells now emptied by relocation)
        to_quar = [i["path"] for i in plan["empty_not_in_drive"]]
        for item in plan["content_not_in_drive"]:
            if _count_files(item["path"]) == 0:
                to_quar.append(item["path"])

        for folder in to_quar:
            if not os.path.isdir(folder):
                continue
            if _count_files(folder) > 0:
                continue   # safety: never quarantine a folder that still has files
            rel = os.path.relpath(os.path.abspath(folder), os.path.abspath(local_root))
            dest = os.path.join(q_root, rel)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.rename(folder, dest)
                quarantined.append({"from": os.path.abspath(folder), "to": dest})
            except OSError as e:
                errors.append(f"quarantine {folder}: {e}")
    finally:
        led.close()

    manifest = {"action": "folder_cleanup", "at": time.time(),
                "local_root": local_root, "quarantine_root": q_root,
                "moved": moved, "quarantined": quarantined}
    mpath = os.path.join(_FOLDER_MANIFEST_DIR, f"folder_{int(time.time()*1000)}.json")
    with open(mpath, "w", encoding="utf-8") as f:
        _json.dump(manifest, f, indent=2, ensure_ascii=False)

    return {"files_relocated": len(moved), "folders_quarantined": len(quarantined),
            "unmatched_left_in_place": unmatched_left, "errors": errors,
            "manifest_file": mpath}


def undo_folder_cleanup(manifest_file: str, confirm: bool = False) -> dict:
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.exists(manifest_file):
        return {"error": f"manifest not found: {manifest_file}"}
    with open(manifest_file, "r", encoding="utf-8") as f:
        man = _json.load(f)
    restored_folders = 0
    restored_files = 0
    errors = []
    # restore quarantined folders first
    for e in man.get("quarantined", []):
        cur, orig = e["to"], e["from"]
        try:
            if os.path.exists(orig):
                errors.append(f"folder exists: {orig}"); continue
            if not os.path.isdir(cur):
                errors.append(f"missing quarantined: {cur}"); continue
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            os.rename(cur, orig)
            restored_folders += 1
        except OSError as ex:
            errors.append(f"{cur}: {ex}")
    # move relocated files back
    for e in man.get("moved", []):
        cur, orig = e["to"], e["from"]
        try:
            if not os.path.isfile(cur):
                errors.append(f"missing moved: {cur}"); continue
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            if os.path.exists(orig):
                errors.append(f"orig exists: {orig}"); continue
            os.rename(cur, orig)
            restored_files += 1
        except OSError as ex:
            errors.append(f"{cur}: {ex}")
    return {"restored_folders": restored_folders, "restored_files": restored_files,
            "errors": errors}


# ============================================================ MD5-driven folder analysis

import os as _os
from collections import Counter as _Counter


def _immediate_files(folder: str) -> list[str]:
    out = []
    try:
        for e in _os.scandir(folder):
            if e.is_file():
                out.append(e.path)
    except OSError:
        pass
    return out


def _immediate_subdirs(folder: str) -> list[str]:
    out = []
    try:
        for e in _os.scandir(folder):
            if e.is_dir() and not e.name.startswith(_OUR_DIR_PREFIXES):
                out.append(e.path)
    except OSError:
        pass
    return out


def _analyze_top_level(dirpath, led, real):
    """Classify a TOP-LEVEL webinar folder by its RECURSIVE files' Drive matches.
    Canonical name = the Drive rel_path segment right AFTER the anchor folder
    (the local_root's final component, e.g. 'HPLive Webinar-'). Appends to `real`
    with name_mismatch set when the local name differs from the canonical."""
    anchor = _os.path.basename(_os.path.abspath(_analyze_top_level.local_root).rstrip("\\/")).lower()
    local_name = _os.path.basename(dirpath.rstrip("\\/"))
    tnorm = _norm(dirpath).lower()
    rows = led._conn.execute(
        "SELECT correct_rel_path FROM files "
        "WHERE status='matched' AND correct_rel_path IS NOT NULL "
        "AND REPLACE(LOWER(path),'\\','/') LIKE ?", (tnorm + "/%",)).fetchall()
    votes = _Counter()
    for (rel,) in rows:
        segs = _norm(rel).split("/")
        low = [s.lower() for s in segs]
        if anchor in low:
            i = low.index(anchor)
            if i + 1 < len(segs):
                votes[segs[i + 1]] += 1
    if not votes:
        return
    canonical = votes.most_common(1)[0][0]
    real.append({
        "path": dirpath,
        "local_name": local_name,
        "top_level": True,
        "canonical_name": canonical,
        "matched_files": sum(votes.values()),
        "immediate_files": 0,
        "name_mismatch": local_name != canonical,
    })


def folder_analysis(local_root: str) -> dict:
    """
    MD5-DRIVEN folder classification (name-agnostic). For each folder under
    local_root, decide its TRUE identity from where its files' Drive md5 matches
    say they belong — so a truncated local name is NOT mistaken for 'not in
    Drive'.

    Per folder (judged on its IMMEDIATE files, not recursive):
      real_folder : its files map (by md5) to ONE dominant Drive folder ->
                    this local folder IS that Drive folder. canonical = the Drive
                    folder's name/path. If local basename != canonical name, it's
                    a rename candidate.
      redundant   : it has files, none map to Drive as a home here, AND every
                    file's md5 also exists somewhere ELSE locally (outside this
                    folder subtree) -> content duplicated elsewhere, removable.
      empty       : no files anywhere beneath.
      unknown     : has files with no Drive match and not duplicated elsewhere ->
                    leave alone (could be local-only content).
    """
    # Build local md5 -> [paths] once (from the local index) for redundancy tests.
    idx = mi.open_index(local_root)
    try:
        md5_paths: dict = {}
        for path, md5 in idx._conn.execute("SELECT path, md5 FROM files WHERE md5<>''"):
            md5_paths.setdefault(md5, []).append(path)
    finally:
        idx.close()

    # ledger: path -> (drive_name, correct_rel_path)
    led = open_ledger()
    led._conn.row_factory = None
    def ledger_dest(path):
        r = led._conn.execute(
            "SELECT drive_name, correct_rel_path FROM files "
            "WHERE path=? AND status='matched'", (path,)).fetchone()
        return (r[0], r[1]) if (r and r[1]) else (None, None)

    root_abs = _norm(_os.path.abspath(local_root)).lower()
    _analyze_top_level.local_root = local_root   # anchor for canonical-name derivation

    def _is_top_level(dp: str) -> bool:
        # direct child of local_root (the webinar-level folder)
        return _norm(_os.path.abspath(_os.path.dirname(dp))).lower() == root_abs

    real, redundant, empty, unknown = [], [], [], []
    try:
        for dirpath, dirs, files in _os.walk(local_root):
            dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIR_PREFIXES)]
            imm_files = [_os.path.join(dirpath, f) for f in files]
            recursive_count = _count_files(dirpath)

            if recursive_count == 0:
                # truly empty (no files anywhere below)
                if not _immediate_subdirs(dirpath):
                    empty.append({"path": dirpath})
                continue

            # TOP-LEVEL webinar folders usually have NO immediate files (content
            # sits in Banner/, Videos/, ...). Judge them by their RECURSIVE files
            # and derive the canonical webinar name from the Drive rel_path segment
            # that follows the local_root's final component.
            if _is_top_level(dirpath):
                _analyze_top_level(dirpath, led, real)
                continue

            if not imm_files:
                continue  # non-top container dir with only subfolders

            # Where do THIS folder's immediate files map on Drive?
            dest_dirs = _Counter()
            matched = 0
            dup_elsewhere = 0
            no_match = 0
            for fp in imm_files:
                dn, rel = ledger_dest(fp)
                if dn and rel:
                    matched += 1
                    drive_dir = dn + "/" + _norm(_os.path.dirname(rel))
                    dest_dirs[drive_dir.rstrip("/")] += 1
                else:
                    # not matched to Drive -> is it duplicated elsewhere locally?
                    md5 = None
                    # find md5 via index paths map (reverse lookup is costly; use ledger row)
                    r = led._conn.execute("SELECT md5 FROM files WHERE path=?", (fp,)).fetchone()
                    md5 = r[0] if r else None
                    others = [p for p in md5_paths.get(md5, [])
                              if _norm(p) != _norm(fp)
                              and not _norm(p).lower().startswith(_norm(dirpath).lower() + "/")]
                    if md5 and others:
                        dup_elsewhere += 1
                    else:
                        no_match += 1

            if matched > 0:
                top = _is_top_level(dirpath)
                local_name = _os.path.basename(dirpath.rstrip("\\/"))
                canonical_name = None
                if top:
                    # For a TOP-LEVEL webinar folder, its canonical name is the
                    # WEBINAR-level Drive folder — i.e. the path component right
                    # after 'HPLive Webinar-' (or the local_root's Drive rel).
                    # Derive it from the dominant Drive dir but take the segment
                    # that sits at the same depth as this folder (top level).
                    root_key = _local_to_drive_key(local_root)   # e.g. "marketing dropbox/hpl/hplive webinar-"
                    depth = len((root_key or "").split("/"))     # segments before the webinar folder
                    best = None
                    votes = _Counter()
                    for ddir, cnt in dest_dirs.items():
                        segs = ddir.split("/")
                        if len(segs) > depth:
                            votes[segs[depth]] += cnt   # the top-level webinar segment
                    if votes:
                        canonical_name = votes.most_common(1)[0][0]
                if canonical_name is None:
                    # non-top-level: don't propose a rename (generic containers are
                    # unreliable) — record dominant dir but mark no mismatch.
                    drive_dir, _cnt = dest_dirs.most_common(1)[0]
                    canonical_name = local_name  # => name_mismatch False
                real.append({
                    "path": dirpath,
                    "local_name": local_name,
                    "top_level": top,
                    "canonical_name": canonical_name,
                    "matched_files": matched,
                    "immediate_files": len(imm_files),
                    "name_mismatch": top and (local_name != canonical_name)
                                     and bool(canonical_name),
                })
            elif dup_elsewhere > 0 and no_match == 0:
                redundant.append({"path": dirpath, "files": len(imm_files),
                                  "reason": "all content duplicated elsewhere"})
            else:
                unknown.append({"path": dirpath, "files": len(imm_files),
                                "no_drive_match": no_match, "dup_elsewhere": dup_elsewhere})
    finally:
        led.close()

    return {
        "local_root": local_root,
        "counts": {
            "real_folder": len(real),
            "rename_candidates": sum(1 for r in real if r["name_mismatch"]),
            "redundant": len(redundant),
            "empty": len(empty),
            "unknown": len(unknown),
        },
        "rename_candidates": [r for r in real if r["name_mismatch"]],
        "redundant": sorted(redundant, key=lambda x: -x["files"]),
        "empty": empty,
        "unknown": unknown,
    }


def apply_folder_fix(local_root: str, do_rename: bool = True,
                     do_redundant: bool = True, do_empty: bool = True,
                     confirm: bool = False) -> dict:
    """Apply MD5-driven folder fixes. Folder-level only (no cross-folder file
    moves). Reversible via manifest.
      do_rename    : rename real folders whose local name != canonical Drive name
      do_redundant : quarantine folders whose content is fully duplicated elsewhere
      do_empty     : quarantine empty folders
    NEVER touches 'unknown' folders (files with no Drive match, not duplicated)."""
    if not confirm:
        return {"error": "confirm=true required"}
    analysis = folder_analysis(local_root)
    os.makedirs(_FOLDER_MANIFEST_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    q_root = os.path.join(local_root, f"_FolderQuarantine_{stamp}")

    renamed = []      # {from, to}
    quarantined = []  # {from, to}
    errors = []

    # 1) rename real folders to canonical Drive name (deepest first so parent
    #    renames don't invalidate child paths)
    if do_rename:
        cands = sorted(analysis["rename_candidates"],
                       key=lambda r: r["path"].count(os.sep), reverse=True)
        for r in cands:
            src = r["path"]
            if not os.path.isdir(src):
                continue
            safe = "".join(("_" if c in _BAD_CHARS else c) for c in r["canonical_name"]).strip()
            if not safe or safe in (".", ".."):
                continue
            dst = os.path.join(os.path.dirname(src), safe)
            if _norm(os.path.abspath(src)).lower() == _norm(os.path.abspath(dst)).lower():
                continue
            if os.path.exists(dst):
                errors.append(f"rename target exists, skipped: {dst}")
                continue
            try:
                os.rename(src, dst)
                renamed.append({"from": src, "to": dst})
            except OSError as e:
                errors.append(f"rename {src}: {e}")

    # 2) quarantine redundant folders (content duplicated elsewhere)
    if do_redundant:
        for r in analysis["redundant"]:
            folder = r["path"]
            if not os.path.isdir(folder):
                continue
            rel = os.path.relpath(os.path.abspath(folder), os.path.abspath(local_root))
            dest = os.path.join(q_root, rel)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.rename(folder, dest)
                quarantined.append({"from": os.path.abspath(folder), "to": dest})
            except OSError as e:
                errors.append(f"quarantine redundant {folder}: {e}")

    # 3) quarantine empties
    if do_empty:
        for r in analysis["empty"]:
            folder = r["path"]
            if not os.path.isdir(folder) or _count_files(folder) > 0:
                continue
            rel = os.path.relpath(os.path.abspath(folder), os.path.abspath(local_root))
            dest = os.path.join(q_root, rel)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.rename(folder, dest)
                quarantined.append({"from": os.path.abspath(folder), "to": dest})
            except OSError as e:
                errors.append(f"quarantine empty {folder}: {e}")

    manifest = {"action": "folder_fix", "at": time.time(),
                "local_root": local_root, "quarantine_root": q_root,
                "renamed": renamed, "quarantined": quarantined}
    mpath = os.path.join(_FOLDER_MANIFEST_DIR, f"folderfix_{int(time.time()*1000)}.json")
    with open(mpath, "w", encoding="utf-8") as f:
        _json.dump(manifest, f, indent=2, ensure_ascii=False)
    return {"folders_renamed": len(renamed), "folders_quarantined": len(quarantined),
            "errors": errors, "manifest_file": mpath}


def undo_folder_fix(manifest_file: str, confirm: bool = False) -> dict:
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.exists(manifest_file):
        return {"error": f"manifest not found: {manifest_file}"}
    with open(manifest_file, "r", encoding="utf-8") as f:
        man = _json.load(f)
    restored = 0
    errors = []
    # un-quarantine (shallowest first), then un-rename (shallowest first)
    for e in sorted(man.get("quarantined", []), key=lambda x: x["from"].count(os.sep)):
        cur, orig = e["to"], e["from"]
        try:
            if os.path.exists(orig):
                errors.append(f"exists: {orig}"); continue
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            os.rename(cur, orig)
            restored += 1
        except OSError as ex:
            errors.append(f"{cur}: {ex}")
    for e in sorted(man.get("renamed", []), key=lambda x: x["from"].count(os.sep)):
        cur, orig = e["to"], e["from"]
        try:
            if os.path.exists(orig):
                errors.append(f"exists: {orig}"); continue
            os.rename(cur, orig)
            restored += 1
        except OSError as ex:
            errors.append(f"{cur}: {ex}")
    return {"restored": restored, "errors": errors}


# ============================================================ merge-into-twin

_MERGE_MANIFEST_DIR = os.path.join(_DB_DIR, "merge_manifests")


def merge_plan(local_root: str) -> dict:
    """DRY RUN. Find split webinar pairs: a truncated top-level folder whose
    canonical Drive name ALREADY EXISTS as a sibling folder. Those should be
    MERGED (source truncated -> target canonical), not renamed."""
    analysis = folder_analysis(local_root)
    pairs = []
    for r in analysis["rename_candidates"]:
        src = r["path"]
        canonical = r["canonical_name"]
        safe = "".join(("_" if c in _BAD_CHARS else c) for c in canonical).strip()
        target = os.path.join(os.path.dirname(src), safe)
        if os.path.isdir(target) and _norm(os.path.abspath(target)).lower() != _norm(os.path.abspath(src)).lower():
            pairs.append({
                "source": src,
                "source_name": r["local_name"],
                "target": target,
                "target_name": safe,
                "source_files": _count_files(src),
                "target_files": _count_files(target),
            })
    return {"local_root": local_root, "count": len(pairs), "pairs": pairs}


def _md5_of_file(path: str) -> Optional[str]:
    import hashlib
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _target_md5set(target: str, idx) -> set:
    """md5s already present under target (from index; fall back to hashing)."""
    s = set()
    tnorm = _norm(target).lower()
    for path, md5 in idx._conn.execute(
            "SELECT path, md5 FROM files WHERE md5<>'' AND REPLACE(LOWER(path),'\\','/') LIKE ?",
            (tnorm + "/%",)):
        s.add(md5)
    return s


def apply_merge(local_root: str, confirm: bool = False) -> dict:
    """Merge each split pair: move source files into target, preserving relative
    subpaths, collision-safe, SKIPPING byte-identical duplicates (same md5).
    Then quarantine the emptied source shell. Reversible."""
    if not confirm:
        return {"error": "confirm=true required"}
    plan = merge_plan(local_root)
    os.makedirs(_MERGE_MANIFEST_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    q_root = os.path.join(local_root, f"_MergeQuarantine_{stamp}")

    # Write the manifest path UP FRONT and flush after every move so a long or
    # interrupted merge stays fully reversible (previous version only wrote at
    # the end, losing the undo record on timeout).
    mpath = os.path.join(_MERGE_MANIFEST_DIR, f"merge_{int(time.time()*1000)}.json")
    manifest = {"action": "folder_merge", "at": time.time(),
                "local_root": local_root, "quarantine_root": q_root,
                "moved": [], "quarantined": []}

    def _flush_manifest():
        try:
            tmp = mpath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(manifest, f, ensure_ascii=False)
            os.replace(tmp, mpath)
        except OSError:
            pass

    idx = mi.open_index(local_root)
    moved = manifest["moved"]
    skipped_identical = 0
    quarantined = manifest["quarantined"]
    errors = []
    _flush_manifest()
    _since_flush = 0
    try:
        for pair in plan["pairs"]:
            src, target = pair["source"], pair["target"]
            if not os.path.isdir(src) or not os.path.isdir(target):
                continue
            target_md5s = _target_md5set(target, idx)
            for r, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIR_PREFIXES)]
                rel = os.path.relpath(r, src)
                dest_dir = target if rel == "." else os.path.join(target, rel)
                for fn in files:
                    sp = os.path.join(r, fn)
                    md5 = _md5_of_file(sp)
                    # skip if a byte-identical copy already exists in target
                    if md5 and md5 in target_md5s:
                        skipped_identical += 1
                        continue
                    try:
                        os.makedirs(dest_dir, exist_ok=True)
                        dp = os.path.join(dest_dir, fn)
                        dp = _collision_safe_path(dp)
                        os.rename(sp, dp)
                        moved.append({"from": sp, "to": dp})
                        if md5:
                            target_md5s.add(md5)
                        _since_flush += 1
                        if _since_flush >= 20:
                            _flush_manifest(); _since_flush = 0
                    except OSError as e:
                        errors.append(f"move {sp}: {e}")
            # quarantine emptied source shell (only if no files remain)
            if _count_files(src) == 0:
                rel = os.path.relpath(os.path.abspath(src), os.path.abspath(local_root))
                dest = os.path.join(q_root, rel)
                try:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    os.rename(src, dest)
                    quarantined.append({"from": os.path.abspath(src), "to": dest})
                    _flush_manifest()
                except OSError as e:
                    errors.append(f"quarantine {src}: {e}")
            else:
                errors.append(f"source not empty after merge, left in place: {src}")
    finally:
        idx.close()
        _flush_manifest()

    return {"pairs_merged": len(plan["pairs"]), "files_moved": len(moved),
            "skipped_identical": skipped_identical,
            "shells_quarantined": len(quarantined), "errors": errors,
            "manifest_file": mpath}


def undo_merge(manifest_file: str, confirm: bool = False) -> dict:
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.exists(manifest_file):
        return {"error": f"manifest not found: {manifest_file}"}
    with open(manifest_file, "r", encoding="utf-8") as f:
        man = _json.load(f)
    restored = 0
    errors = []
    # restore shells first, then move files back
    for e in man.get("quarantined", []):
        cur, orig = e["to"], e["from"]
        try:
            if os.path.exists(orig):
                errors.append(f"exists: {orig}"); continue
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            os.rename(cur, orig); restored += 1
        except OSError as ex:
            errors.append(f"{cur}: {ex}")
    for e in man.get("moved", []):
        cur, orig = e["to"], e["from"]
        try:
            if not os.path.isfile(cur):
                errors.append(f"missing: {cur}"); continue
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            if os.path.exists(orig):
                errors.append(f"orig exists: {orig}"); continue
            os.rename(cur, orig); restored += 1
        except OSError as ex:
            errors.append(f"{cur}: {ex}")
    return {"restored": restored, "errors": errors}


def validate_against_drive(local_folder: str) -> dict:
    """Compare a local folder's content (by md5) to what Google Drive says that
    folder should contain (blueprint). Reports matched / missing_locally /
    extra_locally so you can see if the merged folder equals Drive."""
    # local md5 set under this folder
    root_for_idx = _ORGANIZED_ROOT
    idx = mi.open_index(root_for_idx)
    local = {}
    try:
        tnorm = _norm(local_folder).lower()
        for path, md5 in idx._conn.execute(
                "SELECT path, md5 FROM files WHERE md5<>'' AND REPLACE(LOWER(path),'\\','/') LIKE ?",
                (tnorm + "/%",)):
            local[md5] = os.path.basename(path)
    finally:
        idx.close()

    # figure the Drive counterpart: canonical name under the anchor
    key = _local_to_drive_key(local_folder)  # e.g. "marketing dropbox/hpl/hplive webinar-/<name>"
    if not key:
        return {"error": "folder not under Organized"}
    parts = key.split("/")
    drive_name = parts[0]                    # shared drive top == Organized/<DriveName>
    rel_prefix = "/".join(parts[1:])         # path within the drive

    b = bp.open_blueprint()
    drive = {}
    try:
        for md5, name, rel in b._conn.execute(
                "SELECT md5, name, rel_path FROM files "
                "WHERE is_folder=0 AND md5<>'' AND LOWER(drive_name)=? "
                "AND LOWER(rel_path) LIKE ?",
                (drive_name, rel_prefix + "/%")):
            drive[md5] = name
    finally:
        b.close()

    local_set, drive_set = set(local), set(drive)
    matched = local_set & drive_set
    missing = drive_set - local_set     # in Drive, not local
    extra = local_set - drive_set       # local, not in Drive
    return {
        "local_folder": local_folder,
        "drive_path": f"{drive_name}/{rel_prefix}",
        "local_files": len(local_set),
        "drive_files": len(drive_set),
        "matched": len(matched),
        "missing_locally": len(missing),
        "extra_locally": len(extra),
        "missing_examples": [drive[m] for m in list(missing)[:10]],
        "extra_examples": [local[m] for m in list(extra)[:10]],
        "verdict": ("MATCHES DRIVE" if not missing and not extra
                    else "differs"),
    }


def ledger_stats() -> dict:
    led = open_ledger()
    try:
        return led.stats()
    finally:
        led.close()


def ledger_list(status: str, limit: int = 500) -> list[dict]:
    led = open_ledger()
    try:
        return led.list_by_status(status, limit)
    finally:
        led.close()


# ============================================================ -pinned artifact cleanup
# Google Takeout flattened Drive version metadata into filenames as
# "<name>-at-<ISO-timestamp>-pinned.<ext>". These are almost always byte-identical
# to a clean-named twin already in Organized. This cleanup:
#   - QUARANTINES a -pinned file only if an identical-MD5 NON-pinned twin exists
#     in Organized (so nothing is lost). Reversible.
#   - RENAMES the rare -pinned file that is the sole copy of its content, stripping
#     the "-at-<ts>-pinned" suffix back to the clean name. Reversible.
# Read-only preview first; apply requires confirm.

import re as _re

_PINNED_MANIFEST_DIR = os.path.join(_DB_DIR, "pinned_manifests")
_PINNED_QUARANTINE_PREFIX = "_PinnedQuarantine"

# "-at-<ISO timestamp>-pinned" right before the extension, or a bare "-pinned".
_AT_PINNED_RE = _re.compile(r"-at-\d{4}-\d\d-\d\dt[\d_.]+z-pinned$", _re.I)
_BARE_PINNED_RE = _re.compile(r"-pinned$", _re.I)
_IS_PINNED_RE = _re.compile(r"-pinned\.[^.]+$", _re.I)


def _is_pinned_name(path: str) -> bool:
    return bool(_IS_PINNED_RE.search(os.path.basename(path)))


def _pinned_clean_name(path: str) -> str:
    """The clean filename with the -at-<ts>-pinned (or bare -pinned) suffix removed."""
    name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    stem = _AT_PINNED_RE.sub("", stem)
    stem = _BARE_PINNED_RE.sub("", stem)
    return (stem + ext) if stem else name


def _scan_pinned() -> dict:
    """Build md5->paths from the Organized index, then classify every -pinned file
    as 'redundant' (has a non-pinned identical twin) or 'unique' (no twin)."""
    idx = mi.open_index(_ORGANIZED_ROOT)
    by_md5: dict = {}
    try:
        for path, md5 in idx._conn.execute("SELECT path, md5 FROM files WHERE md5<>''"):
            by_md5.setdefault(md5, []).append(path)
    finally:
        idx.close()

    redundant = []   # {path, keeper, md5}  -> quarantine (twin exists)
    unique = []      # {path, clean_name, md5} -> rename (sole copy)
    for md5, paths in by_md5.items():
        pinned = [p for p in paths if _is_pinned_name(p)]
        if not pinned:
            continue
        non_pinned = [p for p in paths if not _is_pinned_name(p)]
        if non_pinned:
            # keeper: cleanest/shortest non-pinned copy
            keeper = sorted(non_pinned, key=lambda p: (len(p), len(os.path.basename(p))))[0]
            for pp in pinned:
                redundant.append({"path": pp, "keeper": keeper, "md5": md5})
        else:
            # no clean twin; if multiple pinned copies, keep one and the rest are
            # redundant against it; the survivor gets renamed.
            survivor = sorted(pinned, key=lambda p: (len(p), len(os.path.basename(p))))[0]
            for pp in pinned:
                if pp == survivor:
                    unique.append({"path": pp, "clean_name": _pinned_clean_name(pp),
                                   "md5": md5})
                else:
                    redundant.append({"path": pp, "keeper": survivor, "md5": md5})
    return {"redundant": redundant, "unique": unique}


def pinned_preview(examples: int = 12) -> dict:
    """DRY RUN — how many -pinned artifacts are redundant (quarantine) vs unique
    (rename), with examples. No changes."""
    data = _scan_pinned()
    return {
        "redundant_count": len(data["redundant"]),
        "unique_count": len(data["unique"]),
        "redundant_examples": [
            {"drop": r["path"], "keeper": r["keeper"]}
            for r in data["redundant"][:examples]
        ],
        "unique_examples": [
            {"path": u["path"], "rename_to": u["clean_name"]}
            for u in data["unique"][:examples]
        ],
    }


_pinned_jobs: dict = {}


def pinned_quarantine(confirm: bool = False) -> str:
    """Start a BACKGROUND job that quarantines every -pinned artifact with a
    byte-identical NON-pinned twin. Returns a job_id. Cancellable + resumable-safe;
    reversible via manifest. (Was synchronous before — now backgrounded so it can
    be cancelled cleanly.)"""
    if not confirm:
        raise ValueError("confirm=true required")
    job_id = f"pinnedq_{int(time.time())}"
    _pinned_jobs[job_id] = {"status": "running", "phase": "scanning",
                            "quarantined": 0, "skipped": 0, "errors": 0,
                            "manifest_file": None, "cancel": False,
                            "started_at": time.time()}
    import threading
    threading.Thread(target=_pinned_quarantine_worker, args=(job_id,),
                     daemon=True).start()
    return job_id


def get_pinned_job(job_id: str):
    return _pinned_jobs.get(job_id)


def cancel_pinned_job(job_id: str) -> bool:
    job = _pinned_jobs.get(job_id)
    if not job:
        return False
    job["cancel"] = True
    return True


def _pinned_quarantine_worker(job_id: str):
    job = _pinned_jobs[job_id]
    try:
        data = _scan_pinned()
        job["phase"] = "quarantining"
        job["total"] = len(data["redundant"])
        os.makedirs(_PINNED_MANIFEST_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        q_root = os.path.join(_ORGANIZED_ROOT, f"{_PINNED_QUARANTINE_PREFIX}_{stamp}")
        mpath = os.path.join(_PINNED_MANIFEST_DIR,
                             f"pinned_quarantine_{int(time.time()*1000)}.json")
        manifest = {"action": "pinned_quarantine", "at": time.time(),
                    "quarantine_root": q_root, "entries": []}
        job["manifest_file"] = mpath

        def flush():
            try:
                tmp = mpath + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    _json.dump(manifest, f, ensure_ascii=False)
                os.replace(tmp, mpath)
            except OSError:
                pass

        since = 0
        cancelled = False
        flush()
        for r in data["redundant"]:
            if job.get("cancel"):
                cancelled = True
                break
            src, keeper = r["path"], r["keeper"]
            # safety: keeper must exist so we never remove the last copy
            if not os.path.isfile(keeper):
                job["skipped"] += 1
                continue
            if not os.path.isfile(src):
                job["skipped"] += 1
                continue
            # safety: re-verify content still matches the recorded md5
            cur_md5 = _md5_of_file(src)
            if cur_md5 != r["md5"]:
                job["skipped"] += 1
                continue
            try:
                rel = os.path.relpath(os.path.abspath(src), os.path.abspath(_ORGANIZED_ROOT))
                dest = _collision_safe_path(os.path.join(q_root, rel))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.rename(src, dest)
                manifest["entries"].append({"from": os.path.abspath(src), "to": dest})
                job["quarantined"] += 1
                since += 1
                if since >= 50:
                    flush(); since = 0
            except OSError as e:
                job["errors"] += 1
        flush()
        job["status"] = "cancelled" if cancelled else "completed"
        job["phase"] = "cancelled" if cancelled else "complete"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)


def pinned_rename(confirm: bool = False) -> dict:
    """Rename the unique (no-twin) -pinned files, stripping the -at-<ts>-pinned
    suffix back to the clean name. In-place, collision-safe, reversible."""
    if not confirm:
        return {"error": "confirm=true required"}
    data = _scan_pinned()
    os.makedirs(_PINNED_MANIFEST_DIR, exist_ok=True)
    mpath = os.path.join(_PINNED_MANIFEST_DIR, f"pinned_rename_{int(time.time()*1000)}.json")
    manifest = {"action": "pinned_rename", "at": time.time(), "entries": []}
    renamed = 0
    skipped = 0
    errors = []
    for u in data["unique"]:
        src = u["path"]
        if not os.path.isfile(src):
            skipped += 1
            continue
        clean = u["clean_name"]
        if not clean or clean == os.path.basename(src):
            skipped += 1
            continue
        dst = _collision_safe(os.path.join(os.path.dirname(src), clean))
        try:
            os.rename(src, dst)
            manifest["entries"].append({"from": src, "to": dst})
            renamed += 1
        except OSError as e:
            errors.append(f"{src}: {e}")
    with open(mpath, "w", encoding="utf-8") as f:
        _json.dump(manifest, f, ensure_ascii=False, indent=2)
    return {"renamed": renamed, "skipped": skipped, "errors": errors,
            "manifest_file": mpath}


def pinned_undo(manifest_file: str, confirm: bool = False) -> dict:
    """Reverse a pinned_quarantine or pinned_rename batch from its manifest."""
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.exists(manifest_file):
        return {"error": f"manifest not found: {manifest_file}"}
    with open(manifest_file, "r", encoding="utf-8") as f:
        man = _json.load(f)
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
    return {"restored": restored, "errors": errors}
