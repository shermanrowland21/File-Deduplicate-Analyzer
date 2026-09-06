"""
UNIFIED FILESYSTEM CATALOG.

A single, persistent, indexed SQLite catalog of every path across all registered
roots. Crawl once; then searches, purge, dedup, and reconciliation query the
catalog (milliseconds) instead of re-walking the disk. Keep it fresh with an
INCREMENTAL, metadata-only refresh (no file reads): insert new paths, update
changed ones (size/mtime), and drop paths that disappeared — never a full
re-scan of unchanged data.

Why one unified catalog (per the user):
  - One search spans every root at once ("find BDawson anywhere").
  - Cross-tree lookups (same file in Organized AND the Dropbox snapshot) are a
    single query, not a multi-DB juggle.

PORTABILITY NOTE (intended for reuse in the CPMS platform):
  - This module is self-contained: it depends only on the stdlib. No imports
    from the rest of this app.
  - All persistence goes through the `Catalog` class; swap its SQLite calls for
    CPMS's datastore (Postgres, etc.) without touching the crawl/search logic.
  - The public API is small and stable: register_root, refresh (background),
    get_refresh_job, search, list_roots, stats.

Schema:
  roots(root PK, label, added_at, last_scan_at, file_count, dir_count, bytes)
  files(path PK, root, name, parent, is_dir, size, mtime, ctime, scan_gen)
    - name/parent indexed (search + tree nav), name is NOCASE for fast LIKE.
    - scan_gen: each refresh of a root bumps a generation counter; rows under
      that root not re-stamped this generation were deleted on disk and are
      pruned. This is how incremental deletion detection works without diffing
      the whole tree in memory.
"""
import os
import re
import time
import sqlite3
import threading
from typing import Optional

_DB_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "catalog")
_DB_PATH = os.path.join(_DB_DIR, "catalog.db")
_UPSERT_FLUSH = 2000

# Directories we create ourselves — never catalog them as user content.
_OUR_DIRS = ("_EmptyQuarantine", "_MergeQuarantine", "_BackfillQuarantine",
             "_ReconcileQuarantine", "_PurgeQuarantine", "_DeletedSince_",
             "_PreReexport_")

_refresh_jobs: dict = {}
_lock = threading.Lock()


# ============================================================ persistence

