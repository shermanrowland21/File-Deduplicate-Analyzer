"""
DRIVE BLUEPRINT (shared drives only).

Builds an indexed SQLite "blueprint" of the AUTHORITATIVE Google Drive state for
every SHARED DRIVE (no individual My Drives), via GAM. This is the ground truth
we reconstruct the mangled local Takeout extraction against:

    Drive md5Checksum  ->  authoritative { name, full path, drive }

The local files were MD5-hashed separately (md5_index). Matching a local file's
MD5 to a blueprint md5 tells us the file's CORRECT name and CORRECT location, so
we can fix the extraction's scrambled names/folders. Google only exposes MD5
(not SHA-256), which is why the local index is full MD5 too.

Design / portability (concepts to port to CPMS):
  - Self-contained persistence in the Blueprint class (swap SQLite for CPMS DB).
  - Reuses the proven GAM helpers from drive_reconcile (_gam_csv/_read_csv/
    list_shared_drives) — no new GAM plumbing.
  - Resumable PER DRIVE: a `drives` table records which shared drives are fully
    crawled; a restart skips finished drives.

SHARED DRIVES ONLY: we never touch user My Drives here.
"""
import os
import time
import sqlite3
import threading
from typing import Optional

from . import drive_reconcile as dr

_DB_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "blueprint")
_DB_PATH = os.path.join(_DB_DIR, "blueprint.db")
DEFAULT_ADMIN_USER = os.environ.get("GAM_ADMIN_USER", "admin@example.com")

_build_jobs: dict = {}


# ============================================================ persistence

