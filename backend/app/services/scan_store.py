"""
SQLite-backed per-scan store.

WHY: the previous scanner held every file's hash record in an in-memory dict that
grew with file count and ran the process out of memory (~200K files → OOM crash).
Streaming records into an on-disk SQLite DB keeps RAM flat regardless of how many
files are scanned (only a small insert buffer lives in memory), and the DB file is
itself the crash-durable checkpoint — no separate checkpoint file needed. This is
also the foundation for scaling to millions of items (SharePoint/multi-source).

Schema: one row per file, indexed by hash. Duplicate detection is an indexed SQL
query (GROUP BY hash HAVING COUNT(*) > 1) instead of an in-memory grouping.
"""
import os
import sqlite3
import mimetypes
from datetime import datetime
from typing import Optional, Iterator

_STORE_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "scans_db")

_INSERT_BUFFER_FLUSH = 2000   # rows buffered in RAM before a batched commit


def _human_size(n: int) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024:
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.2f} EB"


def _mime(path: str) -> Optional[str]:
    return mimetypes.guess_type(path)[0]


class ScanStore:
    """On-disk store for one scan. Flat memory: only a small insert buffer."""

    def __init__(self, scan_id: str):
        os.makedirs(_STORE_DIR, exist_ok=True)
        self.scan_id = scan_id
        self.db_path = os.path.join(_STORE_DIR, f"{scan_id}.db")
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        # WAL = durable + concurrent read while writing; big cache for speed.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path        TEXT PRIMARY KEY,
                hash        TEXT NOT NULL,
                size        INTEGER NOT NULL,
                mtime       REAL,
                ctime       REAL,
                source      TEXT,
                source_path TEXT
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash)")
        self._conn.commit()
        self._buf: list[tuple] = []

    # ---- writes (buffered + batched) ----
    def add_file(self, path, file_hash, size, mtime, ctime, source, source_path):
        self._buf.append((path, file_hash, size, mtime, ctime, source, source_path))
        if len(self._buf) >= _INSERT_BUFFER_FLUSH:
            self.flush()

    def flush(self):
        if not self._buf:
            return
        # INSERT OR REPLACE so a resumed/overlapping scan updates rather than errors
        self._conn.executemany(
            "INSERT OR REPLACE INTO files "
            "(path, hash, size, mtime, ctime, source, source_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", self._buf)
        self._conn.commit()
        self._buf.clear()

    # ---- reads / stats ----
    def count_processed(self) -> int:
        self.flush()
        cur = self._conn.execute("SELECT COUNT(*) FROM files")
        return cur.fetchone()[0]

    def count_duplicate_files(self) -> int:
        """Total redundant copies: sum(count-1) over hash groups with >1 file."""
        self.flush()
        cur = self._conn.execute("""
            SELECT COALESCE(SUM(c - 1), 0) FROM (
                SELECT COUNT(*) AS c FROM files GROUP BY hash HAVING c > 1
            )
        """)
        return cur.fetchone()[0] or 0

    def count_groups(self) -> int:
        self.flush()
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM (SELECT hash FROM files GROUP BY hash HAVING COUNT(*) > 1)")
        return cur.fetchone()[0]

    def _enrich(self, row: sqlite3.Row) -> dict:
        path = row["path"]
        src_path = row["source_path"] or ""
        subfolder = ""
        if src_path and path.startswith(src_path):
            subfolder = os.path.dirname(path[len(src_path):].lstrip("/"))
        mtime, ctime = row["mtime"], row["ctime"]
        return {
            "path": path,
            "filename": os.path.basename(path),
            "extension": os.path.splitext(path)[1].lower(),
            "size": row["size"],
            "size_human": _human_size(row["size"]),
            "mime_type": _mime(path),
            "hash": row["hash"],
            "modified_time": datetime.fromtimestamp(mtime).isoformat() if mtime else "",
            "created_time": datetime.fromtimestamp(ctime).isoformat() if ctime else "",
            "source": row["source"],
            "source_path": src_path,
            "subfolder": subfolder,
        }

    def iter_duplicate_groups(self, limit: Optional[int] = None) -> Iterator[dict]:
        """Yield duplicate groups (hash with >1 file), largest wasted space first.
        Memory-light: one group at a time, files fetched per hash."""
        self.flush()
        self._conn.row_factory = sqlite3.Row
        # group hashes by wasted space = (count-1)*size
        q = """
            SELECT hash, COUNT(*) AS cnt, MAX(size) AS size
            FROM files GROUP BY hash HAVING cnt > 1
            ORDER BY (cnt - 1) * MAX(size) DESC
        """
        if limit:
            q += f" LIMIT {int(limit)}"
        hashes = self._conn.execute(q).fetchall()
        for h in hashes:
            files = self._conn.execute(
                "SELECT * FROM files WHERE hash = ?", (h["hash"],)).fetchall()
            wasted = (h["cnt"] - 1) * h["size"]
            yield {
                "hash": h["hash"],
                "file_count": h["cnt"],
                "total_wasted_space": wasted,
                "total_wasted_space_human": _human_size(wasted),
                "files": [self._enrich(r) for r in files],
            }

    def duplicates_payload(self, status: str, in_progress: bool,
                           limit: Optional[int] = None) -> dict:
        groups = list(self.iter_duplicate_groups(limit=limit))
        total_wasted = sum(g["total_wasted_space"] for g in groups)
        total_dup = sum(g["file_count"] - 1 for g in groups)
        return {
            "scan_id": self.scan_id,
            "status": status,
            "in_progress": in_progress,
            "total_groups": len(groups),
            "total_duplicate_files": total_dup,
            "total_wasted_space": total_wasted,
            "total_wasted_space_human": _human_size(total_wasted),
            "groups": groups,
        }

    def close(self):
        try:
            self.flush()
            self._conn.close()
        except sqlite3.Error:
            pass