class Catalog:
    """
    All catalog persistence. Isolated so the storage layer can be swapped for
    CPMS's datastore without changing crawl/search logic.
    """

    def __init__(self, db_path: str = _DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS roots (
                root         TEXT PRIMARY KEY,
                label        TEXT,
                added_at     REAL,
                last_scan_at REAL,
                file_count   INTEGER DEFAULT 0,
                dir_count    INTEGER DEFAULT 0,
                bytes        INTEGER DEFAULT 0,
                scan_gen     INTEGER DEFAULT 0
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path     TEXT PRIMARY KEY,
                root     TEXT NOT NULL,
                name     TEXT NOT NULL,
                parent   TEXT,
                is_dir   INTEGER NOT NULL,
                size     INTEGER,
                mtime    REAL,
                ctime    REAL,
                scan_gen INTEGER
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_name ON files(name COLLATE NOCASE)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_files_root ON files(root)")
        self._conn.commit()

    # ---- roots ----
    def register_root(self, root: str, label: Optional[str] = None) -> dict:
        root = os.path.abspath(root)
        label = label or os.path.basename(root.rstrip("\\/")) or root
        self._conn.execute(
            "INSERT OR IGNORE INTO roots (root, label, added_at, scan_gen) "
            "VALUES (?,?,?,0)", (root, label, time.time()))
        self._conn.commit()
        return self.get_root(root)

    def get_root(self, root: str) -> Optional[dict]:
        root = os.path.abspath(root)
        cur = self._conn.execute("SELECT * FROM roots WHERE root = ?", (root,))
        r = cur.fetchone()
        if not r:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, r))

    def list_roots(self) -> list[dict]:
        self._conn.row_factory = sqlite3.Row
        rows = self._conn.execute("SELECT * FROM roots ORDER BY label").fetchall()
        self._conn.row_factory = None
        return [dict(r) for r in rows]

    def next_scan_gen(self, root: str) -> int:
        root = os.path.abspath(root)
        cur = self._conn.execute("SELECT COALESCE(scan_gen,0)+1 FROM roots WHERE root=?", (root,))
        row = cur.fetchone()
        gen = row[0] if row else 1
        self._conn.execute("UPDATE roots SET scan_gen=? WHERE root=?", (gen, root))
        self._conn.commit()
        return gen

    # ---- files (bulk upsert / prune) ----
    def upsert_many(self, rows: list[tuple]):
        """rows: (path, root, name, parent, is_dir, size, mtime, ctime, scan_gen)"""
        if not rows:
            return
        self._conn.executemany(
            "INSERT OR REPLACE INTO files "
            "(path, root, name, parent, is_dir, size, mtime, ctime, scan_gen) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows)
        self._conn.commit()

    def prune_stale(self, root: str, scan_gen: int) -> int:
        """Delete rows for this root NOT stamped with the current gen (= gone)."""
        root = os.path.abspath(root)
        cur = self._conn.execute(
            "DELETE FROM files WHERE root=? AND scan_gen<>?", (root, scan_gen))
        self._conn.commit()
        return cur.rowcount

    def finalize_root_stats(self, root: str):
        root = os.path.abspath(root)
        cur = self._conn.execute(
            "SELECT SUM(CASE WHEN is_dir=0 THEN 1 ELSE 0 END), "
            "       SUM(CASE WHEN is_dir=1 THEN 1 ELSE 0 END), "
            "       COALESCE(SUM(CASE WHEN is_dir=0 THEN size ELSE 0 END),0) "
            "FROM files WHERE root=?", (root,))
        fc, dc, by = cur.fetchone()
        self._conn.execute(
            "UPDATE roots SET last_scan_at=?, file_count=?, dir_count=?, bytes=? "
            "WHERE root=?", (time.time(), fc or 0, dc or 0, by or 0, root))
        self._conn.commit()

    # ---- search ----
    def search_names(self, matchers: list, roots: Optional[list[str]],
                     want_dirs: bool) -> list[dict]:
        """
        Return catalog rows whose NAME matches any matcher. matchers is a list of
        (kind, value): ('sub', lowered_substring) or ('re', compiled_regex).
        SQL does the substring pre-filter for 'sub'; regex applied in Python on
        the reduced set.
        """
        self._conn.row_factory = sqlite3.Row
        where = ["is_dir = ?"]
        params: list = [1 if want_dirs else 0]
        if roots:
            where.append("root IN (%s)" % ",".join("?" * len(roots)))
            params += [os.path.abspath(r) for r in roots]
        # substring pre-filter in SQL (OR of LIKEs) for the 'sub' matchers
        subs = [v for (k, v) in matchers if k == "sub"]
        regexes = [v for (k, v) in matchers if k == "re"]
        rows = []
        if subs:
            like_clause = " OR ".join(["name LIKE ? COLLATE NOCASE"] * len(subs))
            q = f"SELECT * FROM files WHERE {' AND '.join(where)} AND ({like_clause})"
            rows = self._conn.execute(q, params + [f"%{s}%" for s in subs]).fetchall()
        if regexes:
            # regex can't be pushed to SQL cheaply; scan the (name-indexed) set
            q = f"SELECT * FROM files WHERE {' AND '.join(where)}"
            for r in self._conn.execute(q, params).fetchall():
                if any(rx.search(r["name"]) for rx in regexes):
                    rows.append(r)
        self._conn.row_factory = None
        # dedup by path
        seen = set()
        out = []
        for r in rows:
            if r["path"] in seen:
                continue
            seen.add(r["path"])
            out.append(dict(r))
        return out

    def subtree_stats(self, folder_path: str) -> tuple:
        """(file_count, bytes) for everything under folder_path, from catalog."""
        prefix = folder_path.replace("\\", "/").rstrip("/") + "/"
        cur = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM files "
            "WHERE is_dir=0 AND REPLACE(path,'\\','/') LIKE ?", (prefix + "%",))
        fc, by = cur.fetchone()
        return fc or 0, by or 0

    def stats(self) -> dict:
        cur = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN is_dir=0 THEN size ELSE 0 END),0) FROM files")
        n, by = cur.fetchone()
        return {"total_rows": n, "total_bytes": by, "db_path": self.db_path,
                "roots": self.list_roots()}

    def close(self):
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


def _catalog() -> Catalog:
    return Catalog()


# ============================================================ public API

def register_root(root: str, label: Optional[str] = None) -> dict:
    if not os.path.isdir(root):
        raise ValueError(f"root not found: {root}")
    with _lock:
        c = _catalog()
        try:
            return c.register_root(root, label)
        finally:
            c.close()


def list_roots() -> list[dict]:
    c = _catalog()
    try:
        return c.list_roots()
    finally:
        c.close()


def stats() -> dict:
    c = _catalog()
    try:
        return c.stats()
    finally:
        c.close()


# ---- refresh (incremental, metadata-only, background) ----

def refresh(root: Optional[str] = None) -> str:
    """
    Start a background refresh of one root (or ALL registered roots if None).
    Metadata-only crawl (os.scandir, no file reads). Incremental: upserts new/
    changed rows and prunes rows that vanished.
    """
    job_id = f"catalog_{int(time.time())}"
    _refresh_jobs[job_id] = {
        "status": "running", "phase": "starting", "root": root or "ALL",
        "roots_done": 0, "roots_total": 0,
        "dirs_seen": 0, "files_seen": 0, "upserted": 0, "pruned": 0,
        "current": "", "started_at": time.time(), "cancelled": False,
        "errors": [],
    }
    threading.Thread(target=_refresh_worker, args=(job_id, root),
                     daemon=True).start()
    return job_id


def get_refresh_job(job_id: str):
    return _refresh_jobs.get(job_id)


def cancel_refresh(job_id: str) -> bool:
    if job_id in _refresh_jobs:
        _refresh_jobs[job_id]["cancelled"] = True
        return True
    return False


