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

# ---- session keeper rule (guidance-driven) --------------------------------
# The keeper is chosen by, in order:
#   1. prefer_folder  (organized | snapshot | none)      [base]
#   2. prefer_paths   (ordered path substrings; earlier = higher priority)
#   3. avoid_paths    (path substrings that should LOSE if possible, e.g.
#                      transferred-employee "'s files" folders)
#   4. tiebreakers    (ordered list from: cleanest_name, shortest_path,
#                      longest_path, newest, oldest, longest_name)
# When two copies tie on ALL of the above, the group is AMBIGUOUS -> eligible
# for AI per-file resolution. Persisted per machine so it survives restarts.
_RULE_PATH = os.path.join(_STORE, "keeper_rule.json")

_DEFAULT_RULE = {
    "prefer_folder": "organized",
    "prefer_paths": [],
    "avoid_paths": [],
    "tiebreakers": ["cleanest_name", "shortest_path", "longest_name"],
}

# per-md5 AI overrides: md5 -> chosen keeper path (set by resolve-ambiguous)
_ai_overrides: dict = {}


def get_rule() -> dict:
    try:
        with open(_RULE_PATH, "r", encoding="utf-8") as f:
            r = dict(_DEFAULT_RULE)
            r.update(json.load(f))
            return r
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_RULE)


def set_rule(patch: dict) -> dict:
    r = get_rule()
    for k in ("prefer_folder", "prefer_paths", "avoid_paths", "tiebreakers"):
        if k in patch and patch[k] is not None:
            r[k] = patch[k]
    os.makedirs(_STORE, exist_ok=True)
    tmp = _RULE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _RULE_PATH)
    clear_cache()      # keepers change -> rebuild groups
    return r


def set_ai_override(md5: str, keeper_path: str):
    _ai_overrides[md5] = keeper_path
    clear_cache()


def clear_ai_overrides():
    _ai_overrides.clear()
    clear_cache()


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


# ---- system junk (never real content; excluded from dedup, purged separately) --
_JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".localized", ".spotlight-v100"}
_JUNK_EXTS = {".tmp", ".temp", ".download", ".crdownload", ".part"}


def is_junk(path: str) -> bool:
    """True for OS/sidecar/temp junk that isn't real content:
      - macOS AppleDouble sidecars ('._<name>')
      - .DS_Store / Thumbs.db / desktop.ini / .localized
      - .tmp / .temp / .download / .crdownload / .part
    These get filtered OUT of dedup grouping and purged on their own."""
    base = os.path.basename(path.replace("\\", "/"))
    low = base.lower()
    if base.startswith("._"):
        return True
    if low in _JUNK_NAMES:
        return True
    ext = os.path.splitext(low)[1]
    return ext in _JUNK_EXTS


def _load_index(db_path: str, tag: str, into: dict):
    if not os.path.exists(db_path):
        return
    c = sqlite3.connect(db_path)
    try:
        for path, md5, size, mtime in c.execute(
                "SELECT path, md5, size, mtime FROM files WHERE md5<>''"):
            if is_junk(path):
                continue   # never dedup system junk — it pollutes groups
            into.setdefault(md5, []).append(
                {"path": path, "size": size or 0, "mtime": mtime or 0,
                 "folder": tag})
    finally:
        c.close()


def _prefer_rank(path: str, prefer_paths: list) -> int:
    """Index of the first prefer_paths substring that matches (lower = higher
    priority). Returns a large number if none match."""
    low = _norm(path).lower()
    for i, sub in enumerate(prefer_paths):
        if sub and sub.lower() in low:
            return i
    return len(prefer_paths) + 1


def _avoid_hit(path: str, avoid_paths: list) -> int:
    low = _norm(path).lower()
    return 1 if any(sub and sub.lower() in low for sub in avoid_paths) else 0


def _tiebreak_value(c: dict, tb: str):
    """Sort value for one tiebreaker (lower sorts first = preferred keeper)."""
    name = os.path.basename(c["path"])
    if tb == "cleanest_name":
        return _clean_score(name)
    if tb == "shortest_path":
        return len(_norm(c["path"]))
    if tb == "longest_path":
        return -len(_norm(c["path"]))
    if tb == "longest_name":
        return -len(name)
    if tb == "shortest_name":
        return len(name)
    if tb == "newest":
        return -(c.get("mtime") or 0)
    if tb == "oldest":
        return (c.get("mtime") or 0)
    return 0