def import_hash_cache(store: "ScanStore", cache_entries: dict,
                      source_label: str, source_path: str) -> int:
    """Bulk-load an existing hash cache ({path: {hash,size,mtime}}) straight into
    a scan store WITHOUT walking the filesystem or re-hashing. Lets already-hashed
    trees (e.g. Dropbox) become dedup-ready in SQLite near-instantly. Returns the
    number of rows imported."""
    rows = []
    n = 0
    for path, e in cache_entries.items():
        rows.append((path, e.get("hash"), e.get("size", 0),
                     e.get("mtime"), None, source_label, source_path))
        if len(rows) >= 5000:
            store._conn.executemany(
                "INSERT OR REPLACE INTO files "
                "(path, hash, size, mtime, ctime, source, source_path) "
                "VALUES (?,?,?,?,?,?,?)", rows)
            store._conn.commit()
            n += len(rows)
            rows.clear()
    if rows:
        store._conn.executemany(
            "INSERT OR REPLACE INTO files "
            "(path, hash, size, mtime, ctime, source, source_path) "
            "VALUES (?,?,?,?,?,?,?)", rows)
        store._conn.commit()
        n += len(rows)
    return n


def merge_stores(source_scan_ids: list[str], new_scan_id: str) -> dict:
    """Combine several finished scan stores into ONE new store for cross-source
    dedup — no re-scan, no re-hash. Uses SQLite ATTACH + INSERT...SELECT so it's
    fast (seconds). Files keep their original source/source_path, so the merged
    store's GROUP BY hash naturally surfaces duplicates that span sources (e.g. a
    file present in BOTH Dropbox and Google Drive).

    Returns {merged, sources, total_files, duplicate_groups}.
    Skips any source scan_id that has no .db.
    """
    dest = ScanStore(new_scan_id)
    merged_sources = []
    for i, sid in enumerate(source_scan_ids):
        src_path = os.path.join(_STORE_DIR, f"{sid}.db")
        if not os.path.exists(src_path):
            continue
        alias = f"src{i}"
        dest._conn.execute("ATTACH DATABASE ? AS " + alias, (src_path,))
        # INSERT OR IGNORE: if the exact same path appears in two source stores
        # (e.g. re-scanned), keep one row — dedup is by hash, not by duplicated
        # path rows.
        dest._conn.execute(
            f"INSERT OR IGNORE INTO files "
            f"(path, hash, size, mtime, ctime, source, source_path) "
            f"SELECT path, hash, size, mtime, ctime, source, source_path "
            f"FROM {alias}.files")
        dest._conn.commit()
        dest._conn.execute(f"DETACH DATABASE {alias}")
        merged_sources.append(sid)
    total = dest.count_processed()
    groups = dest.count_groups()
    return {
        "merged": True,
        "new_scan_id": new_scan_id,
        "sources": merged_sources,
        "total_files": total,
        "duplicate_groups": groups,
        "store": dest,
    }


def store_exists(scan_id: str) -> bool:
    return os.path.exists(os.path.join(_STORE_DIR, f"{scan_id}.db"))


def open_store(scan_id: str) -> ScanStore:
    return ScanStore(scan_id)


def list_stores() -> list[str]:
    """Return scan_ids that have a SQLite store on disk, newest first."""
    if not os.path.isdir(_STORE_DIR):
        return []
    dbs = [f for f in os.listdir(_STORE_DIR) if f.endswith(".db")]
    dbs.sort(key=lambda f: os.path.getmtime(os.path.join(_STORE_DIR, f)), reverse=True)
    return [f[:-3] for f in dbs]


def store_mtime(scan_id: str) -> float:
    p = os.path.join(_STORE_DIR, f"{scan_id}.db")
    return os.path.getmtime(p) if os.path.exists(p) else 0.0
