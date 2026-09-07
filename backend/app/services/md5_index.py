"""
FULL-MD5 INDEX for large files (the videos).

The dedup scanner stores a full SHA-256 for files < 50MB but only a SAMPLED
fingerprint for larger files (fast, but not a whole-file hash and not comparable
to Google Drive's md5Checksum). This module fills that exact gap: it computes a
REAL full-file MD5 for every file >= a size threshold and stores it in a small,
indexed SQLite DB so we can answer, instantly and exactly:

    "where on disk is the file whose content MD5 == X?"

That is the foundation for the reconstruct-to-Drive folder reconciler: Drive
gives us each file's md5Checksum + authoritative name + path; we match local
files to Drive by exact content (MD5), then correct their names/paths from Drive
and download only the true gaps.

Design notes:
  - Separate DB from scan_store so the dedup schema is untouched.
  - RESUMABLE: a file whose (path,size,mtime) already matches an indexed row is
    skipped without re-reading — so an interrupted run continues cheaply.
  - Read-once MD5 in 4MB chunks; disk-read-bound (~200 MB/s on this machine).
  - Files < threshold are intentionally NOT indexed here (they already have a
    full SHA-256 in the scan DB).
"""
import os
import sqlite3
import threading
import time
import hashlib
from typing import Optional

_STORE_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "md5_index")
DEFAULT_MIN_SIZE = 50 * 1024 * 1024   # 50 MB — matches scanner's LARGE_FILE_THRESHOLD
_CHUNK = 4 * 1024 * 1024
_INSERT_FLUSH = 200

_build_jobs: dict = {}


def _safe_label(root: str) -> str:
    base = os.path.abspath(root).replace("\\", "_").replace("/", "_").replace(":", "")
    return base.strip("_") or "root"


class Md5Index:
    """SQLite-backed md5 -> path index for large files."""

    def __init__(self, label: str):
        os.makedirs(_STORE_DIR, exist_ok=True)
        self.label = label
        self.db_path = os.path.join(_STORE_DIR, f"{label}.db")
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path  TEXT PRIMARY KEY,
                md5   TEXT NOT NULL,
                size  INTEGER NOT NULL,
                mtime REAL
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_md5 ON files(md5)")
        self._conn.commit()
        self._buf: list[tuple] = []

    def get(self, path: str) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT path, md5, size, mtime FROM files WHERE path = ?", (path,))
        r = cur.fetchone()
        if not r:
            return None
        return {"path": r[0], "md5": r[1], "size": r[2], "mtime": r[3]}

    def is_current(self, path: str, size: int, mtime: float) -> bool:
        row = self.get(path)
        return bool(row and row["size"] == size
                    and row["mtime"] is not None
                    and abs(row["mtime"] - mtime) < 1.0)

    def add(self, path: str, md5: str, size: int, mtime: float):
        self._buf.append((path, md5, size, mtime))
        if len(self._buf) >= _INSERT_FLUSH:
            self.flush()

    def flush(self):
        if not self._buf:
            return
        self._conn.executemany(
            "INSERT OR REPLACE INTO files (path, md5, size, mtime) VALUES (?,?,?,?)",
            self._buf)
        self._conn.commit()
        self._buf.clear()

    def find_by_md5(self, md5: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT path, md5, size, mtime FROM files WHERE md5 = ?", (md5.lower(),))
        return [{"path": r[0], "md5": r[1], "size": r[2], "mtime": r[3]}
                for r in cur.fetchall()]

    def stats(self) -> dict:
        self.flush()
        cur = self._conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files")
        n, total = cur.fetchone()
        return {"label": self.label, "db_path": self.db_path,
                "indexed_files": n, "indexed_bytes": total}

    def close(self):
        try:
            self.flush()
            self._conn.close()
        except sqlite3.Error:
            pass


def _long_path(path: str) -> str:
    r"""On Windows, prefix with \\?\ to survive paths > 260 chars (very common in
    the mangled Takeout tree). Harmless no-op elsewhere / for already-prefixed."""
    if os.name == "nt":
        try:
            ap = os.path.abspath(path)
            if not ap.startswith("\\\\?\\"):
                if ap.startswith("\\\\"):      # UNC path
                    return "\\\\?\\UNC\\" + ap[2:]
                return "\\\\?\\" + ap
            return ap
        except Exception:
            return path
    return path


def _compute_md5(path: str) -> Optional[str]:
    """Full-file MD5. Never raises — returns None on ANY failure (locked file,
    permission denied, path too long, disk error, out of memory, etc.) so a
    single bad file can never abort the whole run."""
    h = hashlib.md5()
    try:
        with open(_long_path(path), "rb") as f:
            while True:
                try:
                    chunk = f.read(_CHUNK)
                except (OSError, MemoryError):
                    return None
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError, MemoryError, ValueError):
        return None
    except Exception:
        return None


