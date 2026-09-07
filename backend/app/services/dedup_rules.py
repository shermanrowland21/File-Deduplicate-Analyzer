"""
Deterministic RULES ENGINE for the Dedup / Routing Assistant (Step 3a).

Turns the user's stated PRINCIPLES into deterministic, free, reversible-friendly
classifications over the two E: MD5 indexes (Organized + Dropbox-Snapshot).
Everything here is DRY-RUN and ADVISORY: it produces verdicts + routes + counts.
It NEVER moves or deletes anything — the existing crossdedup / reconstruct tools
do that on explicit approval.

Every file gets TWO independent decisions:

  1. DEDUP VERDICT  — keep | redundant   (from byte-identical MD5 groups)
  2. ROUTE          — cpms | sharepoint_us | sharepoint_china  (structure kept)

Plus a ROT bucket for reporting:  keep | redundant | obsolete | trivial

Core principles baked in (all overridable via RulesConfig):
  - Preserve existing folder structure. Routing is a LABEL, not a move. Employees
    keep landing where they expect. Media is the only thing that changes SYSTEMS
    (to CPMS) and even then keeps its folder structure.
  - Transferred (terminated-employee) files live under
        E:\\Dropbox-Snapshot\\Sherman Rowland\\<Person>'s files\\...
    (apostrophe may be curly U+2019 or straight). A copy sitting in a transferred
    region is a LOW-PRIORITY keeper: if the same MD5 exists outside any transferred
    region, that other copy wins and the transferred copy is REDUNDANT. A
    transferred file that is genuinely unique is kept.
  - Media  -> CPMS (AWS).      Business docs -> SharePoint (US or China).
  - China routing by path signal ("HPL China", CJK in path, or configured names).
"""
from __future__ import annotations

import os
import re
import time
import json
import sqlite3
from dataclasses import dataclass, field, asdict
from typing import Optional

from . import md5_index as mi

ORGANIZED_ROOT = os.environ.get("ORGANIZED_ROOT", r"E:\Google Drive Files\Organized")
SNAPSHOT_ROOT = os.environ.get("SNAPSHOT_ROOT", r"E:\Dropbox-Snapshot")

_STORE = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "advisor")
_CONFIG_PATH = os.path.join(_STORE, "rules_config.json")

# --------------------------------------------------------------------------- #
# File-type classification (routing depends on this)
# --------------------------------------------------------------------------- #
MEDIA_EXTS = {
    # video
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".mpg",
    ".mpeg", ".m2ts", ".mts", ".prores",
    # images / photos
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic",
    ".raw", ".cr2", ".nef", ".arw", ".dng",
    # audio
    ".mp3", ".wav", ".aif", ".aiff", ".flac", ".m4a", ".aac", ".ogg",
    # design / creative
    ".psd", ".ai", ".indd", ".eps", ".svg", ".sketch", ".fig", ".xd", ".aep",
    ".prproj", ".fcpx", ".c4d", ".blend",
}
BUSINESS_EXTS = {
    ".doc", ".docx", ".pdf", ".xls", ".xlsx", ".ppt", ".pptx", ".csv",
    ".txt", ".rtf", ".odt", ".ods", ".odp", ".pages", ".numbers", ".key",
    ".msg", ".eml", ".one",
}

# Trivial / junk (system noise, caches) -> ROT "trivial"
TRIVIAL_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "icon\r", ".localized"}
TRIVIAL_EXTS = {".tmp", ".temp", ".log", ".bak", ".old", ".cache", ".part",
                ".crdownload", ".~lock"}

