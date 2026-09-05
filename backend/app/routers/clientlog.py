"""
Client-log sink: the frontend POSTs its console output, errors, and lifecycle
events here so they can be read server-side. This gives visibility into what the
browser is actually doing (renders, tab switches, state changes, crashes) instead
of guessing from the server side.

Logs are appended to ~/.file_dedup_analyzer/ui.log (newest last) and also kept in
a small in-memory ring buffer for quick retrieval via GET /api/clientlog/recent.
"""
import os
import json
import time
from collections import deque
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, Any

router = APIRouter()

LOG_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer")
LOG_FILE = os.path.join(LOG_DIR, "ui.log")
_ring: deque = deque(maxlen=2000)


class ClientLogEvent(BaseModel):
    # Matches the browser telemetry client contract (ported from the content
    # creation platform's app/lib/telemetry.ts).
    sessionId: Optional[str] = None
    ts: Optional[str] = None                 # client ISO timestamp
    level: Optional[str] = "info"            # debug | info | warn | error
    event: str = ""                          # api | click | nav | error | ...
    message: Optional[str] = None
    url: Optional[str] = None
    component: Optional[str] = None
    httpStatus: Optional[int] = None
    durationMs: Optional[float] = None
    detail: Optional[Any] = None


class LogBatch(BaseModel):
    events: list[ClientLogEvent] = []


def _write(events: list[ClientLogEvent]):
    os.makedirs(LOG_DIR, exist_ok=True)
    lines = []
    for e in events[:200]:  # cap batch
        rec = {
            "server_ts": round(time.time(), 3),
            "client_ts": e.ts,
            "level": e.level,
            "event": e.event,
            "session": e.sessionId,
            "url": e.url,
            "component": e.component,
            "http_status": e.httpStatus,
            "duration_ms": e.durationMs,
            "message": e.message,
            "detail": e.detail,
        }
        _ring.append(rec)
        lines.append(json.dumps(rec, ensure_ascii=False))
    if lines:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


@router.post("")
@router.post("/")
async def ingest(batch: LogBatch):
    """Receive a batch of client telemetry events. Never fails the client."""
    try:
        _write(batch.events)
        return {"ok": True, "written": len(batch.events)}
    except Exception:
        return {"ok": True, "written": 0}


@router.get("/recent")
async def recent(limit: int = 200, level: Optional[str] = None,
                 event: Optional[str] = None):
    """Return the most recent client log entries (filter by level and/or event)."""
    items = list(_ring)
    if level:
        items = [i for i in items if i.get("level") == level]
    if event:
        items = [i for i in items if i.get("event") == event]
    return {"count": len(items[-limit:]), "entries": items[-limit:]}


@router.delete("")
@router.delete("/")
async def clear():
    """Clear the in-memory ring and truncate the log file."""
    _ring.clear()
    try:
        open(LOG_FILE, "w", encoding="utf-8").close()
    except OSError:
        pass
    return {"cleared": True}