def _keeper_sort_key(c: dict, rule: dict):
    """Full ordered sort key: prefer_folder, prefer_paths rank, avoid penalty,
    then each configured tiebreaker in order."""
    pf = rule.get("prefer_folder", "none")
    folder_rank = 0 if (pf in ("organized", "snapshot") and c["folder"] == pf) else 1
    key = [folder_rank,
           _prefer_rank(c["path"], rule.get("prefer_paths", [])),
           _avoid_hit(c["path"], rule.get("avoid_paths", []))]
    for tb in rule.get("tiebreakers", []):
        key.append(_tiebreak_value(c, tb))
    return tuple(key)


def _pick_keeper(copies: list, prefer_folder: str, rule: dict = None):
    """Choose the single keeper for a group using the session keeper rule.
    Returns (keeper_dict, ambiguous_bool). ambiguous=True when >1 copy shares the
    exact same top sort key AND no AI override resolved it — those go to AI."""
    if rule is None:
        rule = dict(get_rule())
        rule["prefer_folder"] = prefer_folder  # caller's prefer_folder wins as base

    ranked = sorted(copies, key=lambda c: _keeper_sort_key(c, rule))
    top = ranked[0]
    top_key = _keeper_sort_key(top, rule)
    tied = [c for c in ranked if _keeper_sort_key(c, rule) == top_key]
    ambiguous = len(tied) > 1
    return top, ambiguous


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

    # Load the session keeper rule once; caller's prefer_folder is the base.
    rule = dict(get_rule())
    rule["prefer_folder"] = prefer_folder

    def choose_keeper(md5: str, pool: list):
        """Apply AI override if present, else the rule. Returns (keeper, ambiguous,
        ai_resolved)."""
        ov = _ai_overrides.get(md5)
        if ov:
            match = next((c for c in pool if c["path"] == ov), None)
            if match:
                return match, False, True
        k, amb = _pick_keeper(pool, prefer_folder, rule)
        return k, amb, False

    groups = []
    redundant_files = 0
    reclaimable = 0
    ambiguous_groups = 0
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
            # keeper is chosen among ORGANIZED copies only; Organized is NEVER removed
            keeper, amb, ai = choose_keeper(md5, organized_copies)
            redundant = snapshot_copies   # remove ALL snapshot copies (Organized survives)
            gb = sum(c["size"] for c in redundant)
            groups.append({
                "md5": md5, "keeper": keeper, "redundant": redundant,
                "cross_folder": True, "reclaim": gb,
                "ambiguous": amb, "ai_resolved": ai,
            })
            redundant_files += len(redundant)
            reclaimable += gb
            if amb:
                ambiguous_groups += 1
            cross_groups += 1
            cross_files += len(redundant)
            cross_bytes += gb
            continue

        if cross_only and not is_cross:
            continue   # skip within-folder-only groups entirely
        keeper, amb, ai = choose_keeper(md5, copies)
        redundant = [c for c in copies if c["path"] != keeper["path"]]
        if not redundant:
            continue
        gb = sum(c["size"] for c in redundant)
        groups.append({
            "md5": md5, "keeper": keeper, "redundant": redundant,
            "cross_folder": is_cross, "reclaim": gb,
            "ambiguous": amb, "ai_resolved": ai,
        })
        redundant_files += len(redundant)
        reclaimable += gb
        if amb:
            ambiguous_groups += 1
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
            "ambiguous_groups": ambiguous_groups,
            "rule": rule,
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
            "ambiguous": g.get("ambiguous", False),
            "ai_resolved": g.get("ai_resolved", False),
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


# ------------------------------------------------ AI resolution of ambiguities
# For groups the deterministic rule can't decide (a true tie), ask Bedrock to
# pick the keeper from the candidate paths, guided by the user's stated intent.
# Batched (many groups per call), advisory, records overrides via set_ai_override.

def ambiguous_groups(prefer_folder: str = "organized", cross_only: bool = False,
                     snapshot_only: bool = False, limit: int = 200) -> list:
    """Return up to `limit` groups the rule marked ambiguous, with their candidate
    keeper paths (the pool the rule tied on)."""
    data = _get_cached_groups(prefer_folder, cross_only, snapshot_only)
    out = []
    for g in data["groups"]:
        if not g.get("ambiguous") or g.get("ai_resolved"):
            continue
        # candidates = keeper + any redundant in the SAME pool it chose from.
        # For snapshot_only the pool is organized copies; otherwise all copies.
        cands = [g["keeper"]["path"]] + [r["path"] for r in g["redundant"]]
        out.append({"md5": g["md5"], "candidates": cands})
        if len(out) >= limit:
            break
    return out