# ---- open-index helper (for the reconciler to query later) ----

def open_index(root: str) -> Md5Index:
    return Md5Index(_safe_label(root))


# ---- background build job ----

def build_index(root: str, min_size: int = DEFAULT_MIN_SIZE) -> str:
    if not os.path.isdir(root):
        raise ValueError(f"root not found: {root}")
    label = _safe_label(root)
    job_id = f"md5idx_{int(time.time())}"
    _build_jobs[job_id] = {
        "status": "running", "phase": "starting", "root": root,
        "label": label, "min_size": min_size,
        "files_seen": 0, "files_hashed": 0, "skipped_current": 0,
        "bytes_hashed": 0, "errors": 0, "current": "",
        "started_at": time.time(), "cancelled": False,
    }
    threading.Thread(target=_build_worker, args=(job_id, root, min_size),
                     daemon=True).start()
    return job_id


def get_build_job(job_id: str):
    return _build_jobs.get(job_id)


def cancel_build(job_id: str) -> bool:
    if job_id in _build_jobs:
        _build_jobs[job_id]["cancelled"] = True
        return True
    return False


def list_jobs() -> list[dict]:
    return [{"job_id": jid, **{k: v for k, v in j.items()
                               if k not in ("cancelled",)}}
            for jid, j in _build_jobs.items()]


def _build_worker(job_id: str, root: str, min_size: int):
    job = _build_jobs[job_id]
    idx = Md5Index(_safe_label(root))

    def _walk_err(err):
        # a directory we can't read (permission/etc.) — count it, keep going,
        # never let it abort the walk.
        job["errors"] = job.get("errors", 0) + 1

    try:
        job["phase"] = "scanning"
        last_flush = time.time()
        for dirpath, dirs, files in os.walk(root, onerror=_walk_err):
            # skip our own quarantine/backup dirs
            try:
                dirs[:] = [d for d in dirs if not d.startswith((
                    "_EmptyQuarantine", "_MergeQuarantine", "_BackfillQuarantine",
                    "_ReconcileQuarantine", "_PurgeQuarantine",
                    "_DeletedSince_", "_PreReexport_"))]
            except Exception:
                pass
            if job.get("cancelled"):
                job["status"] = "cancelled"
                break
            for fn in files:
                if job.get("cancelled"):
                    break
                try:
                    fp = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(_long_path(fp))
                    except (OSError, ValueError):
                        job["errors"] = job.get("errors", 0) + 1
                        continue
                    if st.st_size < min_size:
                        continue
                    job["files_seen"] += 1
                    # RESUME: skip if already indexed with same size+mtime
                    try:
                        if idx.is_current(fp, st.st_size, st.st_mtime):
                            job["skipped_current"] += 1
                            continue
                    except Exception:
                        pass  # if the check fails, just re-hash it
                    job["current"] = fp
                    md5 = _compute_md5(fp)
                    if md5 is None:
                        job["errors"] = job.get("errors", 0) + 1
                        continue
                    try:
                        idx.add(fp, md5, st.st_size, st.st_mtime)
                    except Exception:
                        # DB hiccup — try a flush+retry once, else skip this file
                        try:
                            idx.flush(); idx.add(fp, md5, st.st_size, st.st_mtime)
                        except Exception:
                            job["errors"] = job.get("errors", 0) + 1
                            continue
                    job["files_hashed"] += 1
                    job["bytes_hashed"] += st.st_size
                    # periodic durability flush
                    if time.time() - last_flush > 10:
                        try:
                            idx.flush()
                        except Exception:
                            pass
                        last_flush = time.time()
                except Exception:
                    # absolutely never let one file kill the run
                    job["errors"] = job.get("errors", 0) + 1
                    continue
        try:
            idx.flush()
        except Exception:
            pass
        if not job.get("cancelled"):
            job["phase"] = "complete"
            job["status"] = "completed"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
        try:
            st = idx.stats()
            job["indexed_files_total"] = st["indexed_files"]
            job["indexed_bytes_total"] = st["indexed_bytes"]
        except Exception:
            pass
    except Exception as e:
        # even a catastrophic error leaves the DB flushed and the job marked,
        # so the supervisor can resume from what's committed.
        try:
            idx.flush()
        except Exception:
            pass
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        try:
            idx.close()
        except Exception:
            pass


