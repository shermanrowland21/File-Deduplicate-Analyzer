"""
Deterministic dedup RESOLVER (Phase A) — hash-certain, structure-aware.

Turns a completed scan's duplicate groups into safe, bulk, directory-level
decisions WITHOUT the user drilling group-by-group, and WITHOUT ever deleting
files that are duplicated because they are legitimate project/website internals.

Core ideas (informed by dupeGuru's reference-folder + prioritization model):
  - SOURCE OF TRUTH: user picks one or more directories/prefixes to KEEP. Any
    duplicate that also exists elsewhere is a candidate for removal — but only
    its copies OUTSIDE the source of truth, and only when safe.
  - CROSS-SOURCE REDUNDANCY (safe): a group whose copies span more than one
    top-level SOURCE (e.g. Dropbox-Snapshot AND Google Drive) is a real backup
    duplicate. Removing the non-source-of-truth copy is safe.
  - PROJECT-INTERNAL / STRUCTURAL (protected): a group whose copies are shared
    framework/library/website files (wwwroot, Views, node_modules, .git, vendor,
    Adobe Premiere _prfiles, etc.) OR that are duplicated WITHIN the same project
    tree. Deleting these can break projects, so they are PROTECTED by default and
    never auto-marked for removal.

NEVER hard-deletes: execution moves files to a dated quarantine (reversible).
"""
import os
import re
import json
import time
import shutil
from typing import Optional

from .file_scanner import get_duplicates, human_readable_size

STORE_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer")
SCANS_DIR = os.path.join(STORE_DIR, "scans")

# Path segments that signal a file is a PROJECT/FRAMEWORK internal — duplicated
# by design across projects; deleting copies can break things. Case-insensitive.
_STRUCTURAL_SEGMENTS = {
    "wwwroot", "views", "node_modules", ".git", "vendor", "bin", "obj",
    "packages", "dist", "build", ".next", "silverstripe-cache", "cache",
    "sapphire", "_prfiles", "adobe premiere pro auto-save",
    "adobe premiere pro video previews", "adobe premiere pro audio previews",
    ".vs", "__pycache__", "site-packages", "lib", "libs", "assets",
    "framework", "themes", "modules", "plugins",
}
# Structural filename patterns (framework/template/scaffold files)
_STRUCTURAL_FILE_RE = re.compile(
    r"\.(cshtml|aspx|ascx|master|config|dll|so|dylib|min\.js|min\.css)$", re.I)


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def _path_segments(path: str) -> list[str]:
    return [s for s in _norm(path).lower().split("/") if s]


def _is_structural_path(path: str) -> bool:
    segs = set(_path_segments(path))
    if segs & _STRUCTURAL_SEGMENTS:
        return True
    if _STRUCTURAL_FILE_RE.search(path):
        return True
    return False


def _top_source(f: dict) -> str:
    """The top-level source label the scanner tagged (e.g. Dropbox-Snapshot)."""
    return f.get("source") or f.get("source_path") or ""


def _project_root_signature(path: str) -> Optional[str]:
    """
    If the path is inside a recognizable project/website, return a signature of
    that project root (so we can tell 'same file duplicated within ONE project'
    from 'same file across DIFFERENT projects'). Heuristic: the path element just
    before the first structural segment, joined with that segment.
    e.g. .../old websites/example.com/wwwroot/... -> "example.com/wwwroot"
    """
    segs = _path_segments(path)
    for i, s in enumerate(segs):
        if s in _STRUCTURAL_SEGMENTS:
            root = segs[i - 1] if i > 0 else s
            return f"{root}/{s}"
    return None


def classify_group(group: dict) -> dict:
    """
    Classify a single duplicate group. Returns the group augmented with:
      category: 'cross_source_redundant' | 'structural_internal' | 'single_source'
      sources: distinct top-level sources spanned
      structural: bool (any copy looks like a project/framework internal)
      reason: human explanation
    """
    files = group.get("files", [])
    sources = sorted({_top_source(f) for f in files if _top_source(f)})
    any_structural = any(_is_structural_path(f["path"]) for f in files)

    # project signatures: if copies live under DIFFERENT project roots, they are
    # independent project internals (each project needs its own copy).
    proj_sigs = {_project_root_signature(f["path"]) for f in files}
    proj_sigs.discard(None)
    multi_project_structural = any_structural and len(proj_sigs) > 1

    if any_structural or multi_project_structural:
        category = "structural_internal"
        reason = ("Copies are project/framework/website internals "
                  f"({', '.join(sorted(s for s in proj_sigs)[:3]) or 'framework files'}). "
                  "Each project likely needs its own copy — protected from bulk removal.")
    elif len(sources) > 1:
        category = "cross_source_redundant"
        reason = (f"Same file exists across {len(sources)} sources "
                  f"({', '.join(sources)}) — a real backup duplicate.")
    else:
        category = "single_source"
        reason = ("All copies are within a single source — likely intentional "
                  "local copies; review before removing.")

    return {
        **group,
        "category": category,
        "sources": sources,
        "structural": any_structural,
        "project_roots": sorted(s for s in proj_sigs),
        "reason": reason,
    }


