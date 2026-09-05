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


def analyze(scan_id: str, source_of_truth: Optional[list[str]] = None) -> Optional[dict]:
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

        if cat in ("structural_internal", "single_source"):
            protected_count += len(g["files"])
            protected_space += g.get("total_wasted_space", 0)
            continue

        # cross_source_redundant — build safe removal plan
        files = g["files"]
        # choose the KEEPER
        keeper = None
        if source_of_truth:
            in_sot = [f for f in files if _under_any(f["path"], source_of_truth)]
            if in_sot:
                keeper = max(in_sot, key=lambda f: f["size"])
        if keeper is None:
            keeper = max(files, key=lambda f: f["size"])  # default: biggest

        for f in files:
            if f["path"] == keeper["path"]:
                continue
            # SAFETY: only remove a copy that is NOT under the source of truth,
            # and never a structural path.
            if source_of_truth and _under_any(f["path"], source_of_truth):
                continue
            if _is_structural_path(f["path"]):
                continue
            plan_remove.append({
                "path": f["path"], "size": f["size"], "source": _top_source(f),
                "keep": keeper["path"], "keep_source": _top_source(keeper),
                "hash": g.get("hash"),
            })
            src = _top_source(f)
            s = by_source_removable.setdefault(src, {"files": 0, "space": 0})
            s["files"] += 1
            s["space"] += f["size"]

    remove_space = sum(x["size"] for x in plan_remove)
    return {
        "scan_id": scan_id,
        "source_of_truth": source_of_truth,
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


def execute(scan_id: str, source_of_truth: list[str],
            action: str = "quarantine") -> dict:
    """
    Execute the safe removal plan. Moves each planned file to a dated quarantine
    (reversible). Writes a manifest. Never hard-deletes here.
    """
    analysis = analyze(scan_id, source_of_truth)
    if analysis is None:
        return {"error": "scan not found"}

    # Rebuild the full plan (analysis only returns a sample)
    dup = get_duplicates(scan_id)
    plan = []
    for g in dup.get("groups", []):
        cg = classify_group(g)
        if cg["category"] != "cross_source_redundant":
            continue
        files = cg["files"]
        keeper = None
        in_sot = [f for f in files if _under_any(f["path"], source_of_truth)]
        if in_sot:
            keeper = max(in_sot, key=lambda f: f["size"])
        else:
            keeper = max(files, key=lambda f: f["size"])
        for f in files:
            if f["path"] == keeper["path"]:
                continue
            if source_of_truth and _under_any(f["path"], source_of_truth):
                continue
            if _is_structural_path(f["path"]):
                continue
            plan.append({"path": f["path"], "size": f["size"], "keep": keeper["path"]})

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