# Curly apostrophe used by Dropbox transfer folders, plus straight one.
_CURLY = "\u2019"
_TRANSFER_RE = re.compile(r"[^\\/]+[\u2019'](?:s)? files(?=[\\/]|$)", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")  # CJK ideographs
_PAREN_RE = re.compile(r" \(\d+\)(\.[^.]+)?$")
_TS_RE = re.compile(r"-at-\d[\d\-t:_.z]*", re.I)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class RulesConfig:
    """Everything the user can tune. Persisted to rules_config.json. All lists
    empty-safe: an empty list simply means 'no rule of this kind yet'."""

    # Keeper preference when a group spans both folders.
    prefer_folder: str = "organized"          # organized | snapshot

    # Transferred-employee (terminated) detection.
    transfer_root: str = r"E:\Dropbox-Snapshot\Sherman Rowland"
    # Detected automatically by the "'s files" pattern; explicit names here force it.
    transferred_people: list[str] = field(default_factory=list)

    # China routing signals (any match on the path -> SharePoint China).
    china_path_signals: list[str] = field(default_factory=lambda: ["HPL China"])
    china_use_cjk: bool = True                # treat CJK chars in path as China

    # Obsolete rule: business/media files older than this AND redundant/low-value.
    obsolete_years: float = 4.0
    # File types considered low-value when old + duplicated (e.g. loose graphics).
    low_value_exts: list[str] = field(default_factory=lambda: [
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".psd", ".ai", ".eps"])

    # Free-form principle notes captured from the chat (audit trail, not executed).
    principle_notes: list[str] = field(default_factory=list)

    @staticmethod
    def load() -> "RulesConfig":
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = RulesConfig()
            for k, v in data.items():
                if hasattr(base, k):
                    setattr(base, k, v)
            return base
        except (OSError, json.JSONDecodeError):
            return RulesConfig()

    def save(self) -> None:
        os.makedirs(_STORE, exist_ok=True)
        tmp = _CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)
        os.replace(tmp, _CONFIG_PATH)


def get_config() -> dict:
    return asdict(RulesConfig.load())


def update_config(patch: dict) -> dict:
    cfg = RulesConfig.load()
    for k, v in patch.items():
        if hasattr(cfg, k) and v is not None:
            setattr(cfg, k, v)
    cfg.save()
    return asdict(cfg)


# --------------------------------------------------------------------------- #
# Per-file classifiers (pure functions)
# --------------------------------------------------------------------------- #
def _norm(p: str) -> str:
    return p.replace("\\", "/")


def file_type(path: str) -> str:
    """media | business | trivial | other"""
    name = os.path.basename(path).lower()
    ext = os.path.splitext(name)[1]
    if name in TRIVIAL_NAMES or ext in TRIVIAL_EXTS:
        return "trivial"
    if ext in MEDIA_EXTS:
        return "media"
    if ext in BUSINESS_EXTS:
        return "business"
    return "other"


def is_transferred(path: str, cfg: RulesConfig) -> bool:
    """True if the path sits inside any '<Person>'s files' transferred region,
    or under an explicitly configured transferred person's folder."""
    if _TRANSFER_RE.search(path):
        return True
    for person in cfg.transferred_people:
        if person and person.lower() in path.lower():
            return True
    return False


def route_for(path: str, ftype: str, cfg: RulesConfig) -> str:
    """cpms | sharepoint_china | sharepoint_us | unrouted

    Media -> CPMS regardless of geography. Business docs split US/China by path
    signal. 'other'/trivial files stay unrouted (decided later / dropped)."""
    if ftype == "media":
        return "cpms"
    if ftype == "business":
        low = _norm(path).lower()
        for sig in cfg.china_path_signals:
            if sig and sig.lower() in low:
                return "sharepoint_china"
        if cfg.china_use_cjk and _CJK_RE.search(path):
            return "sharepoint_china"
        return "sharepoint_us"
    return "unrouted"


def _clean_score(name: str) -> int:
    s = 0
    if _PAREN_RE.search(name):
        s += 10
    if _TS_RE.search(name):
        s += 5
    return s


def _keeper_key(copy: dict, cfg: RulesConfig):
    """Sort key: lower is better (chosen as keeper).
    Priority: (1) NOT transferred, (2) preferred folder, (3) shortest path,
    (4) cleanest name, (5) longest/most-descriptive name."""
    name = os.path.basename(copy["path"])
    transferred = 1 if copy["transferred"] else 0
    folder_rank = 0 if copy["folder"] == cfg.prefer_folder else 1
    return (transferred, folder_rank, len(_norm(copy["path"])),
            _clean_score(name), -len(name))