def _under_any(path: str, roots: list[str]) -> bool:
    p = _norm(path).lower().rstrip("/")
    for r in roots:
        rl = _norm(r).lower().rstrip("/")
        if rl and (p == rl or p.startswith(rl + "/")):
            return True
    return False


# Junk files that inflate duplicate counts but aren't real data — excluded from
# review + removal planning entirely (filtered before grouping is presented).
_JUNK_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", "icon\r", ".picasa.ini"}


def is_junk(path: str) -> bool:
    name = os.path.basename(_norm(path)).lower()
    return name in _JUNK_NAMES


def _tiebreak_key(f: dict, tiebreak: str):
    """Sort key so the KEEPER (best copy) sorts FIRST (smallest key). Mirrors
    dupeGuru/Czkawka criteria."""
    path = f.get("path", "")
    if tiebreak == "newest":
        return -(f.get("mtime") or _mtime_of(f))
    if tiebreak == "oldest":
        return (f.get("mtime") or _mtime_of(f))
    if tiebreak == "longest_name":
        return -len(os.path.basename(path))
    if tiebreak == "shortest_path":
        return len(path)
    if tiebreak == "smallest":
        return f.get("size", 0)
    # default: biggest
    return -f.get("size", 0)


def _mtime_of(f: dict) -> float:
    # modified_time is an ISO string in enriched records; fall back to 0
    mt = f.get("modified_time") or ""
    try:
        from datetime import datetime
        return datetime.fromisoformat(mt).timestamp() if mt else 0.0
    except Exception:
        return 0.0


def _pick_keeper(files: list[dict], prefer_folders: list[str], tiebreak: str) -> dict:
    """Choose the keeper for a group by a RULE STACK (dupeGuru model):
    1) prefer any copy under a preferred/keep folder, then
    2) the tiebreaker (biggest by default). Applied consistently in bulk."""
    def key(f):
        under_pref = 0 if (prefer_folders and _under_any(f["path"], prefer_folders)) else 1
        return (under_pref, _tiebreak_key(f, tiebreak))
    return min(files, key=key)


def _plan_removals_for_group(g: dict, source_of_truth: list[str],
                             within_source: bool, tiebreak: str = "biggest"
                             ) -> tuple[Optional[dict], list[dict]]:
    """Given a classified group, return (keeper, [removals]) applying safety rules.
    - structural_internal: never removed (protected).
    - junk files (desktop.ini/.DS_Store/Thumbs.db): dropped entirely.
    - keeper chosen by rule stack: prefer source_of_truth folder, then tiebreak.
    - leave-one-per-group invariant: the keeper is NEVER a removal (a survivor
      always remains).
    - cross_source_redundant: remove OTHER-source copies.
    - single_source: removable only when within_source=True.
    """
    cat = g["category"]
    if cat == "structural_internal":
        return None, []
    if cat == "single_source" and not within_source:
        return None, []

    # drop junk from consideration
    files = [f for f in g["files"] if not is_junk(f["path"])]
    if len(files) < 2:
        return None, []   # nothing to dedupe once junk is removed

    keeper = _pick_keeper(files, source_of_truth, tiebreak)

    removals = []
    for f in files:
        if f["path"] == keeper["path"]:
            continue
        # Never remove a structural/framework file, even within one source.
        if _is_structural_path(f["path"]):
            continue
        # For CROSS-source, never remove a copy that IS under the source of truth.
        # For WITHIN-source, we DO remove other in-source copies (that's the point),
        # but still never the keeper (handled above).
        if cat == "cross_source_redundant" and source_of_truth and _under_any(f["path"], source_of_truth):
            continue
        removals.append({
            "path": f["path"], "size": f["size"], "source": _top_source(f),
            "keep": keeper["path"], "keep_source": _top_source(keeper),
            "hash": g.get("hash"), "category": cat,
        })
    return keeper, removals