def _refresh_worker(job_id: str, root: Optional[str]):
    job = _refresh_jobs[job_id]
    c = _catalog()
    try:
        roots = ([os.path.abspath(root)] if root
                 else [r["root"] for r in c.list_roots()])
        job["roots_total"] = len(roots)
        if not roots:
            job["status"] = "error"; job["error"] = "no roots registered"
            return
        for rt in roots:
            if job.get("cancelled"):
                job["status"] = "cancelled"; break
            if not os.path.isdir(rt):
                job["errors"].append(f"root missing: {rt}")
                job["roots_done"] += 1
                continue
            _refresh_one_root(c, rt, job)
            job["roots_done"] += 1
        if not job.get("cancelled"):
            job["phase"] = "complete"; job["status"] = "completed"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)
    finally:
        c.close()


def _refresh_one_root(c: Catalog, root: str, job: dict):
    # ensure the root exists in the roots table
    if not c.get_root(root):
        c.register_root(root)
    gen = c.next_scan_gen(root)
    job["phase"] = f"scanning {os.path.basename(root)}"
    buf: list[tuple] = []

    def flush():
        if buf:
            c.upsert_many(buf)
            job["upserted"] += len(buf)
            buf.clear()

    for dirpath, dirs, files in os.walk(root):
        if job.get("cancelled"):
            break
        dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIRS)]
        job["current"] = dirpath
        # the directory itself
        parent = os.path.dirname(dirpath)
        try:
            dst = os.stat(dirpath)
            buf.append((dirpath, root, os.path.basename(dirpath.rstrip("\\/")) or dirpath,
                        parent, 1, 0, dst.st_mtime, dst.st_ctime, gen))
            job["dirs_seen"] += 1
        except OSError:
            pass
        for fn in files:
            fp = os.path.join(dirpath, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            buf.append((fp, root, fn, dirpath, 0, st.st_size,
                        st.st_mtime, st.st_ctime, gen))
            job["files_seen"] += 1
            if len(buf) >= _UPSERT_FLUSH:
                flush()
    flush()
    if not job.get("cancelled"):
        pruned = c.prune_stale(root, gen)
        job["pruned"] += pruned
        c.finalize_root_stats(root)


# ---- search (from catalog, instant) ----

def _compile_matchers(keywords: list[str], whole_word: bool) -> list:
    matchers = []
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        if whole_word:
            matchers.append(("re", re.compile(r"\b" + re.escape(kw) + r"\b", re.I)))
        else:
            matchers.append(("sub", kw.lower()))
    return matchers


def is_cataloged(root: str) -> bool:
    c = _catalog()
    try:
        r = c.get_root(root)
        return bool(r and r.get("last_scan_at"))
    finally:
        c.close()


def search(keywords: list[str], whole_word: bool = False,
           roots: Optional[list[str]] = None) -> dict:
    """
    Instant catalog search. Matches file AND folder names. Returns matched
    folders (whole subtree, file_count + bytes from catalog) and loose files
    (whose parent folder did NOT match), same shape as the live purge search.
    """
    matchers = _compile_matchers(keywords, whole_word)
    if not matchers:
        return {"error": "no keywords"}
    c = _catalog()
    try:
        folder_rows = c.search_names(matchers, roots, want_dirs=True)
        file_rows = c.search_names(matchers, roots, want_dirs=False)

        # matched folder prefixes to suppress redundant children
        folder_prefixes = sorted(
            [r["path"].replace("\\", "/").rstrip("/") + "/" for r in folder_rows],
            key=len)

        def inside_matched(path: str) -> bool:
            p = path.replace("\\", "/")
            return any(p.startswith(pre) for pre in folder_prefixes)

        matched_folders = []
        # drop nested matched folders (a folder inside another matched folder)
        for r in sorted(folder_rows, key=lambda x: len(x["path"])):
            p = r["path"].replace("\\", "/").rstrip("/") + "/"
            if any(p != pre and p.startswith(pre) for pre in folder_prefixes):
                continue
            fc, by = c.subtree_stats(r["path"])
            matched_folders.append({"path": r["path"], "keyword": "",
                                    "files": fc, "bytes": by})

        matched_files = []
        for r in file_rows:
            if inside_matched(r["path"]):
                continue  # covered by a matched parent folder
            matched_files.append({"path": r["path"], "keyword": "",
                                  "bytes": r["size"] or 0})

        total_bytes = (sum(f["bytes"] for f in matched_folders)
                       + sum(f["bytes"] for f in matched_files))
        return {
            "source": "catalog",
            "matched_folders": sorted(matched_folders, key=lambda x: -x["bytes"]),
            "matched_files": sorted(matched_files, key=lambda x: -x["bytes"]),
            "counts": {
                "folders": len(matched_folders),
                "files": len(matched_files),
                "total_items": len(matched_folders) + len(matched_files),
                "total_bytes": total_bytes,
                "total_file_count": sum(f["files"] for f in matched_folders)
                                    + len(matched_files),
            },
        }
    finally:
        c.close()
