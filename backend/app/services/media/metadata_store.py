"""
Metadata store - SQLite with FTS5 for full-text search locally.
Designed to migrate to OpenSearch Serverless + DynamoDB.

Stores stratified metadata layers for each analyzed file:
- File info (path, type, duration, size)
- Transcript segments (timestamped text with speaker labels)
- Visual segments (timestamped frame descriptions)
- Topics/keywords per segment
- Embeddings references
"""
import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional

DB_PATH = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "media_metadata.db")
_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    """Get database connection, creating tables if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection):
    """Create all tables and FTS indexes."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS media_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            extension TEXT,
            mime_type TEXT,
            file_size INTEGER,
            duration_seconds REAL,
            width INTEGER,
            height INTEGER,
            analyzed_at TEXT,
            analysis_status TEXT DEFAULT 'pending',
            metadata_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS transcript_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            text TEXT NOT NULL,
            speaker TEXT,
            confidence REAL,
            FOREIGN KEY (file_id) REFERENCES media_files(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS visual_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            timestamp REAL NOT NULL,
            frame_path TEXT,
            description TEXT,
            ocr_text TEXT,
            objects_json TEXT,
            scene_type TEXT,
            FOREIGN KEY (file_id) REFERENCES media_files(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            start_time REAL,
            end_time REAL,
            confidence REAL,
            source TEXT,
            FOREIGN KEY (file_id) REFERENCES media_files(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            start_time REAL,
            end_time REAL,
            frequency INTEGER DEFAULT 1,
            FOREIGN KEY (file_id) REFERENCES media_files(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_transcript_file ON transcript_segments(file_id);
        CREATE INDEX IF NOT EXISTS idx_transcript_time ON transcript_segments(file_id, start_time);
        CREATE INDEX IF NOT EXISTS idx_visual_file ON visual_segments(file_id);
        CREATE INDEX IF NOT EXISTS idx_topics_file ON topics(file_id);
        CREATE INDEX IF NOT EXISTS idx_topics_topic ON topics(topic);
        CREATE INDEX IF NOT EXISTS idx_keywords_keyword ON keywords(keyword);
    """)

    # FTS5 virtual table for full-text search across transcripts
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts
            USING fts5(text, content='transcript_segments', content_rowid='id')
        """)
    except sqlite3.OperationalError:
        pass  # Already exists

    conn.commit()


# --- File Operations ---

def upsert_media_file(
    file_path: str,
    filename: str,
    extension: str = "",
    mime_type: str = "",
    file_size: int = 0,
    duration_seconds: float = 0,
    width: int = 0,
    height: int = 0,
    metadata: dict = None,
) -> int:
    """Insert or update a media file record. Returns the file_id."""
    with _lock:
        conn = _get_db()
        try:
            conn.execute("""
                INSERT INTO media_files (file_path, filename, extension, mime_type,
                    file_size, duration_seconds, width, height, metadata_json, analyzed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    filename=excluded.filename, extension=excluded.extension,
                    mime_type=excluded.mime_type, file_size=excluded.file_size,
                    duration_seconds=excluded.duration_seconds,
                    width=excluded.width, height=excluded.height,
                    metadata_json=excluded.metadata_json,
                    analyzed_at=excluded.analyzed_at
            """, (
                file_path, filename, extension, mime_type, file_size,
                duration_seconds, width, height,
                json.dumps(metadata) if metadata else None,
                datetime.now().isoformat(),
            ))
            conn.commit()
            row = conn.execute(
                "SELECT id FROM media_files WHERE file_path = ?", (file_path,)
            ).fetchone()
            return row["id"]
        finally:
            conn.close()


def set_analysis_status(file_id: int, status: str):
    """Update analysis status: pending, processing, completed, error."""
    with _lock:
        conn = _get_db()
        try:
            conn.execute(
                "UPDATE media_files SET analysis_status = ? WHERE id = ?",
                (status, file_id)
            )
            conn.commit()
        finally:
            conn.close()


def get_media_file(file_path: str) -> Optional[dict]:
    """Get a media file record by path."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM media_files WHERE file_path = ?", (file_path,)
        ).fetchone()
        if row:
            return dict(row)
        return None
    finally:
        conn.close()


# --- Transcript Operations ---

def add_transcript_segments(file_id: int, segments: list[dict]):
    """
    Add transcript segments for a file.
    Each segment: {start_time, end_time, text, speaker, confidence}
    """
    with _lock:
        conn = _get_db()
        try:
            # Clear existing segments for this file
            conn.execute("DELETE FROM transcript_segments WHERE file_id = ?", (file_id,))
            # Insert new segments
            conn.executemany("""
                INSERT INTO transcript_segments (file_id, start_time, end_time, text, speaker, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                (file_id, s["start_time"], s["end_time"], s["text"],
                 s.get("speaker"), s.get("confidence"))
                for s in segments
            ])
            # Update FTS index
            conn.execute("DELETE FROM transcript_fts WHERE rowid IN (SELECT id FROM transcript_segments WHERE file_id = ?)", (file_id,))
            conn.execute("""
                INSERT INTO transcript_fts(rowid, text)
                SELECT id, text FROM transcript_segments WHERE file_id = ?
            """, (file_id,))
            conn.commit()
        finally:
            conn.close()


def get_transcript(file_id: int) -> list[dict]:
    """Get all transcript segments for a file, ordered by time."""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM transcript_segments WHERE file_id = ? ORDER BY start_time",
            (file_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Visual Segment Operations ---

def add_visual_segments(file_id: int, segments: list[dict]):
    """
    Add visual analysis segments.
    Each: {timestamp, frame_path, description, ocr_text, objects, scene_type}
    """
    with _lock:
        conn = _get_db()
        try:
            conn.execute("DELETE FROM visual_segments WHERE file_id = ?", (file_id,))
            conn.executemany("""
                INSERT INTO visual_segments (file_id, timestamp, frame_path, description, ocr_text, objects_json, scene_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [
                (file_id, s["timestamp"], s.get("frame_path"), s.get("description"),
                 s.get("ocr_text"), json.dumps(s.get("objects", [])), s.get("scene_type"))
                for s in segments
            ])
            conn.commit()
        finally:
            conn.close()


# --- Topic/Keyword Operations ---

def add_topics(file_id: int, topics: list[dict]):
    """Add topics. Each: {topic, start_time, end_time, confidence, source}."""
    with _lock:
        conn = _get_db()
        try:
            conn.execute("DELETE FROM topics WHERE file_id = ?", (file_id,))
            conn.executemany("""
                INSERT INTO topics (file_id, topic, start_time, end_time, confidence, source)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                (file_id, t["topic"], t.get("start_time"), t.get("end_time"),
                 t.get("confidence"), t.get("source"))
                for t in topics
            ])
            conn.commit()
        finally:
            conn.close()


def add_keywords(file_id: int, keywords: list[dict]):
    """Add keywords. Each: {keyword, start_time, end_time, frequency}."""
    with _lock:
        conn = _get_db()
        try:
            conn.execute("DELETE FROM keywords WHERE file_id = ?", (file_id,))
            conn.executemany("""
                INSERT INTO keywords (file_id, keyword, start_time, end_time, frequency)
                VALUES (?, ?, ?, ?, ?)
            """, [
                (file_id, k["keyword"], k.get("start_time"), k.get("end_time"),
                 k.get("frequency", 1))
                for k in keywords
            ])
            conn.commit()
        finally:
            conn.close()


# --- Search Operations ---

def search_transcripts(query: str, limit: int = 50) -> list[dict]:
    """
    Full-text search across all transcripts.
    Returns matching segments with file info and timestamps.
    """
    conn = _get_db()
    try:
        rows = conn.execute("""
            SELECT
                ts.id, ts.file_id, ts.start_time, ts.end_time, ts.text, ts.speaker,
                mf.file_path, mf.filename, mf.duration_seconds
            FROM transcript_fts fts
            JOIN transcript_segments ts ON ts.id = fts.rowid
            JOIN media_files mf ON mf.id = ts.file_id
            WHERE transcript_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_topics(query: str, limit: int = 50) -> list[dict]:
    """Search topics/keywords across all files."""
    conn = _get_db()
    try:
        pattern = f"%{query}%"
        rows = conn.execute("""
            SELECT
                t.topic, t.start_time, t.end_time, t.confidence,
                mf.file_path, mf.filename, mf.duration_seconds
            FROM topics t
            JOIN media_files mf ON mf.id = t.file_id
            WHERE t.topic LIKE ?
            ORDER BY t.confidence DESC
            LIMIT ?
        """, (pattern, limit)).fetchall()

        # Also search keywords
        kw_rows = conn.execute("""
            SELECT
                k.keyword as topic, k.start_time, k.end_time, k.frequency as confidence,
                mf.file_path, mf.filename, mf.duration_seconds
            FROM keywords k
            JOIN media_files mf ON mf.id = k.file_id
            WHERE k.keyword LIKE ?
            ORDER BY k.frequency DESC
            LIMIT ?
        """, (pattern, limit)).fetchall()

        results = [dict(r) for r in rows] + [dict(r) for r in kw_rows]
        return results[:limit]
    finally:
        conn.close()


def search_all(query: str, limit: int = 50) -> dict:
    """
    Combined search across transcripts, topics, keywords, and visual descriptions.
    Returns grouped results with file paths and timestamps.
    """
    transcript_results = search_transcripts(query, limit)
    topic_results = search_topics(query, limit)

    # Also search visual descriptions
    conn = _get_db()
    try:
        pattern = f"%{query}%"
        visual_rows = conn.execute("""
            SELECT
                vs.timestamp, vs.description, vs.ocr_text,
                mf.file_path, mf.filename
            FROM visual_segments vs
            JOIN media_files mf ON mf.id = vs.file_id
            WHERE vs.description LIKE ? OR vs.ocr_text LIKE ?
            ORDER BY vs.timestamp
            LIMIT ?
        """, (pattern, pattern, limit)).fetchall()
        visual_results = [dict(r) for r in visual_rows]
    finally:
        conn.close()

    return {
        "query": query,
        "transcript_hits": transcript_results,
        "topic_hits": topic_results,
        "visual_hits": visual_results,
        "total_hits": len(transcript_results) + len(topic_results) + len(visual_results),
    }


def get_analyzed_files(limit: int = 100) -> list[dict]:
    """Get all analyzed media files."""
    conn = _get_db()
    try:
        rows = conn.execute("""
            SELECT * FROM media_files
            ORDER BY analyzed_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_file_analysis(file_path: str) -> Optional[dict]:
    """Get complete analysis for a file - all layers."""
    conn = _get_db()
    try:
        file_row = conn.execute(
            "SELECT * FROM media_files WHERE file_path = ?", (file_path,)
        ).fetchone()
        if not file_row:
            return None

        file_id = file_row["id"]
        file_data = dict(file_row)

        # Get all layers
        transcripts = conn.execute(
            "SELECT * FROM transcript_segments WHERE file_id = ? ORDER BY start_time",
            (file_id,)
        ).fetchall()

        visuals = conn.execute(
            "SELECT * FROM visual_segments WHERE file_id = ? ORDER BY timestamp",
            (file_id,)
        ).fetchall()

        topics_rows = conn.execute(
            "SELECT * FROM topics WHERE file_id = ? ORDER BY start_time",
            (file_id,)
        ).fetchall()

        keywords_rows = conn.execute(
            "SELECT * FROM keywords WHERE file_id = ? ORDER BY frequency DESC",
            (file_id,)
        ).fetchall()

        return {
            "file": file_data,
            "transcript": [dict(r) for r in transcripts],
            "visual_segments": [dict(r) for r in visuals],
            "topics": [dict(r) for r in topics_rows],
            "keywords": [dict(r) for r in keywords_rows],
        }
    finally:
        conn.close()