def analyze(scan_id: str, source_of_truth: Optional[list[str]] = None,
            within_source: bool = False, tiebreak: str = "biggest") -> Optional[dict]:
    """
    Produce a resolver analysis for a scan:
      - classify every duplicate group
      - roll up counts/space by category and by source
      - if source_of_truth roots given, compute a SAFE removal plan:
          for each cross_source_redundant group, keep the copy under the source
          of truth (or the biggest if none), mark OTHER-source copies for removal.
          structural_internal + single_source groups are NEVER auto-marked.
    Returns the analysis dict (does not modify anything on disk).
    """
    dup = get_duplicates(scan_id)
    if dup is None:
        return None
    source_of_truth = source_of_truth or []

    classified = [classify_group(g) for g in dup.get("groups", [])]

    by_cat: dict = {}
    by_source_removable: dict = {}
    plan_remove: list[dict] = []
    protected_count = 0
    protected_space = 0

    for g in classified:
        cat = g["category"]
        c = by_cat.setdefault(cat, {"groups": 0, "files": 0, "wasted": 0})
        c["groups"] += 1
        c["files"] += len(g["files"])
        c["wasted"] += g.get("total_wasted_space", 0)

        keeper, removals = _plan_removals_for_group(g, source_of_truth, within_source, tiebreak)
        if not removals:
            # nothing removable in this group → its extra copies are protected/kept
            protected_count += max(0, len(g["files"]) - 1)
            protected_space += g.get("total_wasted_space", 0)
            continue
        for r in removals:
            plan_remove.append(r)
            s = by_source_removable.setdefault(r["source"], {"files": 0, "space": 0})
            s["files"] += 1
            s["space"] += r["size"]

    remove_space = sum(x["size"] for x in plan_remove)
    return {
        "scan_id": scan_id,
        "source_of_truth": source_of_truth,
        "within_source": within_source,
        "total_groups": len(classified),
        "by_category": {
            k: {**v, "wasted_human": human_readable_size(v["wasted"])}
            for k, v in by_cat.items()
        },
        "removable_by_source": {
            k: {**v, "space_human": human_readable_size(v["space"])}
            for k, v in by_source_removable.items()
        },
        "plan_remove_count": len(plan_remove),
        "plan_remove_space": remove_space,
        "plan_remove_space_human": human_readable_size(remove_space),
        "protected_files": protected_count,
        "protected_space_human": human_readable_size(protected_space),
        # sample of what would be removed (first 100) for UI preview
        "plan_sample": plan_remove[:100],
    }


def build_review(scan_id: str, source_of_truth: Optional[list[str]] = None,
                 within_source: bool = False, limit: int = 300,
                 tiebreak: str = "biggest") -> Optional[dict]:
    """Build the card-based review payload for the redesigned Resolver UI.

    Returns a summary + per-group cards (biggest-waste first, capped) where each
    card has a KEEPER and copy rows flagged removable / smart_selected / structural,
    plus a separate count of PROTECTED groups (structural or nothing-removable).
    This is what the outcome-first UI renders.
    """
    dup = get_duplicates(scan_id, limit=limit)
    if dup is None:
        return None
    source_of_truth = source_of_truth or []

    cards = []
    protected_groups = 0
    protected_files = 0
    auto_selected = 0
    reclaimable = 0
    total_groups = 0

    for g in dup.get("groups", []):
        cg = classify_group(g)
        keeper, removals = _plan_removals_for_group(cg, source_of_truth, within_source, tiebreak)
        removable_paths = {r["path"] for r in removals}
        # non-junk copies only (junk excluded entirely from the review)
        copies = [f for f in cg["files"] if not is_junk(f["path"])]
        if len(copies) < 2:
            continue   # was only junk / single real file
        total_groups += 1

        if not removals:
            protected_groups += 1
            protected_files += max(0, len(cg["files"]) - 1)
            # still emit a card so the UI can show it in the protected section
            cards.append({
                "hash": cg.get("hash"),
                "category": cg["category"],
                "reason": cg.get("reason", ""),
                "structural": cg.get("structural", False),
                "protected": True,
                "wasted_space": cg.get("total_wasted_space", 0),
                "wasted_human": human_readable_size(cg.get("total_wasted_space", 0)),
                "keeper": (keeper or copies[0])["path"] if copies else None,
                "copies": [_review_copy(f, keeper, removable_paths, cg) for f in copies],
            })
            continue

        # smart-select: every removable copy is a safe pick by default
        auto_selected += len(removals)
        reclaimable += sum(r["size"] for r in removals)
        cards.append({
            "hash": cg.get("hash"),
            "category": cg["category"],
            "reason": cg.get("reason", ""),
            "structural": cg.get("structural", False),
            "protected": False,
            "wasted_space": cg.get("total_wasted_space", 0),
            "wasted_human": human_readable_size(cg.get("total_wasted_space", 0)),
            "keeper": keeper["path"] if keeper else None,
            "copies": [_review_copy(f, keeper, removable_paths, cg) for f in copies],
        })

    # actionable cards first (biggest reclaimable), protected last
    cards.sort(key=lambda c: (c["protected"], -c["wasted_space"]))
    return {
        "scan_id": scan_id,
        "within_source": within_source,
        "source_of_truth": source_of_truth,
        "summary": {
            "total_groups": total_groups,
            "auto_selected": auto_selected,
            "reclaimable_space": reclaimable,
            "reclaimable_human": human_readable_size(reclaimable),
            "protected_groups": protected_groups,
            "protected_files": protected_files,
            "capped": total_groups >= limit,
        },
        "cards": cards,
    }