# --------------------------------------------------------------------------- #
# Index loading + grouping
# --------------------------------------------------------------------------- #
def _load_index(db_path: str, folder_tag: str, cfg: RulesConfig, into: dict):
    if not os.path.exists(db_path):
        return
    c = sqlite3.connect(db_path)
    try:
        for path, md5, size, mtime in c.execute(
                "SELECT path, md5, size, mtime FROM files WHERE md5<>''"):
            into.setdefault(md5, []).append({
                "path": path,
                "size": size or 0,
                "mtime": mtime or 0,
                "folder": folder_tag,
                "transferred": is_transferred(path, cfg),
            })
    finally:
        c.close()


def _all_copies(cfg: RulesConfig) -> dict:
    by_md5: dict = {}
    _load_index(mi.open_index(ORGANIZED_ROOT).db_path, "organized", cfg, by_md5)
    _load_index(mi.open_index(SNAPSHOT_ROOT).db_path, "snapshot", cfg, by_md5)
    return by_md5


# --------------------------------------------------------------------------- #
# Full classification (dry run)
# --------------------------------------------------------------------------- #
def classify(cfg: Optional[RulesConfig] = None, examples: int = 15) -> dict:
    """Run the whole rules engine over both indexes. Returns dry-run counts by
    dedup verdict, ROT bucket, and route, plus example rows. No file changes."""
    cfg = cfg or RulesConfig.load()
    started = time.time()
    by_md5 = _all_copies(cfg)

    now = time.time()
    obsolete_cutoff = now - cfg.obsolete_years * 365.25 * 86400
    low_value = set(cfg.low_value_exts)

    verdict_counts = {"keep": 0, "redundant": 0}
    rot_counts = {"keep": 0, "redundant": 0, "obsolete": 0, "trivial": 0}
    route_counts = {"cpms": 0, "sharepoint_us": 0, "sharepoint_china": 0,
                    "unrouted": 0}
    # bytes reclaimable = size of everything marked redundant
    reclaim_bytes = 0
    transferred_redundant = {"files": 0, "bytes": 0}
    total_files = 0
    examples_out: list[dict] = []

    for md5, copies in by_md5.items():
        total_files += len(copies)
        # pick keeper for this md5 group (single keeper; leave-one invariant)
        keeper = min(copies, key=lambda c: _keeper_key(c, cfg)) if len(copies) > 1 else copies[0]

        for c in copies:
            path = c["path"]
            ftype = file_type(path)
            route = route_for(path, ftype, cfg)
            route_counts[route] += 1

            is_keeper = (path == keeper["path"])
            if is_keeper or len(copies) == 1:
                verdict = "keep"
            else:
                verdict = "redundant"

            # ROT bucket
            if ftype == "trivial":
                rot = "trivial"
            elif verdict == "redundant":
                rot = "redundant"
                reclaim_bytes += c["size"]
                if c["transferred"]:
                    transferred_redundant["files"] += 1
                    transferred_redundant["bytes"] += c["size"]
            elif (c["mtime"] and c["mtime"] < obsolete_cutoff
                  and os.path.splitext(path)[1].lower() in low_value
                  and len(copies) > 1):
                # old, low-value, and part of a duplicated set -> likely drop
                rot = "obsolete"
            else:
                rot = "keep"

            verdict_counts[verdict] += 1
            rot_counts[rot] += 1

            if len(examples_out) < examples and verdict == "redundant" and c["transferred"]:
                examples_out.append({
                    "redundant": path, "keeper": keeper["path"],
                    "route": route, "rot": rot,
                    "size_mb": round(c["size"] / 1024 / 1024, 1),
                    "reason": "transferred copy; identical kept elsewhere",
                })

    return {
        "summary": {
            "total_files": total_files,
            "unique_md5": len(by_md5),
            "verdict": verdict_counts,
            "rot": rot_counts,
            "route": route_counts,
            "reclaimable_bytes": reclaim_bytes,
            "reclaimable_gb": round(reclaim_bytes / 1e9, 1),
            "transferred_redundant_files": transferred_redundant["files"],
            "transferred_redundant_gb": round(transferred_redundant["bytes"] / 1e9, 1),
            "prefer_folder": cfg.prefer_folder,
            "elapsed_seconds": round(time.time() - started, 1),
        },
        "examples": examples_out,
    }