# ---- targeted hashing (hash a KNOWN list of files, no full-tree walk) ----

def index_paths(root: str, paths: list[str], min_size: int = 0,
                force: bool = False) -> dict:
    """
    Hash an EXPLICIT list of files into the index for `root`, writing rows
    IDENTICAL to the full walker (same table, same (path, md5, size, mtime)).

    This is the smart-catch-up path: after we DOWNLOAD known files into
    Organized/, we already know exactly where they are — so we hash just those
    instead of re-walking the whole tree. Same DB, so dedup/reconstruct see the
    new files immediately.

    - min_size=0 by default to MATCH the running full index (launched with
      min_size=0); pass a threshold to skip small files if desired.
    - Resumable: a file already indexed with the same (size, mtime) is skipped
      unless force=True.
    - Crash-safe: never raises on a single bad/locked/missing file; periodic
      flush; final flush in finally.
    Returns counts: {hashed, skipped_current, missing, errors, bytes_hashed}.
    """
    idx = open_index(root)
    stats = {"hashed": 0, "skipped_current": 0, "missing": 0,
             "errors": 0, "bytes_hashed": 0, "total": len(paths)}
    last_flush = time.time()
    try:
        for p in paths:
            try:
                fp = os.path.abspath(p)
                try:
                    st = os.stat(_long_path(fp))
                except (OSError, ValueError):
                    stats["missing"] += 1
                    continue
                if not os.path.isfile(_long_path(fp)):
                    stats["missing"] += 1
                    continue
                if min_size and st.st_size < min_size:
                    continue
                if not force:
                    try:
                        if idx.is_current(fp, st.st_size, st.st_mtime):
                            stats["skipped_current"] += 1
                            continue
                    except Exception:
                        pass  # if the check fails, just re-hash it
                md5 = _compute_md5(fp)
                if md5 is None:
                    stats["errors"] += 1
                    continue
                try:
                    idx.add(fp, md5, st.st_size, st.st_mtime)
                except Exception:
                    try:
                        idx.flush(); idx.add(fp, md5, st.st_size, st.st_mtime)
                    except Exception:
                        stats["errors"] += 1
                        continue
                stats["hashed"] += 1
                stats["bytes_hashed"] += st.st_size
                if time.time() - last_flush > 10:
                    try:
                        idx.flush()
                    except Exception:
                        pass
                    last_flush = time.time()
            except Exception:
                stats["errors"] += 1
                continue
    finally:
        try:
            idx.close()   # flushes on close
        except Exception:
            pass
    return stats