def _review_copy(f: dict, keeper: Optional[dict], removable_paths: set, cg: dict) -> dict:
    """Shape one copy row for a review card."""
    is_keeper = bool(keeper) and f["path"] == keeper["path"]
    removable = f["path"] in removable_paths
    return {
        "path": f["path"],
        "size": f["size"],
        "size_human": human_readable_size(f["size"]),
        "source": _top_source(f),
        "modified": f.get("modified_time", ""),
        "subfolder": f.get("subfolder", ""),
        "is_keeper": is_keeper,
        "removable": removable,
        "smart_selected": removable,          # safe picks pre-checked
        "structural": _is_structural_path(f["path"]),
    }


def execute(scan_id: str, source_of_truth: list[str],
            action: str = "quarantine", within_source: bool = False,
            only_paths: Optional[list[str]] = None, tiebreak: str = "biggest") -> dict:
    """
    Execute the safe removal plan. Moves each planned file to a dated quarantine
    (reversible). Writes a manifest. Never hard-deletes here.

    If `only_paths` is given, execute ONLY those paths that are also in the safe
    plan (lets the UI apply a user-reviewed subset). Otherwise the whole plan runs.
    """
    dup = get_duplicates(scan_id)
    if dup is None:
        return {"error": "scan not found"}

    # Rebuild the full safe plan via the shared, safety-checked planner.
    plan = []
    for g in dup.get("groups", []):
        cg = classify_group(g)
        _keeper, removals = _plan_removals_for_group(cg, source_of_truth, within_source, tiebreak)
        for r in removals:
            plan.append({"path": r["path"], "size": r["size"], "keep": r["keep"]})

    if only_paths is not None:
        allow = set(only_paths)
        plan = [p for p in plan if p["path"] in allow]

    qroot = os.path.join(STORE_DIR, f"_DedupQuarantine_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(qroot, exist_ok=True)
    manifest_path = os.path.join(qroot, "_manifest.json")

    removed = 0
    freed = 0
    errors = []
    manifest = []
    for item in plan:
        src = item["path"]
        if not os.path.exists(src):
            continue
        # mirror path under quarantine using a flattened but unique name
        rel = _norm(src).replace(":", "").lstrip("/")
        dest = os.path.join(qroot, rel.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(src, dest)
            removed += 1
            freed += item["size"]
            manifest.append({"removed": src, "quarantined_to": dest,
                             "kept": item["keep"], "size": item["size"]})
        except OSError as e:
            errors.append(f"{src}: {e}")

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"scan_id": scan_id, "source_of_truth": source_of_truth,
                   "removed": removed, "freed": freed, "items": manifest,
                   "errors": errors}, f, indent=2, ensure_ascii=False)

    return {
        "removed": removed,
        "freed": freed,
        "freed_human": human_readable_size(freed),
        "quarantine": qroot,
        "manifest": manifest_path,
        "errors": errors[:20],
        "error_count": len(errors),
    }