def resolve_ambiguous(guidance: str = "", prefer_folder: str = "organized",
                      cross_only: bool = False, snapshot_only: bool = False,
                      max_groups: int = 200, batch: int = 20) -> dict:
    """Send ambiguous groups to Bedrock in batches; it picks the keeper per group
    given the user's guidance. Records AI overrides. Returns counts + examples."""
    from . import settings_store
    from .bedrock_client import get_bedrock_client

    amb = ambiguous_groups(prefer_folder, cross_only, snapshot_only, limit=max_groups)
    if not amb:
        return {"ambiguous": 0, "resolved": 0, "examples": []}

    model_id = settings_store.get_model("dedup_advisor")
    client = get_bedrock_client()
    resolved = 0
    examples = []

    sys_prompt = (
        "You choose which ONE duplicate copy to KEEP. Given the user's guidance and "
        "a list of candidate file paths (identical content, different locations), "
        "return the index of the path to keep and a short reason. Consider folder "
        "meaning: prefer authoritative/final/organized locations; avoid backup, "
        "'stuff to sort', and terminated-employee \"'s files\" folders unless the "
        "guidance says otherwise. Respond ONLY as JSON: a list of "
        '{"i": <group index>, "keep": <candidate index>, "reason": "<short>"}.')

    for start in range(0, len(amb), batch):
        chunk = amb[start:start + batch]
        lines = []
        for gi, g in enumerate(chunk):
            cand_lines = "\n".join(f"    [{ci}] {p}" for ci, p in enumerate(g["candidates"]))
            lines.append(f"  group {gi}:\n{cand_lines}")
        user_msg = (f"Guidance: {guidance or '(none — use folder meaning)'}\n\n"
                    f"Groups:\n" + "\n".join(lines))
        try:
            resp = client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": user_msg}]}],
                system=[{"text": sys_prompt}],
                inferenceConfig={"maxTokens": 2048, "temperature": 0.1})
            txt = ""
            for b in resp["output"]["message"]["content"]:
                if "text" in b:
                    txt += b["text"]
            txt = txt.strip()
            if txt.startswith("```"):
                txt = txt.split("```", 2)[1]
                if txt.startswith("json"):
                    txt = txt[4:]
                txt = txt.strip("`").strip()
            picks = json.loads(txt)
        except Exception:
            continue
        for pick in picks:
            try:
                gi = int(pick["i"]); ci = int(pick["keep"])
                g = chunk[gi]
                keeper_path = g["candidates"][ci]
                _ai_overrides[g["md5"]] = keeper_path
                resolved += 1
                if len(examples) < 10:
                    examples.append({"keeper": keeper_path,
                                     "reason": pick.get("reason", "")})
            except (KeyError, IndexError, ValueError, TypeError):
                continue

    clear_cache()   # overrides change keepers
    return {"ambiguous": len(amb), "resolved": resolved, "examples": examples}


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
                     "manifest_file": None, "cancel": False}
    threading.Thread(target=_purge_worker,
                     args=(job_id, prefer_folder, cross_only, snapshot_only),
                     daemon=True).start()
    return job_id


def get_job(job_id: str):
    return _jobs.get(job_id)


def cancel_job(job_id: str) -> bool:
    """Signal a running purge to stop after the current file. It halts cleanly;
    everything already moved stays recorded in the manifest and is undoable."""
    job = _jobs.get(job_id)
    if not job:
        return False
    job["cancel"] = True
    return True


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
        cancelled = False
        for g in data["groups"]:
            if job.get("cancel"):
                cancelled = True
                break
            keeper_path = g["keeper"]["path"]
            # safety: only remove redundant if the keeper still exists on disk
            if not os.path.isfile(keeper_path):
                continue
            for r in g["redundant"]:
                if job.get("cancel"):
                    cancelled = True
                    break
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
            if cancelled:
                break
        flush()
        clear_cache()   # files moved — stale group cache no longer valid
        if cancelled:
            job["status"] = "cancelled"; job["phase"] = "cancelled"
        else:
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