class Blueprint:
    def __init__(self, db_path: str = _DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS drives (
                drive_id   TEXT PRIMARY KEY,
                name       TEXT,
                crawled_at REAL,
                file_count INTEGER DEFAULT 0,
                status     TEXT DEFAULT 'pending'
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                file_id    TEXT PRIMARY KEY,
                md5        TEXT,
                name       TEXT,
                drive_id   TEXT,
                drive_name TEXT,
                rel_path   TEXT,
                size       INTEGER,
                is_folder  INTEGER DEFAULT 0
            )
        """)
        # md5 is THE join key to the local index — must be fast.
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_bp_md5 ON files(md5)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_bp_drive ON files(drive_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_bp_name ON files(name COLLATE NOCASE)")
        self._conn.commit()

    def drive_status(self, drive_id: str) -> Optional[str]:
        r = self._conn.execute(
            "SELECT status FROM drives WHERE drive_id=?", (drive_id,)).fetchone()
        return r[0] if r else None

    def upsert_drive(self, drive_id: str, name: str, status: str,
                     file_count: int = 0):
        self._conn.execute(
            "INSERT INTO drives (drive_id, name, crawled_at, file_count, status) "
            "VALUES (?,?,?,?,?) ON CONFLICT(drive_id) DO UPDATE SET "
            "name=excluded.name, crawled_at=excluded.crawled_at, "
            "file_count=excluded.file_count, status=excluded.status",
            (drive_id, name, time.time(), file_count, status))
        self._conn.commit()

    def clear_drive_files(self, drive_id: str):
        self._conn.execute("DELETE FROM files WHERE drive_id=?", (drive_id,))
        self._conn.commit()

    def insert_files(self, rows: list[tuple]):
        if not rows:
            return
        self._conn.executemany(
            "INSERT OR REPLACE INTO files "
            "(file_id, md5, name, drive_id, drive_name, rel_path, size, is_folder) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)
        self._conn.commit()

    def find_by_md5(self, md5: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT file_id, md5, name, drive_id, drive_name, rel_path, size "
            "FROM files WHERE md5=? AND is_folder=0", (md5.lower(),))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def stats(self) -> dict:
        nf = self._conn.execute("SELECT COUNT(*) FROM files WHERE is_folder=0").fetchone()[0]
        nmd5 = self._conn.execute("SELECT COUNT(*) FROM files WHERE is_folder=0 AND md5<>''").fetchone()[0]
        self._conn.row_factory = sqlite3.Row
        drives = [dict(r) for r in self._conn.execute("SELECT * FROM drives ORDER BY name").fetchall()]
        self._conn.row_factory = None
        return {"files": nf, "files_with_md5": nmd5, "db_path": self.db_path,
                "drives": drives}

    def close(self):
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


def open_blueprint() -> Blueprint:
    return Blueprint()


# ============================================================ path rebuild

def _rebuild_paths(nodes: dict) -> dict:
    """nodes: file_id -> {name, parent, is_folder}. Return file_id -> rel_path
    (path within the shared drive; the drive root is not in nodes so recursion
    stops there)."""
    cache: dict = {}

    def walk(fid, seen):
        if fid in cache:
            return cache[fid]
        if fid in seen or fid not in nodes:
            return ""
        seen.add(fid)
        n = nodes[fid]
        parent = n["parent"]
        if parent and parent in nodes:
            up = walk(parent, seen)
            path = (up + "/" + n["name"]) if up else n["name"]
        else:
            path = n["name"]   # top-level under the shared drive root
        cache[fid] = path
        return path

    return {fid: walk(fid, set()) for fid in nodes}


# ============================================================ crawl one drive

def _crawl_drive(bp: Blueprint, drive_id: str, drive_name: str,
                 admin_user: str, job: dict):
    """GAM print filelist for one shared drive; store files with md5 + rebuilt
    path. Overwrites any prior rows for this drive (clean re-crawl)."""
    safe = drive_id
    csv_path = os.path.join(dr.RECON_DIR, f"bp_{safe}.csv")
    os.makedirs(dr.RECON_DIR, exist_ok=True)
    args = ["user", admin_user, "print", "filelist",
            "select", "teamdriveid", drive_id,
            "query", "trashed = false",
            "fields", "id,name,mimetype,parents,md5Checksum,size"]
    # expect_rows False: an empty drive is legitimate
    dr._gam_csv(csv_path, args, timeout=5400, expect_rows=False, retries=2)
    rows = dr._read_csv(csv_path)

    # build node map for path rebuild
    nodes = {}
    for row in rows:
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
            "is_folder": mime == "application/vnd.google-apps.folder",
            "md5": (row.get("md5Checksum") or "").strip().lower(),
            "size": int(row["size"]) if (row.get("size") or "").isdigit() else 0,
        }

    paths = _rebuild_paths(nodes)

    bp.clear_drive_files(drive_id)
    out_rows = []
    file_count = 0
    for fid, n in nodes.items():
        rel = paths.get(fid, n["name"])
        out_rows.append((fid, n["md5"], n["name"], drive_id, drive_name,
                         rel, n["size"], 1 if n["is_folder"] else 0))
        if not n["is_folder"]:
            file_count += 1
        if len(out_rows) >= 2000:
            bp.insert_files(out_rows); out_rows.clear()
    bp.insert_files(out_rows)
    bp.upsert_drive(drive_id, drive_name, "done", file_count)
    job["files_indexed"] += file_count
    return file_count


# ============================================================ build job

def build(admin_user: str = DEFAULT_ADMIN_USER, force: bool = False) -> str:
    job_id = f"blueprint_{int(time.time())}"
    _build_jobs[job_id] = {
        "status": "running", "phase": "listing_shared_drives",
        "admin_user": admin_user, "force": force,
        "drives_total": 0, "drives_done": 0, "drives_skipped": 0,
        "files_indexed": 0, "current": "", "errors": [],
        "started_at": time.time(), "cancelled": False,
    }
    threading.Thread(target=_build_worker, args=(job_id, admin_user, force),
                     daemon=True).start()
    return job_id


def get_build_job(job_id: str):
    return _build_jobs.get(job_id)


def cancel_build(job_id: str) -> bool:
    if job_id in _build_jobs:
        _build_jobs[job_id]["cancelled"] = True
        return True
    return False


def _build_worker(job_id: str, admin_user: str, force: bool):
    job = _build_jobs[job_id]
    bp = Blueprint()
    try:
        if not dr.gam_available():
            job["status"] = "error"; job["error"] = "GAM not available"
            return
        drives = dr.list_shared_drives()   # SHARED DRIVES ONLY
        job["drives_total"] = len(drives)
        for d in drives:
            if job.get("cancelled"):
                job["status"] = "cancelled"; break
            did, dname = d["id"], d["name"]
            job["current"] = dname
            # resumable: skip drives already done unless force
            if not force and bp.drive_status(did) == "done":
                job["drives_skipped"] += 1
                job["drives_done"] += 1
                continue
            try:
                bp.upsert_drive(did, dname, "crawling")
                _crawl_drive(bp, did, dname, admin_user, job)
            except Exception as e:
                job["errors"].append(f"{dname}: {e}")
                try:
                    bp.upsert_drive(did, dname, "error")
                except Exception:
                    pass
            job["drives_done"] += 1
        if not job.get("cancelled"):
            job["phase"] = "complete"; job["status"] = "completed"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)
    finally:
        bp.close()