# ============================================================ system-junk purge
# ._ AppleDouble sidecars, .DS_Store, Thumbs.db, desktop.ini, temp/partial files.
# These are OS noise, never real content, so we quarantine ALL of them (no
# keeper needed). Runs across BOTH folders. Reversible; cancellable.

_junk_jobs: dict = {}


def _scan_junk() -> list:
    """Return every junk file path across both indexes as
    [{"path", "folder", "size"}]. Junk is defined by is_junk()."""
    out = []
    for tag, root in (("organized", ORGANIZED_ROOT), ("snapshot", SNAPSHOT_ROOT)):
        db = mi.open_index(root).db_path
        if not os.path.exists(db):
            continue
        c = sqlite3.connect(db)
        try:
            for path, size in c.execute("SELECT path, size FROM files"):
                if is_junk(path):
                    out.append({"path": path, "folder": tag, "size": size or 0})
        finally:
            c.close()
    return out


def junk_preview(examples: int = 15) -> dict:
    """DRY RUN — count system-junk files across both folders, with examples."""
    items = _scan_junk()
    by_kind = {"appledouble": 0, "ds_store": 0, "thumbs": 0, "desktop_ini": 0,
               "temp": 0, "other": 0}
    total_bytes = 0
    for it in items:
        total_bytes += it["size"]
        base = os.path.basename(it["path"].replace("\\", "/")).lower()
        if base.startswith("._"):
            by_kind["appledouble"] += 1
        elif base == ".ds_store":
            by_kind["ds_store"] += 1
        elif base == "thumbs.db":
            by_kind["thumbs"] += 1
        elif base == "desktop.ini":
            by_kind["desktop_ini"] += 1
        elif os.path.splitext(base)[1] in _JUNK_EXTS:
            by_kind["temp"] += 1
        else:
            by_kind["other"] += 1
    return {
        "total": len(items),
        "total_bytes": total_bytes,
        "by_kind": by_kind,
        "examples": [it["path"] for it in items[:examples]],
    }


def junk_purge(confirm: bool = False) -> str:
    """Start a background job that quarantines ALL system-junk files across both
    folders. Cancellable, reversible via manifest. Returns job id."""
    if not confirm:
        raise ValueError("confirm=true required")
    job_id = f"junk_{int(time.time())}"
    _junk_jobs[job_id] = {"status": "running", "phase": "scanning",
                          "quarantined": 0, "reclaimed_bytes": 0, "errors": 0,
                          "cancel": False, "manifest_file": None,
                          "started_at": time.time()}
    threading.Thread(target=_junk_worker, args=(job_id,), daemon=True).start()
    return job_id


def get_junk_job(job_id: str):
    return _junk_jobs.get(job_id)


def cancel_junk_job(job_id: str) -> bool:
    job = _junk_jobs.get(job_id)
    if not job:
        return False
    job["cancel"] = True
    return True


def _junk_worker(job_id: str):
    job = _junk_jobs[job_id]
    try:
        os.makedirs(_MANIFEST_DIR, exist_ok=True)
        items = _scan_junk()
        job["total"] = len(items)
        job["phase"] = "quarantining"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        q_roots = {
            "organized": os.path.join(ORGANIZED_ROOT, f"_JunkQuarantine_{stamp}"),
            "snapshot": os.path.join(SNAPSHOT_ROOT, f"_JunkQuarantine_{stamp}"),
        }
        mpath = os.path.join(_MANIFEST_DIR, f"junk_{int(time.time()*1000)}.json")
        manifest = {"action": "junk_purge", "at": time.time(), "entries": []}
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
        cancelled = False
        for it in items:
            if job.get("cancel"):
                cancelled = True
                break
            src = it["path"]
            if not os.path.isfile(src):
                continue
            root = _root_for(it["folder"])
            q_root = q_roots[it["folder"]]
            try:
                rel = os.path.relpath(os.path.abspath(src), os.path.abspath(root))
                dest = _collision_safe(os.path.join(q_root, rel))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.rename(src, dest)
                manifest["entries"].append({"from": os.path.abspath(src), "to": dest})
                job["quarantined"] += 1
                job["reclaimed_bytes"] += it["size"]
                since += 1
                if since >= 100:
                    flush(); since = 0
            except OSError:
                job["errors"] += 1
        flush()
        job["status"] = "cancelled" if cancelled else "completed"
        job["phase"] = "cancelled" if cancelled else "complete"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)


def junk_undo(manifest_file: str, confirm: bool = False) -> dict:
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
    return {"restored": restored, "errors": errors}
